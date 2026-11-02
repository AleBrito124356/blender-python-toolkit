"""``bpt``: one command for every toolkit script, with safe Blender flags.

    bpt doctor                       # runtime, engines, GPU, FFmpeg, export formats, numpy, PyYAML
    bpt city --blocks 6 --render     # = blender --background --factory-startup
                                     #   --python-exit-code 1 --python scripts/procedural_city.py -- ...
    bpt --dry-run bars --csv data/sales.csv --render
    bpt --json export --input model.glb --usdz      # one JSON object on stdout, logs on stderr
    bpt run path/to/your_script.py --your-flag

The runtime is a Blender executable or a Python interpreter that can
``import bpy`` (``pip install bpy``); see :mod:`bpt.discovery`. For an
interpreter the command is ``python bpt/blender_shim.py`` followed by exactly
the Blender arguments, and the shim emulates Blender's command line.

Every run adds ``--background --factory-startup --python-exit-code 1`` so a
Python error in a script fails the process (plain ``blender --python`` exits 0
on an uncaught exception) and user add-ons or preferences cannot change the
result. The script's exit code is returned unchanged.

With ``--json`` the launcher streams the run's output to stderr and prints one
JSON object on stdout: the exit code and its meaning, timings, the runtime,
and the summary the script wrote (outputs, QA results, per-file status) to the
file named by the ``BPT_SUMMARY`` environment variable.

This module is pure standard-library Python and runs on the system
interpreter; Blender (or the bpy interpreter) is only started as a subprocess.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from bpt import __version__
from bpt.discovery import BlenderNotFound, find_blender, find_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_shim.py")
SUMMARY_ENV = "BPT_SUMMARY"

COMMANDS = {
    "city": ("procedural_city.py", "seeded procedural city at dusk"),
    "turntable": ("product_turntable.py", "product turntable / beauty still / MP4"),
    "bars": ("csv_to_bars3d.py", "CSV -> 3D bar chart (still or animated)"),
    "materials": ("material_library.py", "8 procedural PBR materials -> .blend + preview"),
    "cleanup": ("cleanup_scene.py", "scene hygiene, dry-run by default"),
    "batch": ("batch_render.py", "render a folder of .blend files, CI exit code"),
    "presets": ("render_presets.py", "apply preview / social / print presets"),
    "inspect": ("inspect_render.py", "QA report for any .blend and render"),
    "export": ("export_web.py", "web/AR export: .glb (+ .usdz), report, round-trip check"),
    "thumbnails": ("thumbnails.py", "folder of models/.blend -> PNG thumbnails + contact sheet"),
}

# Exit codes shared by every script (documented in the README).
EXIT_STATUS = {0: "ok", 1: "failed", 2: "invalid_input", 3: "check_failed", 127: "not_executable",
               130: "interrupted"}

DOCTOR_SNIPPET = r"""
import json, sys
import bpy
info = {"version": bpy.app.version_string, "python": sys.version.split()[0],
        "python_executable": sys.executable, "bpy_module": not bool(bpy.app.binary_path)}
scene = bpy.context.scene
original = scene.render.engine
engines = []
candidates = ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"]
candidates += [c.bl_idname for c in bpy.types.RenderEngine.__subclasses__() if getattr(c, "bl_idname", None)]
for engine in dict.fromkeys(candidates):
    try:
        scene.render.engine = engine
        engines.append(engine)
    except TypeError:
        pass
scene.render.engine = original
info["engines"] = engines
info["ffmpeg"] = bool(getattr(getattr(bpy.app, "ffmpeg", None), "supported", False))
gpus = {}
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        try:
            devices = prefs.get_devices_for_type(backend)
        except Exception:
            continue
        names = [d.name for d in devices if d.type != "CPU"]
        if names:
            gpus[backend] = names
except Exception as exc:
    info["cycles_error"] = str(exc)
