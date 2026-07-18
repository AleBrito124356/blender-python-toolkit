"""Apply a named render preset from presets.yaml to a .blend.

Presets (preview / social / print) live in presets.yaml at the repo root and
set resolution, samples, engine, color management and output format in one
shot. Handy for pushing the same scene to a fast look-dev pass and a final
deliverable without hand-tweaking a dozen settings.

PyYAML is used when available; if it is not installed into Blender's bundled
Python, a small built-in parser reads the flat preset structure so the script
still runs out of the box.

Run headless:

    blender --background --python scripts/render_presets.py -- --list
    blender --background --python scripts/render_presets.py -- --input scene.blend --preset social --render

Arguments (after the "--" separator):
    --input PATH    The .blend to configure (required unless --list).
    --preset NAME   Preset name from presets.yaml (preview / social / print).
    --presets PATH  Preset file (default presets.yaml at the repo root).
    --list          Print available presets and exit.
    --render        Render a still after applying the preset.
    --out PATH      Output image path (default out/<preset>.png).
    --engine NAME   Override the preset engine (eevee / cycles).
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from lib.common import (  # noqa: E402
    ensure_dir,
    set_engine,
    set_samples,
    set_view_transform,
    get_argv_after_dashes,
)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="render_presets.py",
        description="Apply a named render preset to a .blend.",
    )
    parser.add_argument("--input", default=None, help="Input .blend file.")
    parser.add_argument("--preset", default="preview", help="Preset name.")
    parser.add_argument("--presets", default=os.path.join(REPO_ROOT, "presets.yaml"),
                        help="Preset YAML file.")
    parser.add_argument("--list", action="store_true", help="List presets and exit.")
    parser.add_argument("--render", action="store_true", help="Render after applying.")
    parser.add_argument("--out", default=None, help="Output image path.")
    parser.add_argument("--engine", default=None, help="Override engine (eevee/cycles).")
    return parser.parse_args(get_argv_after_dashes())


def _coerce(value):
    """Convert a scalar string to int / float / bool / None where sensible."""
    value = value.strip()
    if value == "" or value.lower() in {"null", "~", "none"}:
        return None
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if (value[0] == value[-1]) and value[0] in {'"', "'"}:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _parse_inline_list(value):
    """Parse an inline list like ``[1920, 1080]`` into a Python list."""
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    return [_coerce(part) for part in inner.split(",")]


def _fallback_yaml(path):
    """Minimal parser for the two-level preset file when PyYAML is absent.

    Handles: comment lines, top-level ``name:`` keys, and 2-space indented
    ``key: value`` pairs whose value is a scalar or an inline ``[a, b]`` list.
    This is intentionally narrow - it only needs to read presets.yaml.
    """
    data = {}
    current = None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.split(" #", 1)[0].strip()  # drop trailing comments
            if indent == 0:
                current = {}
                data[key] = current
            else:
                if current is None:
                    continue
                if value.startswith("[") and value.endswith("]"):
                    current[key] = _parse_inline_list(value)
                else:
                    current[key] = _coerce(value)
    return data


def load_presets(path):
    """Load presets, preferring PyYAML and falling back to the built-in parser."""
    if not os.path.exists(path):
        raise SystemExit(f"Presets file not found: {path}")
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except ImportError:
        print("[presets] PyYAML not found in Blender's Python; using built-in parser.")
        return _fallback_yaml(path)


def apply_preset(scene, preset):
    """Apply one preset dict onto the scene's render settings."""
    resolution = preset.get("resolution")
    if resolution and len(resolution) == 2:
        scene.render.resolution_x = int(resolution[0])
        scene.render.resolution_y = int(resolution[1])
    scene.render.resolution_percentage = int(preset.get("percentage", 100))

    engine = set_engine(scene, preset.get("engine", "eevee"))
    set_samples(scene, preset.get("samples", 64))

    view_transform = preset.get("view_transform", "auto")
    if view_transform in (None, "auto"):
        applied_vt = set_view_transform(scene)
    else:
        try:
            scene.view_settings.view_transform = view_transform
            applied_vt = view_transform
        except (TypeError, AttributeError):
            applied_vt = set_view_transform(scene)

    fmt = preset.get("format", "PNG")
    scene.render.image_settings.file_format = fmt
    scene.render.film_transparent = bool(preset.get("film_transparent", False))

    return {
        "engine": engine,
        "resolution": (scene.render.resolution_x, scene.render.resolution_y),
        "samples": preset.get("samples", 64),
        "view_transform": applied_vt,
        "format": fmt,
    }


def main():
    args = parse_args()
    presets = load_presets(os.path.abspath(args.presets))

    if args.list:
        print("Available presets:")
        for name, preset in presets.items():
            res = preset.get("resolution", ["?", "?"])
            print(f"  {name:<10} {res[0]}x{res[1]}  "
                  f"{preset.get('engine', 'eevee')}  "
                  f"{preset.get('samples', '?')} samples")
        return

    if args.preset not in presets:
        raise SystemExit(
            f"Unknown preset {args.preset!r}. Available: {', '.join(presets)}"
        )
    if not args.input:
        raise SystemExit("--input is required unless you pass --list.")

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        raise SystemExit(f"Input .blend not found: {input_path}")

    bpy.ops.wm.open_mainfile(filepath=input_path)
    scene = bpy.context.scene

    preset = dict(presets[args.preset])
    if args.engine:
        preset["engine"] = args.engine

    applied = apply_preset(scene, preset)
    note = "AgX (Blender 4.0+)" if applied["view_transform"] == "AgX" else applied["view_transform"]
    print(f"[presets] applied '{args.preset}': "
          f"{applied['resolution'][0]}x{applied['resolution'][1]}, "
          f"engine={applied['engine']}, samples={applied['samples']}, "
          f"view_transform={note}, format={applied['format']}")

    if args.render:
        if scene.camera is None:
            raise SystemExit("Scene has no active camera to render from.")
        out_path = os.path.abspath(args.out) if args.out else os.path.abspath(
            os.path.join("out", f"{args.preset}.png")
        )
        ensure_dir(os.path.dirname(out_path))
        scene.render.filepath = out_path
        print(f"[presets] rendering -> {out_path}")
        bpy.ops.render.render(write_still=True)
        print(f"[presets] wrote {out_path}")


if __name__ == "__main__":
    main()
