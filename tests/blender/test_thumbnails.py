"""scripts/thumbnails.py (bpt thumbnails) in a real Blender or a bpy interpreter."""

import os
import shutil

import pytest

from tests.blender.conftest import load_json

pytestmark = pytest.mark.blender


@pytest.fixture
def asset_folder(fixtures_dir, tmp_path):
    folder = tmp_path / "assets"
    folder.mkdir()
    for name in ("cone.obj", "cone.mtl", "torus.stl", "broken.glb"):
        shutil.copy(fixtures_dir / "models" / name, folder / name)
    shutil.copy(fixtures_dir / "suzanne.glb", folder / "suzanne.glb")
    shutil.copy(fixtures_dir / "textured.blend", folder / "textured.blend")
    return folder


def image_probe(run_blender, paths):
    run = run_blender(probes=["images"], env={"BPT_PROBE_IMAGES": os.pathsep.join(str(p) for p in paths)})
    assert run.returncode == 0, run
    return run.probe["images"]


def test_folder_to_thumbnails_sheet_and_index(run_blender, asset_folder, tmp_path):
    out = tmp_path / "thumbs"
    run = run_blender("thumbnails.py", ["--input", asset_folder, "--output", out, "--size", 64,
                                        "--engine", "workbench"])
    assert run.returncode == 0, run  # a broken file is reported, not fatal
    index = load_json(out / "index.json")
    items = {i["file"]: i for i in index["items"]}
    assert set(items) == {"broken.glb", "cone.obj", "suzanne.glb", "textured.blend", "torus.stl"}
    assert index["counts"] == {"ok": 4, "error": 1, "qa_failed": 0}
    broken = items["broken.glb"]
    assert broken["status"] == "error" and "importer failed" in broken["error"] and broken["thumbnail"] is None
    assert items["torus.stl"]["triangles"] == 1152 and items["cone.obj"]["triangles"] == 62
    assert items["torus.stl"]["bounds"]["size"] == pytest.approx([2.6, 2.6, 0.6], abs=1e-4)
    assert items["textured.blend"]["objects"] == 2  # ball + label; the backdrop floor is not a subject
    thumbs = [out / f"{stem}.png" for stem in ("cone", "suzanne", "textured", "torus")]
    probe = image_probe(run_blender, thumbs + [out / "contact_sheet.png"])
    for path in thumbs:
        info = probe[path.name]
        assert info["size"] == [64, 64] and info["channels"] == 4, path
        assert info["alpha_min"] == 0.0 and 0.15 < info["opaque_fraction"] < 0.9, (path, info)
        assert info["border_alpha_max"] < 0.05, (path, info)  # framed with a margin: nothing touches the edge
    sheet = probe["contact_sheet.png"]
    assert sheet["size"][0] > 3 * 64 and sheet["alpha_min"] == 1.0
    assert items["cone.obj"]["sheet_cell"] == [1, 0]  # sorted: broken, cone, suzanne / textured, torus


def test_strict_turns_a_broken_file_into_exit_1(run_blender, asset_folder, tmp_path):
    for name in ("suzanne.glb", "textured.blend", "torus.stl", "cone.obj", "cone.mtl"):
        (asset_folder / name).unlink()
    run = run_blender("thumbnails.py", ["--input", asset_folder, "--output", tmp_path / "t", "--size", 32,
                                        "--engine", "workbench", "--strict", "--no-sheet"])
    assert run.returncode == 1, run
    assert load_json(tmp_path / "t" / "index.json")["exit_code"] == 1
    assert not (tmp_path / "t" / "contact_sheet.png").exists()


def test_solid_background_default_eevee_and_missing_texture(run_blender, fixtures_dir, tmp_path):
    out = tmp_path / "thumbs"
    run = run_blender("thumbnails.py", ["--input", fixtures_dir / "missing_tex.blend",
                                        fixtures_dir / "models" / "cone.obj", "--output", out, "--size", 48,
                                        "--samples", 1, "--background", "#204060", "--no-sheet"])
    assert run.returncode == 0, run
    index = load_json(out / "index.json")
    assert index["engine"] == "eevee" and index["background"] == "#204060"
    items = {i["file"]: i for i in index["items"]}
    assert items["missing_tex.blend"]["missing_textures"] == ["Gone"]
    assert items["missing_tex.blend"]["status"] == "ok" and items["missing_tex.blend"]["warnings"]
    probe = image_probe(run_blender, [out / "cone.png"])["cone.png"]
    assert probe["alpha_min"] == 1.0  # composited over the colour
    assert probe["corner_rgba"] == pytest.approx([0x20 / 255, 0x40 / 255, 0x60 / 255, 1.0], abs=1 / 255)


def test_missing_input_is_invalid(run_blender, tmp_path):
    run = run_blender("thumbnails.py", ["--input", tmp_path / "nope", "--output", tmp_path / "t"])
    assert run.returncode == 2 and "input not found" in run.output