info["cycles_gpu"] = gpus
gltf = "io_scene_gltf2" in bpy.context.preferences.addons
draco = None
if gltf:
    try:
        from io_scene_gltf2.io.com.library import dll_path
        path = dll_path("bf_intern_draco_bridge", "Draco")
        draco = bool(path is not None and path.is_file())
    except Exception:
        try:
            from io_scene_gltf2.io.com.gltf2_io_draco_compression_extension import dll_path
            draco = bool(dll_path().is_file())
        except Exception:
            draco = None
info["export"] = {"gltf": gltf, "draco": draco,
                  "usd": bool(getattr(bpy.app.build_options, "usd", False)) and hasattr(bpy.ops.wm, "usd_export")}
if sys.platform.startswith("linux"):
    import ctypes.util
    info["egl"] = ctypes.util.find_library("EGL")
try:
    import numpy
    info["numpy"] = numpy.__version__
except ImportError:
    info["numpy"] = None
try:
    import yaml
    info["pyyaml"] = yaml.__version__
except ImportError:
    info["pyyaml"] = None
print("BPT_DOCTOR=" + json.dumps(info))
sys.stdout.flush()
"""


def format_command(cmd):
    """Render ``cmd`` the way the current platform's shell would need it."""
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def runtime_prefix(executable, kind="blender"):
    """The start of every command: ``[blender]`` or ``[python, bpt/blender_shim.py]``."""
    if kind == "python":
        return [executable, SHIM]
    return [executable]


def build_command(blender, script, script_args=(), factory_startup=True, kind="blender"):
    """The full, safe Blender command line for one toolkit script.

    ``kind="python"`` runs the same arguments through the bpy shim.
    """
    cmd = runtime_prefix(blender, kind) + ["--background"]
    if factory_startup:
        cmd.append("--factory-startup")
    cmd += ["--python-exit-code", "1", "--python", script]
    args = list(script_args)
    if args and args[0] == "--":
        args = args[1:]
    cmd.append("--")
    cmd += args
    return cmd


def resolve_script(command, args):
    """Map a sub-command to ``(script_path, remaining_args)``."""
    if command == "run":
        if not args:
            raise SystemExit("bpt run: give the path of a Python script to run inside Blender")
        script = os.path.abspath(args[0])
        if not os.path.isfile(script):
            raise SystemExit(f"bpt run: script not found: {script}")
        return script, args[1:]
    name = COMMANDS[command][0]
    script = os.path.join(SCRIPTS_DIR, name)
    if not os.path.isfile(script):
        raise SystemExit(
            f"bpt: {script} is missing. bpt runs the scripts of a source checkout; "
            "install it with 'pip install -e .' from the repository root."
        )
    return script, args


def run_blender(cmd, runner=subprocess.run):
    """Run Blender, streaming its output, and return its exit code."""
    try:
        completed = runner(cmd)
    except FileNotFoundError:
        print(f"bpt: cannot execute {cmd[0]!r}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def run_to_stderr(cmd, env, popen=subprocess.Popen, keep=40):
    """Run ``cmd`` copying its stdout and stderr, line by line, to our stderr.

    Returns ``(exit_code, last_lines)``. Blender prints from C as well as from
    Python, so the only way to keep stdout clean for one JSON object is to
    take all of the child's output and pass it on as logs.
    """
    tail = collections.deque(maxlen=keep)
    try:
        proc = popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        message = f"bpt: cannot execute {cmd[0]!r}: {exc}"
        print(message, file=sys.stderr)
        return 127, [message]
    try:
        for line in proc.stdout:
            sys.stderr.write(line)
            tail.append(line.rstrip("\r\n"))
        code = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        code = 130
    sys.stderr.flush()
    return code, list(tail)


def exit_status(code):
    """Name of an exit code (``ok``, ``failed``, ``invalid_input``, ``check_failed``...)."""
    return EXIT_STATUS.get(code, "error")


def runtime_info(candidate):
    if candidate is None:
        return None
    return {"kind": candidate.kind, "path": candidate.path, "source": candidate.source}


def run_json(cmd, command, candidate, popen=subprocess.Popen):
    """Run with logs on stderr and print one JSON summary object on stdout."""
    workdir = tempfile.mkdtemp(prefix="bpt_")
    summary_path = os.path.join(workdir, "summary.json")
    env = dict(os.environ)
    env[SUMMARY_ENV] = summary_path
    started = time.time()
    try:
        code, tail = run_to_stderr(cmd, env, popen=popen)
        summary, summary_error = None, None
        if os.path.isfile(summary_path):
            try:
                with open(summary_path, encoding="utf-8") as fh:
                    summary = json.load(fh)
            except (OSError, ValueError) as exc:
                summary_error = f"unreadable summary: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    payload = {
        "tool": "bpt",
        "bpt_version": __version__,
        "command": command,
        "status": exit_status(code),
        "exit_code": code,
        "seconds": round(time.time() - started, 3),
        "runtime": runtime_info(candidate),
        "argv": cmd,
        "summary": summary,
    }
    if summary_error:
        payload["summary_error"] = summary_error
    if code != 0:
        payload["log_tail"] = tail
    print(json.dumps(payload, indent=2))
    sys.stdout.flush()
    return code


