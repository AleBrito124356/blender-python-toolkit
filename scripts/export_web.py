"""Export a .blend or a model file as web / AR deliverables, with a report and a round-trip check.

Writes a ``.glb`` (binary glTF 2.0, +Y up, modifiers applied) and optionally a
``.usdz`` (USD with UsdPreviewSurface materials, the format of Apple's AR
Quick Look), after optional decimation and texture downscaling. Every run
writes a JSON report (triangles and vertices before and after, file sizes,
world bounds in metres, materials, textures and their sizes, warnings) and
then **verifies** each file: it is re-imported into a fresh, empty scene and
its triangle count and bounds are compared with the report. The ``.glb`` is
also read byte by byte (no Blender involved) to count its triangles, check
that Draco compression is really in the file and that no embedded texture is
larger than ``--texture-max``. Any disagreement fails the run with exit code 3.

Why the checks exist: Blender's glTF exporter silently writes an uncompressed
file when its Draco library is missing (the ``bpy`` 5.2 wheel on PyPI ships
without it), and it re-reads the original image file when a texture was only
resized in memory, so neither "--draco" nor "--texture-max" can be trusted
without looking at the output.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/export_web.py -- --input chair.blend --max-triangles 50000 --texture-max 1024 --usdz

Arguments (after the "--" separator):
    --input PATH          A .blend, or a model: .glb/.gltf/.obj/.fbx/.stl/.ply/.usd/.usda/.usdc/.usdz
                          (whatever this Blender build can import).
    --output PATH         The .glb to write (default out/export/<input name>.glb).
    --usdz                Also write <output name>.usdz next to the .glb.
    --draco [LEVEL]       Draco mesh compression, level 0-10 (6 when no level is given). Refused
                          with exit code 2 when this build has no Draco encoder.
    --decimate RATIO      Collapse-decimate every mesh to RATIO of its triangles (0 < RATIO < 1).
    --max-triangles N     Decimate (one ratio for every mesh) until the export has at most N triangles.
    --texture-max SIZE    Downscale textures whose larger side exceeds SIZE pixels (aspect kept).
    --image-format FMT    Textures inside the .glb: auto (keep PNG / JPEG), jpeg or webp.
    --no-apply-modifiers  Export base meshes without modifiers (.glb only; USD always applies them).
    --animations          Also export animations (default: static geometry).
    --report PATH         The JSON report (default <output name>.export.json).
    --tolerance T         Round-trip tolerance: fraction of the bounding-box diagonal and of the
                          triangle count (default 0.001).
    --no-verify           Skip the round-trip re-import (the byte-level .glb checks still run).

What is exported: every geometry object that renders in the scene's view
layer, except objects tagged ``qa_role`` = ``backdrop`` / ``ignore`` (floors
and cycloramas built by this toolkit); curves, text and surfaces are converted
to meshes first so the .glb and the .usdz contain the same triangles. Cameras
and lights are not exported.

Exit codes: 0 every check passed; 2 invalid input or a request this build
cannot do (--draco without an encoder, --usdz without USD); 3 a file was
written but a verification check failed; 1 an unexpected error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    GEOMETRY_TYPES,
    bounds_dict,
    ensure_dir,
    get_argv_after_dashes,
    mesh_stats,
    material_images,
    mesh_world_bounds,
    np,
    positive_int,
    renderable_geometry,
)
from lib.core import (  # noqa: E402
    MODEL_EXTENSIONS,
    ROUNDTRIP_TOLERANCE,
    EXIT_CHECK_FAILED,
    decimate_ratio,
    export_exit_code,
    fail_input,
    fit_size,
    format_export_text,
    gltf_stats,
    make_check,
    read_glb,
    refine_ratio,
    roundtrip_checks,
    swap_extension,
    write_summary,
)
from lib.studio import ModelImportError, import_model  # noqa: E402

REPORT_VERSION = 1


def ratio_type(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number in (0, 1), got {text!r}") from None
    if not 0.0 < value < 1.0:
        raise argparse.ArgumentTypeError(f"expected a number in (0, 1), got {text!r}")
    return value


def draco_level(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Draco level must be an integer 0-10, got {text!r}") from None
    if not 0 <= value <= 10:
        raise argparse.ArgumentTypeError(f"Draco level must be an integer 0-10, got {text!r}")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="export_web.py",
                                     description="Export .glb (+ .usdz) for the web and AR, report and verify.")
    parser.add_argument("--input", required=True, help=".blend or model file.")
    parser.add_argument("--output", default=None, help="The .glb to write (default out/export/<name>.glb).")
    parser.add_argument("--usdz", action="store_true", help="Also write a .usdz next to the .glb.")
    parser.add_argument("--draco", nargs="?", const=6, default=None, type=draco_level, metavar="LEVEL",
                        help="Draco mesh compression level 0-10 (default 6).")
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--decimate", type=ratio_type, default=None, metavar="RATIO",
                        help="Collapse-decimate every mesh to this share of its triangles.")
    budget.add_argument("--max-triangles", type=positive_int, default=None, metavar="N",
                        help="Decimate until the export has at most N triangles.")
    parser.add_argument("--texture-max", type=positive_int, default=None, metavar="SIZE",
                        help="Downscale textures larger than SIZE pixels on their longer side.")
    parser.add_argument("--image-format", choices=("auto", "jpeg", "webp"), default="auto",
                        help="Texture format inside the .glb (default auto: keep PNG / JPEG).")
    parser.add_argument("--no-apply-modifiers", dest="apply_modifiers", action="store_false",
                        help="Export base meshes without modifiers (.glb only).")
    parser.add_argument("--animations", action="store_true", help="Also export animations.")
    parser.add_argument("--report", default=None, help="JSON report path (default <output>.export.json).")
    parser.add_argument("--tolerance", type=float, default=ROUNDTRIP_TOLERANCE,
                        help=f"Round-trip tolerance (default {ROUNDTRIP_TOLERANCE}).")
    parser.add_argument("--no-verify", dest="verify", action="store_false", help="Skip the round-trip re-import.")
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if not args.apply_modifiers and (args.decimate or args.max_triangles):
        parser.error("--decimate / --max-triangles work through a modifier, so they need modifiers applied "
                     "(drop --no-apply-modifiers)")
    if args.tolerance < 0:
        parser.error("--tolerance must be >= 0")
    return args


# ---------------------------------------------------------------------------
# Capabilities of this Blender build
# ---------------------------------------------------------------------------
def export_capabilities():
    """``{"gltf", "draco", "usd"}``: what this Blender can write.

    Draco is looked up the way the glTF add-on does it (its encoder bridge
    library); the exporter itself only logs an error and writes an
    uncompressed file when the library is missing.
    """
    gltf = "io_scene_gltf2" in bpy.context.preferences.addons
    draco = False
    if gltf:
        try:
            from io_scene_gltf2.io.com.library import dll_path

            path = dll_path("bf_intern_draco_bridge", "Draco")
            draco = bool(path is not None and path.is_file())
        except Exception:  # noqa: BLE001 - older add-on layouts
            try:
                from io_scene_gltf2.io.com.gltf2_io_draco_compression_extension import dll_path

                draco = bool(dll_path().is_file())
            except Exception:  # noqa: BLE001
                draco = False
    usd = bool(getattr(bpy.app.build_options, "usd", False)) and hasattr(bpy.ops.wm, "usd_export")
    return {"gltf": gltf, "draco": draco, "usd": usd}


# ---------------------------------------------------------------------------
# Loading and preparing the scene
# ---------------------------------------------------------------------------
def load_input(path):
    """Open a .blend or import a model into an empty scene; return the export objects."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        try:
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
        except RuntimeError as exc:
            reason = (str(exc).strip().splitlines() or ["unknown error"])[0].rsplit(": ", 1)[-1]
            fail_input(f"[export] cannot open .blend: {reason}")
        return renderable_geometry(), "blend"
    if ext not in MODEL_EXTENSIONS:
        fail_input(f"[export] unsupported input {ext!r}: use a .blend or {', '.join(MODEL_EXTENSIONS)}")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        imported = import_model(path)
    except ModelImportError as exc:
        fail_input(f"[export] {exc}")
    keep = set(renderable_geometry())
    return [o for o in imported if o in keep], "model"


