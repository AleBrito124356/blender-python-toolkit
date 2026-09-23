"""QA decision rules and report structure (lib/core.py; measurements are faked)."""

import json

import pytest

from lib import core


def obj(name, role="subject", **kw):
    base = {"name": name, "role": role, "grounded": role == "subject", "in_frame": True,
            "visible_in_frame": True, "cropped": False, "behind_camera": False,
            "ground_gap": 0.0, "size": 2.0, "bbox": [0.4, 0.4, 0.6, 0.6]}
    base.update(kw)
    return base


def comp(objects, coverage=0.3, ground_z=0.0, camera="Cam"):
    return {"camera": camera, "coverage": coverage, "ground_z": ground_z, "objects": objects}


def statuses(checks):
    return {c["id"]: c["status"] for c in checks}


def test_clean_composition_passes():
    checks = core.evaluate_composition(comp([obj("Box")]))
    assert statuses(checks) == {"camera": "pass", "behind_camera": "pass", "in_frame": "pass",
                                "ground_contact": "pass", "coverage": "pass"}


def test_missing_camera_fails_immediately():
    checks = core.evaluate_composition(comp([obj("Box")], camera=None))
    assert statuses(checks) == {"camera": "fail"}


def test_no_subjects_fails():
    checks = core.evaluate_composition(comp([obj("Floor", role="backdrop")]))
    assert statuses(checks)["subjects_present"] == "fail"


def test_cropped_and_outside_objects_fail_with_names():
    checks = core.evaluate_composition(comp([
        obj("Half", cropped=True, in_frame=False),
        obj("Gone", visible_in_frame=False, in_frame=False),
        obj("Fine"),
    ]))
    in_frame = next(c for c in checks if c["id"] == "in_frame")
    assert in_frame["status"] == "fail"
    assert in_frame["cropped"] == ["Half"] and in_frame["outside"] == ["Gone"]


def test_behind_camera_fails():
    checks = core.evaluate_composition(comp([obj("Behind", behind_camera=True)]))
    assert statuses(checks)["behind_camera"] == "fail"


def test_floating_subject_fails_but_tolerance_applies():
    checks = core.evaluate_composition(comp([obj("Float", ground_gap=1.0)]))
    assert statuses(checks)["ground_contact"] == "fail"
    checks = core.evaluate_composition(comp([obj("Tiny", ground_gap=0.001)]))
    assert statuses(checks)["ground_contact"] == "pass"
    # relative tolerance: a 1000-unit building may be 1 unit off
    checks = core.evaluate_composition(comp([obj("Huge", ground_gap=1.0, size=1000.0)]))
    assert statuses(checks)["ground_contact"] == "pass"


def test_sunk_subject_warns_and_annotations_may_float():
    checks = core.evaluate_composition(comp([obj("Sunk", ground_gap=-0.5)]))
    assert statuses(checks)["ground_contact"] == "warn"
    checks = core.evaluate_composition(comp([obj("Title", role="annotation", ground_gap=5.0), obj("Box")]))
    assert statuses(checks)["ground_contact"] == "pass"


def test_ground_check_skips_without_ground():
    checks = core.evaluate_composition(comp([obj("Box", grounded=False, ground_gap=None)], ground_z=None))
    assert statuses(checks)["ground_contact"] == "skip"


@pytest.mark.parametrize("coverage, status", [(0.5, "pass"), (0.05, "warn"), (0.001, "fail"), (None, "skip")])
def test_coverage_thresholds(coverage, status):
    checks = core.evaluate_composition(comp([obj("Box")], coverage=coverage))
    assert statuses(checks)["coverage"] == status


def test_label_overlap_warns():
    labels = [obj("A", role="annotation", bbox=[0.1, 0.1, 0.3, 0.2]),
              obj("B", role="annotation", bbox=[0.25, 0.15, 0.4, 0.25]),
              obj("C", role="annotation", bbox=[0.6, 0.6, 0.7, 0.7])]
    checks = core.evaluate_composition(comp(labels + [obj("Bar")]))
    overlap = next(c for c in checks if c["id"] == "annotation_overlap")
    assert overlap["status"] == "warn" and overlap["pairs"] == [["A", "B"]]


