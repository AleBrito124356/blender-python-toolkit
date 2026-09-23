"""Find a Blender executable on Windows, macOS and Linux.

Order of precedence:

1. an explicit path (``bpt --blender PATH``),
2. the ``BLENDER_EXE`` environment variable,
3. ``blender`` on ``PATH``,
4. the newest version among the usual install locations
   (``Program Files/Blender Foundation/Blender X.Y``, Steam,
   ``/Applications/Blender*.app``, ``/opt/blender*``, snap, flatpak, ...).

Everything is injectable (environment, platform, ``which``, ``glob``,
``isfile``, home directory) so the logic is unit-tested with fake install
trees instead of a real Blender.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import shutil
import sys
from dataclasses import dataclass

_VERSION_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{1,3}))?(?![\d])")


class BlenderNotFound(RuntimeError):
    """Raised when no usable Blender executable can be located."""


@dataclass(frozen=True)
class Candidate:
    path: str
    version: tuple | None
    source: str

    @property
    def version_string(self):
        return ".".join(str(v) for v in self.version) if self.version else "unknown"


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


def find_candidates(env=None, platform=None, which=shutil.which, glob=_glob.glob,
                    isfile=os.path.isfile, home=None):
    """Every Blender found, in precedence order (explicit path not included)."""
    env = os.environ if env is None else env
    found, seen = [], set()

    def add(path, source):
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            return
        seen.add(norm)
        found.append(Candidate(path=path, version=version_from_path(path), source=source))

    exe = env.get("BLENDER_EXE")
    if exe and isfile(exe):
        add(exe, "BLENDER_EXE")
    on_path = which("blender")
    if on_path:
        add(on_path, "PATH")
    installs = []
    for pattern in install_patterns(platform, env, home):
        for path in glob(pattern):
            if isfile(path):
                installs.append(Candidate(path=path, version=version_from_path(path), source="install"))
    for candidate in sorted(installs, key=_version_key, reverse=True):
        add(candidate.path, "install")
    return found


def find_blender(explicit=None, env=None, **kwargs):
    """Return the :class:`Candidate` to use, or raise :class:`BlenderNotFound`."""
    env = os.environ if env is None else env
    isfile = kwargs.get("isfile", os.path.isfile)
    if explicit:
        if not isfile(explicit):
            raise BlenderNotFound(f"--blender {explicit!r} is not a file")
        return Candidate(path=explicit, version=version_from_path(explicit), source="--blender")
    exe = env.get("BLENDER_EXE")
    if exe and not isfile(exe):
        raise BlenderNotFound(f"BLENDER_EXE={exe!r} does not exist; fix it or unset it")
    candidates = find_candidates(env=env, **kwargs)
    if not candidates:
        raise BlenderNotFound(
            "Blender not found. Install it from https://www.blender.org/download/ or point "
            "BLENDER_EXE (or --blender) at the executable, e.g. "
            "'C:/Program Files/Blender Foundation/Blender 5.2/blender.exe'."
        )
    return candidates[0]
