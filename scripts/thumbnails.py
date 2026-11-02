"""Square PNG thumbnails for a folder of models and .blend files, with a contact sheet and index.json.

Every file is opened in isolation (a fresh empty scene for a model, the file
itself for a .blend), normalised into a 2-unit box like the turntable, lit by
the turntable's key / fill / rim rig turned to the camera's azimuth, and framed
by fitting every evaluated vertex into a square frame with the same margin, so
thumbnails of very different assets line up in an asset browser. A file that
cannot be imported or rendered is recorded in ``index.json`` with its error and
skipped; the batch goes on.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/thumbnails.py -- --input assets --output out/thumbnails --size 256

Arguments (after the "--" separator):
    --input PATH [PATH ...]  Folders to scan and/or files: .blend and models (.glb/.gltf/.obj/.fbx/
                             .stl/.ply/.usd/.usda/.usdc/.usdz, whatever this build can import).
    --output DIR             Where thumbnails, contact_sheet.png and index.json go (default out/thumbnails).
    --size N                 Thumbnail edge in pixels (default 256).
    --background BG          transparent (default) or a colour: '#20242a', white, black.
    --engine NAME            eevee (default), workbench (fastest, flat studio light) or cycles.
    --samples N              Render samples for EEVEE / Cycles (default 16).
    --device NAME            Cycles device: gpu (default, falls back to CPU) or cpu.
    --azimuth DEG            Camera azimuth (default 35: a 3/4 view from the front right).
    --elevation DEG          Camera elevation (default 20).
    --margin M               Framing margin, 1.0 = touching the frame (default 1.08).
    --recursive              Scan subfolders too.
    --columns N              Contact sheet columns (default: a square-ish grid).
    --sheet-cell N           Contact sheet cell size (default: min(size, 256)).
    --no-sheet               Do not write the contact sheet.
    --strict                 Exit with code 1 when any file fails (import, render or image QA)
                             or when no file was found.

What ``index.json`` holds per file: the thumbnail path, ``status`` (``ok``,
``error`` or ``qa_failed`` when the rendered thumbnail is empty, black or
flat), the error message, bounds in the file's own units (metres for glTF),
triangle / vertex / object counts and the seconds it took.

Exit codes: 0 (failures are recorded, not fatal); 1 with --strict when any
file failed; 2 invalid input (missing folder, bad flags).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import qa  # noqa: E402
from lib.common import (  # noqa: E402
    bounds_dict,
    direction_from_angles,
    ensure_dir,
    evaluated_world_vertices,
    frame_points,
    get_argv_after_dashes,
    image_has_pixels,
    mesh_stats,
    mesh_world_bounds,
    new_camera,
    new_world,
    np,
    object_images,
    positive_int,
    render_still,
    renderable_geometry,
    set_engine,
    set_samples,
    set_view_transform,
    sync_viewport_colors,
)
from lib.core import (  # noqa: E402
    GLYPH_H,
    MODEL_EXTENSIONS,
    SCENE_EXTENSIONS,
    condense_report,
    contact_sheet_layout,
    elide_middle,
    evaluate_image,
    fail_input,
    find_inputs,
    parse_background,
    summarize_checks,
    text_bitmap,
    thumbnail_names,
    thumbnails_exit_code,
    write_summary,
)
from lib.studio import ModelImportError, import_model, normalize_objects, three_point_rig  # noqa: E402

INDEX_VERSION = 1
SHEET_BG = (0.13, 0.14, 0.16)
ERROR_BG = (0.36, 0.09, 0.09)
LABEL_COLOR = (0.86, 0.87, 0.89)


def background_type(text):
    try:
        return parse_background(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="thumbnails.py",
                                     description="Square PNG thumbnails + contact sheet + index.json.")
    parser.add_argument("--input", nargs="+", required=True, help="Folders and/or model / .blend files.")
    parser.add_argument("--output", default=os.path.join("out", "thumbnails"), help="Output folder.")
    parser.add_argument("--size", type=positive_int, default=256, help="Thumbnail edge in pixels (default 256).")
    parser.add_argument("--background", type=background_type, default=None,
                        help="transparent (default) or a colour such as '#20242a'.")
    parser.add_argument("--engine", choices=("eevee", "workbench", "cycles"), default="eevee",
                        help="Render engine (default eevee).")
    parser.add_argument("--samples", type=positive_int, default=16, help="Samples for EEVEE / Cycles (default 16).")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu", help="Cycles device.")
    parser.add_argument("--azimuth", type=float, default=35.0, help="Camera azimuth in degrees (default 35).")
    parser.add_argument("--elevation", type=float, default=20.0, help="Camera elevation in degrees (default 20).")
    parser.add_argument("--margin", type=float, default=1.08, help="Framing margin (default 1.08).")
    parser.add_argument("--recursive", action="store_true", help="Scan subfolders too.")
    parser.add_argument("--columns", type=positive_int, default=None, help="Contact sheet columns.")
    parser.add_argument("--sheet-cell", type=positive_int, default=None, help="Contact sheet cell size.")
    parser.add_argument("--no-sheet", dest="sheet", action="store_false", help="Skip the contact sheet.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when any file fails.")
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if args.size < 16:
        parser.error("--size must be at least 16 pixels")
    if args.margin < 1.0:
        parser.error("--margin must be >= 1.0")
    return args


def gather_inputs(paths, recursive):
    """Files to process: folders are scanned, files are taken as given (if their type is known)."""
    extensions = MODEL_EXTENSIONS + SCENE_EXTENSIONS
    files = []
    for raw in paths:
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            files += find_inputs(path, extensions, recursive=recursive)
        elif os.path.isfile(path):
            if os.path.splitext(path)[1].lower() not in extensions:
                fail_input(f"[thumbnails] not a model or .blend file: {path}")
            files.append(path)
        else:
            fail_input(f"[thumbnails] input not found: {path}")
    return list(dict.fromkeys(files))


# ---------------------------------------------------------------------------
# One thumbnail
# ---------------------------------------------------------------------------
def load_subjects(path):
    """Load ``path`` and return the geometry to show; everything else stops rendering."""
    if os.path.splitext(path)[1].lower() == ".blend":
        try:
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
        except RuntimeError as exc:
            reason = (str(exc).strip().splitlines() or ["unknown error"])[0].rsplit(": ", 1)[-1]
            raise ModelImportError(f"cannot open .blend: {reason}") from exc
        subjects = renderable_geometry()
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        imported = import_model(path)
        keep = set(renderable_geometry())
        subjects = [o for o in imported if o in keep]
    if not subjects:
        raise ModelImportError("no renderable geometry (everything is hidden or tagged as backdrop)")
    keep = set(subjects)
    for obj in bpy.context.scene.objects:  # the file's own lights, floors and backdrops do not render
        if obj.type == "LIGHT" or (obj not in keep and obj.type not in {"CAMERA", "EMPTY", "ARMATURE"}):
            obj.hide_render = True
    return subjects


def configure_render(scene, args):
    engine = set_engine(scene, args.engine, device=args.device)
    set_samples(scene, args.samples)
    set_view_transform(scene)
    scene.render.resolution_x = scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    scene.render.film_transparent = True
    if engine == "BLENDER_WORKBENCH":
        sync_viewport_colors()
    return engine


def stage(subjects, args):
    """Normalise the model, add camera, rig and world. Returns the camera."""
    normalize_objects(subjects, target_size=2.0, name="ThumbnailPivot")
    _lo, _hi, center, radius = mesh_world_bounds(subjects)
    new_world("ThumbnailWorld", color=(0.05, 0.05, 0.055), strength=1.0)
    three_point_rig(center, max(radius, 1.0), azimuth_deg=args.azimuth)
    cam = new_camera(name="ThumbnailCamera", lens=50.0)
    frame_points(cam, evaluated_world_vertices(subjects),
                 direction=direction_from_angles(args.azimuth, args.elevation), margin=args.margin)
    return cam


def read_rgba(path, size=None):
    """Pixels of an image file as a top-down (h, w, 4) float array, optionally resized."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        if size and tuple(img.size) != (size, size):
            img.scale(size, size)
        w, h = img.size
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
    return px.reshape(h, w, 4)[::-1]


