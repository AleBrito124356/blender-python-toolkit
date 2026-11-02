"""`bpt --json`: one JSON object on stdout for batch / inspect, logs on stderr, stable exit codes."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.blender.conftest import TIMEOUT, TOOLKIT_ROOT

pytestmark = pytest.mark.blender


def bpt_json(runtime, *args, cwd=None):
    proc = subprocess.run([sys.executable, "-m", "bpt", "--blender", runtime.path, "--json", *map(str, args)],
                          capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd or TOOLKIT_ROOT,
                          env={**os.environ}, encoding="utf-8", errors="replace")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(f"stdout is not one JSON document:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    assert data["exit_code"] == proc.returncode
    return proc, data


def test_inspect_json_reports_qa_without_failing(blender_runtime, fixtures_dir):
    proc, data = bpt_json(blender_runtime, "inspect", "--input", fixtures_dir / "qa_float.blend")
    assert proc.returncode == 0 and data["status"] == "ok"
    qa = data["summary"]["qa"]
    assert qa["passed"] is False and qa["failing"] == ["ground_contact"]
    assert "QA FAIL" in proc.stderr


def test_inspect_json_qa_strict_is_exit_3(blender_runtime, fixtures_dir, tmp_path):
    report = tmp_path / "r.json"
    proc, data = bpt_json(blender_runtime, "inspect", "--input", fixtures_dir / "qa_crop.blend",
                          "--render", tmp_path / "crop.png", "--res", "96x54", "--samples", 1,
                          "--report", report, "--qa-strict")
    assert proc.returncode == 3 and data["status"] == "check_failed"
    summary = data["summary"]
    assert summary["status"] == "check_failed" and summary["report"] == str(report)
    assert summary["outputs"] == [str(tmp_path / "crop.png")] and "in_frame" in summary["qa"]["failing"]
    assert data["log_tail"]  # the end of the log travels with a failure


def test_batch_json_has_per_file_status(blender_runtime, fixtures_dir, tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    for name in ("hero", "nocam", "corrupt"):
        shutil.copy(fixtures_dir / f"{name}.blend", src / f"{name}.blend")
    proc, data = bpt_json(blender_runtime, "batch", "--input", src, "--output", tmp_path / "out",
                          "--resolution", "96x54", "--samples", 1)
    assert proc.returncode == 1 and data["status"] == "failed"
    summary = data["summary"]
    assert summary["counts"] == {"OK": 1, "SKIPPED": 1, "ERROR": 1, "QA_FAILED": 0}
    by_file = {r["file"]: r for r in summary["results"]}
    assert by_file["hero"]["qa"]["passed"] and by_file["hero"]["output"].endswith("hero.png")
    assert by_file["corrupt"]["error"] and by_file["nocam"]["status"] == "SKIPPED"
    assert summary["summary_json"].endswith("summary.json")


def test_generator_json_lists_outputs(blender_runtime, tmp_path):
    out = tmp_path / "city.png"
    proc, data = bpt_json(blender_runtime, "city", "--blocks", 1, "--render", "--res", "64x36", "--samples", 1,
                          "--engine", "cycles", "--out", out, "--report", tmp_path / "q.json")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert data["summary"]["outputs"] == [str(out)] and data["summary"]["qa"]["passed"] is not None


@pytest.mark.parametrize("command, args, message", [
    ("inspect", ["--input", "missing.blend"], "Input .blend not found"),
    ("batch", ["--input", "no_such_folder"], "Input folder not found"),
    ("bars", ["--csv", "missing.csv"], "CSV not found"),
    ("turntable", ["--model", "missing.glb"], "Model not found"),
    ("cleanup", ["--input", "missing.blend"], "Input .blend not found"),
    ("presets", ["--input", "missing.blend"], "Input .blend not found"),
    ("export", ["--input", "missing.glb"], "input not found"),
    ("thumbnails", ["--input", "missing_dir"], "input not found"),
])
def test_missing_input_is_exit_2_everywhere(blender_runtime, tmp_path, command, args, message):
    proc, data = bpt_json(blender_runtime, command, *args, cwd=tmp_path)
    assert proc.returncode == 2 and data["status"] == "invalid_input", proc.stderr[-2000:]
    assert any(message in line for line in data["log_tail"])
