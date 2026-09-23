"""product_turntable.py in a real Blender."""

import pytest

from lib import core
from tests.blender.conftest import load_json

pytestmark = pytest.mark.blender


def test_animation_renders_every_frame_with_linear_keys(run_blender, tmp_path):
    out = tmp_path / "tt"
    run = run_blender("product_turntable.py",
                      ["--frames", 6, "--render", "--res", "96x96", "--samples", 1, "--out", out],
                      probes=["keyframes", "camera_path"])
    assert run.returncode == 0, run
    assert sorted(p.name for p in out.glob("frame_*.png")) == [f"frame_{i:04d}.png" for i in range(1, 7)]
    keys = run.probe["keyframes"]["OrbitPivot"]
    assert keys, run
    assert all(k[2] == "LINEAR" for curve in keys for k in curve["keys"])
    rot_z = next(c for c in keys if c["path"] == "rotation_euler" and c["index"] == 2)
    assert [k[0] for k in rot_z["keys"]] == [1.0, 7.0]  # 360 degrees lands on frames + 1: seamless loop
    positions = list(run.probe["camera_path"].values())
    assert positions[0] != positions[-1]  # the camera really orbits


def test_video_mp4_has_every_frame(run_blender, tmp_path):
    out = tmp_path / "tt"
    run = run_blender("product_turntable.py",
                      ["--frames", 6, "--render", "--video", "mp4", "--res", "96x96", "--samples", 1,
                       "--out", out])
    assert run.returncode == 0, run
    video = out / "turntable.mp4"
    assert video.exists() and video.stat().st_size > 0
    movie = run_blender(probes=["movie"], env={"BPT_PROBE_MOVIE": str(video)})
    assert movie.returncode == 0, movie
    assert movie.probe["movie"]["frames"] == 6
    assert movie.probe["movie"]["size"] == [96, 96]


def test_video_rejects_odd_resolution(run_blender):
    run = run_blender("product_turntable.py", ["--video", "mp4", "--res", "95x96", "--render"])
    assert run.returncode == 2 and "even width and height" in run.output


def test_rotated_import_rests_on_the_floor_and_fits(run_blender, fixtures_dir):
    run = run_blender("product_turntable.py", ["--model", fixtures_dir / "suzanne.glb", "--still"],
                      probes=["lowest_z", "projection"])
    assert run.returncode == 0, run
    monkey = [name for name in run.probe["lowest_z"] if name.startswith("Suzanne")]
    assert monkey, run.probe
    assert run.probe["lowest_z"][monkey[0]] == pytest.approx(0.0, abs=1e-4)  # was 0.043 (floating)
    proj = run.probe["projection"]["objects"][monkey[0]]
    assert 0.0 <= proj["x"][0] and proj["x"][1] <= 1.0 and 0.0 <= proj["y"][0] and proj["y"][1] <= 1.0
    area = (proj["x"][1] - proj["x"][0]) * (proj["y"][1] - proj["y"][0])
    assert area > 0.3  # was ~0.15 of the frame


def test_orbit_qa_report_passes_on_every_sampled_frame(run_blender, tmp_path):
    report_path = tmp_path / "tt.json"
    run = run_blender("product_turntable.py",
                      ["--frames", 24, "--backdrop", "cyclorama", "--report", report_path, "--qa-strict"])
    assert run.returncode == 0, run
    report = load_json(report_path)
    assert core.validate_report(report) == []
    assert report["passed"] and len(report["composition"]["frames"]) == 8
    roles = {o["name"]: o["role"] for o in report["composition"]["objects"]}
    assert roles["Cyclorama"] == "backdrop" and roles["DemoProduct"] == "subject"
