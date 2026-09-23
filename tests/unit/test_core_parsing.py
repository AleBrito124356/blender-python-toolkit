"""Argument, number, CSV and frame-list parsing (lib/core.py, no Blender needed)."""

import argparse
import io

import pytest

from lib import core


def test_argv_after_dashes_returns_script_args():
    argv = ["blender", "--background", "--python", "x.py", "--", "--seed", "7", "--render"]
    assert core.get_argv_after_dashes(argv) == ["--seed", "7", "--render"]


def test_argv_after_dashes_without_separator_is_empty():
    assert core.get_argv_after_dashes(["blender", "--background"]) == []


def test_argv_after_dashes_only_first_separator_counts():
    assert core.get_argv_after_dashes(["b", "--", "--a", "--", "--b"]) == ["--a", "--", "--b"]


@pytest.mark.parametrize("text, expected", [
    ("1600x900", (1600, 900)),
    ("1920X1080", (1920, 1080)),
    (" 640 x 360 ", (640, 360)),
    ("800*600", (800, 600)),
    ("1024\u00d7768", (1024, 768)),
])
def test_parse_resolution_accepts_common_spellings(text, expected):
    assert core.parse_resolution(text) == expected


@pytest.mark.parametrize("text", ["1600", "1600x", "x900", "abc", "0x900", "3x3", "100000x10", "-5x10"])
def test_parse_resolution_rejects_garbage(text):
    with pytest.raises(ValueError, match="--res"):
        core.parse_resolution(text)


def test_resolution_type_raises_argparse_error():
    with pytest.raises(argparse.ArgumentTypeError):
        core.resolution_type("big")


def test_positive_int():
    assert core.positive_int("3") == 3
    for bad in ("0", "-1", "x"):
        with pytest.raises(argparse.ArgumentTypeError):
            core.positive_int(bad)


@pytest.mark.parametrize("text, expected", [
    ("42", 42.0),
    ("-3.5", -3.5),
    ("1,234", 1234.0),
    ("1,234.5", 1234.5),
    ("1.234,5", 1234.5),
    ("1.234.567", 1234567.0),
    ("3,5", 3.5),
    ("1 234", 1234.0),
    ("1_000", 1000.0),
    ("1'000", 1000.0),
    ("$1,200", 1200.0),
    ("\u20ac-5", -5.0),
    ("(1,200)", -1200.0),
    ("12%", 12.0),
    ("\u22124", -4.0),
    (".5", 0.5),
    ("1e3", 1000.0),
])
def test_parse_number_handles_human_formats(text, expected):
    assert core.parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "   ", "n/a", "abc", "1-2", "nan", "inf", None])
def test_parse_number_rejects_non_numbers(text):
    with pytest.raises(ValueError):
        core.parse_number(text)


def test_csv_reads_sample_file(repo_root):
    data = core.read_label_value_csv(repo_root / "data" / "sales.csv")
    assert data.labels[0] == "Jan" and data.values[-1] == 120.0
    assert len(data.rows) == 12 and data.skipped == []
    assert (data.label_col, data.value_col) == ("label", "value")


def test_csv_reports_skipped_rows_with_line_numbers():
    text = "label,value\nA,1\n,2\nB,not-a-number\nC\nD,4\n\n"
    data = core.read_label_value_csv(io.StringIO(text))
    assert data.rows == [("A", 1.0), ("D", 4.0)]
    lines = [line for line, _ in data.skipped]
    assert lines == [3, 4, 5]
    assert "empty label" in data.skipped[0][1]
    assert "not a number" in data.skipped[1][1]


def test_csv_semicolon_decimal_comma_and_named_columns():
    text = 'region;quarter;margin\nNorth;Q1;"12,5"\nSouth;Q1;"-4,2"\n'
    data = core.read_label_value_csv(io.StringIO(text), value_col="MARGIN")
    assert data.delimiter == ";"
    assert data.rows == [("North", 12.5), ("South", -4.2)]


def test_csv_auto_picks_first_numeric_column():
    text = "name,team,score\nAna,red,10\nBo,blue,12\n"
    data = core.read_label_value_csv(io.StringIO(text))
    assert data.value_col == "score" and data.values == [10.0, 12.0]


def test_csv_columns_by_index_and_thousands():
    text = "k,v\nA,\"1,500\"\nB,250\n"
    data = core.read_label_value_csv(io.StringIO(text), label_col=0, value_col="1")
    assert data.values == [1500.0, 250.0]


def test_csv_errors_are_explicit():
    with pytest.raises(core.CsvError, match="empty"):
        core.read_label_value_csv(io.StringIO(""))
    with pytest.raises(core.CsvError, match="not found"):
        core.read_label_value_csv(io.StringIO("a,b\nx,1\n"), value_col="nope")
    with pytest.raises(core.CsvError, match="No usable"):
        core.read_label_value_csv(io.StringIO("a,b\nx,y\n"))


def test_csv_strips_utf8_bom(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes("\ufefflabel,value\nA,1\n".encode("utf-8"))
    assert core.read_label_value_csv(path).label_col == "label"


@pytest.mark.parametrize("text, expected", [
    ("1,30,60", [1, 30, 60]),
    ("1-5", [1, 2, 3, 4, 5]),
    ("1-10:4", [1, 5, 9, 10]),
    ("3, 3, 1", [1, 3]),
])
def test_parse_frames(text, expected):
    assert core.parse_frames(text) == expected


@pytest.mark.parametrize("text", ["", "a", "5-1", "1-5:0"])
def test_parse_frames_rejects_bad_specs(text):
    with pytest.raises(ValueError):
        core.parse_frames(text)


def test_format_for_path():
    assert core.format_for_path("out/x.JPG") == "JPEG"
    assert core.format_for_path("a.exr") == "OPEN_EXR"
    assert core.format_for_path("noext") == "PNG"
    assert core.image_extension("JPEG") == ".jpg"
    assert core.swap_extension("out/a.png", ".exr") == "out/a.exr"
