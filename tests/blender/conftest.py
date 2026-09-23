"""Fixtures for the headless-Blender integration tests.

Blender is found with the same discovery as ``bpt`` (``BLENDER_EXE`` first).
When none is found every test in this folder is skipped, so ``pytest`` still
passes on machines without Blender.

``BPT_TOOLKIT_ROOT`` points the tests at another checkout of the toolkit (for
example the previous release) while keeping these tests and helpers; that is
how the regression tests were shown to fail before the fixes.
"""

import json
import os
import pathlib
import subprocess
from dataclasses import dataclass

import pytest

from bpt.discovery import BlenderNotFound, find_blender

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLKIT_ROOT = pathlib.Path(os.environ.get("BPT_TOOLKIT_ROOT", REPO_ROOT)).resolve()
HELPERS = pathlib.Path(__file__).resolve().parent / "helpers"
TIMEOUT = int(os.environ.get("BPT_TEST_TIMEOUT", "600"))


@dataclass
class BlenderRun:
    returncode: int
    output: str
    probe: dict
    cmd: list

    def __str__(self):  # shown by pytest when an assertion fails
        tail = "\n".join(self.output.splitlines()[-40:])
        return f"exit {self.returncode}: {' '.join(self.cmd)}\n{tail}"


@pytest.fixture(scope="session")
def blender_exe():
    try:
        return find_blender().path
    except BlenderNotFound as exc:
        pytest.skip(f"Blender not available: {exc}")


@pytest.fixture(scope="session")
def toolkit_root():
    return TOOLKIT_ROOT


@pytest.fixture
def run_blender(blender_exe, tmp_path):
    """Run a toolkit script (and optional probes) in a fresh headless Blender.

    ``script``: file name under ``scripts/`` (or None); ``args``: the script's
    arguments; ``blend``: file to open first; ``probes``: names from
    ``helpers/probe.py`` run after the script in the same process.
    """
    counter = {"n": 0}

    def run(script=None, args=(), blend=None, probes=(), env=None, extra_python=()):
        counter["n"] += 1
        cmd = [blender_exe, "--background", "--factory-startup"]
        if blend is not None:
            cmd.append(str(blend))
        cmd += ["--python-exit-code", "1"]
        if script:
            cmd += ["--python", str(TOOLKIT_ROOT / "scripts" / script)]
        for extra in extra_python:
            cmd += ["--python", str(extra)]
        full_env = dict(os.environ)
        probe_out = tmp_path / f"probe_{counter['n']}.json"
        if probes:
            cmd += ["--python", str(HELPERS / "probe.py")]
            full_env["BPT_PROBE"] = ",".join(probes)
            full_env["BPT_PROBE_OUT"] = str(probe_out)
        full_env.update(env or {})
        cmd += ["--"] + [str(a) for a in args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp_path,
                              env=full_env, encoding="utf-8", errors="replace")
        probe = json.loads(probe_out.read_text(encoding="utf-8")) if probes and probe_out.exists() else {}
        return BlenderRun(proc.returncode, proc.stdout + proc.stderr, probe, cmd)

    return run


@pytest.fixture(scope="session")
def fixtures_dir(blender_exe, tmp_path_factory):
    """Generate the .blend/.glb fixtures once per session (nothing binary is committed)."""
    out = tmp_path_factory.mktemp("fixtures")
    proc = subprocess.run(
        [blender_exe, "--background", "--factory-startup", "--python-exit-code", "1",
         "--python", str(HELPERS / "make_fixtures.py"), "--", str(out)],
        capture_output=True, text=True, timeout=TIMEOUT, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return out


def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