def test_label_occlusion_thresholds():
    labels = [obj("Hidden", role="annotation", occluded=0.8), obj("Partly", role="annotation", occluded=0.3,
                                                                    bbox=[0.7, 0.7, 0.8, 0.8])]
    check = next(c for c in core.evaluate_composition(comp(labels + [obj("Bar")]))
                 if c["id"] == "annotation_occluded")
    assert check["status"] == "fail" and check["objects"] == ["Hidden"] and check["partly"] == ["Partly"]
    labels = [obj("Ok", role="annotation", occluded=0.0)]
    check = next(c for c in core.evaluate_composition(comp(labels)) if c["id"] == "annotation_occluded")
    assert check["status"] == "pass"


def image(**kw):
    base = {"mean_luminance": 0.35, "std_luminance": 0.2, "clipped": 0.0, "crushed": 0.02,
            "alpha_coverage": 1.0, "has_alpha": False}
    base.update(kw)
    return base


def test_good_image_passes():
    assert set(statuses(core.evaluate_image(image())).values()) == {"pass"}


@pytest.mark.parametrize("stats, check_id, status", [
    ({"mean_luminance": 0.001, "std_luminance": 0.001, "crushed": 1.0}, "image_exposure", "fail"),
    ({"mean_luminance": 0.03}, "image_exposure", "warn"),
    ({"mean_luminance": 0.97}, "image_exposure", "warn"),
    ({"std_luminance": 0.0005}, "image_detail", "fail"),
    ({"clipped": 0.4}, "image_clipping", "warn"),
    ({"crushed": 0.9}, "image_clipping", "warn"),
])
def test_bad_images_are_flagged(stats, check_id, status):
    assert statuses(core.evaluate_image(image(**stats)))[check_id] == status


def test_empty_transparent_render_fails_early():
    checks = core.evaluate_image(image(has_alpha=True, alpha_coverage=0.0))
    assert statuses(checks) == {"image_alpha": "fail"}


def test_unreadable_image_fails():
    assert statuses(core.evaluate_image({"error": "file not found"})) == {"image_readable": "fail"}


def test_thresholds_can_be_overridden():
    checks = core.evaluate_image(image(mean_luminance=0.05), thresholds={"dark_mean_warn": 0.01})
    assert statuses(checks)["image_exposure"] == "pass"


def test_build_and_validate_report():
    checks = core.evaluate_composition(comp([obj("Box")])) + core.evaluate_image(image())
    report = core.build_report("unit", {"engine": "BLENDER_EEVEE"}, comp([obj("Box")]),
                               [{"path": "x.png"}], checks, extra={"created": "now"})
    assert report["passed"] is True and report["status"] == "pass"
    assert core.validate_report(report) == []
    assert json.loads(json.dumps(report)) == report
    text = core.format_report_text(report)
    assert text.startswith("QA PASS  (unit)") and "[PASS] camera" in text


def test_report_with_a_failure_is_not_passed():
    checks = core.evaluate_composition(comp([obj("Box", ground_gap=2.0)]))
    report = core.build_report("unit", {}, None, [], checks)
    assert report["passed"] is False and report["status"] == "fail"
    assert "-> Box" in core.format_report_text(report)


def test_validate_report_catches_structural_problems():
    assert core.validate_report([]) == ["report must be an object"]
    report = core.build_report("unit", {}, None, [], [{"id": "x", "status": "pass", "message": "ok"}])
    report["passed"] = False
    assert "'passed' disagrees with the check statuses" in core.validate_report(report)
    del report["scene"]
    report["checks"].append({"id": 1, "status": "maybe"})
    problems = core.validate_report(report)
    assert "missing key 'scene'" in problems
    assert any("checks[1].status" in p for p in problems)
    assert any("checks[1].id" in p for p in problems)
