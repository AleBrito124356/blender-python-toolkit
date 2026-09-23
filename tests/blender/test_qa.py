"""Render QA: inspect_render.py on deliberately broken scenes, and every script's report."""

import pytest

from lib import core
from tests.blender.conftest import HELPERS, load_json

pytestmark = pytest.mark.blender


@pytest.mark.parametrize("scene, failing", [
    ("qa_good", set()),
    ("qa_crop", {"in_frame"}),
    ("qa_black", {"image_exposure", "image_detail"}),
    ("qa_float", {"ground_contact"}),
])
def test_inspect_flags_each_broken_scene(run_blender, fixtures_dir, tmp_path, scene, failing):
    report_path = tmp_path / f"{scene}.json"
    run = run_blender("inspect_render.py", ["--input", fixtures_dir / f"{scene}.blend",
                                            "--render", tmp_path / f"{scene}.png",
                                            "--report", report_path, "--qa-strict"])
    assert run.returncode == (3 if failing else 0), run
    report = load_json(report_path)
    assert core.validate_report(report) == []
    assert {c["id"] for c in report["checks"] if c["status"] == "fail"} == failing
    assert f"QA {'FAIL' if failing else 'PASS'}" in run.output


def test_inspect_without_strict_reports_but_exits_0(run_blender, fixtures_dir):
    run = run_blender("inspect_render.py", ["--input", fixtures_dir / "qa_float.blend"])
    assert run.returncode == 0 and "[FAIL] ground_contact" in run.output


def test_inspect_declared_ground_and_no_ground_check(run_blender, fixtures_dir, tmp_path):
    run = run_blender("inspect_render.py", ["--input", fixtures_dir / "qa_float.blend", "--no-ground-check",
                                            "--qa-strict"])
    assert run.returncode == 0, run
    run = run_blender("inspect_render.py", ["--input", fixtures_dir / "qa_float.blend", "--ground-z", 1.0,
                                            "--qa-strict"])
    assert run.returncode == 0, run


def test_projection_matches_bpy_extras(run_blender, tmp_path):
    out = tmp_path / "proj.json"
    run = run_blender(extra_python=[HELPERS / "check_projection.py"], env={"PROJ_OUT": str(out)},
                      args=[])
    assert run.returncode == 0, run
    result = load_json(out)
    for case, err in result.items():
        assert err < 1e-6, (case, err)


@pytest.mark.parametrize("script, args", [
    ("procedural_city.py", ["--blocks", 2, "--out", "img.png"]),
    ("product_turntable.py", ["--still", "--out", "tt"]),
    ("csv_to_bars3d.py", ["--out", "img.png"]),
    ("material_library.py", ["--spheres", "--out", "lib.blend", "--preview", "img.png"]),
])
def test_every_toolkit_script_passes_its_own_qa(run_blender, tmp_path, script, args):
    report_path = tmp_path / "report.json"
    run = run_blender(script, args + ["--render", "--res", "240x136", "--samples", 2,
                                      "--report", report_path, "--qa-strict"])
    assert run.returncode == 0, run
    report = load_json(report_path)
    assert core.validate_report(report) == []
    assert report["passed"], core.format_report_text(report)
    assert report["images"] and "mean_luminance" in report["images"][0]
