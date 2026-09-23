"""batch_render.py, render_presets.py and cleanup_scene.py in a real Blender."""

import hashlib
import shutil

import pytest

from tests.blender.conftest import load_json

pytestmark = pytest.mark.blender


def md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def copy_fixtures(fixtures_dir, dest, names):
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(fixtures_dir / f"{name}.blend", dest / f"{name}.blend")
    return dest


def test_batch_survives_bad_files_and_exits_1(run_blender, fixtures_dir, tmp_path):
    src = copy_fixtures(fixtures_dir, tmp_path / "in", ["hero", "nocam", "videoout", "corrupt"])
    out = tmp_path / "out"
    run = run_blender("batch_render.py", ["--input", src, "--output", out, "--resolution", "160x90",
                                          "--samples", 1])
    assert run.returncode == 1, run  # was 0 even with failures
    summary = load_json(out / "summary.json")
    status = {r["file"]: r["status"] for r in summary["results"]}
    assert status == {"corrupt": "ERROR", "hero": "OK", "nocam": "SKIPPED", "videoout": "OK"}
    assert summary["exit_code"] == 1 and summary["counts"]["OK"] == 2
    assert (out / "hero.png").exists() and (out / "videoout.png").exists()  # video-configured file renders
    assert (out / "summary.csv").read_text(encoding="utf-8").startswith("file,status,seconds")
    ok = [r for r in summary["results"] if r["status"] == "OK"]
    assert all(r["qa"]["passed"] for r in ok)
    corrupt = next(r for r in summary["results"] if r["file"] == "corrupt")
    assert corrupt["detail"].startswith("cannot open .blend")


def test_batch_skips_only_fail_with_strict(run_blender, fixtures_dir, tmp_path):
    src = copy_fixtures(fixtures_dir, tmp_path / "in", ["hero", "nocam"])
    args = ["--input", src, "--output", tmp_path / "out", "--resolution", "160x90", "--samples", 1, "--no-qa"]
    assert run_blender("batch_render.py", args).returncode == 0
    assert run_blender("batch_render.py", args + ["--strict"]).returncode == 1


def test_presets_save_writes_a_new_file_and_keeps_the_input(run_blender, fixtures_dir, tmp_path):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["hero"]) / "hero.blend"
    before = md5(scene)
    run = run_blender("render_presets.py", ["--input", scene, "--preset", "preview", "--save"])
    assert run.returncode == 0, run
    saved = tmp_path / "hero_preview.blend"
    assert saved.exists() and md5(scene) == before
    reopened = run_blender(blend=saved, probes=["scene"])
    assert reopened.probe["scene"]["resolution"] == [960, 540, 100]
    assert reopened.probe["scene"]["samples"] == 16
    again = run_blender("render_presets.py", ["--input", scene, "--preset", "preview", "--save"])
    assert again.returncode == 0 and (tmp_path / "hero_preview_01.blend").exists()  # never overwrites


def test_presets_without_save_persist_nothing_and_say_so(run_blender, fixtures_dir, tmp_path):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["hero"]) / "hero.blend"
    before = md5(scene)
    run = run_blender("render_presets.py", ["--input", scene, "--preset", "social"])
    assert run.returncode == 0 and "nothing saved" in run.output
    assert md5(scene) == before


def test_presets_in_place_overwrites_on_request(run_blender, fixtures_dir, tmp_path):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["hero"]) / "hero.blend"
    run = run_blender("render_presets.py", ["--input", scene, "--preset", "social", "--in-place"])
    assert run.returncode == 0, run
    assert run_blender(blend=scene, probes=["scene"]).probe["scene"]["resolution"] == [1080, 1080, 100]


def test_presets_render_a_video_configured_scene(run_blender, fixtures_dir, tmp_path, repo_root):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["videoout"]) / "videoout.blend"
    presets = tmp_path / "tiny.yaml"
    presets.write_text("tiny:\n  resolution: [160, 90]\n  samples: 1\n  format: JPEG\n", encoding="utf-8")
    run = run_blender("render_presets.py", ["--input", scene, "--presets", presets, "--preset", "tiny",
                                            "--render", "--out", tmp_path / "shot.png"])
    assert run.returncode == 0, run  # was TypeError: enum "PNG" not found in ('FFMPEG')
    assert (tmp_path / "shot.jpg").exists()  # extension follows the preset format


def test_presets_list_and_validation(run_blender, tmp_path):
    run = run_blender("render_presets.py", ["--list"])
    assert run.returncode == 0 and "preview" in run.output and "builtin parser" in run.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("oops:\n  samples: 0\n  engine: luxcore\n", encoding="utf-8")
    run = run_blender("render_presets.py", ["--presets", bad, "--list"])
    assert run.returncode == 1 and "oops: samples" in run.output and "oops: engine" in run.output


def test_cleanup_keeps_normals_outward_and_reports_shared_mirrors(run_blender, fixtures_dir, tmp_path):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["messy"]) / "messy.blend"
    report = tmp_path / "cleanup.json"
    run = run_blender("cleanup_scene.py", ["--input", scene, "--execute", "--json", report])
    assert run.returncode == 0, run
    assert "shares mesh" in run.output
    cleaned = tmp_path / "messy_clean.blend"
    normals = run_blender(blend=cleaned, probes=["normals"]).probe["normals"]
    cube = normals["MESH_Cube"]
    assert cube["scale"] == [1.0, 1.0, 1.0]
    assert (cube["outward"], cube["inward"]) == (6, 0)  # was outward=0 inward=6 (double flip)
    shared = normals["MESH_SharedA"]
    assert shared["scale"][1] == -1.0  # left mirrored instead of silently un-mirrored with abs()
    assert load_json(report)["mirrored_shared"] == ["SharedA"]


def test_cleanup_dry_run_writes_nothing(run_blender, fixtures_dir, tmp_path):
    scene = copy_fixtures(fixtures_dir, tmp_path, ["messy"]) / "messy.blend"
    run = run_blender("cleanup_scene.py", ["--input", scene])
    assert run.returncode == 0 and "DRY-RUN" in run.output
    assert not list(tmp_path.glob("messy_clean*.blend"))
