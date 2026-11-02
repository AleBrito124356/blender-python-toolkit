"""Find a Blender runtime on Windows, macOS and Linux.

A runtime is either a **Blender executable** or a **Python interpreter that can
``import bpy``** (``pip install bpy``: Blender as a Python module, no Blender
install needed). ``bpt`` runs scripts the same way on both; see
``bpt/blender_shim.py`` for how a bpy interpreter is given Blender's command
line.

Order of precedence:

1. an explicit path (``bpt --blender PATH``): a Blender executable or a Python
   interpreter,
2. the ``BLENDER_EXE`` environment variable (either kind as well),
3. ``blender`` on ``PATH``,
4. the newest version among the usual install locations
   (``Program Files/Blender Foundation/Blender X.Y``, Steam,
   ``/Applications/Blender*.app``, ``/opt/blender*``, snap, flatpak, ...),
5. a bpy interpreter: ``BLENDER_PYTHON``, then the interpreter running ``bpt``
   when ``importlib.util.find_spec("bpy")`` finds the module.

The kind of an explicit path is decided by its file name (``python3.13``,
``python.exe`` -> Python; anything containing ``blender`` -> Blender) and, for
any other name, by running ``PATH --version`` (``Blender 5.2.1`` vs
``Python 3.13.1``).

Everything is injectable (environment, platform, ``which``, ``glob``,
``isfile``, home directory, the ``--version`` probe and the current-interpreter
check) so the logic is unit-tested with fake install trees instead of a real
Blender.
"""

from __future__ import annotations

import glob as _glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

_VERSION_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{1,3}))?(?![\d])")
_PYTHON_NAME_RE = re.compile(r"^(?:python|pythonw|pypy)(?:\d+(?:\.\d+)*)?[a-z]?$")

KINDS = ("blender", "python")


class BlenderNotFound(RuntimeError):
    """Raised when no usable Blender runtime can be located."""


@dataclass(frozen=True)
class Candidate:
    path: str
    version: tuple | None
    source: str
    kind: str = "blender"  # "blender" (executable) or "python" (interpreter with the bpy module)

    @property
    def version_string(self):
        return ".".join(str(v) for v in self.version) if self.version else "unknown"

    @property
    def is_python(self):
        return self.kind == "python"


def version_from_path(path):
    """Best-effort ``(major, minor[, patch])`` from an install path.

    ``.../Blender Foundation/Blender 5.2/blender.exe`` -> ``(5, 2)``,
    ``/opt/blender-4.2.3-linux-x64/blender`` -> ``(4, 2, 3)``. Returns None
    when no component carries a version.
    """
    parts = re.split(r"[\\/]", str(path))
    for part in reversed(parts[:-1] or parts):
        if "blender" not in part.lower():
            continue
        match = _VERSION_RE.search(part)
        if match:
            return tuple(int(g) for g in match.groups() if g is not None)
    return None


