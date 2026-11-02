"""bpy-free core logic for blender-python-toolkit.

Nothing in this module imports ``bpy``, ``mathutils`` or ``numpy``, so it runs
on any CPython >= 3.9 as well as inside Blender's bundled interpreter. That is
what lets ``tests/unit`` exercise the maths, parsing and QA decisions without
Blender installed.

``lib/common.py`` re-exports the public names that used to live there
(``get_argv_after_dashes``, ``srgb_to_linear``, ``hex_to_linear_rgba`` and
``ramp_color``), so existing ``from lib.common import ...`` lines keep working.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Command line helpers
# ---------------------------------------------------------------------------


def get_argv_after_dashes(argv=None):
    """Return the CLI arguments that follow the first ``--`` separator.

    Blender consumes everything before ``--`` for itself, so scripts receive
    their own options after it. If ``--`` is absent an empty list is returned
    (the script runs with its defaults).

        blender --background --python scripts/x.py -- --seed 7 --render
                                                   ^^-- returned: ["--seed", "7", "--render"]
    """
    argv = sys.argv if argv is None else argv
    if "--" in argv:
        return list(argv[argv.index("--") + 1:])
    return []


_RES_RE = re.compile(r"^\s*(\d+)\s*[xX*\u00d7]\s*(\d+)\s*$")


def parse_resolution(text, name="--res"):
    """Parse ``"1600x900"`` into ``(1600, 900)``.

    Accepts ``x``, ``X``, ``*`` or ``\u00d7`` as the separator. Raises
    ``ValueError`` with a message that names the offending flag.
    """
    match = _RES_RE.match(str(text))
    if not match:
        raise ValueError(f"{name} must look like 1600x900, got {text!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if not (4 <= width <= 65536 and 4 <= height <= 65536):
        raise ValueError(f"{name} must be between 4 and 65536 pixels per side, got {text!r}")
    return width, height


def resolution_type(text):
    """``argparse`` type wrapper around :func:`parse_resolution`."""
    import argparse

    try:
        return parse_resolution(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def positive_int(text):
    """``argparse`` type for integers >= 1."""
    import argparse

    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


_FRAMES_RE = re.compile(r"^(-?\d+)(?:\s*-\s*(-?\d+))?(?::(\d+))?$")


def parse_frames(text):
    """Parse a frame list: ``"1,30,60"``, ``"1-60"`` or ``"1-60:10"`` (every 10th).

    The end of a range is always included. Returns a sorted list of unique
    frame numbers; raises ``ValueError``.
    """
    frames = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        match = _FRAMES_RE.match(part)
        if not match:
            raise ValueError(f"bad frame spec {part!r}; use 1,30,60 or 1-60 or 1-60:10")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        step = int(match.group(3) or 1)
        if end < start or step < 1:
            raise ValueError(f"bad frame range {part!r}")
        frames.update(range(start, end + 1, step))
        frames.add(end)
    if not frames:
        raise ValueError(f"no frames in {text!r}")
    return sorted(frames)


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


def srgb_to_linear(c):
    """Convert a single sRGB channel in [0, 1] to linear light."""
    c = float(c)
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c):
    """Convert a single linear channel to sRGB (clamped to [0, 1])."""
    c = max(0.0, min(1.0, float(c)))
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def hex_to_linear_rgba(hex_color, alpha=1.0):
    """Convert ``"#RRGGBB"`` (or ``"RRGGBB"`` / ``"#RGB"``) to linear RGBA.

    Blender stores colours in linear space, so an sRGB hex picked from a design
    tool must be converted before it is assigned to a node input.
    """
    h = str(hex_color).strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
        raise ValueError(f"Expected a 6-digit hex colour, got {hex_color!r}")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), float(alpha))


RAMP_STOPS = (
    (0.00, (0.15, 0.35, 0.90)),  # blue
    (0.50, (0.10, 0.70, 0.65)),  # teal
    (0.80, (0.95, 0.72, 0.15)),  # amber
    (1.00, (0.92, 0.30, 0.20)),  # warm red
)


def ramp_color(t):
    """Map ``t`` in [0, 1] to a linear RGBA colour on a blue-teal-amber-red ramp."""
    t = max(0.0, min(1.0, float(t)))
    for (t0, c0), (t1, c1) in zip(RAMP_STOPS, RAMP_STOPS[1:]):
        if t <= t1:
            k = (t - t0) / ((t1 - t0) or 1.0)
            rgb = [c0[i] + (c1[i] - c0[i]) * k for i in range(3)]
            return (srgb_to_linear(rgb[0]), srgb_to_linear(rgb[1]), srgb_to_linear(rgb[2]), 1.0)
    r, g, b = RAMP_STOPS[-1][1]
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), 1.0)


def luminance(r, g, b):
    """Rec. 709 relative luminance of an RGB triple (any encoding)."""
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------------------
# Numbers and CSV
# ---------------------------------------------------------------------------

_CURRENCY = "$\u20ac\u00a3\u00a5\u20b9"
_THOUSANDS_COMMA = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")


def parse_number(text):
    """Parse a human-formatted number into a float.

    Handles thousands separators (``1,234.5``, ``1.234,5``, ``1 234``,
    ``1_234``, ``1'234``), a decimal comma (``3,5``), currency symbols, a
    trailing ``%`` (kept as the plain number), the Unicode minus sign and
    accounting-style negatives ``(1,200)``. Raises ``ValueError`` otherwise.

    A lone comma followed by exactly three digits (``1,234``) is read as a
    thousands separator, which is the convention of the sample data.
    """
    if text is None:
        raise ValueError("empty value")
    s = str(text).strip().replace("\u2212", "-").replace("\u00a0", " ")
    if not s:
        raise ValueError("empty value")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith(("-", "+")):
        negative ^= s[0] == "-"
        s = s[1:].strip()
    s = s.strip(_CURRENCY).strip()
    if s.startswith(("-", "+")):  # "$-5"
        negative ^= s[0] == "-"
        s = s[1:].strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    for sep in (" ", "_", "'"):
        s = s.replace(sep, "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,5 -> European
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.5 -> US
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if _THOUSANDS_COMMA.match(s) else s.replace(",", ".")
    elif _THOUSANDS_DOT.match(s) and s.count(".") > 1:
        s = s.replace(".", "")
    if not re.match(r"^(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$", s):
        raise ValueError(f"not a number: {text!r}")
    value = float(s)
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"not a finite number: {text!r}")
    return -value if negative else value


class CsvError(ValueError):
    """Raised when a CSV cannot produce any usable label/value rows."""


@dataclass
class CsvData:
    """Result of :func:`read_label_value_csv`."""

    rows: list  # list of (label, value)
    label_col: str
    value_col: str
    delimiter: str
    skipped: list = field(default_factory=list)  # list of (line_number, reason)

    @property
    def labels(self):
        return [label for label, _ in self.rows]

    @property
    def values(self):
        return [value for _, value in self.rows]


def _resolve_column(header, spec, what):
    """Resolve a column given by header name (case-insensitive) or 0-based index."""
    if spec is None:
        return None
    spec_s = str(spec).strip()
    lowered = [h.strip().lower() for h in header]
    if spec_s.lower() in lowered:
        return lowered.index(spec_s.lower())
    if spec_s.lstrip("-").isdigit():
        idx = int(spec_s)
        if -len(header) <= idx < len(header):
            return idx % len(header)
    raise CsvError(f"{what} column {spec!r} not found; header is {header}")


def read_label_value_csv(source, label_col=None, value_col=None, delimiter=None):
    """Read a CSV into ``(label, value)`` rows, reporting skipped rows.

    ``source`` is a path or a file-like object. The first row is the header.
    ``label_col`` / ``value_col`` select columns by header name or 0-based
    index; by default the first column is the label and the first other column
    whose cells mostly parse as numbers is the value. The delimiter is sniffed
    among ``, ; TAB |`` unless given. Rows that cannot be used are returned in
    ``skipped`` with their 1-based line number and a reason, instead of being
    dropped silently. Raises :class:`CsvError` if no row is usable.
    """
    if hasattr(source, "read"):
        text = source.read()
        name = getattr(source, "name", "<stream>")
    else:
        name = str(source)
        with open(source, newline="", encoding="utf-8-sig") as fh:
            text = fh.read()
    if not text.strip():
        raise CsvError(f"CSV {name!r} is empty.")

    if delimiter is None:
        first_line = text.splitlines()[0]
        counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
        delimiter = max(counts, key=counts.get) if max(counts.values()) > 0 else ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    records = list(reader)
    header = [h.strip() for h in records[0]]
    body = records[1:]

    li = _resolve_column(header, label_col, "label")
    if li is None:
        li = 0
    vi = _resolve_column(header, value_col, "value")
    if vi is None:
        best, best_hits = None, -1
        for ci in range(len(header)):
            if ci == li:
                continue
            hits = 0
            for row in body:
                try:
                    parse_number(row[ci])
                    hits += 1
                except (IndexError, ValueError):
                    pass
            if hits > best_hits:
                best, best_hits = ci, hits
        if best is None:
            raise CsvError(f"CSV {name!r} needs at least two columns (label and value); header is {header}")
        vi = best
    if vi == li:
        raise CsvError("label and value columns must differ")

    rows, skipped = [], []
    for offset, row in enumerate(body):
        line_no = offset + 2
        if not any(cell.strip() for cell in row):
            continue  # blank line, not worth reporting
        if len(row) <= max(li, vi):
            skipped.append((line_no, f"expected at least {max(li, vi) + 1} columns, got {len(row)}"))
            continue
        label = row[li].strip()
        if not label:
            skipped.append((line_no, "empty label"))
            continue
        try:
            value = parse_number(row[vi])
        except ValueError as exc:
            skipped.append((line_no, f"value {row[vi]!r} is not a number ({exc})"))
            continue
        rows.append((label, value))

    if not rows:
        raise CsvError(f"No usable label,value rows found in {name!r} ({len(skipped)} skipped).")
    return CsvData(rows=rows, label_col=header[li], value_col=header[vi],
                   delimiter=delimiter, skipped=skipped)


# ---------------------------------------------------------------------------
# Bar chart layout
# ---------------------------------------------------------------------------


@dataclass
class BarLayout:
    index: int
    label: str
    value: float
    x: float
    height: float  # signed: negative bars hang below the zero axis

    @property
    def top_z(self):
        return max(self.height, 0.0)

    @property
    def bottom_z(self):
        return min(self.height, 0.0)


MIN_VISIBLE_HEIGHT = 0.02


def chart_layout(rows, max_height=6.0, bar_width=0.8, gap=0.5):
    """Lay out bars on a zero baseline.

    Height is proportional to the value: ``height = value * scale``. The scale
    is chosen so the whole value range, from ``min(0, lowest)`` to
    ``max(0, highest)``, spans ``max_height`` units; for all-positive data
    the tallest bar is exactly ``max_height``. A value twice as large is always
    exactly twice as tall. Zero values get a sliver of ``MIN_VISIBLE_HEIGHT``
    so the bar still exists. Returns ``(bars, scale, total_width)``.
    """
    rows = list(rows)
    if not rows:
        raise ValueError("chart_layout needs at least one row")
    values = [float(v) for _, v in rows]
    extent = max(max(values), 0.0) - min(min(values), 0.0)
    scale = (max_height / extent) if extent > 0 else 0.0
    step = bar_width + gap
    total_width = len(rows) * step - gap
    x0 = -total_width / 2.0 + bar_width / 2.0
    bars = []
    for i, (label, value) in enumerate(rows):
        height = float(value) * scale
        if abs(height) < MIN_VISIBLE_HEIGHT:
            height = MIN_VISIBLE_HEIGHT if value >= 0 else -MIN_VISIBLE_HEIGHT
        bars.append(BarLayout(i, label, float(value), x0 + i * step, height))
    return bars, scale, total_width


def chart_height_for(count, bar_width=0.8, gap=0.5, lo=3.0, hi=6.0):
    """Height budget that keeps a chart of ``count`` bars roughly 2:1 wide."""
    total_width = count * (bar_width + gap) - gap
    return max(lo, min(hi, 0.5 * total_width))


def nice_ticks(max_value, target=5):
    """Round axis ticks from 0 up to at least ``max_value`` (1-2-2.5-5 steps).

    ``nice_ticks(120)`` -> ``[0, 25, 50, 75, 100, 125]``. Returns ``[0]`` for
    a non-positive maximum.
    """
    max_value = float(max_value)
    if max_value <= 0 or math.isnan(max_value):
        return [0.0]
    raw = max_value / max(int(target), 1)
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if step >= raw:
            break
    count = int(math.ceil(max_value / step - 1e-9))
    return [float(round(i * step, 10)) for i in range(count + 1)]


def axis_ticks(vmin, vmax, target=5):
    """Evenly stepped ticks covering ``[min(0, vmin), max(0, vmax)]`` and 0.

    One step size is used on both sides of zero, so positive and negative
    gridlines line up: ``axis_ticks(-9.6, 12.5)`` ->
    ``[-10, -5, 0, 5, 10, 15]``.
    """
    lo, hi = min(0.0, float(vmin)), max(0.0, float(vmax))
    if hi - lo <= 0:
        return [0.0]
    raw = (hi - lo) / max(int(target), 1)
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if step >= raw:
            break
    first = int(math.floor(lo / step + 1e-9))
    last = int(math.ceil(hi / step - 1e-9))
    return [float(round(i * step, 10)) + 0.0 for i in range(first, last + 1)]


def format_value(value):
    """Short label for a data value: ``120``, ``3.14``, ``12,500``, ``-4.5``."""
    value = float(value)
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def grow_schedule(count, frames, stagger=0.35, first_frame=1):
    """Frame ranges for a staggered grow-in animation.

    Returns ``count`` ``(start, end)`` integer frame pairs inside
    ``[first_frame, first_frame + frames - 1]``. ``stagger`` in [0, 1) is the
    fraction of the timeline spread across bar start times; 0 makes every bar
    grow together.
    """
    if count < 1:
        return []
    frames = max(int(frames), 2)
    stagger = min(max(float(stagger), 0.0), 0.9)
    last = first_frame + frames - 1
    spread = (frames - 1) * stagger
    duration = max((frames - 1) - spread, 1.0)
    out = []
    for i in range(count):
        start = first_frame + (spread * i / (count - 1) if count > 1 else 0.0)
        end = min(start + duration, last)
        s, e = int(round(start)), int(round(end))
        if e <= s:
            e = min(s + 1, last)
            s = min(s, e - 1)
        out.append((s, e))
    return out


# ---------------------------------------------------------------------------
# YAML fallback parser and render preset schema
# ---------------------------------------------------------------------------


class PresetError(ValueError):
    """Raised for unreadable or invalid preset files."""


def _strip_comment(value):
    """Remove a trailing ``# comment`` that is outside quotes."""
    quote = None
    for i, ch in enumerate(value):
        if ch in "'\"":
            quote = None if quote == ch else (ch if quote is None else quote)
        elif ch == "#" and quote is None and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value


def coerce_scalar(value):
    """Convert a YAML-ish scalar string to None / bool / int / float / str."""
    value = value.strip()
    if value == "" or value.lower() in {"null", "~", "none"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_simple_yaml(text):
    """Parse the two-level mapping subset of YAML used by ``presets.yaml``.

    Supported: comments, top-level ``name:`` keys, indented ``key: value``
    pairs whose value is a scalar or an inline ``[a, b]`` list, and quoted
    strings. Anything else raises :class:`PresetError` with the line number
    rather than being misread.
    """
    data = {}
    current = None
    child_indent = None
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise PresetError(f"line {line_no}: tabs are not allowed for indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if stripped.startswith("- "):
            raise PresetError(f"line {line_no}: block lists are not supported; use [a, b]")
        if ":" not in stripped:
            raise PresetError(f"line {line_no}: expected 'key: value', got {stripped!r}")
        key, _, value = stripped.partition(":")
        key = key.strip().strip("'\"")
        value = _strip_comment(value).strip()
        if indent == 0:
            if value:
                raise PresetError(f"line {line_no}: top-level key {key!r} must start a mapping")
            current = {}
            data[key] = current
            child_indent = None
            continue
        if current is None:
            raise PresetError(f"line {line_no}: indented key {key!r} has no parent")
        if child_indent is None:
            child_indent = indent
        elif indent != child_indent:
            raise PresetError(f"line {line_no}: nested mappings deeper than one level are not supported")
        if value.startswith("["):
            if not value.endswith("]"):
                raise PresetError(f"line {line_no}: unterminated inline list")
            inner = value[1:-1].strip()
            current[key] = [coerce_scalar(p) for p in inner.split(",")] if inner else []
        elif value.startswith("{"):
            raise PresetError(f"line {line_no}: inline mappings are not supported")
        else:
            current[key] = coerce_scalar(value)
    return data


IMAGE_FORMAT_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "OPEN_EXR": ".exr",
    "OPEN_EXR_MULTILAYER": ".exr",
    "TIFF": ".tif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "AVIF": ".avif",
    "HDR": ".hdr",
    "JPEG2000": ".jp2",
}

PRESET_KEYS = {
    "resolution", "percentage", "samples", "engine", "device", "view_transform",
    "look", "format", "film_transparent",
}


def validate_preset(name, preset):
    """Return a list of human-readable problems with one preset (empty = valid)."""
    errors = []
    if not isinstance(preset, dict):
        return [f"{name}: preset must be a mapping, got {type(preset).__name__}"]
    for key in sorted(set(preset) - PRESET_KEYS):
        errors.append(f"{name}: unknown key {key!r} (allowed: {', '.join(sorted(PRESET_KEYS))})")
    res = preset.get("resolution")
    if res is not None:
        if (not isinstance(res, (list, tuple)) or len(res) != 2
                or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 4 for v in res)):
            errors.append(f"{name}: resolution must be [width, height] with integers >= 4, got {res!r}")
    for key, lo, hi in (("percentage", 1, 1000), ("samples", 1, 1 << 20)):
        val = preset.get(key)
        if val is not None and (not isinstance(val, int) or isinstance(val, bool) or not lo <= val <= hi):
            errors.append(f"{name}: {key} must be an integer in [{lo}, {hi}], got {val!r}")
    engine = preset.get("engine")
    if engine is not None and str(engine).lower() not in {"eevee", "cycles"}:
        errors.append(f"{name}: engine must be 'eevee' or 'cycles', got {engine!r}")
    device = preset.get("device")
    if device is not None and str(device).lower() not in {"gpu", "cpu"}:
        errors.append(f"{name}: device must be 'gpu' or 'cpu', got {device!r}")
    fmt = preset.get("format")
    if fmt is not None and str(fmt).upper() not in IMAGE_FORMAT_EXTENSIONS:
        errors.append(f"{name}: format must be one of {', '.join(sorted(IMAGE_FORMAT_EXTENSIONS))}, got {fmt!r}")
    for key in ("view_transform", "look"):
        val = preset.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{name}: {key} must be a string, got {val!r}")
    ft = preset.get("film_transparent")
    if ft is not None and not isinstance(ft, bool):
        errors.append(f"{name}: film_transparent must be true or false, got {ft!r}")
    return errors


def load_presets_text(text, prefer_yaml=True):
    """Parse preset YAML text, validate every preset and return the mapping.

    Uses PyYAML when importable (and ``prefer_yaml``), otherwise
    :func:`parse_simple_yaml`. Returns ``(presets, parser_name)``. Raises
    :class:`PresetError` listing every problem found.
    """
    data, parser_name = None, "builtin"
    if prefer_yaml:
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None
        if yaml is not None:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:  # pragma: no cover - depends on PyYAML
                raise PresetError(f"YAML syntax error: {exc}") from None
            parser_name = "PyYAML"
    if data is None and parser_name == "builtin":
        data = parse_simple_yaml(text)
    if not isinstance(data, dict) or not data:
        raise PresetError("preset file must be a non-empty mapping of preset names")
    problems = []
    for name, preset in data.items():
        problems.extend(validate_preset(str(name), preset))
    if problems:
        raise PresetError("invalid presets:\n  " + "\n  ".join(problems))
    return data, parser_name


def image_extension(fmt):
    """File extension (with dot) Blender writes for an image format id."""
    return IMAGE_FORMAT_EXTENSIONS.get(str(fmt).upper(), ".png")


_EXTENSION_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF",
    ".tiff": "TIFF", ".webp": "WEBP", ".bmp": "BMP", ".tga": "TARGA", ".avif": "AVIF",
    ".hdr": "HDR", ".jp2": "JPEG2000",
}


