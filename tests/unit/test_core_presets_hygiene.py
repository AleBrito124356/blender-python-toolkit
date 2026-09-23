"""Preset parsing/validation, rename planning, batch summaries (lib/core.py)."""

import csv
import json

import pytest

from lib import core


def test_repo_presets_parse_identically_with_both_parsers(repo_root):
    text = (repo_root / "presets.yaml").read_text(encoding="utf-8")
    builtin = core.parse_simple_yaml(text)
    assert set(builtin) == {"preview", "social", "print"}
    assert builtin["preview"]["resolution"] == [960, 540]
    assert builtin["social"]["film_transparent"] is True
    presets, parser_name = core.load_presets_text(text, prefer_yaml=False)
    assert parser_name == "builtin" and presets == builtin
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(text) == builtin


def test_simple_yaml_scalars_quotes_and_comments():
    text = (
        "# header\n"
        "a:\n"
        "  name: 'hello # not a comment'  # a comment\n"
        "  n: 3\n"
        "  f: 0.5\n"
        "  flag: yes\n"
        "  nothing: ~\n"
        "  list: [1, 2.5, x]\n"
        "  empty: []\n"
    )
    data = core.parse_simple_yaml(text)
    assert data == {"a": {"name": "hello # not a comment", "n": 3, "f": 0.5, "flag": True,
                          "nothing": None, "list": [1, 2.5, "x"], "empty": []}}


@pytest.mark.parametrize("text, message", [
    ("a:\n  - 1\n", "block lists"),
    ("a:\n  b: {c: 1}\n", "inline mappings"),
    ("a:\n  b: 1\n    c: 2\n", "deeper than one level"),
    ("  b: 1\n", "no parent"),
    ("a: 5\n", "must start a mapping"),
    ("a:\n  b: [1, 2\n", "unterminated"),
    ("a:\n\tb: 1\n", "tabs"),
    ("a:\n  just text\n", "expected 'key: value'"),
])
def test_simple_yaml_rejects_what_it_cannot_read(text, message):
    with pytest.raises(core.PresetError, match=message):
        core.parse_simple_yaml(text)


def test_validate_preset_accepts_the_shipped_shape():
    good = {"resolution": [1920, 1080], "percentage": 100, "samples": 64, "engine": "cycles",
            "device": "cpu", "view_transform": "auto", "format": "jpeg", "film_transparent": False}
    assert core.validate_preset("ok", good) == []


@pytest.mark.parametrize("preset, fragment", [
    ({"resolution": [1920]}, "resolution"),
    ({"resolution": [1920, 0]}, "resolution"),
    ({"resolution": [True, 1080]}, "resolution"),
    ({"samples": 0}, "samples"),
    ({"samples": "many"}, "samples"),
    ({"percentage": 5000}, "percentage"),
    ({"engine": "luxcore"}, "engine"),
    ({"device": "tpu"}, "device"),
    ({"format": "GIF"}, "format"),
    ({"film_transparent": "yes please"}, "film_transparent"),
    ({"view_transform": 3}, "view_transform"),
    ({"resolutoin": [1, 2]}, "unknown key"),
])
def test_validate_preset_reports_each_problem(preset, fragment):
    errors = core.validate_preset("bad", preset)
    assert errors and any(fragment in e for e in errors)


def test_load_presets_text_lists_every_problem():
    text = "a:\n  samples: 0\nb:\n  engine: luxcore\n"
    with pytest.raises(core.PresetError) as info:
        core.load_presets_text(text, prefer_yaml=False)
    assert "a: samples" in str(info.value) and "b: engine" in str(info.value)
    with pytest.raises(core.PresetError, match="non-empty"):
        core.load_presets_text("# nothing\n", prefer_yaml=False)


def test_rename_plan_prefixes_by_type_and_is_idempotent():
    objects = [("Cube", "MESH"), ("Camera", "CAMERA"), ("Sun", "LIGHT"), ("MESH_Floor", "MESH"),
               ("Thing", "SOMETHING_NEW")]
    plan = dict(core.plan_prefix_renames(objects))
    assert plan == {"Cube": "MESH_Cube", "Camera": "CAM_Camera", "Sun": "LGT_Sun", "Thing": "OBJ_Thing"}
    renamed = [(plan.get(n, n), t) for n, t in objects]
    assert core.plan_prefix_renames(renamed) == []


def test_rename_plan_retypes_wrong_prefix():
    assert core.plan_prefix_renames([("CAM_Rig", "EMPTY")]) == [("CAM_Rig", "EMP_Rig")]


def test_rename_plan_never_collides():
    objects = [("Box", "MESH"), ("MESH_Box", "EMPTY"), ("EMP_Box", "MESH")]
    plan = core.plan_prefix_renames(objects)
    final = {n: n for n, _ in objects}
    final.update(dict(plan))
    assert len(set(final.values())) == len(objects)
    assert dict(plan)["Box"] in {"MESH_Box", "MESH_Box.001"}


def test_rename_plan_respects_name_length():
    long_name = "x" * 70
    (_, new), = core.plan_prefix_renames([(long_name, "MESH")])
    assert len(new) <= 63 and new.startswith("MESH_")


def test_unique_path(tmp_path):
    target = tmp_path / "scene_preview.blend"
    assert core.unique_path(str(target)) == str(target)
    target.write_text("x")
    assert core.unique_path(str(target)).endswith("scene_preview_01.blend")
    (tmp_path / "scene_preview_01.blend").write_text("x")
    assert core.unique_path(str(target)).endswith("scene_preview_02.blend")


RESULTS = [
    {"file": "hero", "status": "OK", "seconds": 1.2, "detail": "hero.png", "output": "out/hero.png",
     "qa": {"passed": True}},
    {"file": "nocam", "status": "SKIPPED", "seconds": 0.1, "detail": "no active camera", "output": None},
    {"file": "broken", "status": "ERROR", "seconds": 0.0, "detail": "cannot open .blend", "output": None},
]


def test_batch_exit_code_rules():
    assert core.batch_exit_code(RESULTS) == 1
    assert core.batch_exit_code(RESULTS[:2]) == 0
    assert core.batch_exit_code(RESULTS[:2], strict=True) == 1
    assert core.batch_exit_code([{"status": "QA_FAILED"}]) == 1
    assert core.batch_exit_code([]) == 0


def test_write_batch_summary(tmp_path):
    json_path, csv_path = core.write_batch_summary(RESULTS, str(tmp_path), meta={"exit_code": 1})
    data = json.loads(open(json_path, encoding="utf-8").read())
    assert data["counts"] == {"OK": 1, "SKIPPED": 1, "ERROR": 1, "QA_FAILED": 0}
    assert data["total"] == 3 and data["exit_code"] == 1
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["status"] for r in rows] == ["OK", "SKIPPED", "ERROR"]
    assert rows[0]["qa_passed"] == "true" and rows[1]["qa_passed"] == ""


def test_format_batch_table():
    table = core.format_batch_table(RESULTS)
    assert "1/3 rendered OK, 2 failed or skipped" in table
    assert "SKIPPED" in table and "no active camera" in table
    assert core.format_batch_table([]) == "[batch] no .blend files found."
