"""Colour maths, chart layout, ticks and animation schedule (lib/core.py)."""

import pytest

from lib import core


def test_srgb_linear_round_trip():
    for v in (0.0, 0.01, 0.04045, 0.2, 0.5, 0.8, 1.0):
        assert core.linear_to_srgb(core.srgb_to_linear(v)) == pytest.approx(v, abs=1e-6)


def test_srgb_to_linear_known_values():
    assert core.srgb_to_linear(0.5) == pytest.approx(0.21404, abs=1e-5)
    assert core.srgb_to_linear(1.0) == pytest.approx(1.0)


def test_hex_to_linear_rgba():
    assert core.hex_to_linear_rgba("#ffffff") == (1.0, 1.0, 1.0, 1.0)
    assert core.hex_to_linear_rgba("000000", alpha=0.5) == (0.0, 0.0, 0.0, 0.5)
    assert core.hex_to_linear_rgba("#f00") == core.hex_to_linear_rgba("#ff0000")
    with pytest.raises(ValueError):
        core.hex_to_linear_rgba("#12345")
    with pytest.raises(ValueError):
        core.hex_to_linear_rgba("#gggggg")


def test_ramp_color_endpoints_and_clamping():
    assert core.ramp_color(-1) == core.ramp_color(0)
    assert core.ramp_color(2) == core.ramp_color(1)
    r, g, b, a = core.ramp_color(0.0)
    assert b > r and a == 1.0  # starts blue
    r, g, b, _ = core.ramp_color(1.0)
    assert r > b  # ends warm


def test_chart_layout_is_proportional_to_values(repo_root):
    rows = core.read_label_value_csv(repo_root / "data" / "sales.csv").rows
    bars, scale, width = core.chart_layout(rows, max_height=6.0)
    by_label = {b.label: b for b in bars}
    # The audit bug: Dec/Jan rendered 15x when the data ratio is 2.86x.
    assert by_label["Dec"].height / by_label["Jan"].height == pytest.approx(120 / 42)
    assert max(b.height for b in bars) == pytest.approx(6.0)
    assert all(b.height == pytest.approx(b.value * scale) for b in bars)
    assert width == pytest.approx(12 * 1.3 - 0.5)


def test_chart_layout_centres_bars_and_spaces_them():
    bars, _, width = core.chart_layout([("a", 1), ("b", 2), ("c", 3)], bar_width=1.0, gap=0.5)
    xs = [b.x for b in bars]
    assert xs == pytest.approx([-1.5, 0.0, 1.5])
    assert width == pytest.approx(4.0)


def test_chart_layout_negative_values_hang_below_zero():
    bars, scale, _ = core.chart_layout([("up", 10), ("down", -5)], max_height=6.0)
    up, down = bars
    assert up.height == pytest.approx(4.0) and down.height == pytest.approx(-2.0)
    assert down.top_z == 0.0 and down.bottom_z == pytest.approx(-2.0)
    assert up.top_z - down.bottom_z == pytest.approx(6.0)  # whole range spans max_height


def test_chart_layout_zero_values_stay_visible():
    bars, scale, _ = core.chart_layout([("z", 0), ("one", 1)])
    assert bars[0].height == pytest.approx(core.MIN_VISIBLE_HEIGHT)
    bars, scale, _ = core.chart_layout([("z", 0), ("z2", 0)])
    assert scale == 0.0 and all(b.height > 0 for b in bars)


def test_chart_layout_requires_rows():
    with pytest.raises(ValueError):
        core.chart_layout([])


def test_chart_height_budget():
    assert core.chart_height_for(12) == 6.0
    assert core.chart_height_for(2) == 3.0
    assert 3.0 < core.chart_height_for(7) <= 6.0


@pytest.mark.parametrize("vmax, expected", [
    (120, [0, 25, 50, 75, 100, 125]),
    (7, [0, 2, 4, 6, 8]),
    (1000, [0, 200, 400, 600, 800, 1000]),
])
def test_nice_ticks(vmax, expected):
    assert core.nice_ticks(vmax) == [float(v) for v in expected]


def test_nice_ticks_degenerate():
    assert core.nice_ticks(0) == [0.0]
    assert core.nice_ticks(-3) == [0.0]


def test_axis_ticks_share_one_step_across_zero():
    ticks = core.axis_ticks(-9.6, 12.5)
    assert ticks == [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0]
    steps = {round(b - a, 9) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1
    assert 0.0 in core.axis_ticks(3, 9)
    assert core.axis_ticks(-5, -1)[-1] == 0.0
    assert core.axis_ticks(0, 0) == [0.0]


@pytest.mark.parametrize("value, text", [(120, "120"), (3.14159, "3.14"), (12500, "12,500"),
                                         (-4.5, "-4.5"), (0.1, "0.1"), (2.0, "2")])
def test_format_value(value, text):
    assert core.format_value(value) == text


def test_grow_schedule_stays_inside_timeline_and_staggers():
    sched = core.grow_schedule(12, 54, stagger=0.4)
    assert len(sched) == 12
    assert all(1 <= s < e <= 54 for s, e in sched)
    starts = [s for s, _ in sched]
    assert starts == sorted(starts) and starts[0] == 1 and starts[-1] > starts[0]


def test_grow_schedule_without_stagger_is_simultaneous():
    sched = core.grow_schedule(4, 30, stagger=0.0)
    assert len(set(sched)) == 1 and sched[0] == (1, 30)
    assert core.grow_schedule(0, 30) == []
    assert core.grow_schedule(1, 2) == [(1, 2)]


def test_convex_hull_and_area():
    square = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5), (0.2, 0.9)]
    hull = core.convex_hull(square)
    assert sorted(hull) == [(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)]
    assert core.polygon_area(hull) == pytest.approx(1.0)
    assert core.polygon_area(core.convex_hull([(0, 0), (1, 1)])) == 0.0


def test_rects_overlap():
    assert core.rects_overlap((0, 0, 1, 1), (0.5, 0.5, 2, 2))
    assert not core.rects_overlap((0, 0, 1, 1), (1.1, 0, 2, 1))
    assert not core.rects_overlap((0, 0, 1, 1), (1, 0, 2, 1))  # touching edges are fine