def format_for_path(path, default="PNG"):
    """Blender image format id implied by a file name (``out.jpg`` -> ``JPEG``)."""
    return _EXTENSION_FORMATS.get(os.path.splitext(str(path))[1].lower(), default)


# ---------------------------------------------------------------------------
# Scene hygiene: type-prefix renames
# ---------------------------------------------------------------------------

TYPE_PREFIX = {
    "MESH": "MESH_",
    "CURVE": "CRV_",
    "CURVES": "HAIR_",
    "SURFACE": "SRF_",
    "META": "MBALL_",
    "FONT": "TXT_",
    "CAMERA": "CAM_",
    "LIGHT": "LGT_",
    "LIGHT_PROBE": "PRB_",
    "EMPTY": "EMP_",
    "ARMATURE": "ARM_",
    "LATTICE": "LAT_",
    "GPENCIL": "GP_",
    "GREASEPENCIL": "GP_",
    "SPEAKER": "SPK_",
    "VOLUME": "VOL_",
    "POINTCLOUD": "PTS_",
}
FALLBACK_PREFIX = "OBJ_"
_ALL_PREFIXES = sorted(set(TYPE_PREFIX.values()) | {FALLBACK_PREFIX}, key=len, reverse=True)


def strip_type_prefix(name):
    """Remove one known type prefix from ``name`` (idempotent renames)."""
    for prefix in _ALL_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


