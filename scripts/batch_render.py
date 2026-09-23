"""Batch-render every .blend in a folder, with overrides, QA and a CI-ready exit code.

Opens each .blend, optionally overrides engine / resolution / samples, forces
a still-image output format (safe on Blender 5 files saved with video output),
renders a still (or the frame range), checks the result with the QA module and
writes ``summary.json`` + ``summary.csv`` next to the renders. Each file is
isolated in its own try/except, so one corrupt .blend never stops the batch.

Exit code: 0 when every file rendered, 1 when any file errored (or failed QA
with ``--qa-strict``). Files without a camera are SKIPPED and only count as a
failure with ``--strict``.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/batch_render.py -- --input scenes --output out/batch

Arguments (after the "--" separator):
    --input DIR        Folder to scan for .blend files (required).
    --output DIR       Where to write renders and the summary (default out/batch).
    --engine NAME      Override engine: eevee or cycles (default: keep the file's).
    --device NAME      Cycles device: gpu (default, falls back to CPU) or cpu.
    --resolution WxH   Override resolution (default: keep the file's).
    --samples N        Override sample count (default: keep the file's).
    --format FMT       Still image format id: PNG, JPEG, OPEN_EXR, TIFF, WEBP... (default PNG).
    --animation        Render the full frame range instead of a single still.
    --recursive        Recurse into subfolders.
    --strict           Count SKIPPED files (no camera) as failures.
    --no-qa            Skip the per-file QA report.
    --qa-strict        Count files whose QA report fails as failures (status QA_FAILED).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    ensure_dir,
    get_argv_after_dashes,
    positive_int,
    resolution_type,
    set_engine,
    set_image_output,
    set_samples,
)
from lib.core import (  # noqa: E402
    IMAGE_FORMAT_EXTENSIONS,
    batch_exit_code,
    format_batch_table,
    image_extension,
    write_batch_summary,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="batch_render.py",
        description="Render every .blend in a folder with optional overrides.",
    )
    parser.add_argument("--input", required=True, help="Folder of .blend files.")
    parser.add_argument("--output", default=os.path.join("out", "batch"), help="Output folder.")
    parser.add_argument("--engine", choices=("eevee", "cycles"), default=None, help="Override engine.")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu", help="Cycles device.")
    parser.add_argument("--resolution", type=resolution_type, default=None, help="Override resolution WxH.")
    parser.add_argument("--samples", type=positive_int, default=None, help="Override sample count.")
    parser.add_argument("--format", default="PNG", type=str.upper,
                        choices=sorted(IMAGE_FORMAT_EXTENSIONS), help="Output image format id.")
    parser.add_argument("--animation", action="store_true", help="Render the full frame range.")
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders.")
    parser.add_argument("--strict", action="store_true", help="SKIPPED files count as failures.")
    parser.add_argument("--no-qa", action="store_true", help="Skip the per-file QA report.")
    parser.add_argument("--qa-strict", action="store_true", help="QA failures count as failures.")
    return parser.parse_args(get_argv_after_dashes() if argv is None else argv)


def find_blend_files(input_dir, recursive):
    """Return a sorted list of .blend files (``.blend1`` backups are skipped)."""
    pattern = os.path.join(input_dir, "**", "*.blend") if recursive else os.path.join(input_dir, "*.blend")
    return sorted(glob.glob(pattern, recursive=recursive))


def apply_overrides(scene, args):
    """Apply CLI overrides onto the currently open scene. Returns the engine label."""
    engine = scene.render.engine
    if args.engine:
        engine = set_engine(scene, args.engine, device=args.device)
    if args.samples is not None:
        set_samples(scene, args.samples)
    if args.resolution:
        scene.render.resolution_x, scene.render.resolution_y = args.resolution
        scene.render.resolution_percentage = 100
    # media_type-safe: a file saved with FFmpeg video output would otherwise
    # reject "PNG" on Blender 5 ("enum 'PNG' not found in ('FFMPEG')").
    color_mode = scene.render.image_settings.color_mode
    if args.format == "JPEG" and color_mode == "RGBA":
        color_mode = "RGB"
    set_image_output(scene.render.image_settings, args.format, color_mode=color_mode)
    return engine


def render_one(blend_path, args):
    """Open and render a single .blend, returning a result dict."""
    name = os.path.splitext(os.path.basename(blend_path))[0]
    started = time.time()
    try:
        bpy.ops.wm.open_mainfile(filepath=blend_path)
    except RuntimeError as exc:
        # Blender's message repeats the full path twice; keep the reason.
        reason = str(exc).strip().splitlines()[0].rsplit(": ", 1)[-1]
        raise RuntimeError(f"cannot open .blend: {reason}") from exc
    scene = bpy.context.scene
    result = {"file": name, "path": os.path.abspath(blend_path), "status": "OK", "output": None}

    if scene.camera is None:
        result.update(status="SKIPPED", detail="no active camera", seconds=time.time() - started)
        return result

    engine = apply_overrides(scene, args)
    out_dir = os.path.abspath(args.output)
    scene.render.use_file_extension = True
    images = []
    if args.animation:
        scene.render.filepath = os.path.join(out_dir, name + "_")
        bpy.ops.render.render(animation=True)
        ext = image_extension(args.format)
        images = [os.path.join(out_dir, f"{name}_{f:04d}{ext}") for f in (scene.frame_start, scene.frame_end)]
        result["output"] = os.path.join(out_dir, f"{name}_####{ext}")
        detail = f"frames {scene.frame_start}-{scene.frame_end}"
    else:
        out_path = os.path.join(out_dir, name + image_extension(args.format))
        scene.render.use_file_extension = False
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
        images = [out_path]
        result["output"] = out_path
        detail = os.path.basename(out_path)
    result["engine"] = engine
    result["resolution"] = [scene.render.resolution_x, scene.render.resolution_y]

    if not args.no_qa:
        from lib import qa

        report = qa.build_scene_report(scene, script=f"batch_render:{name}", images=images,
                                       frames=[scene.frame_start, scene.frame_end] if args.animation else None)
        result["qa"] = report
        failing = [c["id"] for c in report["checks"] if c["status"] == "fail"]
        if failing:
            detail += f"  (QA fail: {', '.join(failing)})"
            if args.qa_strict:
                result["status"] = "QA_FAILED"
    result["detail"] = detail
    result["seconds"] = time.time() - started
    return result


def main(argv=None):
    args = parse_args(argv)
    input_dir = os.path.abspath(args.input)
    if not os.path.isdir(input_dir):
        raise SystemExit(f"Input folder not found: {input_dir}")

    out_dir = ensure_dir(os.path.abspath(args.output))
    blend_files = find_blend_files(input_dir, args.recursive)
    print(f"[batch] found {len(blend_files)} .blend file(s) in {input_dir}")

    results = []
    for blend_path in blend_files:
        print(f"[batch] rendering {os.path.basename(blend_path)} ...")
        started = time.time()
        try:
            results.append(render_one(blend_path, args))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
            first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            results.append({
                "file": os.path.splitext(os.path.basename(blend_path))[0],
                "path": os.path.abspath(blend_path),
                "status": "ERROR",
                "detail": first_line[:120],
                "error": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                "seconds": time.time() - started,
                "output": None,
            })
            print(f"[batch] ERROR on {os.path.basename(blend_path)}: {first_line}")

    print()
    print(format_batch_table(results))
    code = batch_exit_code(results, strict=args.strict)
    json_path, csv_path = write_batch_summary(results, out_dir, meta={
        "input": input_dir, "blender_version": bpy.app.version_string,
        "strict": args.strict, "qa_strict": args.qa_strict, "exit_code": code,
    })
    print(f"[batch] summary -> {json_path}")
    print(f"[batch] summary -> {csv_path}")
    if code:
        print(f"[batch] exiting with code {code} (errors{' or skipped files' if args.strict else ''})")
    sys.stdout.flush()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