def bake_to_meshes(objects, warnings):
    """Replace curve / text / surface / metaball objects by mesh objects of their evaluated shape.

    The glTF exporter converts them itself, but the USD exporter writes
    curves as USD curves, so both files would not hold the same triangles.
    Returns the new object list.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    out, baked = [], []
    for obj in objects:
        if obj.type == "MESH":
            out.append(obj)
            continue
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                               depsgraph=depsgraph)
        if len(mesh.vertices) == 0:
            warnings.append(f"{obj.type.lower()} object {obj.name!r} has no geometry and was skipped")
            bpy.data.meshes.remove(mesh)
            continue
        new = bpy.data.objects.new(obj.name + "_mesh", mesh)
        for collection in obj.users_collection:
            collection.objects.link(new)
        new.parent = obj.parent
        new.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
        new.matrix_basis = obj.matrix_basis.copy()
        for child in list(obj.children):
            child.parent = new
        name = obj.name
        bpy.data.objects.remove(obj, do_unlink=True)
        new.name = name
        out.append(new)
        baked.append(name)
    if baked:
        bpy.context.view_layer.update()
        warnings.append(f"converted {len(baked)} curve/text/surface object(s) to meshes: {', '.join(baked[:8])}"
                        + (" ..." if len(baked) > 8 else ""))
    return out


def base_mesh_stats(objects):
    """Triangles / vertices / bounds of the meshes *without* modifiers (for --no-apply-modifiers).

    Bounds use the vertices of faces only, as the exporter writes them.
    """
    tris = verts = 0
    chunks = []
    for obj in objects:
        mesh = obj.data
        mesh.calc_loop_triangles()
        tris += len(mesh.loop_triangles)
        verts += len(mesh.vertices)
        if len(mesh.loop_triangles):
            co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
            mesh.vertices.foreach_get("co", co)
            tri = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
            mesh.loop_triangles.foreach_get("vertices", tri)
            mw = np.array(obj.matrix_world, dtype=np.float64)
            chunks.append(co.reshape(-1, 3)[np.unique(tri)] @ mw[:3, :3].T + mw[:3, 3])
    pts = np.concatenate(chunks) if chunks else np.zeros((1, 3))
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    r = lambda v: [round(float(x), 6) for x in v]  # noqa: E731
    return ({"objects": len(objects), "triangles": tris, "vertices": verts},
            {"min": r(lo), "max": r(hi), "size": r(hi - lo), "center": r((lo + hi) / 2)})


def decimate(objects, ratio, max_triangles=None):
    """Add Weld + collapse Decimate modifiers at the end of every mesh's stack.

    Imported meshes are usually split at every UV seam and hard edge (the
    glTF importer leaves a flat-shaded mesh as one island per triangle), and
    collapsing an edge cannot cross an island, so Decimate would delete whole
    triangles and punch holes. The Weld modifier first merges coincident
    vertices (UVs and normals live on face corners, so they survive), then
    Decimate collapses a connected surface. With a triangle budget the ratio
    is refined (up to 5 times) until the evaluated total fits.
    Returns ``(ratio, stats)``.
    """
    lo, hi, _, _ = mesh_world_bounds(objects)
    weld_distance = max(float((hi - lo).length) * 1e-6, 1e-8)
    mods = []
    for obj in objects:
        weld = obj.modifiers.new("bpt_weld", "WELD")
        weld.merge_threshold = weld_distance / max(max(obj.matrix_world.to_scale()), 1e-9)
        mod = obj.modifiers.new("bpt_decimate", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
        mods.append(mod)
    stats = mesh_stats(objects)
    if max_triangles:
        for _ in range(5):
            if stats["triangles"] <= max_triangles:
                break
            ratio = refine_ratio(ratio, stats["triangles"], max_triangles)
            for mod in mods:
                mod.ratio = ratio
            stats = mesh_stats(objects)
    return ratio, stats


def collect_materials(objects):
    """``(materials, images)``: per-material users/textures, and every image they use."""
    materials, images = {}, []
    for obj in objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            entry = materials.setdefault(mat.name, {"name": mat.name, "objects": 0, "textures": []})
            entry["objects"] += 1
            for img in material_images(mat):
                if img.name not in entry["textures"]:
                    entry["textures"].append(img.name)
                if img not in images:
                    images.append(img)
    return list(materials.values()), images


def image_source(img):
    if img.packed_file is not None:
        return "packed"
    return "file" if img.source == "FILE" else img.source.lower()


def downscale_images(images, max_size, workdir, warnings):
    """Replace every image larger than ``max_size`` by a downscaled copy saved to ``workdir``.

    A fresh image datablock written to disk is used instead of resizing in
    place, because the glTF exporter re-reads the original file (or packed
    bytes) of an unmodified image and a resize alone does not mark it as
    modified. Returns texture records for the report.
    """
    records = []
    for img in images:
        record = {"name": img.name, "source": image_source(img), "filepath": img.filepath or None,
                  "colorspace": img.colorspace_settings.name}
        try:
            width, height = (int(v) for v in img.size)
        except (TypeError, ValueError):
            width = height = 0
        record["source_size"] = [width, height]
        record["size"] = [width, height]
        record["downscaled"] = False
        if width == 0 or height == 0:
            warnings.append(f"texture {img.name!r} has no pixels (missing file?); it is exported as is")
            records.append(record)
            continue
        if max_size:
            new_w, new_h = fit_size(width, height, max_size)
            if (new_w, new_h) != (width, height):
                if img.is_float:
                    warnings.append(f"texture {img.name!r} is a float image; float textures are not downscaled")
                else:
                    replace_with_scaled(img, new_w, new_h, workdir)
                    record["size"] = [new_w, new_h]
                    record["downscaled"] = True
        records.append(record)
    return records


def replace_with_scaled(img, width, height, workdir):
    """Swap ``img`` for a ``width`` x ``height`` copy on disk, keeping its name and colour space."""
    tmp = img.copy()
    tmp.scale(width, height)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    tmp.pixels.foreach_get(pixels)
    bpy.data.images.remove(tmp)
    name = img.name
    jpeg = img.file_format == "JPEG"
    new = bpy.data.images.new(name + "_web", width, height, alpha=not jpeg)
    new.colorspace_settings.name = img.colorspace_settings.name
    new.alpha_mode = img.alpha_mode
    new.pixels.foreach_set(pixels)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "texture"
    new.filepath_raw = os.path.join(workdir, f"{safe}_{width}x{height}" + (".jpg" if jpeg else ".png"))
    new.file_format = "JPEG" if jpeg else "PNG"
    if jpeg:
        new.save(quality=90)
    else:
        new.save()
    img.user_remap(new)
    img.name = name + "_source"
    new.name = name
    return new


def select_only(objects):
    for obj in bpy.context.view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


# ---------------------------------------------------------------------------
# Export and verification
# ---------------------------------------------------------------------------
def export_glb(path, args):
    kwargs = dict(filepath=path, export_format="GLB", use_selection=True, export_apply=args.apply_modifiers,
                  export_yup=True, export_cameras=False, export_lights=False, export_animations=args.animations,
                  export_image_format=args.image_format.upper())
    if args.draco is not None:
        kwargs.update(export_draco_mesh_compression_enable=True, export_draco_mesh_compression_level=args.draco)
    bpy.ops.export_scene.gltf(**kwargs)


def export_usdz(path, args):
    kwargs = dict(filepath=path, selected_objects_only=True, export_animation=args.animations,
                  export_materials=True, generate_preview_surface=True, export_lights=False, export_cameras=False,
                  evaluation_mode="VIEWPORT", export_subdivision="TESSELLATE", triangulate_meshes=False,
                  convert_orientation=True, export_global_up_selection="Y",
                  export_global_forward_selection="NEGATIVE_Z")
    if args.texture_max:
        kwargs.update(usdz_downscale_size="CUSTOM", usdz_downscale_custom_size=args.texture_max)
    bpy.ops.wm.usd_export(**kwargs)


def measure_reimport(path):
    """Import ``path`` into an empty scene and measure it like the source was measured."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    import_model(path)
    objects = [o for o in bpy.context.scene.objects if o.type in GEOMETRY_TYPES]
    stats = mesh_stats(objects)
    return {"objects": stats["objects"], "triangles": stats["triangles"], "vertices": stats["vertices"],
            "bounds": bounds_dict(objects, faces_only=True)}


