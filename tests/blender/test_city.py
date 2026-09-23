"""procedural_city.py in a real Blender."""

import pytest

pytestmark = pytest.mark.blender

MAX_FOOT = 8.0 - 2.2  # BLOCK_SIZE - STREET_GAP


def test_buildings_stand_on_the_ground_at_full_size(run_blender):
    run = run_blender("procedural_city.py", ["--blocks", 3, "--seed", 7], probes=["lowest_z", "projection"])
    assert run.returncode == 0, run
    buildings = {k: v for k, v in run.probe["lowest_z"].items() if k.startswith("Building_")}
    assert len(buildings) == 9
    for name, z in buildings.items():
        assert z == pytest.approx(0.0, abs=1e-4), name  # was 1.0-7.07 (floating)
    for name, proj in run.probe["projection"]["objects"].items():
        if name.startswith("Building_"):
            assert proj["x"][0] >= 0 and proj["x"][1] <= 1 and proj["y"][0] >= 0 and proj["y"][1] <= 1, name


def test_buildings_have_their_intended_dimensions(run_blender, tmp_path):
    blend = tmp_path / "city.blend"
    assert run_blender("procedural_city.py", ["--blocks", 2, "--save-blend", blend]).returncode == 0
    check = tmp_path / "dims.py"
    check.write_text(
        "import bpy, json, os\n"
        "d = {o.name: list(o.dimensions) for o in bpy.data.objects if o.name.startswith('Building_')}\n"
        "open(os.environ['DIMS_OUT'], 'w').write(json.dumps(d))\n")
    out = tmp_path / "dims.json"
    run = run_blender(blend=blend, extra_python=[check], env={"DIMS_OUT": str(out)})
    assert run.returncode == 0, run
    import json

    dims = json.loads(out.read_text())
    for name, (w, d, h) in dims.items():
        assert MAX_FOOT * 0.55 - 1e-6 <= w <= MAX_FOOT + 1e-6, name  # was half size
        assert MAX_FOOT * 0.55 - 1e-6 <= d <= MAX_FOOT + 1e-6, name
        assert h >= 4.0 - 1e-6, name


def test_dusk_sky_uses_a_physical_model(run_blender):
    run = run_blender("procedural_city.py", ["--blocks", 1], probes=["scene"])
    assert run.returncode == 0, run
    skies = run.probe["scene"]["sky_types"]
    assert skies and skies[0] in {"MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING"}
    assert "sky=" + skies[0] in run.output  # the chosen model is reported, not silently swallowed


def test_window_grid_appears_on_all_four_walls(run_blender):
    run = run_blender("procedural_city.py", ["--blocks", 1, "--seed", 3, "--lit", 0.6], probes=["facade"])
    assert run.returncode == 0, run
    walls = run.probe["facade"]
    assert set(walls) == {"-X", "+X", "-Y", "+Y"}
    for wall, stats in walls.items():
        # A grid varies both across (columns) and up (rows) the wall; the old XY brick
        # texture gave streaks on the Y walls and flat colour on the X walls.
        assert stats["col_profile_std"] > 0.02, (wall, stats)
        assert stats["row_profile_std"] > 0.02, (wall, stats)
        assert 0.03 < stats["bright_fraction"] < 0.9, (wall, stats)  # some windows lit, not all


def test_cycles_engine_opt_in_renders(run_blender, tmp_path):
    out = tmp_path / "cyc.png"
    run = run_blender("procedural_city.py", ["--blocks", 2, "--render", "--engine", "cycles", "--res",
                                             "160x90", "--samples", 2, "--out", out], probes=["scene"])
    assert run.returncode == 0, run
    assert out.exists() and run.probe["scene"]["engine"] == "CYCLES"


def test_legacy_cycles_flag_cannot_reach_a_script(run_blender):
    """Documents why --cycles was replaced: Blender's Cycles add-on rejects it first."""
    run = run_blender("procedural_city.py", ["--blocks", 1, "--cycles"])
    assert run.returncode == 2
    assert "ambiguous option: --cycles" in run.output
