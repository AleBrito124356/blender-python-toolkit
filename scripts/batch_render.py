"""Batch-render every .blend in a folder with per-file CLI overrides.

Opens each .blend, optionally overrides resolution / samples / engine, renders
a still (or the full animation), and writes an output image named after the
source file. Each file is wrapped in try/except so one broken .blend cannot
kill the whole batch; a summary table is printed at the end.

Run headless:

    blender --background --python scripts/batch_render.py -- --input scenes --output out/batch
    blender --background --python scripts/batch_render.py -- --input scenes --engine eevee --resolution 1920x1080 --samples 64

Arguments (after the "--" separator):
    --input DIR        Folder to scan for .blend files (required).
    --output DIR       Where to write renders (default out/batch).
    --engine NAME      Override engine: eevee or cycles (default: keep file's).
    --resolution WxH   Override resolution (default: keep file's).
    --samples N        Override sample count (default: keep file's).
    --format FMT       Image format id, e.g. PNG, JPEG, OPEN_EXR (default PNG).
    --animation        Render the full frame range instead of a single still.
    --recursive        Recurse into subfolders.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    ensure_dir,
    set_engine,
    set_samples,
    get_argv_after_dashes,
)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="batch_render.py",
        description="Render every .blend in a folder with optional overrides.",
    )
    parser.add_argument("--input", required=True, help="Folder of .blend files.")
    parser.add_argument("--output", default=os.path.join("out", "batch"), help="Output folder.")
    parser.add_argument("--engine", default=None, help="Override engine: eevee or cycles.")
    parser.add_argument("--resolution", default=None, help="Override resolution WxH.")
    parser.add_argument("--samples", type=int, default=None, help="Override sample count.")
    parser.add_argument("--format", default="PNG", help="Output image format id.")
    parser.add_argument("--animation", action="store_true", help="Render the full frame range.")
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders.")
    return parser.parse_args(get_argv_after_dashes())


def find_blend_files(input_dir, recursive):
    """Return a sorted list of .blend files, skipping .blend1 backups."""
    if recursive:
        pattern = os.path.join(input_dir, "**", "*.blend")
        files = glob.glob(pattern, recursive=True)
    else:
        files = glob.glob(os.path.join(input_dir, "*.blend"))
    return sorted(f for f in files if not f.endswith(".blend1"))


def apply_overrides(scene, args):
    """Apply CLI overrides onto the currently open scene."""
    if args.engine:
        set_engine(scene, args.engine)
    if args.samples is not None:
        set_samples(scene, args.samples)
    if args.resolution:
        try:
            width, height = (int(v) for v in args.resolution.lower().split("x"))
        except ValueError:
            raise SystemExit(f"--resolution must look like 1920x1080, got {args.resolution!r}")
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = args.format


def render_one(blend_path, args):
    """Open and render a single .blend, returning a result dict."""
    name = os.path.splitext(os.path.basename(blend_path))[0]
    started = time.time()

    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene

    if scene.camera is None:
        return {
            "file": name,
            "status": "SKIPPED",
            "detail": "no active camera",
            "seconds": time.time() - started,
        }

    apply_overrides(scene, args)

    ext = scene.render.file_extension or ".png"
    if args.animation:
        out_path = os.path.join(os.path.abspath(args.output), name + "_")
        scene.render.filepath = out_path
        bpy.ops.render.render(animation=True)
        detail = f"frames {scene.frame_start}-{scene.frame_end}"
    else:
        out_path = os.path.join(os.path.abspath(args.output), name + ext)
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
        detail = os.path.basename(out_path)

    return {
        "file": name,
        "status": "OK",
        "detail": detail,
        "seconds": time.time() - started,
    }


def print_summary(results):
    """Print an aligned summary table of the batch results."""
    if not results:
        print("[batch] no .blend files found.")
        return

    name_w = max(len(r["file"]) for r in results)
    name_w = max(name_w, len("FILE"))
    header = f"{'FILE'.ljust(name_w)}  {'STATUS':<8}  {'TIME':>7}  DETAIL"
    line = "-" * len(header)
    print("\n" + line)
    print(header)
    print(line)
    ok = 0
    for r in results:
        if r["status"] == "OK":
            ok += 1
        print(f"{r['file'].ljust(name_w)}  {r['status']:<8}  {r['seconds']:>6.1f}s  {r['detail']}")
    print(line)
    print(f"{ok}/{len(results)} rendered OK, {len(results) - ok} failed or skipped")
    print(line)


def main():
    args = parse_args()
    input_dir = os.path.abspath(args.input)
    if not os.path.isdir(input_dir):
        raise SystemExit(f"Input folder not found: {input_dir}")

    ensure_dir(os.path.abspath(args.output))
    blend_files = find_blend_files(input_dir, args.recursive)
    print(f"[batch] found {len(blend_files)} .blend file(s) in {input_dir}")

    results = []
    for blend_path in blend_files:
        print(f"[batch] rendering {os.path.basename(blend_path)} ...")
        try:
            results.append(render_one(blend_path, args))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
            results.append({
                "file": os.path.splitext(os.path.basename(blend_path))[0],
                "status": "ERROR",
                "detail": str(exc).splitlines()[0][:60],
                "seconds": 0.0,
            })
            print(f"[batch] ERROR on {os.path.basename(blend_path)}: {exc}")

    print_summary(results)


if __name__ == "__main__":
    main()