def kind_from_name(path):
    """``"python"``, ``"blender"`` or None (undecided) from the file name alone."""
    name = re.split(r"[\\/]", str(path))[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if _PYTHON_NAME_RE.match(name):
        return "python"
    if "blender" in name:
        return "blender"
    return None


def probe_version_banner(path, runner=subprocess.run, timeout=120):
    """Output of ``PATH --version`` (empty string when it cannot be run)."""
    try:
        proc = runner([path, "--version"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def kind_from_banner(text):
    """``"blender"`` / ``"python"`` / None from a ``--version`` output."""
    for line in str(text).splitlines():
        line = line.strip()
        if re.match(r"^Blender\s+\d", line):
            return "blender"
        if re.match(r"^(Python|PyPy)\s+\d", line):
            return "python"
    return None


def detect_kind(path, probe=probe_version_banner):
    """Is ``path`` a Blender executable or a Python interpreter?

    The file name decides when it is unambiguous; otherwise ``probe(path)``
    (``PATH --version``) does. Raises :class:`BlenderNotFound` when neither
    answers.
    """
    kind = kind_from_name(path)
    if kind:
        return kind
    banner = probe(path)
    kind = kind_from_banner(banner)
    if kind:
        return kind
    first = next((ln.strip() for ln in str(banner).splitlines() if ln.strip()), "nothing")
    raise BlenderNotFound(
        f"{path!r} is neither a Blender executable nor a Python interpreter "
        f"('{os.path.basename(str(path))} --version' printed {first!r})"
    )


def current_bpy_python():
    """The interpreter running ``bpt`` if it can import ``bpy``, else None.

    Uses ``importlib.util.find_spec`` so ``bpy`` is located, not imported.
    """
    if not sys.executable:
        return None
    try:
        found = importlib.util.find_spec("bpy") is not None
    except (ImportError, ValueError):
        found = False
    return sys.executable if found else None


def _version_key(candidate):
    version = candidate.version or ()
    return (1 if version else 0, tuple(version) + (0,) * (3 - len(version)))


def install_patterns(platform=None, env=None, home=None):
    """Glob patterns for common Blender installs on ``platform``."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    patterns = []
    if platform.startswith("win"):
        roots = []
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if env.get(key) and env[key] not in roots:
                roots.append(env[key])
        if not roots:
            roots = ["C:/Program Files"]
        for root in roots:
            patterns.append(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
            patterns.append(os.path.join(root, "Steam", "steamapps", "common", "Blender", "blender.exe"))
        if env.get("LOCALAPPDATA"):
            patterns.append(os.path.join(env["LOCALAPPDATA"], "Programs", "Blender Foundation", "Blender*",
                                         "blender.exe"))
    elif platform == "darwin":
        patterns += [
            "/Applications/Blender*.app/Contents/MacOS/Blender",
            os.path.join(home, "Applications", "Blender*.app", "Contents", "MacOS", "Blender"),
        ]
    else:
        patterns += [
            "/usr/bin/blender",
            "/usr/local/bin/blender",
            "/snap/bin/blender",
            "/var/lib/flatpak/exports/bin/org.blender.Blender",
            "/opt/blender*/blender",
            os.path.join(home, "blender*", "blender"),
            os.path.join(home, ".local", "bin", "blender"),
            os.path.join(home, "Applications", "blender*", "blender"),
        ]
    return patterns


def _candidate(path, source, kind):
    return Candidate(path=path, version=version_from_path(path) if kind == "blender" else None,
                     source=source, kind=kind)


def find_candidates(env=None, platform=None, which=shutil.which, glob=_glob.glob,
                    isfile=os.path.isfile, home=None, probe=probe_version_banner, bpy_python=None):
    """Every runtime found, in precedence order (explicit path not included).

    ``bpy_python`` returns the current interpreter when it can import bpy
    (default :func:`current_bpy_python`, looked up at call time).
    """
    env = os.environ if env is None else env
    bpy_python = bpy_python or (lambda: current_bpy_python())
    found, seen = [], set()

    def add(path, source, kind):
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            return
        seen.add(norm)
        found.append(_candidate(path, source, kind))

    exe = env.get("BLENDER_EXE")
    if exe and isfile(exe):
        add(exe, "BLENDER_EXE", detect_kind(exe, probe))
    on_path = which("blender")
    if on_path:
        add(on_path, "PATH", "blender")
    installs = []
    for pattern in install_patterns(platform, env, home):
        for path in glob(pattern):
            if isfile(path):
                installs.append(_candidate(path, "install", "blender"))
    for candidate in sorted(installs, key=_version_key, reverse=True):
        add(candidate.path, "install", "blender")
    py = env.get("BLENDER_PYTHON")
    if py and isfile(py):
        add(py, "BLENDER_PYTHON", detect_kind(py, probe))
    current = bpy_python()
    if current:
        add(current, "current Python", "python")
    return found


NOT_FOUND_HELP = (
    "Blender not found. Either install Blender from https://www.blender.org/download/ and point "
    "BLENDER_EXE (or --blender) at the executable, e.g. "
    "'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe', or use Blender as a Python module "
    "without installing it: 'pip install bpy' (bpy 5.x needs Python 3.13) and set BLENDER_EXE or "
    "BLENDER_PYTHON to that interpreter (or run bpt from it)."
)


def find_blender(explicit=None, env=None, **kwargs):
    """Return the :class:`Candidate` to use, or raise :class:`BlenderNotFound`."""
    env = os.environ if env is None else env
    isfile = kwargs.get("isfile", os.path.isfile)
    which = kwargs.get("which", shutil.which)
    probe = kwargs.get("probe", probe_version_banner)
    if explicit:
        path = explicit
        if not isfile(path):
            resolved = which(explicit) if not re.search(r"[\\/]", str(explicit)) else None
            if not resolved:
                raise BlenderNotFound(f"--blender {explicit!r} is not a file")
            path = resolved
        return _candidate(path, "--blender", detect_kind(path, probe))
    exe = env.get("BLENDER_EXE")
    if exe and not isfile(exe):
        raise BlenderNotFound(f"BLENDER_EXE={exe!r} does not exist; fix it or unset it")
    candidates = find_candidates(env=env, **kwargs)
    if not candidates:
        py = env.get("BLENDER_PYTHON")
        if py and not isfile(py):
            raise BlenderNotFound(f"BLENDER_PYTHON={py!r} does not exist; fix it or unset it")
        raise BlenderNotFound(NOT_FOUND_HELP)
    return candidates[0]