def plan_prefix_renames(objects, max_len=63):
    """Plan type-prefix renames for ``[(name, type), ...]``.

    Returns ``[(old, new), ...]`` only for names that change. New names never
    collide with each other or with untouched names: a collision gets a
    ``.001``-style suffix, the same convention Blender uses, so the plan
    matches what Blender will actually do. Names are capped at Blender's
    63-byte limit.
    """
    objects = list(objects)
    targets = {}
    for name, obj_type in objects:
        new = TYPE_PREFIX.get(obj_type, FALLBACK_PREFIX) + strip_type_prefix(name)
        targets[name] = new[:max_len]
    unchanged = {name for name, _ in objects if targets[name] == name}
    taken = set(unchanged)
    plan = []
    for name, _ in objects:
        new = targets[name]
        if new == name:
            continue
        candidate, n = new, 0
        while candidate in taken:
            n += 1
            suffix = f".{n:03d}"
            candidate = new[: max_len - len(suffix)] + suffix
        taken.add(candidate)
        plan.append((name, candidate))
    return plan


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def unique_path(path):
    """Return ``path`` if it does not exist, else ``stem_01.ext``, ``stem_02.ext``..."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{base}_{n:02d}{ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def swap_extension(path, ext):
    """Replace the extension of ``path`` with ``ext`` (which includes the dot)."""
    base, _ = os.path.splitext(path)
    return base + ext


# ---------------------------------------------------------------------------
# Batch results
# ---------------------------------------------------------------------------

BATCH_STATUSES = ("OK", "SKIPPED", "ERROR", "QA_FAILED")


def batch_exit_code(results, strict=False):
    """Exit code for a batch run: 1 if any ERROR / QA_FAILED (or SKIPPED with strict)."""
    bad = {"ERROR", "QA_FAILED"} | ({"SKIPPED"} if strict else set())
    return 1 if any(r.get("status") in bad for r in results) else 0


def write_batch_summary(results, out_dir, meta=None):
    """Write ``summary.json`` and ``summary.csv`` into ``out_dir``.

    ``results`` is a list of dicts with at least ``file``, ``status``,
    ``seconds`` and ``detail``; an optional ``output`` and ``qa`` (report dict)
    are kept in the JSON. Returns ``(json_path, csv_path)``.
    """
    os.makedirs(out_dir, exist_ok=True)
    counts = {status: sum(1 for r in results if r.get("status") == status) for status in BATCH_STATUSES}
    payload = {
        "tool": "blender-python-toolkit",
        "kind": "batch_summary",
        "total": len(results),
        "counts": counts,
        "results": results,
    }
    if meta:
        payload.update(meta)
    json_path = os.path.join(out_dir, "summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    csv_path = os.path.join(out_dir, "summary.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "status", "seconds", "output", "qa_passed", "detail"])
        for r in results:
            qa = r.get("qa") or {}
            writer.writerow([
                r.get("file", ""), r.get("status", ""), f"{float(r.get('seconds', 0.0)):.2f}",
                r.get("output", "") or "", "" if not qa else str(bool(qa.get("passed"))).lower(),
                r.get("detail", ""),
            ])
    return json_path, csv_path


def format_batch_table(results):
    """Return the aligned summary table printed at the end of a batch run."""
    if not results:
        return "[batch] no .blend files found."
    name_w = max(max(len(r["file"]) for r in results), len("FILE"))
    header = f"{'FILE'.ljust(name_w)}  {'STATUS':<9}  {'TIME':>7}  DETAIL"
    line = "-" * max(len(header), 60)
    out = [line, header, line]
    for r in results:
        out.append(f"{r['file'].ljust(name_w)}  {r['status']:<9}  {float(r['seconds']):>6.1f}s  {r['detail']}")
    ok = sum(1 for r in results if r["status"] == "OK")
    out.append(line)
    out.append(f"{ok}/{len(results)} rendered OK, {len(results) - ok} failed or skipped")
    out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Geometry helpers used by the QA module
# ---------------------------------------------------------------------------


def convex_hull(points):
    """2D convex hull (Andrew's monotone chain), counter-clockwise, no repeats."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_area(poly):
    """Shoelace area of a simple polygon (absolute value)."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def rects_overlap(a, b, pad=0.0):
    """True if axis-aligned rectangles ``(x0, y0, x1, y1)`` overlap."""
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


# ---------------------------------------------------------------------------
# QA decisions (the numbers are measured in Blender by lib/qa.py)
# ---------------------------------------------------------------------------

REPORT_VERSION = 1

DEFAULT_THRESHOLDS = {
    # composition
    "crop_tolerance": 0.002,      # NDC units a vertex may poke outside the frame
    "ground_tolerance": 0.002,    # scene units a grounded subject may float
    "ground_tolerance_rel": 0.002,  # ... or this fraction of the subject size
    "min_coverage_warn": 0.08,    # subjects cover less than 8% of the frame
    "min_coverage_fail": 0.005,   # subjects are practically invisible
    "occluded_warn": 0.10,        # share of a label's sampled points hidden by other objects
    "occluded_fail": 0.50,
    # image
    "black_mean": 0.012,          # mean display luminance below this = black frame
    "uniform_std": 0.003,         # std of display luminance below this = flat frame
    "min_alpha_coverage": 0.001,  # transparent render with (almost) nothing in it
    "dark_mean_warn": 0.06,
    "bright_mean_warn": 0.93,
    "clipped_warn": 0.10,         # share of pixels at the white point
    "crushed_warn": 0.60,         # share of pixels at the black point
}

STATUS_ORDER = {"pass": 0, "skip": 0, "warn": 1, "fail": 2}


def _check(check_id, status, message, **extra):
    item = {"id": check_id, "status": status, "message": message}
    item.update(extra)
    return item


make_check = _check  # public name: ``make_check("id", "pass", "message", value=1)``


def evaluate_composition(composition, thresholds=None):
    """Turn measured composition facts into pass/warn/fail checks.

    ``composition`` is the dict produced by ``lib.qa.measure_composition``:
    ``camera`` (name or None), ``coverage`` (0..1 or None), ``ground_z``
    (float or None) and ``objects``: a list of dicts with ``name``, ``role``
    (subject/annotation/backdrop), ``grounded`` (bool), ``in_frame`` /
    ``cropped`` / ``behind_camera`` (bools), ``ground_gap`` (float or None),
    ``size`` (float) and ``bbox`` (NDC rectangle or None). Optional
    ``frames`` lists the frames that were sampled.
    """
    th = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    checks = []
    if not composition.get("camera"):
        checks.append(_check("camera", "fail", "scene has no active camera"))
        return checks
    checks.append(_check("camera", "pass", f"camera {composition['camera']!r}"))

    objs = [o for o in composition.get("objects", []) if o.get("role") in ("subject", "annotation")]
    if not objs:
        checks.append(_check("subjects_present", "fail", "no visible subject objects to check"))
        return checks

    behind = [o["name"] for o in objs if o.get("behind_camera")]
    checks.append(_check(
        "behind_camera", "fail" if behind else "pass",
        (f"{len(behind)} object(s) are behind the camera or cut by its near clip plane"
         if behind else "nothing is behind the camera"),
        objects=behind))

    out = [o["name"] for o in objs if not o.get("behind_camera") and not o.get("visible_in_frame", True)]
    cropped = [o["name"] for o in objs if o.get("cropped") and o["name"] not in out]
    if out or cropped:
        parts = []
        if cropped:
            parts.append(f"{len(cropped)} cropped by the frame edge")
        if out:
            parts.append(f"{len(out)} completely outside the frame")
        checks.append(_check("in_frame", "fail", "; ".join(parts), objects=cropped + out,
                             cropped=cropped, outside=out))
    else:
        checks.append(_check("in_frame", "pass", f"all {len(objs)} subject/annotation object(s) fully in frame"))

    ground_z = composition.get("ground_z")
    grounded = [o for o in objs if o.get("grounded") and o.get("ground_gap") is not None]
    if ground_z is None and not grounded:
        checks.append(_check("ground_contact", "skip", "no ground plane detected or declared"))
    elif not grounded:
        checks.append(_check("ground_contact", "skip", "no object is expected to rest on the ground"))
    else:
        floating, sunk = [], []
        for o in grounded:
            tol = max(th["ground_tolerance"], th["ground_tolerance_rel"] * float(o.get("size") or 0.0))
            if o["ground_gap"] > tol:
                floating.append(o["name"])
            elif o["ground_gap"] < -tol:
                sunk.append(o["name"])
        if floating:
            worst = max(o["ground_gap"] for o in grounded)
            checks.append(_check("ground_contact", "fail",
                                 f"{len(floating)} object(s) float above the ground (worst gap {worst:.4g})",
                                 objects=floating))
        elif sunk:
            checks.append(_check("ground_contact", "warn",
                                 f"{len(sunk)} object(s) sink below the ground", objects=sunk))
        else:
            checks.append(_check("ground_contact", "pass",
                                 f"{len(grounded)} grounded object(s) rest on the ground"))

    coverage = composition.get("coverage")
    if coverage is None:
        checks.append(_check("coverage", "skip", "coverage not measured"))
    elif coverage < th["min_coverage_fail"]:
        checks.append(_check("coverage", "fail", f"subjects cover only {coverage:.1%} of the frame",
                             value=coverage))
    elif coverage < th["min_coverage_warn"]:
        checks.append(_check("coverage", "warn", f"subjects cover only {coverage:.1%} of the frame",
                             value=coverage))
    else:
        checks.append(_check("coverage", "pass", f"subjects cover {coverage:.1%} of the frame", value=coverage))

    measured = [o for o in objs if o.get("role") == "annotation" and o.get("occluded") is not None]
    if measured:
        hidden = [o["name"] for o in measured if o["occluded"] > th["occluded_fail"]]
        partly = [o["name"] for o in measured
                  if th["occluded_warn"] < o["occluded"] <= th["occluded_fail"]]
        if hidden:
            checks.append(_check("annotation_occluded", "fail",
                                 f"{len(hidden)} label(s) are mostly hidden behind other objects",
                                 objects=hidden, partly=partly))
        elif partly:
            checks.append(_check("annotation_occluded", "warn",
                                 f"{len(partly)} label(s) are partly hidden behind other objects",
                                 objects=partly))
        else:
            checks.append(_check("annotation_occluded", "pass",
                                 f"{len(measured)} label(s) visible to the camera"))

    notes = [o for o in objs if o.get("role") == "annotation" and o.get("bbox") and o.get("visible_in_frame", True)]
    overlaps = []
    for i, a in enumerate(notes):
        for b in notes[i + 1:]:
            if rects_overlap(a["bbox"], b["bbox"]):
                overlaps.append([a["name"], b["name"]])
    if notes:
        checks.append(_check(
            "annotation_overlap", "warn" if overlaps else "pass",
            f"{len(overlaps)} pair(s) of labels overlap on screen" if overlaps else
            f"{len(notes)} label(s), none overlapping", pairs=overlaps))
    return checks


def evaluate_image(stats, thresholds=None):
    """Turn measured image statistics into pass/warn/fail checks.

    ``stats`` has ``mean_luminance``, ``std_luminance``, ``clipped`` and
    ``crushed`` (fractions of pixels), ``alpha_coverage`` and ``has_alpha``.
    Luminance is measured on display-referred (sRGB) values in [0, 1].
    """
    th = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    checks = []
    if stats.get("error"):
        return [_check("image_readable", "fail", f"could not read image: {stats['error']}")]
    alpha_cov = stats.get("alpha_coverage", 1.0)
    if stats.get("has_alpha") and alpha_cov < th["min_alpha_coverage"]:
        checks.append(_check("image_alpha", "fail",
                             f"transparent render is empty ({alpha_cov:.2%} opaque pixels)", value=alpha_cov))
        return checks
    if stats.get("has_alpha"):
        checks.append(_check("image_alpha", "pass", f"{alpha_cov:.1%} of pixels are opaque", value=alpha_cov))

    mean, std = stats["mean_luminance"], stats["std_luminance"]
    if mean < th["black_mean"]:
        checks.append(_check("image_exposure", "fail",
                             f"image is black (mean luminance {mean:.4f}); check lights, world and exposure",
                             value=mean))
    elif mean < th["dark_mean_warn"]:
        checks.append(_check("image_exposure", "warn", f"image is very dark (mean luminance {mean:.3f})",
                             value=mean))
    elif mean > th["bright_mean_warn"]:
        checks.append(_check("image_exposure", "warn", f"image is washed out (mean luminance {mean:.3f})",
                             value=mean))
    else:
        checks.append(_check("image_exposure", "pass", f"mean luminance {mean:.3f}", value=mean))

    if std < th["uniform_std"]:
        checks.append(_check("image_detail", "fail",
                             f"image is a flat, uniform frame (luminance std {std:.4f})", value=std))
    else:
        checks.append(_check("image_detail", "pass", f"luminance std {std:.3f}", value=std))

    clipped, crushed = stats.get("clipped", 0.0), stats.get("crushed", 0.0)
    if clipped > th["clipped_warn"] or crushed > th["crushed_warn"]:
        parts = []
        if clipped > th["clipped_warn"]:
            parts.append(f"{clipped:.1%} of pixels clipped to white")
        if crushed > th["crushed_warn"]:
            parts.append(f"{crushed:.1%} of pixels crushed to black")
        checks.append(_check("image_clipping", "warn", "; ".join(parts), clipped=clipped, crushed=crushed))
    else:
        checks.append(_check("image_clipping", "pass",
                             f"{clipped:.1%} clipped, {crushed:.1%} crushed", clipped=clipped, crushed=crushed))
    return checks


def summarize_checks(checks):
    """Return ``(passed, worst_status, counts)`` for a list of checks."""
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    worst = "pass"
    for c in checks:
        if STATUS_ORDER.get(c["status"], 0) > STATUS_ORDER[worst]:
            worst = c["status"]
    return counts["fail"] == 0, worst, counts


def build_report(script, scene_facts, composition, images, checks, extra=None):
    """Assemble the JSON-serialisable render report."""
    passed, worst, counts = summarize_checks(checks)
    report = {
        "tool": "blender-python-toolkit",
        "report_version": REPORT_VERSION,
        "script": script,
        "passed": passed,
        "status": worst,
        "counts": counts,
        "checks": checks,
        "scene": scene_facts,
        "composition": composition,
        "images": images,
    }
    if extra:
        report.update(extra)
    return report


REPORT_REQUIRED = {
    "tool": str, "report_version": int, "script": str, "passed": bool, "status": str,
    "counts": dict, "checks": list, "scene": dict, "composition": (dict, type(None)), "images": list,
}


def validate_report(report):
    """Structural validation of a report dict. Returns a list of problems."""
    problems = []
    if not isinstance(report, dict):
        return ["report must be an object"]
    for key, typ in REPORT_REQUIRED.items():
        if key not in report:
            problems.append(f"missing key {key!r}")
        elif not isinstance(report[key], typ) or (typ is int and isinstance(report[key], bool)):
            problems.append(f"{key!r} has type {type(report[key]).__name__}")
    for i, check in enumerate(report.get("checks") or []):
        if not isinstance(check, dict):
            problems.append(f"checks[{i}] must be an object")
            continue
        for key in ("id", "status", "message"):
            if not isinstance(check.get(key), str):
                problems.append(f"checks[{i}].{key} must be a string")
        if check.get("status") not in STATUS_ORDER:
            problems.append(f"checks[{i}].status {check.get('status')!r} is not pass/warn/fail/skip")
    if isinstance(report.get("checks"), list) and isinstance(report.get("passed"), bool):
        expected = not any(isinstance(c, dict) and c.get("status") == "fail" for c in report["checks"])
        if expected != report["passed"]:
            problems.append("'passed' disagrees with the check statuses")
    try:
        json.dumps(report)
    except (TypeError, ValueError) as exc:
        problems.append(f"report is not JSON-serialisable: {exc}")
    return problems


def format_report_text(report):
    """One-screen, plain-text summary of a report, for humans and agents."""
    lines = [f"QA {report['status'].upper()}  ({report['script']})  "
             f"pass={report['counts'].get('pass', 0)} warn={report['counts'].get('warn', 0)} "
             f"fail={report['counts'].get('fail', 0)} skip={report['counts'].get('skip', 0)}"]
    for check in report["checks"]:
        lines.append(f"  [{check['status'].upper():<4}] {check['id']:<18} {check['message']}")
        names = check.get("objects")
        if names and check["status"] in ("fail", "warn"):
            shown = ", ".join(names[:8]) + (f" (+{len(names) - 8} more)" if len(names) > 8 else "")
            lines.append(f"         -> {shown}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Exit codes and machine-readable summaries (bpt --json)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILED = 1           # Python/Blender error, or files that failed in a batch
EXIT_INVALID_INPUT = 2    # bad flags, missing/unsupported input, a request this build cannot do
EXIT_CHECK_FAILED = 3     # outputs written but a check failed (--qa-strict QA, export round trip)

SUMMARY_ENV = "BPT_SUMMARY"


def fail_input(message, stream=None):
    """Print ``message`` to stderr and exit with code 2 (invalid input)."""
    print(message, file=stream or sys.stderr)
    (stream or sys.stderr).flush()
    raise SystemExit(EXIT_INVALID_INPUT)


def summary_path(env=None):
    """Where ``bpt --json`` wants the script's summary (``$BPT_SUMMARY``), or None."""
    return (os.environ if env is None else env).get(SUMMARY_ENV) or None


def write_summary(payload, path=None, env=None):
    """Write the run's summary object for ``bpt --json``; no-op when not requested.

    Returns the path written or None. The last call wins, so a script may
    write a partial summary early and the complete one at the end.
    """
    path = path or summary_path(env)
    if not path:
        return None
    data = {"tool": "blender-python-toolkit"}
    data.update(payload)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    return path


def condense_report(report):
    """A QA report reduced to its verdict and one line per check (for summaries)."""
    if not report:
        return None
    checks = []
    for c in report.get("checks", []):
        item = {"id": c["id"], "status": c["status"], "message": c["message"]}
        if c.get("image"):
            item["image"] = c["image"]
        checks.append(item)
    return {
        "passed": report.get("passed"),
        "status": report.get("status"),
        "counts": report.get("counts"),
        "failing": [c["id"] for c in checks if c["status"] == "fail"],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Model files
# ---------------------------------------------------------------------------

MODEL_EXTENSIONS = (".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply", ".usd", ".usda", ".usdc", ".usdz")
SCENE_EXTENSIONS = (".blend",)


def find_inputs(folder, extensions, recursive=False):
    """Sorted files under ``folder`` whose extension (any case) is in ``extensions``.

    Hidden files and Blender backups (``.blend1``) are skipped.
    """
    extensions = {e.lower() for e in extensions}
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in extensions:
                out.append(os.path.join(root, name))
        if not recursive:
            break
    return sorted(out, key=lambda p: os.path.relpath(p, folder).lower())


def thumbnail_names(paths, root=None, ext=".png"):
    """Unique, readable output names for ``paths`` (``chair.glb`` -> ``chair.png``).

    Files sharing a stem get their extension added (``chair_glb.png``,
    ``chair_obj.png``); files that still collide (same name in different
    subfolders) get their relative folder too. Returns ``{path: name}``.
    """
    paths = list(paths)

    def stem(p):
        return os.path.splitext(os.path.basename(p))[0]

    def safe(text):
        return re.sub(r"[^\w.\-]+", "_", text).strip("_") or "model"

    by_stem = {}
    for p in paths:
        by_stem.setdefault(stem(p).lower(), []).append(p)
    names, taken = {}, set()
    for p in paths:
        if len(by_stem[stem(p).lower()]) == 1:
            name = safe(stem(p))
        else:
            name = safe(f"{stem(p)}_{os.path.splitext(p)[1].lstrip('.').lower()}")
            if root and name.lower() in taken:
                rel = os.path.splitext(os.path.relpath(p, root))[0]
                name = safe(rel.replace(os.sep, "__").replace("/", "__") + "_" + os.path.splitext(p)[1].lstrip("."))
        base, n = name, 1
        while name.lower() in taken:
            name = f"{base}_{n:02d}"
            n += 1
        taken.add(name.lower())
        names[p] = name + ext
    return names


def parse_background(text):
    """``transparent`` / ``none`` -> None; ``#RRGGBB`` / ``#RGB`` / white / black -> sRGB (r, g, b) in 0..1."""
    value = str(text).strip().lower()
    if value in {"transparent", "none", "alpha"}:
        return None
    named = {"white": "ffffff", "black": "000000", "gray": "808080", "grey": "808080"}
    h = named.get(value, value).lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6 or any(ch not in "0123456789abcdef" for ch in h):
        raise ValueError(f"background must be 'transparent' or a colour like '#20242a', got {text!r}")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def contact_sheet_layout(count, cell, columns=None, pad=8, label_height=0):
    """Grid geometry for a contact sheet of ``count`` square cells.

    Returns a dict with ``columns``, ``rows``, ``width``, ``height`` and
    ``cells``: the top-left ``(x, y)`` of each image, y measured from the top.
    The column count defaults to ``ceil(sqrt(count))``.
    """
    count = max(int(count), 0)
    cols = int(columns) if columns else max(1, math.ceil(math.sqrt(count or 1)))
    cols = max(1, min(cols, max(count, 1)))
    rows = max(1, math.ceil(count / cols)) if count else 1
    width = cols * cell + (cols + 1) * pad
    height = rows * (cell + label_height) + (rows + 1) * pad
    cells = []
    for i in range(count):
        r, c = divmod(i, cols)
        cells.append((pad + c * (cell + pad), pad + r * (cell + label_height + pad)))
    return {"columns": cols, "rows": rows, "width": width, "height": height, "cells": cells}


# A 5x7 bitmap font for contact-sheet labels (no font files or PIL needed in
# Blender's Python). Each glyph is 7 rows of 5 bits, most significant bit left.
_GLYPHS = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11), "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E), "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F), "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F), "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E), "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11), "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11), "N": (0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E), "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D), "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E), "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E), "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A), "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x11, 0x0A, 0x04, 0x04, 0x04), "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E), "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F), "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02), "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E), "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E), "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C), "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F), "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08), " ": (0x00,) * 7,
    "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04), "!": (0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04),
    "+": (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00), "=": (0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10), ":": (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00),
    ",": (0x00, 0x00, 0x00, 0x00, 0x0C, 0x04, 0x08), "#": (0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A),
}
GLYPH_W, GLYPH_H = 5, 7


