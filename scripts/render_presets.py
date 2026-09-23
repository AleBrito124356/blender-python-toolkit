"""Apply a named render preset from presets.yaml to a .blend, then save and/or render.

Presets (preview / social / print, or your own) live in ``presets.yaml`` at the
repo root and set resolution, samples, engine, colour management, output
format and transparency in one shot. Every preset is validated (unknown keys,
bad resolutions, unknown formats...) before anything is touched.

Applying a preset only changes the scene in memory, so choose what to keep:

* ``--save`` writes ``<name>_<preset>.blend`` next to the input (never
  overwriting an existing file; a numeric suffix is added instead),
* ``--in-place`` overwrites the input .blend (explicit opt-in; Blender keeps
  its usual ``.blend1`` backup),
* ``--render`` renders a still; the file extension follows the preset format.

PyYAML is used when Blender's Python has it; otherwise a built-in parser reads
the two-level preset format, so stock Blender works out of the box.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/render_presets.py -- --list
    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/render_presets.py -- --input scene.blend --preset social --save --render

Arguments (after the "--" separator):
    --input PATH    The .blend to configure (required unless --list).
    --preset NAME   Preset name from the presets file (default preview).
    --presets PATH  Preset file (default presets.yaml at the repo root).
    --list          Print available presets (validated) and exit.
    --save          Save the configured scene as <name>_<preset>.blend.
    --in-place      Overwrite the input .blend instead.
    --render        Render a still after applying the preset.
    --out PATH      Output image path (default out/<preset>.<ext for the format>).
    --engine NAME   Override the preset engine (eevee / cycles).
    --device NAME   Cycles device: gpu (default, falls back to CPU) or cpu.
    --report PATH   Write a JSON QA report for the render.
    --qa-strict     Exit with code 3 if any QA check fails.
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
    get_argv_after_dashes,
    run_qa,
    set_engine,
    set_image_output,
    set_samples,
    set_view_transform,
)
from lib.core import (  # noqa: E402
    PresetError,
    image_extension,
    load_presets_text,
    parse_simple_yaml,  # noqa: F401  (kept importable for older callers)
    swap_extension,
    unique_path,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="render_presets.py",
        description="Apply a named render preset to a .blend.",
    )
    parser.add_argument("--input", default=None, help="Input .blend file.")
    parser.add_argument("--preset", default="preview", help="Preset name.")
    parser.add_argument("--presets", default=os.path.join(REPO_ROOT, "presets.yaml"), help="Preset YAML file.")
    parser.add_argument("--list", action="store_true", help="List presets and exit.")
    parser.add_argument("--save", action="store_true", help="Save as <name>_<preset>.blend.")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the input .blend.")
    parser.add_argument("--render", action="store_true", help="Render after applying.")
    parser.add_argument("--out", default=None, help="Output image path.")
    parser.add_argument("--engine", choices=("eevee", "cycles"), default=None, help="Override engine.")
    parser.add_argument("--device", choices=("gpu", "cpu"), default=None, help="Cycles device.")
    parser.add_argument("--report", default=None, help="Write a JSON QA report for the render.")
    parser.add_argument("--qa-strict", action="store_true", help="Exit 3 if a QA check fails.")
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if args.save and args.in_place:
        parser.error("--save and --in-place are mutually exclusive")
    return args


def load_presets(path):
    """Load and validate presets; returns ``(presets, parser_name)``."""
    if not os.path.exists(path):
        raise SystemExit(f"Presets file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return load_presets_text(text)
    except PresetError as exc:
        raise SystemExit(f"[presets] {path}: {exc}") from None


def apply_preset(scene, preset):
    """Apply one validated preset dict onto the scene's render settings."""
    resolution = preset.get("resolution")
    if resolution:
        scene.render.resolution_x = int(resolution[0])
        scene.render.resolution_y = int(resolution[1])
    scene.render.resolution_percentage = int(preset.get("percentage", 100))

    engine = set_engine(scene, preset.get("engine", "eevee"), device=preset.get("device", "gpu"))
    samples = int(preset.get("samples", 64))
    set_samples(scene, samples)

    view_transform = preset.get("view_transform", "auto")
    if view_transform in (None, "auto"):
        applied_vt = set_view_transform(scene)
    else:
        try:
            scene.view_settings.view_transform = view_transform
            applied_vt = view_transform
        except (TypeError, AttributeError):
            print(f"[presets] view transform {view_transform!r} not available; using the default")
            applied_vt = set_view_transform(scene)
    if preset.get("look"):
        try:
            scene.view_settings.look = preset["look"]
        except TypeError:
            print(f"[presets] look {preset['look']!r} not available; keeping {scene.view_settings.look!r}")

    fmt = str(preset.get("format", "PNG")).upper()
    transparent = bool(preset.get("film_transparent", False))
    scene.render.film_transparent = transparent
    color_mode = "RGBA" if transparent and fmt not in {"JPEG", "BMP", "HDR"} else "RGB"
    set_image_output(scene.render.image_settings, fmt, color_mode=color_mode)
    return {
        "engine": engine,
        "resolution": (scene.render.resolution_x, scene.render.resolution_y),
        "samples": samples,
        "view_transform": applied_vt,
        "format": fmt,
        "film_transparent": transparent,
    }


def main(argv=None):
    args = parse_args(argv)
    presets, parser_name = load_presets(os.path.abspath(args.presets))

    if args.list:
        print(f"Available presets ({parser_name} parser, {os.path.abspath(args.presets)}):")
        for name, preset in presets.items():
            res = preset.get("resolution", ["?", "?"])
            print(f"  {name:<10} {res[0]}x{res[1]}  {preset.get('engine', 'eevee'):<6}  "
                  f"{preset.get('samples', '?')} samples  {preset.get('format', 'PNG')}"
                  f"{'  transparent' if preset.get('film_transparent') else ''}")
        return

    if args.preset not in presets:
        raise SystemExit(f"Unknown preset {args.preset!r}. Available: {', '.join(presets)}")
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
    if args.device:
        preset["device"] = args.device
    applied = apply_preset(scene, preset)
    print(f"[presets] applied '{args.preset}': "
          f"{applied['resolution'][0]}x{applied['resolution'][1]}, "
          f"engine={applied['engine']}, samples={applied['samples']}, "
          f"view_transform={applied['view_transform']}, format={applied['format']}")

    if args.save or args.in_place:
        if args.in_place:
            target = input_path
        else:
            base = os.path.splitext(input_path)[0]
            target = unique_path(f"{base}_{args.preset}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=target)
        print(f"[presets] saved -> {target}")
    elif not args.render:
        print("[presets] nothing saved: pass --save (new file), --in-place (overwrite) or --render")

    images = []
    if args.render:
        if scene.camera is None:
            raise SystemExit("Scene has no active camera to render from.")
        ext = image_extension(applied["format"])
        if args.out:
            out_path = os.path.abspath(args.out)
            if os.path.splitext(out_path)[1].lower() != ext:
                out_path = swap_extension(out_path, ext)
                print(f"[presets] output extension follows the preset format -> {out_path}")
        else:
            out_path = os.path.abspath(os.path.join("out", f"{args.preset}{ext}"))
        ensure_dir(os.path.dirname(out_path))
        scene.render.use_file_extension = False
        scene.render.filepath = out_path
        print(f"[presets] rendering -> {out_path}")
        bpy.ops.render.render(write_still=True)
        print(f"[presets] wrote {out_path}")
        images.append(out_path)
        run_qa(args, "render_presets", images=images)


if __name__ == "__main__":
    main()
