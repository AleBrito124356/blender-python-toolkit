"""Blender's command line on top of the ``bpy`` module (``pip install bpy``).

    python bpt/blender_shim.py --background --factory-startup --python-exit-code 1 \\
        [file.blend] --python scripts/procedural_city.py -- --blocks 6 --render

behaves like

    blender --background --factory-startup --python-exit-code 1 \\
        [file.blend] --python scripts/procedural_city.py -- --blocks 6 --render

for any Python interpreter that can ``import bpy``, so every toolkit script and
test runs unchanged without a Blender executable. ``bpt`` builds this command
when its runtime is a Python interpreter; ``bpt --dry-run`` prints it.

What is emulated, and how:

* Arguments are processed in order, as Blender does: a ``.blend`` path opens
  that file (with ``load_ui=False``; scripts embedded in it do not run unless
  ``-y`` / ``--enable-autoexec`` is given), ``--python FILE`` (``-P``) runs a
  script as ``__main__`` with ``__file__`` set, ``--python-expr EXPR`` runs
  a string, ``--python-exit-code N`` sets the code used for the scripts after it.
* ``--factory-startup`` resets to factory settings (no user preferences or
  add-ons) before anything else. Without it the user's preferences and
  startup file are loaded, as Blender does (``import bpy`` alone does not
  read them).
* ``sys.argv`` is ``[<this python>, <the same arguments>]`` and is set
  *before* ``import bpy``: Blender's Cycles add-on reads the arguments after
  ``--`` while it registers, in Blender and in the module alike, so
  ``--cycles-device`` works and an ambiguous ``--cycles`` exits with code 2,
  exactly as in Blender. ``lib.core.get_argv_after_dashes`` and the scripts'
  ``argparse`` see the same list as under Blender.
* An uncaught exception prints its traceback; with ``--python-exit-code N``
  (N > 0) the process exits with N immediately, otherwise processing continues
  and the exit code stays 0 (Blender's behaviour, which is why ``bpt`` always
  passes ``--python-exit-code 1``). ``sys.exit(n)`` / ``SystemExit`` ends the
  process with ``n``; a string message is printed and gives 1.
* A ``.blend`` that cannot be opened ends the process with code 1, like
  Blender in background mode. Reaching the end of the arguments exits with 0.
* ``sys.path``: the folder of this file is removed (Python puts a script's
  folder first; Blender does not) and the toolkit root is appended, so
  ``import lib`` works even before a script adds the root itself.

``-b`` / ``--background``, ``--python-use-system-env``, ``-noaudio`` and
``--disable-autoexec`` / ``-Y`` are accepted; ``bpy`` is always a background
session. Other Blender flags (render flags such as ``-f`` / ``-a``, ``-E``,
``-o`` ...) are rejected with exit code 1 and a message, because the toolkit's
scripts render through ``bpy.ops.render.render`` instead.

This file must stay runnable on its own (no imports from ``bpt``) and keep
Python 3.9 syntax.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLKIT_ROOT = os.path.dirname(HERE)

NO_OP_FLAGS = {"-b", "--background", "--python-use-system-env", "-noaudio", "-Y", "--disable-autoexec"}
AUTOEXEC_FLAGS = {"-y", "--enable-autoexec"}


class UsageError(ValueError):
    """An argument this shim does not understand."""


def parse_args(argv):
    """Split Blender-style ``argv`` into ``(settings, actions)``.

    ``settings``: ``factory_startup`` and ``autoexec`` (global, like Blender's
    early argument passes). ``actions``: ordered ``(kind, value)`` pairs with
    kind ``exit_code``, ``blend``, ``python`` or ``expr``. Everything after a
    lone ``--`` belongs to the scripts and is not parsed.
    """
    settings = {"factory_startup": False, "autoexec": False}
    actions = []
    args = list(argv)
    if "--" in args:
        args = args[:args.index("--")]
    i = 0
    while i < len(args):
        arg = args[i]

        def value(flag=arg):
            if i + 1 >= len(args):
                raise UsageError(f"{flag} needs a value")
            return args[i + 1]

        if arg in NO_OP_FLAGS:
            i += 1
        elif arg == "--factory-startup":
            settings["factory_startup"] = True
            i += 1
        elif arg in AUTOEXEC_FLAGS:
            settings["autoexec"] = True
            i += 1
        elif arg == "--python-exit-code":
            raw = value()
            try:
                code = int(raw)
            except ValueError:
                raise UsageError(f"--python-exit-code expects an integer in [0..255], got {raw!r}") from None
            if not 0 <= code <= 255:
                raise UsageError(f"--python-exit-code expects an integer in [0..255], got {raw!r}")
            actions.append(("exit_code", code))
            i += 2
        elif arg in ("--python", "-P"):
            actions.append(("python", value()))
            i += 2
        elif arg == "--python-expr":
            actions.append(("expr", value()))
            i += 2
        elif arg.startswith("-"):
            raise UsageError(
                f"unsupported Blender argument {arg!r} (the bpy shim understands --background, "
                "--factory-startup, --python-exit-code N, --python FILE, --python-expr EXPR, -y and a "
                ".blend path; put script arguments after '--')")
        else:
            actions.append(("blend", arg))
            i += 1
    return settings, actions


def fix_sys_path(path=None):
    """Drop this file's folder from ``path`` and append the toolkit root."""
    path = sys.path if path is None else path
    here = os.path.normcase(HERE)
    path[:] = [p for p in path if os.path.normcase(os.path.abspath(p or os.curdir)) != here]
    if TOOLKIT_ROOT not in path:
        path.append(TOOLKIT_ROOT)
    return path