def write_rgba(path, pixels):
    """Save a top-down (h, w, 4) float array as an 8-bit PNG."""
    h, w = pixels.shape[:2]
    img = bpy.data.images.new("bpt_write", w, h, alpha=True)
    try:
        img.pixels.foreach_set(np.ascontiguousarray(pixels[::-1], dtype=np.float32).ravel())
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return path


def over(pixels, rgb):
    """Composite straight-alpha RGBA ``pixels`` over a solid colour (display space)."""
    alpha = pixels[..., 3:4]
    out = np.empty_like(pixels)
    out[..., :3] = pixels[..., :3] * alpha + np.asarray(rgb, dtype=np.float32) * (1.0 - alpha)
    out[..., 3] = 1.0
    return out


def render_thumbnail(path, out_png, args):
    """Load, stage and render one file. Returns a dict with counts, bounds and QA."""
    subjects = load_subjects(path)
    scene = bpy.context.scene
    stats = mesh_stats(subjects)
    bounds = bounds_dict(subjects)
    missing = [img.name for img in object_images(subjects) if not image_has_pixels(img)]
    engine = configure_render(scene, args)
    stage(subjects, args)
    render_still(scene, out_png, fmt="PNG", color_mode="RGBA")
    stats_img = qa.image_stats(out_png)
    checks = evaluate_image(stats_img)
    for check in checks:
        check["image"] = os.path.basename(out_png)
    passed, worst, counts = summarize_checks(checks)
    if args.background is not None:
        write_rgba(out_png, over(read_rgba(out_png), args.background))
    image = {k: stats_img.get(k) for k in ("alpha_coverage", "mean_luminance", "std_luminance")}
    warnings = [f"texture {name!r} has no pixels (missing file?); it renders magenta" for name in missing]
    return {"engine": engine, "objects": len(subjects), "triangles": stats["triangles"],
            "vertices": stats["vertices"], "bounds": bounds, "image": image, "missing_textures": missing,
            "warnings": warnings, "qa": {"passed": passed, "status": worst, "counts": counts, "checks": checks}}


# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------
def draw_label(sheet, text, x, y, scale, color):
    """Draw ``text`` (5x7 bitmap font) into the top-down ``sheet`` at (x, y)."""
    rows = np.asarray(text_bitmap(text), dtype=bool)
    if rows.size == 0:
        return
    mask = np.kron(rows, np.ones((scale, scale), dtype=bool))
    h, w = mask.shape
    h, w = min(h, sheet.shape[0] - y), min(w, sheet.shape[1] - x)
    if h <= 0 or w <= 0:
        return
    region = sheet[y:y + h, x:x + w]
    region[mask[:h, :w], :3] = color


def build_contact_sheet(items, out_path, cell, columns, background):
    scale = 2 if cell >= 192 else 1
    label_h = GLYPH_H * scale + 6 * scale
    pad = max(4, cell // 24)
    layout = contact_sheet_layout(len(items), cell, columns=columns, pad=pad, label_height=label_h)
    base = background if background is not None else SHEET_BG
    sheet = np.ones((layout["height"], layout["width"], 4), dtype=np.float32)
    sheet[..., :3] = base
    max_chars = max(4, (cell + scale) // (6 * scale))
    for i, (item, (x, y)) in enumerate(zip(items, layout["cells"])):
        thumb = item.get("thumbnail")
        if thumb and os.path.isfile(thumb):
            px = read_rgba(thumb, size=cell)
            sheet[y:y + cell, x:x + cell] = over(px, base)
        else:
            sheet[y:y + cell, x:x + cell, :3] = ERROR_BG
            draw_label(sheet, "ERROR", x + (cell - 29 * scale) // 2, y + cell // 2 - 4 * scale, scale, LABEL_COLOR)
        item["sheet_cell"] = [i % layout["columns"], i // layout["columns"]]  # [column, row]
        draw_label(sheet, elide_middle(item["file"], max_chars), x, y + cell + 3 * scale, scale, LABEL_COLOR)
    return write_rgba(out_path, sheet), layout


def main(argv=None):
    started = time.time()
    args = parse_args(argv)
    files = gather_inputs(args.input, args.recursive)
    out_dir = ensure_dir(os.path.abspath(args.output))
    roots = [os.path.abspath(p) for p in args.input if os.path.isdir(p)]
    names = thumbnail_names(files, root=roots[0] if len(roots) == 1 else None)
    print(f"[thumbnails] {len(files)} file(s) -> {out_dir} ({args.size}px, {args.engine})")
    if not files:
        print("[thumbnails] nothing to do: no model or .blend files found")

    items = []
    for path in files:
        out_png = os.path.join(out_dir, names[path])
        item = {"file": os.path.basename(path), "path": path, "thumbnail": None, "status": "ok", "error": None}
        t0 = time.time()
        try:
            item.update(render_thumbnail(path, out_png, args))
            item["thumbnail"] = out_png
            if not item["qa"]["passed"]:
                item["status"] = "qa_failed"
                failing = ", ".join(c["id"] for c in item["qa"]["checks"] if c["status"] == "fail")
                item["error"] = f"thumbnail failed image QA: {failing}"
        except ModelImportError as exc:
            item.update(status="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            item.update(status="error", error=f"{type(exc).__name__}: {first}",
                        traceback=traceback.format_exc(limit=6))
        item["seconds"] = round(time.time() - t0, 3)
        items.append(item)
        detail = item["thumbnail"] if item["status"] == "ok" else item["error"]
        print(f"[thumbnails] {item['status'].upper():<9} {item['file']}  ({item['seconds']:.1f}s)  {detail}")

    sheet_path = None
    if args.sheet and items:
        cell = args.sheet_cell or min(args.size, 256)
        sheet_path, _ = build_contact_sheet(items, os.path.join(out_dir, "contact_sheet.png"), cell,
                                            args.columns, args.background)
        print(f"[thumbnails] contact sheet -> {sheet_path}")

    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "error", "qa_failed")}
    code = thumbnails_exit_code(items, strict=args.strict)
    background = "transparent" if args.background is None else "#" + "".join(
        f"{round(c * 255):02x}" for c in args.background)
    index = {
        "tool": "blender-python-toolkit",
        "kind": "thumbnail_index",
        "index_version": INDEX_VERSION,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "blender_version": bpy.app.version_string,
        "input": [os.path.abspath(p) for p in args.input],
        "output": out_dir,
        "size": args.size,
        "engine": args.engine,
        "background": background,
        "camera": {"azimuth": args.azimuth, "elevation": args.elevation, "margin": args.margin},
        "contact_sheet": sheet_path,
        "counts": counts,
        "strict": args.strict,
        "exit_code": code,
        "seconds": round(time.time() - started, 3),
        "items": items,
    }
    index_path = os.path.join(out_dir, "index.json")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    print(f"[thumbnails] {counts['ok']}/{len(items)} ok, {counts['error']} error, {counts['qa_failed']} qa_failed")
    print(f"[thumbnails] index -> {index_path}")
    write_summary({
        "script": "thumbnails",
        "status": "failed" if code else "ok",
        "counts": counts,
        "outputs": [i["thumbnail"] for i in items if i["thumbnail"]] + [p for p in (sheet_path, index_path) if p],
        "index": index_path,
        "contact_sheet": sheet_path,
        "seconds": index["seconds"],
        "items": [{"file": i["file"], "path": i["path"], "status": i["status"], "thumbnail": i["thumbnail"],
                   "error": i["error"], "warnings": i.get("warnings", []), "triangles": i.get("triangles"),
                   "seconds": i["seconds"],
                   "qa": condense_report(i.get("qa"))}
                  for i in items],
    })
    sys.stdout.flush()
    if code:
        print(f"[thumbnails] --strict and {len(items) - counts['ok']} file(s) failed; exiting with code {code}")
        raise SystemExit(code)


if __name__ == "__main__":
    main()