def elide_middle(text, max_chars):
    """Shorten ``text`` to ``max_chars`` by replacing its middle with ``..``."""
    text = str(text)
    if max_chars < 4 or len(text) <= max_chars:
        return text[:max(max_chars, 0)] if len(text) > max_chars else text
    keep = max_chars - 2
    head = (keep + 1) // 2
    return text[:head] + ".." + text[len(text) - (keep - head):]


def text_bitmap(text):
    """Rows (top to bottom) of 0/1 pixels for ``text`` in the 5x7 font.

    Letters are drawn upper-case; characters without a glyph become ``?``.
    Glyphs are separated by one blank column. Width = 6 * len(text) - 1.
    """
    text = str(text).upper()
    if not text:
        return [[] for _ in range(GLYPH_H)]
    rows = [[] for _ in range(GLYPH_H)]
    for i, ch in enumerate(text):
        glyph = _GLYPHS.get(ch, _GLYPHS["?"])
        for r in range(GLYPH_H):
            if i:
                rows[r].append(0)
            bits = glyph[r]
            rows[r].extend((bits >> (GLYPH_W - 1 - b)) & 1 for b in range(GLYPH_W))
    return rows


def thumbnails_exit_code(entries, strict=False):
    """0, or 1 with ``strict`` when any entry is not ``ok`` (or nothing was found)."""
    if not strict:
        return EXIT_OK
    if not entries or any(e.get("status") != "ok" for e in entries):
        return EXIT_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# glTF / GLB inspection and web-export maths (scripts/export_web.py)