def load_blend(bpy, path, autoexec):
    """Open ``path`` like ``blender file.blend``; False (and a message) on failure."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        print(f'Error: Cannot read file "{path}": No such file or directory', file=sys.stderr)
        return False
    try:
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=bool(autoexec))
    except RuntimeError as exc:
        reason = (str(exc).strip().splitlines() or ["unknown error"])[-1]
        print(f'Error: Cannot read file "{path}": {reason}', file=sys.stderr)
        return False
    print(f'Read blend: "{path}"')
    return True


def load_user_startup(bpy):
    """Load the user's preferences and startup file, as Blender does without --factory-startup.

    ``import bpy`` starts from factory defaults and does not read
    ``userpref.blend`` (measured on bpy 5.2.3), unlike the Blender executable.
    """
    try:
        bpy.ops.wm.read_userpref()
        bpy.ops.wm.read_homefile(use_factory_startup=False)
    except RuntimeError as exc:
        print(f"blender_shim: could not load the user preferences / startup file: {exc}", file=sys.stderr)


def run_python(kind, value):
    """Run a script file or an expression as ``__main__``; False after an exception.

    ``SystemExit`` is not caught: it ends the process, as in Blender.
    """
    import runpy
    import traceback

    try:
        if kind == "python":
            path = os.path.abspath(value)
            if not os.path.isfile(path):
                print(f'Error: Cannot read file "{path}": No such file or directory', file=sys.stderr)
                return False
            runpy.run_path(path, run_name="__main__")
        else:
            import builtins

            exec(compile(value, "<string>", "exec"), {"__name__": "__main__", "__builtins__": builtins})
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - reported like Blender does, then the exit-code rule applies
        traceback.print_exc()
        sys.stderr.flush()
        return False
    return True


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        settings, actions = parse_args(args)
    except UsageError as exc:
        print(f"blender_shim: {exc}", file=sys.stderr)
        return 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # interleave with Blender's own C-level output
        except (AttributeError, ValueError):
            pass
    fix_sys_path()
    sys.argv = [sys.executable or "python"] + args  # before import bpy: Cycles parses it on register
    try:
        import bpy
    except ImportError as exc:
        print(f"blender_shim: this Python cannot import bpy ({exc}). Install Blender as a module with "
              f"'{sys.executable} -m pip install bpy' (bpy 5.x needs Python 3.13) or use a Blender "
              "executable.", file=sys.stderr)
        return 1
    if settings["factory_startup"]:
        bpy.ops.wm.read_factory_settings(use_empty=False)
    else:
        load_user_startup(bpy)
    exit_code_on_error = 0
    for kind, value in actions:
        if kind == "exit_code":
            exit_code_on_error = value
        elif kind == "blend":
            if not load_blend(bpy, value, settings["autoexec"]):
                return 1
        elif not run_python(kind, value) and exit_code_on_error:
            return exit_code_on_error
    return 0


if __name__ == "__main__":
    sys.exit(main())
