"""``bpt``: one command for every toolkit script, with safe Blender flags.

    bpt doctor                       # what Blender, engines, GPU, FFmpeg, numpy, PyYAML
    bpt city --blocks 6 --render     # = blender --background --factory-startup
                                     #   --python-exit-code 1 --python scripts/procedural_city.py -- ...
    bpt --dry-run bars --csv data/sales.csv --render
    bpt run path/to/your_script.py --your-flag

Every run adds ``--background --factory-startup --python-exit-code 1`` so a
Python error in a script fails the process (plain ``blender --python`` exits 0
on an uncaught exception) and user add-ons or preferences cannot change the
result. The script's exit code is returned unchanged.

This module is pure standard-library Python and runs on the system
interpreter; Blender is only started as a subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

from bpt import __version__
from bpt.discovery import BlenderNotFound, find_blender, find_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")

COMMANDS = {
    "city": ("procedural_city.py", "seeded procedural city at dusk"),
    "turntable": ("product_turntable.py", "product turntable / beauty still / MP4"),
    "bars": ("csv_to_bars3d.py", "CSV -> 3D bar chart (still or animated)"),
    "materials": ("material_library.py", "8 procedural PBR materials -> .blend + preview"),
    "cleanup": ("cleanup_scene.py", "scene hygiene, dry-run by default"),
    "batch": ("batch_render.py", "render a folder of .blend files, CI exit code"),
    "presets": ("render_presets.py", "apply preview / social / print presets"),
    "inspect": ("inspect_render.py", "QA report for any .blend and render"),
}

DOCTOR_SNIPPET = r"""
import json, sys
import bpy
info = {"version": bpy.app.version_string, "python": sys.version.split()[0],
        "python_executable": sys.executable}
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


def build_command(blender, script, script_args=(), factory_startup=True):
    """The full, safe Blender command line for one toolkit script."""
    cmd = [blender, "--background"]
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


def doctor(candidate, runner=subprocess.run, as_json=False):
    """Probe the Blender install and the host; print a readable report."""
    cmd = [candidate.path, "--background", "--factory-startup", "--python-exit-code", "1",
           "--python-expr", DOCTOR_SNIPPET]
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
        print(f"bpt doctor: Blender did not answer (exit code {completed.returncode})", file=sys.stderr)
        tail = "\n".join(((completed.stdout or "") + (completed.stderr or "")).splitlines()[-15:])
        print(tail, file=sys.stderr)
        return 1
    host = {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}
    others = [c for c in find_candidates() if os.path.normcase(c.path) != os.path.normcase(candidate.path)]
    if as_json:
        print(json.dumps({"blender": {"path": candidate.path, "source": candidate.source, **info},
                          "host": host, "other_installs": [c.path for c in others]}, indent=2))
        return 0
    gpu = ", ".join(f"{k}: {', '.join(v)}" for k, v in info["cycles_gpu"].items()) or \
        "none found -> Cycles renders on the CPU (EEVEE is unaffected)"
    yaml_line = info["pyyaml"] or ("not installed -> presets use the built-in parser (fine). Optional: "
                                   f"\"{info['python_executable']}\" -m pip install pyyaml")
    rows = [
        ("Blender", f"{info['version']}  ({candidate.path}, via {candidate.source})"),
        ("Python", f"{info['python']} (bundled with Blender)"),
        ("Engines", ", ".join(info["engines"])),
        ("Cycles GPU", gpu),
        ("Video output", "FFmpeg built in: --video mp4/mkv/webm works" if info["ffmpeg"]
         else "no FFmpeg in this build: render PNG frames instead of --video"),
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
        print(f"  {'Other installs'.ljust(width)}  " + "; ".join(f"{c.path} ({c.version_string})" for c in others))
    return 0


def build_parser():
    lines = "\n".join(f"  {name:<10} {desc}" for name, (_, desc) in COMMANDS.items())
    parser = argparse.ArgumentParser(
        prog="bpt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run blender-python-toolkit scripts headless with safe defaults.",
        epilog=(f"commands:\n  doctor     check the Blender install\n{lines}\n"
                "  run        run any Python file inside Blender: bpt run my_script.py [args]\n\n"
                "Arguments after the command go to the script, e.g. 'bpt city --help'."),
    )
    parser.add_argument("--blender", default=None, help="Blender executable (default: auto-discovery).")
    parser.add_argument("--dry-run", action="store_true", help="Print the Blender command and exit.")
    parser.add_argument("--no-factory-startup", action="store_true",
                        help="Load your Blender preferences and add-ons (not recommended for CI).")
    parser.add_argument("--json", action="store_true", help="doctor: print JSON.")
    parser.add_argument("--version", action="version", version=f"bpt {__version__}")
    parser.add_argument("command", choices=["doctor", "run", *COMMANDS], metavar="command")
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv=None, runner=subprocess.run):
    parser = build_parser()
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        candidate = find_blender(ns.blender)
    except BlenderNotFound as exc:
        if ns.dry_run and ns.command != "doctor":
            candidate = None
        else:
            print(f"bpt: {exc}", file=sys.stderr)
            return 1

    if ns.command == "doctor":
        unknown = [a for a in ns.args if a != "--json"]
        if unknown:
            parser.error(f"doctor takes no arguments besides --json (got {' '.join(unknown)})")
        return doctor(candidate, runner=runner, as_json=ns.json or "--json" in ns.args)

    script, script_args = resolve_script(ns.command, ns.args)
    blender = candidate.path if candidate else "blender"
    cmd = build_command(blender, script, script_args, factory_startup=not ns.no_factory_startup)
    if ns.dry_run:
        print(format_command(cmd))
        return 0
    sys.stdout.flush()
    return run_blender(cmd, runner=runner)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