# ---------------------------------------------------------------------------


class GltfError(ValueError):
    """Raised for files that are not valid glTF 2.0 binaries."""


_GLB_JSON, _GLB_BIN = 0x4E4F534A, 0x004E4942


def read_glb(data):
    """Split a GLB (binary glTF 2.0) into ``(json_dict, bin_chunk_bytes)``."""
    import struct

    data = bytes(data)
    if len(data) < 20 or data[:4] != b"glTF":
        raise GltfError("not a GLB file (missing 'glTF' magic)")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise GltfError(f"GLB version {version} is not supported (glTF 2.0 only)")
    if length > len(data):
        raise GltfError(f"GLB is truncated: header says {length} bytes, file has {len(data)}")
    offset, doc, binary = 12, None, b""
    while offset + 8 <= length:
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8: offset + 8 + chunk_len]
        if chunk_type == _GLB_JSON and doc is None:
            try:
                doc = json.loads(chunk.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GltfError(f"GLB JSON chunk is invalid: {exc}") from None
        elif chunk_type == _GLB_BIN and not binary:
            binary = chunk
        offset += 8 + chunk_len
    if doc is None:
        raise GltfError("GLB has no JSON chunk")
    return doc, binary


def image_size(data):
    """``(format, width, height)`` of PNG / JPEG / WebP bytes, or None."""
    import struct

    data = bytes(data[:65536]) if len(data) > 65536 else bytes(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "PNG", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 2 if marker != 0xFF else 1
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "JPEG", w, h
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return "WEBP", w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L" and len(data) >= 25:
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return "WEBP", w, h
        if kind == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return "WEBP", w, h
    return None


def _mesh_instances(doc):
    """``{mesh_index: instance_count}`` over the default scene's node trees."""
    nodes = doc.get("nodes", [])
    scenes = doc.get("scenes", [])
    if scenes:
        roots = list(scenes[doc.get("scene", 0)].get("nodes", []))
    else:  # no scene: every node that is nobody's child
        children = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in children]
    counts, stack, seen = {}, list(roots), set()
    while stack:
        idx = stack.pop()
        if idx in seen or not 0 <= idx < len(nodes):
            continue
        seen.add(idx)
        node = nodes[idx]
        if "mesh" in node:
            counts[node["mesh"]] = counts.get(node["mesh"], 0) + 1
        stack.extend(node.get("children", []))
    return counts


def gltf_stats(doc, binary=b""):
    """Counts read straight from a glTF document (no Blender involved).

    ``triangles`` and ``vertices`` multiply each mesh by the nodes that
    instance it in the default scene; triangle strips/fans are counted as
    ``n - 2``, lines and points as 0. ``images`` lists embedded images with
    their pixel size, read from the bytes in the binary chunk.
    """
    accessors = doc.get("accessors", [])
    instances = _mesh_instances(doc)
    triangles = vertices = primitives = 0
    for mesh_index, mesh in enumerate(doc.get("meshes", [])):
        n = instances.get(mesh_index, 0)
        for prim in mesh.get("primitives", []):
            primitives += n
            pos = prim.get("attributes", {}).get("POSITION")
            vcount = accessors[pos]["count"] if pos is not None and pos < len(accessors) else 0
            icount = accessors[prim["indices"]]["count"] if "indices" in prim else vcount
            mode = prim.get("mode", 4)
            if mode == 4:
                tris = icount // 3
            elif mode in (5, 6):
                tris = max(icount - 2, 0)
            else:
                tris = 0
            triangles += n * tris
            vertices += n * vcount
    images = []
    views = doc.get("bufferViews", [])
    for i, img in enumerate(doc.get("images", [])):
        entry = {"index": i, "name": img.get("name"), "mime": img.get("mimeType"), "uri": img.get("uri")}
        bv = img.get("bufferView")
        if bv is not None and bv < len(views) and binary:
            view = views[bv]
            start = view.get("byteOffset", 0)
            blob = binary[start:start + view["byteLength"]]
            entry["bytes"] = len(blob)
            size = image_size(blob)
            if size:
                entry["format"], entry["width"], entry["height"] = size
        images.append(entry)
    used = list(doc.get("extensionsUsed", []))
    return {
        "generator": doc.get("asset", {}).get("generator"),
        "nodes": len(doc.get("nodes", [])),
        "meshes": len(doc.get("meshes", [])),
        "mesh_instances": sum(instances.values()),
        "primitives": primitives,
        "triangles": triangles,
        "vertices": vertices,
        "materials": len(doc.get("materials", [])),
        "textures": len(doc.get("textures", [])),
        "images": images,
        "extensions_used": used,
        "extensions_required": list(doc.get("extensionsRequired", [])),
        "draco": "KHR_draco_mesh_compression" in used,
    }


def decimate_ratio(triangles, ratio=None, max_triangles=None):
    """Collapse ratio to request, or None when no reduction is needed.

    ``ratio`` must be in (0, 1]; ``max_triangles`` >= 1 gives
    ``max_triangles / triangles`` when the mesh is over budget.
    """
    if ratio is not None and max_triangles is not None:
        raise ValueError("use either a decimate ratio or a triangle budget, not both")
    if ratio is not None:
        ratio = float(ratio)
        if not 0.0 < ratio <= 1.0:
            raise ValueError(f"decimate ratio must be in (0, 1], got {ratio}")
        return None if ratio >= 1.0 else ratio
    if max_triangles is not None:
        if int(max_triangles) < 1:
            raise ValueError(f"triangle budget must be >= 1, got {max_triangles}")
        if triangles <= max_triangles:
            return None
        return max_triangles / float(triangles)
    return None


def refine_ratio(ratio, achieved, target):
    """Next collapse ratio when ``achieved`` triangles overshot ``target``.

    Decimation is not exact (small meshes cannot shrink below a few
    triangles), so the ratio is scaled by ``target / achieved`` with a 2%
    safety margin.
    """
    if achieved <= target or achieved <= 0:
        return ratio
    return max(ratio * (target / float(achieved)) * 0.98, 1e-6)


def fit_size(width, height, max_size):
    """``(w, h)`` scaled so the larger side is at most ``max_size`` (aspect kept, >= 1)."""
    width, height, max_size = int(width), int(height), int(max_size)
    if max_size < 1:
        raise ValueError(f"texture size must be >= 1, got {max_size}")
    if max(width, height) <= max_size:
        return width, height
    scale = max_size / float(max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def human_bytes(n):
    """``1536`` -> ``'1.5 KB'``."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"  # pragma: no cover


ROUNDTRIP_TOLERANCE = 1e-3  # fraction of the bounding-box diagonal / of the triangle count


def roundtrip_checks(expected, measured, tolerance=ROUNDTRIP_TOLERANCE, label="glb"):
    """Compare what was exported with what a fresh import measured.

    ``expected`` and ``measured`` have ``triangles`` and ``bounds``
    (``{"min": [x, y, z], "max": [x, y, z]}``, metres). Bounds may differ by
    ``tolerance`` x the expected diagonal (plus 1e-5 m for float32 storage);
    triangles by ``tolerance`` x the expected count (rounded down). Returns QA
    checks in the same shape as the render reports.
    """
    checks = []
    exp_t, got_t = int(expected["triangles"]), int(measured["triangles"])
    allowed = int(tolerance * exp_t)
    ok = abs(got_t - exp_t) <= allowed
    checks.append(_check(f"{label}_triangles", "pass" if ok else "fail",
                         f"{got_t} triangles re-imported, {exp_t} exported"
                         + ("" if ok else f" (allowed difference {allowed})"),
                         expected=exp_t, measured=got_t))
    lo_e, hi_e = expected["bounds"]["min"], expected["bounds"]["max"]
    lo_m, hi_m = measured["bounds"]["min"], measured["bounds"]["max"]
    diag = math.sqrt(sum((h - l) ** 2 for l, h in zip(lo_e, hi_e)))
    tol = tolerance * diag + 1e-5
    worst = max(max(abs(a - b) for a, b in zip(lo_e, lo_m)), max(abs(a - b) for a, b in zip(hi_e, hi_m)))
    ok = worst <= tol
    checks.append(_check(f"{label}_bounds", "pass" if ok else "fail",
                         f"bounds agree within {worst:.3g} m (tolerance {tol:.3g} m)" if ok else
                         f"bounds differ by {worst:.4g} m (tolerance {tol:.3g} m): exported "
                         f"{_fmt_box(lo_e, hi_e)}, re-imported {_fmt_box(lo_m, hi_m)}",
                         expected=expected["bounds"], measured=measured["bounds"], difference=worst))
    return checks


def _fmt_box(lo, hi):
    return "[" + ", ".join(f"{a:.4g}..{b:.4g}" for a, b in zip(lo, hi)) + "]"


def export_exit_code(report):
    """0 when every verification check passed, 3 when one failed."""
    failed = any(c.get("status") == "fail" for c in report.get("verification", {}).get("checks", []))
    return EXIT_CHECK_FAILED if failed else EXIT_OK


def format_export_text(report):
    """Short human summary of an export report."""
    src, out = report["source"], report["export"]
    size = report["bounds"]["size"]
    lines = [
        f"EXPORT {report['status'].upper()}  {os.path.basename(report['input'])}",
        f"  triangles   {src['triangles']} -> {out['triangles']}"
        + (f"  (decimate ratio {out['decimate_ratio']:.4g})" if out.get("decimate_ratio") else ""),
        f"  vertices    {src['vertices']} -> {out['vertices']} (Blender count; glTF splits along seams)",
        f"  area (m2)   {src.get('area', 0):.4g} -> {out.get('area', 0):.4g}",
        f"  size (m)    {size[0]:.4g} x {size[1]:.4g} x {size[2]:.4g}  (X x Y x Z, Blender axes)",
        f"  materials   {len(report['materials'])}, textures {len(report['textures'])}",
    ]
    for item in report["outputs"]:
        lines.append(f"  {item['format']:<11} {item['path']}  {human_bytes(item['bytes'])}"
                     + ("  draco" if item.get("draco") else ""))
    for check in report.get("verification", {}).get("checks", []):
        lines.append(f"  [{check['status'].upper():<4}] {check['id']:<16} {check['message']}")
    for warning in report.get("warnings", []):
        lines.append(f"  ! {warning}")
    return "\n".join(lines)
