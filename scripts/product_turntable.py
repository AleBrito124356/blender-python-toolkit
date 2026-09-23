"""Render a product turntable with a studio 3-point lighting rig, EEVEE-first.

Imports a model (.glb/.gltf/.obj/.fbx/.stl/.ply, by extension) or falls back
to a bevelled demo cube. The model is centred, scaled to a 2-unit box and
dropped so its lowest *vertex* touches the floor (measured on the evaluated
mesh, so rotated imports and modifiers are handled). A key/fill/rim area-light
rig and the camera orbit the product together, so the lighting stays the same
on every frame. The camera is fitted to the product over the whole orbit for
the render's aspect ratio. Output is a PNG sequence, a single beauty still or
an H.264 MP4 / WebM written straight by Blender's FFmpeg output.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/product_turntable.py -- --frames 60 --render --video mp4
    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/product_turntable.py -- --model assets/shoe.glb --still --render

Arguments (after the "--" separator):
    --model PATH     Model to import. Omit for the demo cube.
    --frames N       Frames for a full 360 degree orbit (default 60).
    --still          Render one beauty frame instead of the orbit.
    --render         Actually render (otherwise just build the scene).
    --video KIND     Write the orbit as a video: mp4 (H.264), mkv or webm.
    --fps N          Frame rate for the orbit/video (default 30).
    --backdrop KIND  floor (default), cyclorama (seamless curved backdrop) or none.
    --elevation DEG  Camera height angle above the product (default 14).
    --out DIR        Output directory (default out/turntable).
    --save-blend P   Also save the built scene as a .blend.
    --res WxH        Render resolution (default 1280x1280).
    --samples N      Render samples (default 96).
    --engine NAME    eevee (default) or cycles.
    --device NAME    gpu (default, falls back to CPU) or cpu. Cycles only.
    --report PATH    Write a JSON QA report checked over the whole orbit.
    --qa-strict      Exit with code 3 if any QA check fails.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    add_render_args,
    apply_render_settings,
    clean_default_scene,
    direction_from_angles,
    ensure_dir,
    evaluated_world_vertices,
    frame_points,
    get_argv_after_dashes,
    look_at,
    mesh_world_bounds,
    new_area_light,
    new_camera,
    new_world,
    parent_keep_transform,
    render_still,
    rotated_about_z,
    run_qa,
    set_image_output,
    set_interpolation,
    set_video_output,
    simple_material,
    tag_role,
)

IMPORTERS = (".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="product_turntable.py",
        description="Render a product turntable with a 3-point studio rig.",
    )
    parser.add_argument("--model", default=None, help="Model file to import.")
    parser.add_argument("--frames", type=int, default=60, help="Frames for a full 360 orbit.")
    parser.add_argument("--still", action="store_true", help="Single beauty shot only.")
    parser.add_argument("--render", action="store_true", help="Render output.")
    parser.add_argument("--video", choices=("mp4", "mkv", "webm"), default=None,
                        help="Encode the orbit straight to a video file.")
    parser.add_argument("--fps", type=int, default=30, help="Frame rate (default 30).")
    parser.add_argument("--backdrop", choices=("floor", "cyclorama", "none"), default="floor",
                        help="Studio backdrop (default floor).")
    parser.add_argument("--elevation", type=float, default=14.0, help="Camera elevation in degrees.")
    parser.add_argument("--out", default=os.path.join("out", "turntable"), help="Output directory.")
    parser.add_argument("--save-blend", default=None, help="Save the built scene to this .blend.")
    add_render_args(parser, res="1280x1280", samples=96)
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if args.frames < 2 and not args.still:
        parser.error("--frames must be >= 2 for an orbit (or pass --still)")
    if args.video and args.still:
        parser.error("--video renders the orbit; it cannot be combined with --still")
    if args.video and (args.res[0] % 2 or args.res[1] % 2):
        parser.error("--video needs an even width and height (H.264 / VP9 requirement)")
    return args


def import_model(path):
    """Import a model by extension, returning the list of new mesh objects."""
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    if ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # Blender < 3.3
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == ".stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif ext == ".ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    else:
        raise SystemExit(f"Unsupported model extension {ext!r} (use {', '.join(IMPORTERS)}).")
    new_objects = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new_objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit(f"No mesh objects found in {path!r}.")
    return meshes


def build_demo_product():
    """Fallback subject: a bevelled cube with a clean plastic material."""
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    cube.name = "DemoProduct"
    bevel = cube.modifiers.new(name="Bevel", type="BEVEL")
    bevel.width = 0.12
    bevel.segments = 4
    bpy.ops.object.modifier_apply(modifier=bevel.name)
    bpy.ops.object.shade_smooth()
    cube.data.materials.append(simple_material("ProductPlastic", (0.02, 0.35, 0.75), roughness=0.35))
    return [cube]


def normalize_objects(objects, target_size=2.0):
    """Centre the model on the origin, scale it into ``target_size`` and put it on Z=0.

    Everything is parented under one empty (keeping world transforms), so a
    single uniform transform moves the whole model. All measurements come from
    the evaluated vertices: object-aligned ``bound_box`` corners of a rotated
    object overestimate its extent and made imports float above the floor.
    """
    pivot = bpy.data.objects.new("ProductPivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    tops = []
    for obj in objects:
        top = obj
        while top.parent is not None:
            top = top.parent
        if top not in tops:
            tops.append(top)
    for top in tops:
        parent_keep_transform(top, pivot)
    bpy.context.view_layer.update()

    lo, hi, _, _ = mesh_world_bounds(objects)
    size = max(hi - lo)
    scale = target_size / size if size > 1e-9 else 1.0
    center = (lo + hi) / 2.0
    pivot.scale = (scale, scale, scale)
    pivot.location = Vector((-center.x * scale, -center.y * scale, -lo.z * scale))
    bpy.context.view_layer.update()
    for obj in objects:
        tag_role(obj, "subject")
    return pivot


def build_cyclorama(width, depth, height, radius, material):
    """Seamless studio backdrop: a floor that curves up into a back wall (+Y side)."""
    profile = [(-depth, 0.0)]
    steps = 16
    y_curve = depth * 0.35
    for i in range(steps + 1):
        a = (math.pi / 2.0) * i / steps
        profile.append((y_curve + math.sin(a) * radius, radius - math.cos(a) * radius))
    profile.append((y_curve + radius, height))
    verts, faces = [], []
    for x in (-width / 2.0, width / 2.0):
        for y, z in profile:
            verts.append((x, y, z))
    n = len(profile)
    for i in range(n - 1):
        faces.append((i, n + i, n + i + 1, i + 1))
    mesh = bpy.data.meshes.new("Cyclorama")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    for poly in mesh.polygons:
        poly.use_smooth = True
    obj = bpy.data.objects.new("Cyclorama", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.materials.append(material)
    return obj


def build_studio(subjects, args):
    """Backdrop, camera, orbit pivot and a light rig that orbits with the camera."""
    scene = bpy.context.scene
    lo, hi, center, radius = mesh_world_bounds(subjects)
    d = max(radius, 1.0)
    scene["qa_ground_z"] = 0.0
    backdrop_mat = simple_material("StudioBackdrop", (0.18, 0.18, 0.19), roughness=0.7)

    orbit = bpy.data.objects.new("OrbitPivot", None)
    scene.collection.objects.link(orbit)
    orbit.location = Vector((center.x, center.y, 0.0))

    backdrop = None
    if args.backdrop == "floor":
        bpy.ops.mesh.primitive_plane_add(size=40.0 * d, location=(center.x, center.y, 0.0))
        backdrop = bpy.context.active_object
        backdrop.name = "StudioFloor"
        backdrop.data.materials.append(backdrop_mat)
    elif args.backdrop == "cyclorama":
        backdrop = build_cyclorama(width=16.0 * d, depth=12.0 * d, height=10.0 * d, radius=3.0 * d,
                                   material=backdrop_mat)
        backdrop.location = (center.x, center.y, 0.0)
    if backdrop is not None:
        tag_role(backdrop, "backdrop")

    new_world("StudioWorld", color=(0.05, 0.05, 0.055), strength=1.0)

    # Key: bright, front-left, high. Fill: soft, front-right, low. Rim: behind.
    key = new_area_light("Key", center + Vector((-3.0 * d, -3.0 * d, 4.0 * d)), energy=1200.0, size=4.0 * d)
    fill = new_area_light("Fill", center + Vector((4.0 * d, -2.0 * d, 2.0 * d)), energy=350.0, size=6.0 * d)
    rim = new_area_light("Rim", center + Vector((0.5 * d, 4.0 * d, 3.5 * d)), energy=900.0, size=3.0 * d)
    for light in (key, fill, rim):
        look_at(light, center)

    cam = new_camera(name="TurntableCamera", lens=70.0)
    # Fit the union of the model at every orbit angle, so no frame crops it.
    pts = evaluated_world_vertices(subjects)
    angles = [i * 360.0 / 24 for i in range(24)] if not args.still else [0.0]
    frame_points(cam, rotated_about_z(pts, center, angles),
                 direction=direction_from_angles(0.0, args.elevation), margin=1.12)

    for obj in (cam, key, fill, rim) + ((backdrop,) if backdrop is not None and args.backdrop == "cyclorama" else ()):
        parent_keep_transform(obj, orbit)
    return orbit, cam


def animate_turntable(orbit, frames):
    """Keyframe a full, evenly spaced 360 degree orbit over ``frames``.

    The 360 degree key sits on ``frames + 1`` so frames 1..N cover the circle
    without a duplicated first/last frame, which makes the loop seamless.
    """
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = frames
    orbit.rotation_euler = (0.0, 0.0, 0.0)
    orbit.keyframe_insert(data_path="rotation_euler", frame=1)
    orbit.rotation_euler = (0.0, 0.0, math.radians(360.0))
    orbit.keyframe_insert(data_path="rotation_euler", frame=frames + 1)
    set_interpolation(orbit, "LINEAR")


def sample_frames(frames, count=8):
    """Evenly spaced frames (1-based) used to QA the orbit."""
    if frames <= count:
        return list(range(1, frames + 1))
    return sorted({1 + round(i * (frames - 1) / (count - 1)) for i in range(count)})


def main(argv=None):
    args = parse_args(argv)
    clean_default_scene()
    scene = bpy.context.scene

    if args.model:
        model_path = os.path.abspath(args.model)
        if not os.path.exists(model_path):
            raise SystemExit(f"Model not found: {model_path}")
        subjects = import_model(model_path)
        print(f"[turntable] imported {len(subjects)} mesh object(s) from {model_path}")
    else:
        subjects = build_demo_product()
        print("[turntable] no --model given; using the demo bevelled cube")

    # Transparent film (PNG with alpha) unless a cyclorama fills the view or the
    # output is a video, which has no alpha channel.
    transparent = args.backdrop != "cyclorama" and not args.video
    engine = apply_render_settings(scene, args, transparent=transparent)
    scene.render.fps = args.fps
    normalize_objects(subjects, target_size=2.0)
    orbit, _cam = build_studio(subjects, args)
    if not args.still:
        animate_turntable(orbit, args.frames)
    scene.frame_set(1)

    if args.save_blend:
        path = os.path.abspath(args.save_blend)
        ensure_dir(os.path.dirname(path))
        bpy.ops.wm.save_as_mainfile(filepath=path)
        print(f"[turntable] saved scene -> {path}")

    images, qa_frames = [], None if args.still else sample_frames(args.frames)
    if args.render:
        out_dir = ensure_dir(os.path.abspath(args.out))
        if args.still:
            print(f"[turntable] rendering still with {engine}")
            images.append(render_still(scene, os.path.join(out_dir, "beauty.png"),
                                       color_mode="RGBA" if scene.render.film_transparent else "RGB"))
            print(f"[turntable] wrote {images[-1]}")
        elif args.video:
            ext = set_video_output(scene, args.video)
            scene.render.use_file_extension = False
            video_path = os.path.join(out_dir, "turntable" + ext)
            scene.render.filepath = video_path
            print(f"[turntable] rendering {args.frames} frames with {engine} -> {video_path}")
            bpy.ops.render.render(animation=True)
            print(f"[turntable] wrote {video_path}")
            images.append(video_path)
        else:
            set_image_output(scene.render.image_settings, "PNG",
                             color_mode="RGBA" if scene.render.film_transparent else "RGB")
            scene.render.use_file_extension = True
            scene.render.filepath = os.path.join(out_dir, "frame_")
            print(f"[turntable] rendering {args.frames} frames with {engine} -> {out_dir}")
            bpy.ops.render.render(animation=True)
            print(f"[turntable] wrote {args.frames} frames to {out_dir}")
            last = args.frames
            images.extend(os.path.join(out_dir, f"frame_{f:04d}.png") for f in sorted({1, (last + 1) // 2, last}))
    else:
        print("[turntable] scene built (pass --render to write images)")
    run_qa(args, "product_turntable", images=images, frames=qa_frames)


if __name__ == "__main__":
    main()
