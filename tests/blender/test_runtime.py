"""The runtime contract every script relies on, for a Blender executable and a bpy interpreter alike.

Run against both kinds: ``BLENDER_EXE=/path/to/blender`` or
``BLENDER_EXE=/path/to/venv/bin/python`` (an interpreter that can import bpy,
driven through ``bpt/blender_shim.py``). Each test only asserts behaviour that
Blender itself has, so the shim is held to Blender's command line.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.blender.conftest import TIMEOUT, TOOLKIT_ROOT

pytestmark = pytest.mark.blender

REPORT_SCRIPT = r'''
import json, os, sys
sys.path.insert(0, os.environ["BPT_ROOT"])
from lib.core import get_argv_after_dashes
import bpy
print("BPT_RUNTIME=" + json.dumps({
    "name": __name__,
    "file": __file__,
    "args": get_argv_after_dashes(),
    "background": bpy.app.background,
    "objects": sorted(o.name for o in bpy.data.objects),
    "blend": bpy.data.filepath,
}))
'''


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def run(blender_cmd, args, tmp_path):
    proc = subprocess.run(blender_cmd + [str(a) for a in args], capture_output=True, text=True, timeout=TIMEOUT,
                          cwd=tmp_path, env={**os.environ, "BPT_ROOT": str(TOOLKIT_ROOT)},
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout + proc.stderr


def reported(output):
    lines = [ln for ln in output.splitlines() if ln.startswith("BPT_RUNTIME=")]
    return [json.loads(ln[len("BPT_RUNTIME="):]) for ln in lines]


def test_script_sees_its_arguments_file_and_main_name(blender_cmd, tmp_path):
    script = write(tmp_path / "report.py", REPORT_SCRIPT)
    code, out = run(blender_cmd, ["--background", "--factory-startup", "--python-exit-code", "1", "--python", script,
                                  "--", "--seed", "7", "--name", "with space", "--", "x"], tmp_path)
    assert code == 0, out
    (info,) = reported(out)
    assert info["name"] == "__main__" and info["file"] == str(script)
    assert info["args"] == ["--seed", "7", "--name", "with space", "--", "x"]
    assert info["background"] is True
    assert info["objects"] == ["Camera", "Cube", "Light"]  # factory startup scene


@pytest.mark.parametrize("body, expected", [
    ("raise ValueError('boom')", 1),
    ("import sys; sys.exit(5)", 5),
    ("raise SystemExit('stopped with a message')", 1),
    ("import sys; sys.exit(0)", 0),
])
def test_exit_codes_pass_through_and_stop_processing(blender_cmd, tmp_path, body, expected):
    first = write(tmp_path / "first.py", body + "\n")
    after = write(tmp_path / "after.py", "print('AFTER_RAN')\n")
    code, out = run(blender_cmd, ["--background", "--factory-startup", "--python-exit-code", "1",
                                  "--python", first, "--python", after, "--"], tmp_path)
    assert code == expected, out
    assert "AFTER_RAN" not in out  # an exception (with the exit code set) or sys.exit ends the run
    if "message" in body:
        assert "stopped with a message" in out
    if "boom" in body:
        assert "ValueError: boom" in out


def test_without_python_exit_code_an_exception_exits_0(blender_cmd, tmp_path):
    """Why bpt always passes --python-exit-code 1: Blender's default hides failures."""
    first = write(tmp_path / "first.py", "raise RuntimeError('ignored by default')\n")
    after = write(tmp_path / "after.py", "print('AFTER_RAN')\n")
    code, out = run(blender_cmd, ["--background", "--factory-startup", "--python", first, "--python", after],
                    tmp_path)
    assert code == 0 and "ignored by default" in out and "AFTER_RAN" in out


def test_blend_argument_is_opened_before_the_script(blender_cmd, fixtures_dir, tmp_path):
    script = write(tmp_path / "report.py", REPORT_SCRIPT)
    blend = fixtures_dir / "qa_good.blend"
    code, out = run(blender_cmd, ["--background", "--factory-startup", blend, "--python-exit-code", "1",
                                  "--python", script, "--"], tmp_path)
    assert code == 0, out
    (info,) = reported(out)
    assert info["blend"] == str(blend) and "Ball" in info["objects"] and "Cube" not in info["objects"]


def test_unreadable_blend_fails_and_skips_the_script(blender_cmd, fixtures_dir, tmp_path):
    script = write(tmp_path / "report.py", REPORT_SCRIPT)
    code, out = run(blender_cmd, ["--background", "--factory-startup", fixtures_dir / "corrupt.blend",
                                  "--python-exit-code", "1", "--python", script, "--"], tmp_path)
    assert code != 0, out
    assert not reported(out)


def test_python_expr_runs(blender_cmd, tmp_path):
    code, out = run(blender_cmd, ["--background", "--factory-startup", "--python-exit-code", "1",
                                  "--python-expr", "import bpy; print('EXPR', bpy.app.version_string)"], tmp_path)
    assert code == 0 and "EXPR " in out, out


def test_bpt_launcher_runs_a_script_end_to_end(blender_runtime, tmp_path):
    """python -m bpt --blender <runtime> run ...: the same command line bpt users get."""
    script = write(tmp_path / "report.py", REPORT_SCRIPT)
    proc = subprocess.run([sys.executable, "-m", "bpt", "--blender", blender_runtime.path, "run", str(script),
                           "--flag", "1"], capture_output=True, text=True, timeout=TIMEOUT, cwd=TOOLKIT_ROOT,
                          env={**os.environ, "BPT_ROOT": str(TOOLKIT_ROOT)},
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (info,) = reported(proc.stdout + proc.stderr)
    assert info["args"] == ["--flag", "1"]


def test_factory_startup_ignores_user_preferences_and_its_absence_loads_them(blender_cmd, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    env_args = ["--background", "--factory-startup", "--python-exit-code", "1", "--python-expr",
                "import bpy; bpy.context.preferences.view.ui_scale = 1.7; bpy.ops.wm.save_userpref()"]
    proc = subprocess.run(blender_cmd + env_args, capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp_path,
                          env={**os.environ, "BLENDER_USER_CONFIG": str(config)}, encoding="utf-8", errors="replace")
    assert proc.returncode == 0 and (config / "userpref.blend").exists(), proc.stdout + proc.stderr
    show = "import bpy; print('UI_SCALE', round(bpy.context.preferences.view.ui_scale, 2))"
    for flags, expected in ((["--factory-startup"], "UI_SCALE 1.0"), ([], "UI_SCALE 1.7")):
        proc = subprocess.run(blender_cmd + ["--background", *flags, "--python-exit-code", "1", "--python-expr", show],
                              capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp_path,
                              env={**os.environ, "BLENDER_USER_CONFIG": str(config)},
                              encoding="utf-8", errors="replace")
        assert proc.returncode == 0 and expected in proc.stdout, (flags, proc.stdout[-2000:], proc.stderr[-2000:])