def doctor(candidate, runner=subprocess.run, as_json=False):
    """Probe the Blender runtime and the host; print a readable report."""
    cmd = runtime_prefix(candidate.path, candidate.kind) + [
        "--background", "--factory-startup", "--python-exit-code", "1", "--python-expr", DOCTOR_SNIPPET]
    try:
        completed = runner(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"bpt doctor: could not run {candidate.path}: {exc}", file=sys.stderr)
        return 1
    info = None
    for line in (completed.stdout or "").splitlines():
        if line.startswith("BPT_DOCTOR="):
            info = json.loads(line[len("BPT_DOCTOR="):])
    if info is None:
        what = "this Python could not import bpy" if candidate.is_python else "Blender did not answer"
        print(f"bpt doctor: {what} (exit code {completed.returncode})", file=sys.stderr)
        tail = "\n".join(((completed.stdout or "") + (completed.stderr or "")).splitlines()[-15:])
        print(tail, file=sys.stderr)
        return 1
    host = {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}
    others = [c for c in find_candidates() if os.path.normcase(c.path) != os.path.normcase(candidate.path)]
    if as_json:
        print(json.dumps({"blender": {"path": candidate.path, "source": candidate.source, "kind": candidate.kind,
                                      **info},
                          "host": host,
                          "other_installs": [{"path": c.path, "kind": c.kind, "source": c.source} for c in others]},
                         indent=2))
        return 0
    gpu = ", ".join(f"{k}: {', '.join(v)}" for k, v in info["cycles_gpu"].items()) or \
        "none found -> Cycles renders on the CPU (EEVEE is unaffected)"
    yaml_line = info["pyyaml"] or ("not installed -> presets use the built-in parser (fine). Optional: "
                                   f"\"{info['python_executable']}\" -m pip install pyyaml")
    if candidate.is_python:
        runtime = (f"{info['version']}  (bpy module in {candidate.path}, via {candidate.source})")
        python = f"{info['python']} (the interpreter that imports bpy)"
    else:
        runtime = f"{info['version']}  ({candidate.path}, via {candidate.source})"
        python = f"{info['python']} (bundled with Blender)"
    rows = [
        ("Blender", runtime),
        ("Runtime", "Python interpreter + bpy module (no Blender install needed)" if candidate.is_python
         else "Blender executable"),
        ("Python", python),
        ("Engines", ", ".join(info["engines"])),
        ("Cycles GPU", gpu),
        ("Video output", "FFmpeg built in: --video mp4/mkv/webm works" if info["ffmpeg"]
         else "no FFmpeg in this build: render PNG frames instead of --video"),
    ]
    export = info.get("export")
    if export is not None:
        draco = {True: "Draco yes", False: "Draco no (encoder library missing from this build: --draco is refused)",
                 None: "Draco unknown"}[export.get("draco")]
        rows.append(("Export", f"glTF {'yes' if export.get('gltf') else 'no'}; {draco}; "
                               f"USD/USDZ {'yes' if export.get('usd') else 'no'}"))
    if "egl" in info:
        rows.append(("EGL (Linux)", f"{info['egl']} -> EEVEE and Workbench can render headless" if info["egl"]
                     else "libEGL not found -> EEVEE/Workbench cannot render headless; "
                          "install libegl1 (Debian/Ubuntu). Cycles still works"))
    rows += [
        ("numpy", f"{info['numpy']} -> render QA (--report) available" if info["numpy"]
         else "missing -> --report/--qa-strict will not work"),
        ("PyYAML", yaml_line),
        ("ffprobe on PATH", host["ffprobe"] or "no (only needed to inspect videos outside Blender)"),
        ("bpt", f"{__version__} (scripts in {SCRIPTS_DIR})"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")
    if others:
        print(f"  {'Other runtimes'.ljust(width)}  " + "; ".join(
            f"{c.path} ({'bpy module' if c.is_python else c.version_string}, {c.source})" for c in others))
    return 0


def build_parser():
    lines = "\n".join(f"  {name:<10} {desc}" for name, (_, desc) in COMMANDS.items())
    parser = argparse.ArgumentParser(
        prog="bpt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run blender-python-toolkit scripts headless with safe defaults.",
        epilog=(f"commands:\n  doctor     check the Blender runtime\n{lines}\n"
                "  run        run any Python file inside Blender: bpt run my_script.py [args]\n\n"
                "Arguments after the command go to the script, e.g. 'bpt city --help'.\n"
                "Exit codes: 0 ok, 1 failed (Python/Blender error, failed files), 2 invalid input,\n"
                "3 a check failed (--qa-strict QA, export round trip)."),
    )
    parser.add_argument("--blender", default=None,
                        help="Blender executable or Python interpreter with bpy (default: auto-discovery).")
    parser.add_argument("--dry-run", action="store_true", help="Print the command and exit.")
    parser.add_argument("--no-factory-startup", action="store_true",
                        help="Load your Blender preferences and add-ons (not recommended for CI).")
    parser.add_argument("--json", action="store_true",
                        help="Print one JSON object on stdout (status, exit code, outputs, QA, timings); "
                             "the run's logs go to stderr.")
    parser.add_argument("--version", action="version", version=f"bpt {__version__}")
    parser.add_argument("command", choices=["doctor", "run", *COMMANDS], metavar="command")
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv=None, runner=subprocess.run, popen=subprocess.Popen):
    parser = build_parser()
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        candidate = find_blender(ns.blender)
    except BlenderNotFound as exc:
        if ns.dry_run and ns.command != "doctor":
            candidate = None
        else:
            if ns.json and ns.command != "doctor":
                print(json.dumps({"tool": "bpt", "bpt_version": __version__, "command": ns.command,
                                  "status": "no_runtime", "exit_code": 1, "error": str(exc)}, indent=2))
            print(f"bpt: {exc}", file=sys.stderr)
            return 1

    if ns.command == "doctor":
        unknown = [a for a in ns.args if a != "--json"]
        if unknown:
            parser.error(f"doctor takes no arguments besides --json (got {' '.join(unknown)})")
        return doctor(candidate, runner=runner, as_json=ns.json or "--json" in ns.args)

    script, script_args = resolve_script(ns.command, ns.args)
    executable = candidate.path if candidate else "blender"
    kind = candidate.kind if candidate else "blender"
    cmd = build_command(executable, script, script_args, factory_startup=not ns.no_factory_startup, kind=kind)
    if ns.dry_run:
        if ns.json:
            print(json.dumps({"tool": "bpt", "bpt_version": __version__, "command": ns.command, "dry_run": True,
                              "runtime": runtime_info(candidate), "argv": cmd, "shell": format_command(cmd)},
                             indent=2))
        else:
            print(format_command(cmd))
        return 0
    sys.stdout.flush()
    if ns.json:
        return run_json(cmd, ns.command, candidate, popen=popen)
    return run_blender(cmd, runner=runner)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