def glb_file_checks(stats, expected_triangles, args, tolerance):
    """Checks on the bytes of the .glb, independent of Blender's importer."""
    checks = []
    allowed = int(tolerance * expected_triangles)
    ok = abs(stats["triangles"] - expected_triangles) <= allowed
    checks.append(make_check("glb_file_triangles", "pass" if ok else "fail",
                         f"the file declares {stats['triangles']} triangles, {expected_triangles} exported",
                         expected=expected_triangles, measured=stats["triangles"]))
    if args.draco is not None:
        checks.append(make_check("glb_draco", "pass" if stats["draco"] else "fail",
                             "KHR_draco_mesh_compression is in the file" if stats["draco"] else
                             "Draco was requested but the file is not compressed"))
    if args.texture_max:
        sized = [im for im in stats["images"] if im.get("width")]
        too_big = [f"{im.get('name') or im['index']} {im['width']}x{im['height']}" for im in sized
                   if max(im["width"], im["height"]) > args.texture_max]
        unknown = [str(im.get("name") or im["index"]) for im in stats["images"] if not im.get("width")]
        if too_big:
            checks.append(make_check("glb_textures", "fail",
                                 f"{len(too_big)} texture(s) exceed {args.texture_max}px: {', '.join(too_big)}",
                                 objects=too_big))
        else:
            checks.append(make_check("glb_textures", "pass",
                                 f"{len(sized)} embedded texture(s), none larger than {args.texture_max}px"
                                 + (f" ({len(unknown)} of unknown size)" if unknown else "")))
    return checks


