"""material_library.py in a real Blender."""

import pytest

pytestmark = pytest.mark.blender

EXPECTED = {"Brushed Metal", "Plastic", "Rubber", "Wood", "Glass", "Emission", "Car Paint", "Ceramic"}


def test_library_blend_keeps_eight_fake_user_materials(run_blender, tmp_path):
    lib = tmp_path / "lib.blend"
    run = run_blender("material_library.py", ["--out", lib])
    assert run.returncode == 0, run
    reopened = run_blender(blend=lib, probes=["materials"])
    assert reopened.returncode == 0, reopened
    mats = reopened.probe["materials"]
    assert EXPECTED <= set(mats)
    assert all(mats[name]["fake_user"] for name in EXPECTED)
    assert mats["Glass"]["raytrace_refraction"] in (True, None)


def test_preview_row_fits_the_default_frame(run_blender, tmp_path):
    run = run_blender("material_library.py", ["--spheres", "--out", tmp_path / "lib.blend"],
                      probes=["projection", "scene"])
    assert run.returncode == 0, run
    assert run.probe["projection"]["resolution"] == [1600, 600]
    spheres = {k: v for k, v in run.probe["projection"]["objects"].items() if k.startswith("Preview_")
               and k != "PreviewFloor"}
    assert len(spheres) == 8
    for name, proj in spheres.items():
        # Brushed Metal and Ceramic used to be cropped at x=-0.02 / 1.02.
        assert proj["x"][0] >= 0 and proj["x"][1] <= 1 and proj["y"][0] >= 0 and proj["y"][1] <= 1, name
    assert run.probe["scene"]["eevee_raytracing"] in (True, None)
