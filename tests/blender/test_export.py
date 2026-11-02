"""scripts/export_web.py (bpt export) in a real Blender or a bpy interpreter."""

import json
import os
import subprocess
import sys

import pytest

from lib import core
from tests.blender.conftest import HELPERS, TIMEOUT, TOOLKIT_ROOT, load_json

pytestmark = pytest.mark.blender


def glb_stats(path):
    """Read the .glb with lib.core (no Blender): the file itself, not the report."""
    doc, binary = core.read_glb(path.read_bytes())
    return core.gltf_stats(doc, binary)


def test_blend_to_glb_and_usdz_with_budget_and_texture_limit(run_blender, fixtures_dir, tmp_path, blender_caps):
    out = tmp_path / "web" / "product.glb"
    args = ["--input", fixtures_dir / "textured.blend", "--output", out, "--max-triangles", 2000,
            "--texture-max", 64]
    if blender_caps["usd"]:
        args.append("--usdz")
    run = run_blender("export_web.py", args)
    assert run.returncode == 0, run
    report = load_json(tmp_path / "web" / "product.export.json")
    assert report["status"] == "ok" and report["verification"]["passed"], run
    # The floor is tagged qa_role=backdrop and is not exported; the text label is converted to a mesh.
    assert report["source"]["objects"] == 2
    assert any("Label" in w for w in report["warnings"])
    assert report["source"]["triangles"] > 2000 >= report["export"]["triangles"] > 1500
    assert 0 < report["export"]["decimate_ratio"] < 1
    # Bounds in metres: the ball is 1 m across and stands on z=0 (it does not include the 6 m floor).
    assert report["bounds"]["size"][0] == pytest.approx(1.0, abs=0.02)
    assert report["bounds"]["min"][2] == pytest.approx(0.0, abs=0.03)
    (texture,) = report["textures"]
    assert texture["source_size"] == [512, 256] and texture["size"] == [64, 32] and texture["downscaled"]
    assert report["materials"] == [{"name": "Painted", "objects": 1, "textures": ["Checker"]}]
    # The file on disk, read without Blender, agrees with the report.
    stats = glb_stats(out)
    assert stats["triangles"] == report["export"]["triangles"]
    assert [(im["width"], im["height"]) for im in stats["images"]] == [(64, 32)]
    assert report["outputs"][0]["bytes"] == out.stat().st_size
    ids = {c["id"]: c["status"] for c in report["verification"]["checks"]}
    assert ids["glb_triangles"] == ids["glb_bounds"] == ids["glb_file_triangles"] == ids["glb_textures"] == "pass"
    if blender_caps["usd"]:
        usdz = out.with_suffix(".usdz")
        assert usdz.exists() and usdz.read_bytes()[:2] == b"PK"  # USDZ is a zip archive
        assert ids["usdz_triangles"] == ids["usdz_bounds"] == "pass"


def test_decimating_an_imported_glb_keeps_its_surface(run_blender, fixtures_dir, tmp_path):
    """glTF imports are split at every seam; without welding, Decimate deletes triangles (holes)."""
    out = tmp_path / "monkey.glb"
    run = run_blender("export_web.py", ["--input", fixtures_dir / "suzanne.glb", "--output", out,
                                        "--decimate", 0.5])
    assert run.returncode == 0, run
    report = load_json(tmp_path / "monkey.export.json")
    src, exp = report["source"], report["export"]
    assert exp["triangles"] == pytest.approx(src["triangles"] * 0.5, rel=0.1)
    assert exp["area"] == pytest.approx(src["area"], rel=0.08)  # deleting half the triangles would halve it
    for i in range(3):  # Blender Z-up bounds survive the +Y-up file
        assert report["bounds"]["size"][i] == pytest.approx(src["bounds"]["size"][i], rel=0.05)


def test_no_apply_modifiers_exports_the_base_mesh(run_blender, fixtures_dir, tmp_path):
    out = tmp_path / "base.glb"
    run = run_blender("export_web.py", ["--input", fixtures_dir / "textured.blend", "--output", out,
                                        "--no-apply-modifiers"])
    assert run.returncode == 0, run
    report = load_json(tmp_path / "base.export.json")
    assert report["export"]["triangles"] < report["source"]["triangles"]  # subdivision not applied
    assert glb_stats(out)["triangles"] == report["export"]["triangles"]


def test_draco_is_refused_or_really_compresses(run_blender, fixtures_dir, tmp_path):
    out = tmp_path / "draco.glb"
    run = run_blender("export_web.py", ["--input", fixtures_dir / "suzanne.glb", "--output", out, "--draco", 7])
    if run.returncode == 2:  # e.g. the PyPI bpy wheel: no Draco encoder library
        assert "no Draco encoder" in run.output and not out.exists()
    else:
        assert run.returncode == 0, run
        assert glb_stats(out)["draco"]


def test_a_round_trip_that_disagrees_fails_with_exit_code_3(run_blender, fixtures_dir, tmp_path):
    out = tmp_path / "drift.glb"
    run = run_blender(extra_python=[HELPERS / "export_with_fault.py"],
                      args=["--input", fixtures_dir / "suzanne.glb", "--output", out])
    assert run.returncode == 3, run
    report = load_json(tmp_path / "drift.export.json")
    status = {c["id"]: c["status"] for c in report["verification"]["checks"]}
    assert report["status"] == "check_failed" and status["glb_bounds"] == "fail"
    assert status["glb_triangles"] == "pass" and out.exists()


@pytest.mark.parametrize("name, message", [
    ("missing.glb", "input not found"),
    ("corrupt.blend", "cannot open .blend"),
    ("models/broken.glb", "importer failed"),
    ("messy.blend", None),  # a valid input: control case
])
def test_bad_inputs_exit_2(run_blender, fixtures_dir, tmp_path, name, message):
    run = run_blender("export_web.py", ["--input", fixtures_dir / name, "--output", tmp_path / "x.glb",
                                        "--no-verify"])
    if message is None:
        assert run.returncode == 0, run
    else:
        assert run.returncode == 2 and message in run.output, run


def test_bpt_json_mode_prints_one_summary_object(blender_runtime, fixtures_dir, tmp_path):
    out = tmp_path / "agent.glb"
    proc = subprocess.run([sys.executable, "-m", "bpt", "--blender", blender_runtime.path, "--json", "export",
                           "--input", str(fixtures_dir / "suzanne.glb"), "--output", str(out),
                           "--max-triangles", "500"],
                          capture_output=True, text=True, timeout=TIMEOUT, cwd=TOOLKIT_ROOT,
                          env={**os.environ}, encoding="utf-8", errors="replace")
    data = json.loads(proc.stdout)  # stdout holds exactly one JSON document
    assert proc.returncode == 0 and data["exit_code"] == 0 and data["status"] == "ok", proc.stderr[-3000:]
    assert data["runtime"]["kind"] == blender_runtime.kind
    summary = data["summary"]
    assert summary["script"] == "export_web" and summary["status"] == "ok"
    assert str(out) in summary["outputs"]
    assert summary["export"]["export"]["triangles"] <= 500
    assert summary["export"]["verification"]["passed"]
    assert "[export] wrote" in proc.stderr  # human logs went to stderr