def main(argv=None):
    started = time.time()
    args = parse_args(argv)
    in_path = os.path.abspath(args.input)
    if not os.path.isfile(in_path):
        fail_input(f"[export] input not found: {in_path}")
    stem = os.path.splitext(os.path.basename(in_path))[0]
    out_path = os.path.abspath(args.output or os.path.join("out", "export", stem + ".glb"))
    if not out_path.lower().endswith(".glb"):
        fail_input(f"[export] --output must be a .glb path, got {out_path}")
    usdz_path = swap_extension(out_path, ".usdz") if args.usdz else None
    report_path = os.path.abspath(args.report) if args.report else swap_extension(out_path, ".export.json")

    caps = export_capabilities()
    if not caps["gltf"]:
        fail_input("[export] this Blender build has no glTF exporter (io_scene_gltf2 add-on)")
    if args.draco is not None and not caps["draco"]:
        fail_input("[export] --draco: this Blender build has no Draco encoder (the glTF add-on's "
                   "bf_intern_draco_bridge library is missing; the PyPI bpy wheel ships without it). "
                   "Drop --draco or use a Blender build that includes it.")
    if args.usdz and not caps["usd"]:
        fail_input("[export] --usdz: this Blender build has no USD exporter")

    timings = {}
    t0 = time.time()
    objects, input_kind = load_input(in_path)
    if not objects:
        fail_input(f"[export] nothing to export: no renderable geometry in {in_path}")
    timings["load"] = round(time.time() - t0, 3)
    print(f"[export] {len(objects)} object(s) to export from {in_path}")

    t0 = time.time()
    warnings = []
    scene = bpy.context.scene
    unit_scale = float(scene.unit_settings.scale_length)
    if abs(unit_scale - 1.0) > 1e-9:
        warnings.append(f"the scene's unit scale is {unit_scale:g}: glTF and USD store Blender units as metres, "
                        f"so the files are {unit_scale:g}x off from the sizes Blender displays")
    source = mesh_stats(objects)
    source["objects"] = len(objects)
    source_bounds = bounds_dict(objects, faces_only=True)
    objects = bake_to_meshes(objects, warnings)
    if not objects:
        fail_input(f"[export] nothing to export: no mesh geometry in {in_path}")

    try:
        ratio = decimate_ratio(source["triangles"], ratio=args.decimate, max_triangles=args.max_triangles)
    except ValueError as exc:
        fail_input(f"[export] {exc}")
    if ratio is not None:
        ratio, evaluated = decimate(objects, ratio, max_triangles=args.max_triangles)
        print(f"[export] decimated with ratio {ratio:.4g}: {source['triangles']} -> {evaluated['triangles']} "
              "triangles")
    elif args.max_triangles:
        print(f"[export] {source['triangles']} triangles already within --max-triangles {args.max_triangles}")
    evaluated = mesh_stats(objects)
    evaluated_bounds = bounds_dict(objects, faces_only=True)
    if args.apply_modifiers:
        glb_expected, glb_bounds = evaluated, evaluated_bounds
    else:
        glb_expected, glb_bounds = base_mesh_stats(objects)
        if glb_expected["triangles"] != evaluated["triangles"] and args.usdz:
            warnings.append("--no-apply-modifiers affects the .glb only: the .usdz always has modifiers applied")

    materials, images = collect_materials(objects)
    workdir = tempfile.mkdtemp(prefix="bpt_export_")
    textures = downscale_images(images, args.texture_max, workdir, warnings)
    for item in textures:
        if item["downscaled"]:
            print(f"[export] texture {item['name']}: {item['source_size'][0]}x{item['source_size'][1]} -> "
                  f"{item['size'][0]}x{item['size'][1]}")
    timings["prepare"] = round(time.time() - t0, 3)

    outputs, checks = [], []
    try:
        ensure_dir(os.path.dirname(out_path))
        select_only(objects)
        t0 = time.time()
        export_glb(out_path, args)
        timings["export_glb"] = round(time.time() - t0, 3)
        with open(out_path, "rb") as fh:
            doc, binary = read_glb(fh.read())
        glb = gltf_stats(doc, binary)
        outputs.append({"format": "glb", "path": out_path, "bytes": os.path.getsize(out_path), "draco": glb["draco"],
                        "file": glb})
        print(f"[export] wrote {out_path}")
        checks += glb_file_checks(glb, glb_expected["triangles"], args, args.tolerance)
        if usdz_path:
            select_only(objects)
            t0 = time.time()
            export_usdz(usdz_path, args)
            timings["export_usdz"] = round(time.time() - t0, 3)
            outputs.append({"format": "usdz", "path": usdz_path, "bytes": os.path.getsize(usdz_path)})
            print(f"[export] wrote {usdz_path}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if args.verify:
        t0 = time.time()
        expected = {"glb": {"triangles": glb_expected["triangles"], "bounds": glb_bounds},
                    "usdz": {"triangles": evaluated["triangles"], "bounds": evaluated_bounds}}
        for item in outputs:
            try:
                measured = measure_reimport(item["path"])
            except ModelImportError as exc:
                checks.append(make_check(f"{item['format']}_reimport", "fail", f"re-import failed: {exc}"))
                continue
            item["reimport"] = measured
            checks += roundtrip_checks(expected[item["format"]], measured, args.tolerance, label=item["format"])
        timings["verify"] = round(time.time() - t0, 3)

    report = {
        "tool": "blender-python-toolkit",
        "kind": "export_report",
        "report_version": REPORT_VERSION,
        "input": in_path,
        "input_kind": input_kind,
        "blender_version": bpy.app.version_string,
        "options": {"draco": args.draco, "decimate": args.decimate, "max_triangles": args.max_triangles,
                    "texture_max": args.texture_max, "image_format": args.image_format,
                    "apply_modifiers": args.apply_modifiers, "animations": args.animations,
                    "usdz": bool(args.usdz), "tolerance": args.tolerance, "verify": args.verify},
        "capabilities": caps,
        "units": "metres (glTF convention: 1 Blender unit = 1 m)",
        "up_axis": "+Y in the files; bounds below are in Blender axes (Z up)",
        "source": {**source, "bounds": source_bounds},
        "export": {**glb_expected, "decimate_ratio": ratio},
        "bounds": glb_bounds,
        "materials": materials,
        "textures": textures,
        "outputs": outputs,
        "verification": {"passed": not any(c["status"] == "fail" for c in checks), "checks": checks},
        "warnings": warnings,
        "timings": timings,
    }
    code = export_exit_code(report)
    report["status"] = "ok" if code == 0 else "check_failed"
    report["seconds"] = round(time.time() - started, 3)
    ensure_dir(os.path.dirname(report_path))
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(format_export_text(report))
    print(f"[export] report -> {report_path}")
    write_summary({"script": "export_web", "status": report["status"], "input": in_path,
                   "outputs": [o["path"] for o in outputs] + [report_path], "report": report_path,
                   "export": report, "seconds": report["seconds"]})
    sys.stdout.flush()
    if code:
        print(f"[export] a verification check failed; exiting with code {EXIT_CHECK_FAILED}")
        raise SystemExit(code)


if __name__ == "__main__":
    main()
