"""csv_to_bars3d.py in a real Blender."""

import pytest

from lib import core
from tests.blender.conftest import load_json

pytestmark = pytest.mark.blender


@pytest.fixture
def sales_csv(repo_root):
    return repo_root / "data" / "sales.csv"


def test_heights_are_proportional_and_bars_stand_on_zero(run_blender, sales_csv):
    run = run_blender("csv_to_bars3d.py", ["--csv", sales_csv], probes=["bars"])
    assert run.returncode == 0, run
    bars = run.probe["bars"]["bars"]
    assert len(bars) == 12
    by_index = {b["index"]: b for b in bars.values()}
    jan, dec = by_index[0], by_index[11]
    assert dec["height"] / jan["height"] == pytest.approx(120 / 42, rel=1e-6)  # was 15.0
    for bar in bars.values():
        assert bar["bottom"] == pytest.approx(0.0, abs=1e-5)  # was 0.10-1.50 (floating)


def test_labels_sit_in_front_of_bars_and_values_above(run_blender, sales_csv):
    run = run_blender("csv_to_bars3d.py", ["--csv", sales_csv], probes=["bars"])
    assert run.returncode == 0, run
    bars = {b["index"]: b for b in run.probe["bars"]["bars"].values()}
    labels = run.probe["bars"]["labels"]
    camera_y = run.probe["bars"]["camera_y"]
    for i, bar in bars.items():
        assert camera_y < bar["front_y"]  # the camera looks at the front faces
        label = labels[f"Label_{i:02d}"]
        # Every vertex of the category label lies between the camera and the bar's
        # front face (it used to sit 0.95 behind it: 'Feb' rendered as 'b').
        assert label["max_y"] < bar["front_y"], i
        value = labels[f"Value_{i:02d}"]
        assert value["min_z"] > bar["top"], i


def test_chart_fits_the_default_frame(run_blender, sales_csv):
    run = run_blender("csv_to_bars3d.py", ["--csv", sales_csv], probes=["projection"])
    assert run.returncode == 0, run
    assert run.probe["projection"]["resolution"] == [1600, 900]
    for name, proj in run.probe["projection"]["objects"].items():
        if proj["role"] in ("subject", "annotation"):
            assert proj["x"][0] >= 0 and proj["x"][1] <= 1 and proj["y"][0] >= 0 and proj["y"][1] <= 1, name


def test_negative_values_hang_below_the_axis(run_blender, repo_root, tmp_path):
    report = tmp_path / "neg.json"
    run = run_blender("csv_to_bars3d.py", ["--csv", repo_root / "data" / "profit_margin.csv",
                                           "--value-col", "margin_pct", "--report", report],
                      probes=["bars"])
    assert run.returncode == 0, run
    assert "skipped line 7" in run.output  # the 'n/a' row is reported, not silently dropped
    bars = {b["index"]: b for b in run.probe["bars"]["bars"].values()}
    south = bars[1]
    assert south["top"] == pytest.approx(0.0, abs=1e-5) and south["bottom"] < 0
    north = bars[0]
    assert north["height"] / south["height"] == pytest.approx(12.5 / 4.2, rel=1e-6)
    assert load_json(report)["passed"]


def test_missing_csv_fails(run_blender, tmp_path):
    run = run_blender("csv_to_bars3d.py", ["--csv", tmp_path / "missing.csv"])
    assert run.returncode != 0 and "CSV not found" in run.output


def test_strict_csv_fails_on_skipped_rows(run_blender, tmp_path):
    csv_path = tmp_path / "dirty.csv"
    csv_path.write_text("label,value\nA,1\nB,oops\nC,3\n", encoding="utf-8")
    ok = run_blender("csv_to_bars3d.py", ["--csv", csv_path])
    assert ok.returncode == 0 and "skipped line 3" in ok.output
    strict = run_blender("csv_to_bars3d.py", ["--csv", csv_path, "--strict-csv"])
    assert strict.returncode == 2


def test_grow_in_animation_is_eased_and_renders_to_mp4(run_blender, sales_csv, tmp_path):
    out = tmp_path / "bars.png"
    run = run_blender("csv_to_bars3d.py", ["--csv", sales_csv, "--animate", "--frames", 12, "--render",
                                           "--video", "mp4", "--res", "160x90", "--samples", 1, "--out", out],
                      probes=["keyframes"])
    assert run.returncode == 0, run
    curves = run.probe["keyframes"]["Bar_00_Jan"]
    scale_z = next(c for c in curves if c["path"] == "scale" and c["index"] == 2)
    (f0, v0, interp, easing), (f1, v1, _, _) = scale_z["keys"]
    assert (v0, v1) == (0.0, 1.0) and f0 < f1
    assert (interp, easing) == ("CUBIC", "EASE_OUT")
    video = tmp_path / "bars.mp4"
    assert video.exists()
    movie = run_blender(probes=["movie"], env={"BPT_PROBE_MOVIE": str(video)})
    assert movie.probe["movie"]["frames"] == 12


def test_report_has_no_label_problems(run_blender, sales_csv, tmp_path):
    report_path = tmp_path / "bars.json"
    run = run_blender("csv_to_bars3d.py", ["--csv", sales_csv, "--report", report_path, "--qa-strict"])
    assert run.returncode == 0, run
    report = load_json(report_path)
    assert core.validate_report(report) == []
    status = {c["id"]: c["status"] for c in report["checks"]}
    assert status["annotation_overlap"] == "pass" and status["annotation_occluded"] == "pass"
