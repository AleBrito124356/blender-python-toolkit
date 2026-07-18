"""Render a product turntable with a studio 3-point lighting rig, EEVEE-first.

Imports a model (.glb/.gltf/.obj/.fbx, autodetected) or falls back to a
beveled cube "product". The model is auto-centered and scaled to fit a unit
box, lit with a key/fill/rim area-light rig built in code, and orbited by a
camera parented to an empty pivot. Renders either an N-frame turntable
animation or a single still beauty shot.

Why EEVEE: it renders each frame in a fraction of a second on consumer GPUs
and never hangs a headless session, which matters for a 60-120 frame orbit.
Cycles is available with --cycles for path-traced accuracy when you need it.

Run headless:

    blender --background --python scripts/product_turntable.py -- --frames 60 --render
    blender --background --python scripts/product_turntable.py -- --model assets/shoe.glb --still --render

Arguments (after the "--" separator):
    --model PATH   Model to import (.glb/.gltf/.obj/.fbx). Omit for the demo cube.
    --frames N     Turntable frame count for a full 360 (default 60).
    --still        Render one beauty frame instead of the full turntable.
    --render       Actually render (otherwise just builds the scene).
    --out DIR      Output directory (default out/turntable).
    --res WxH      Render resolution (default 1280x1280).
    --samples N    EEVEE render samples (default 96).
    --cycles       Use Cycles instead of EEVEE.
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
    clean_default_scene,
    ensure_dir,
    frame_object,
    look_at,
    new_area_light,
    new_camera,
    set_engine,
    set_samples,
    set_view_transform,
    world_bounds,
    get_argv_after_dashes,
)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="product_turntable.py",
        description="Render a product turntable with a 3-point studio rig.",
    )
    parser.add_argument("--model", default=None, help="Model file to import.")
    parser.add_argument("--frames", type=int, default=60, help="Turntable frames for 360.")
    parser.add_argument("--still", action="store_true", help="Single beauty shot only.")
    parser.add_argument("--render", action="store_true", help="Render output.")
    parser.add_argument("--out", default=os.path.join("out", "turntable"), help="Output directory.")
    parser.add_argument("--res", default="1280x1280", help="Render resolution WxH.")
    parser.add_argument("--samples", type=int, default=96, help="Render samples.")
    parser.add_argument("--cycles", action="store_true", help="Use Cycles instead of EEVEE.")
    return parser.parse_args(get_argv_after_dashes())


def import_model(path):
    """Import a model by extension, returning the list of new mesh objects."""
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)

    if ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        # Blender 4.x uses wm.obj_import; older releases use import_scene.obj.
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    else:
        raise SystemExit(f"Unsupported model extension {ext!r} (use .glb/.gltf/.obj/.fbx).")

    new_objects = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new_objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit(f"No mesh objects found in {path!r}.")
    return meshes


def build_demo_product():
    """Fallback subject: a beveled cube with a clean plastic material."""
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    cube.name = "DemoProduct"

    bevel = cube.modifiers.new(name="Bevel", type="BEVEL")
    bevel.width = 0.12
    bevel.segments = 4
    bpy.ops.object.modifier_apply(modifier=bevel.name)
    bpy.ops.object.shade_smooth()

    mat = bpy.data.materials.new("ProductPlastic")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.02, 0.35, 0.75, 1.0)
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.35
    cube.data.materials.append(mat)
    return [cube]


def normalize_objects(objects, target_size=2.0):
    """Center the objects at the origin and scale them into a unit-ish box.

    Everything is parented under a single empty so a uniform transform moves
    and scales the whole model together without altering local mesh data.
    """
    pivot = bpy.data.objects.new("ProductPivot", None)
    bpy.context.scene.collection.objects.link(pivot)

    for obj in objects:
        if obj.parent is None:
            obj.parent = pivot

    bpy.context.view_layer.update()
    center, radius = world_bounds(objects)
    scale = (target_size * 0.5) / radius if radius > 0 else 1.0

    # Move pivot so the model's center sits at the world origin, then scale.
    pivot.location = -center * scale
    pivot.scale = (scale, scale, scale)
    bpy.context.view_layer.update()

    # Drop the model so its base rests on Z=0.
    _, _ = world_bounds(objects)
    min_z = min(
        (obj.matrix_world @ Vector(corner)).z
        for obj in objects
        for corner in obj.bound_box
    )
    pivot.location.z -= min_z
    bpy.context.view_layer.update()
    return pivot


def build_studio(subjects):
    """Build a floor, 3-point area-light rig, camera and orbit pivot."""
    # Neutral studio floor.
    bpy.ops.mesh.primitive_plane_add(size=40.0, location=(0.0, 0.0, 0.0))
    floor = bpy.context.active_object
    floor.name = "StudioFloor"
    floor_mat = bpy.data.materials.new("StudioFloor")
    floor_mat.use_nodes = True
    fbsdf = floor_mat.node_tree.nodes.get("Principled BSDF")
    if fbsdf is not None:
        fbsdf.inputs["Base Color"].default_value = (0.18, 0.18, 0.19, 1.0)
        if "Roughness" in fbsdf.inputs:
            fbsdf.inputs["Roughness"].default_value = 0.7
    floor.data.materials.append(floor_mat)

    center, radius = world_bounds(subjects)
    d = max(radius, 1.0)

    # Key: bright, front-left, high. Fill: soft, front-right, low. Rim: behind.
    key = new_area_light("Key", (-3.0 * d, -3.0 * d, 4.0 * d), energy=1200.0, size=4.0 * d)
    fill = new_area_light("Fill", (4.0 * d, -2.0 * d, 2.0 * d), energy=350.0, size=6.0 * d)
    rim = new_area_light("Rim", (0.5 * d, 4.0 * d, 3.5 * d), energy=900.0, size=3.0 * d)
    for light in (key, fill, rim):
        look_at(light, center)

    # Neutral gray world so nothing is pitch black.
    world = bpy.data.worlds.new("StudioWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.05, 0.05, 0.055, 1.0)
        bg.inputs["Strength"].default_value = 1.0
    bpy.context.scene.world = world

    # Frame the camera in world space BEFORE parenting, so frame_object's
    # world-space math is valid.
    cam = new_camera(name="TurntableCamera", lens=70.0)
    cam.location = Vector((0.0, -6.0 * d, center.z + 1.2 * d))
    look_at(cam, center)
    frame_object(cam, subjects, margin=1.35)
    look_at(cam, center)  # re-aim after framing changed the distance

    # Orbit pivot at the subject center. Parenting with matrix_parent_inverse
    # set to the pivot's inverse world matrix preserves the framed camera
    # transform at frame 1, so rotating the pivot cleanly orbits the camera
    # around the product.
    orbit = bpy.data.objects.new("OrbitPivot", None)
    bpy.context.scene.collection.objects.link(orbit)
    orbit.location = Vector((center.x, center.y, center.z * 0.6))
    bpy.context.view_layer.update()
    cam.parent = orbit
    cam.matrix_parent_inverse = orbit.matrix_world.inverted()
    return orbit, cam


def animate_turntable(orbit, frames):
    """Keyframe a full 360 rotation of the orbit pivot over ``frames``."""
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = frames

    orbit.rotation_euler = (0.0, 0.0, 0.0)
    orbit.keyframe_insert(data_path="rotation_euler", frame=1)
    orbit.rotation_euler = (0.0, 0.0, math.radians(360.0))
    orbit.keyframe_insert(data_path="rotation_euler", frame=frames + 1)

    # Linear interpolation so the spin is perfectly even.
    if orbit.animation_data and orbit.animation_data.action:
        for fcurve in orbit.animation_data.action.fcurves:
            for kp in fcurve.keyframe_points:
                kp.interpolation = "LINEAR"


def configure_render(args):
    scene = bpy.context.scene
    engine = set_engine(scene, "cycles" if args.cycles else "eevee")
    set_samples(scene, args.samples)
    set_view_transform(scene)

    try:
        width, height = (int(v) for v in args.res.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--res must look like 1280x1280, got {args.res!r}")
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    return engine


def main():
    args = parse_args()
    clean_default_scene()

    if args.model:
        model_path = os.path.abspath(args.model)
        if not os.path.exists(model_path):
            raise SystemExit(f"Model not found: {model_path}")
        subjects = import_model(model_path)
        print(f"[turntable] imported {len(subjects)} mesh object(s) from {model_path}")
    else:
        subjects = build_demo_product()
        print("[turntable] no --model given; using the demo beveled cube")

    normalize_objects(subjects, target_size=2.0)
    orbit, cam = build_studio(subjects)

    if not args.still:
        animate_turntable(orbit, args.frames)

    if not args.render:
        print("[turntable] scene built (pass --render to write images)")
        return

    engine = configure_render(args)
    out_dir = os.path.abspath(args.out)
    ensure_dir(out_dir)
    scene = bpy.context.scene

    if args.still:
        scene.render.filepath = os.path.join(out_dir, "beauty.png")
        print(f"[turntable] rendering still with {engine} -> {scene.render.filepath}")
        bpy.ops.render.render(write_still=True)
        print(f"[turntable] wrote {scene.render.filepath}")
    else:
        scene.render.filepath = os.path.join(out_dir, "frame_")
        print(f"[turntable] rendering {args.frames} frames with {engine} -> {out_dir}")
        bpy.ops.render.render(animation=True)
        print(f"[turntable] wrote {args.frames} frames to {out_dir}")


if __name__ == "__main__":
    main()
