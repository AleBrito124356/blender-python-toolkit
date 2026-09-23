"""Generate a seeded procedural city at dusk, EEVEE-first.

Builds an N x N grid of buildings with varied heights and footprints, all
standing on the ground at their full size, a procedural facade whose window
grid wraps all four walls (each window randomly lit or dark, no windows on
roofs), an asphalt ground, a physical dusk sky with a matching low sun, and a
camera fitted to the whole skyline for the render's aspect ratio.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/procedural_city.py -- --blocks 6 --seed 7 --render

Arguments (after the "--" separator):
    --blocks N          Grid is N x N city blocks (default 5).
    --seed N            Random seed for reproducible layouts (default 1).
    --lit FRACTION      Share of windows that are lit, 0..1 (default 0.55).
    --sun-elevation DEG Sun height above the horizon; 2-6 reads as dusk (default 3).
    --render            Render a still to --out.
    --out PATH          Output image path (default out/city.png).
    --save-blend PATH   Also save the generated scene as a .blend.
    --res WxH           Render resolution (default 1600x900).
    --samples N         Render samples (default 64).
    --engine NAME       eevee (default) or cycles.
    --device NAME       gpu (default, falls back to CPU) or cpu. Cycles only.
    --report PATH       Write a JSON QA report (framing, ground contact, exposure).
    --qa-strict         Exit with code 3 if any QA check fails.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    add_box,
    add_render_args,
    apply_render_settings,
    clean_default_scene,
    configure_sky,
    direction_from_angles,
    ensure_dir,
    frame_objects,
    get_argv_after_dashes,
    hex_to_linear_rgba,
    new_camera,
    new_material,
    new_world,
    principled,
    render_still,
    run_qa,
    set_input,
    sun_rotation_for_sky,
    tag_role,
)

BLOCK_SIZE = 8.0     # world units (metres) per block cell: building + street
STREET_GAP = 2.2     # gap between the building footprint and the cell edge
WINDOW_W = 1.3       # facade grid cell width (metres)
WINDOW_H = 2.1       # facade grid cell height, roughly one storey
SUN_ROTATION = 35.0  # sky sun azimuth (0 = +Y, 90 = +X): behind the skyline
GROUND_SIZE = 20000.0  # metres; reaches the visual horizon from street level


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="procedural_city.py",
        description="Generate a seeded procedural city at dusk.",
    )
    parser.add_argument("--blocks", type=int, default=5, help="Grid is N x N blocks.")
    parser.add_argument("--seed", type=int, default=1, help="Random seed.")
    parser.add_argument("--lit", type=float, default=0.55, help="Share of lit windows (0..1).")
    parser.add_argument("--sun-elevation", type=float, default=3.0, help="Sun elevation in degrees.")
    parser.add_argument("--render", action="store_true", help="Render a still.")
    parser.add_argument("--out", default=os.path.join("out", "city.png"), help="Output image path.")
    parser.add_argument("--save-blend", default=None, help="Save the generated scene to this .blend.")
    add_render_args(parser, res="1600x900", samples=64)
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if args.blocks < 1:
        parser.error("--blocks must be >= 1")
    if not 0.0 <= args.lit <= 1.0:
        parser.error("--lit must be between 0 and 1")
    return args


def _math(nt, op, a=None, b=None, location=(0, 0), value_b=None):
    node = nt.nodes.new("ShaderNodeMath")
    node.operation = op
    node.location = location
    if a is not None:
        nt.links.new(a, node.inputs[0])
    if b is not None:
        nt.links.new(b, node.inputs[1])
    elif value_b is not None:
        node.inputs[1].default_value = value_b
    return node.outputs[0]


def build_facade_material(lit_ratio, window_strength=2.2):
    """Facade with a box-projected window grid on every wall.

    Object coordinates are in metres because buildings are built at their
    real size (``add_box``). ``u = (x + y) / WINDOW_W`` runs along whichever
    horizontal axis a wall spans, ``v = z / WINDOW_H`` runs up it, so the same
    grid appears on all four walls. ``fract(u), fract(v)`` inside a margin
    marks a window; the integer cell id plus the object's random value feed a
    White Noise that decides per window whether it is lit and its tint. Faces
    whose normal points up (roofs) get no windows.
    """
    mat = new_material("Facade")
    nt = mat.node_tree
    bsdf = principled(mat)
    bsdf.location = (900, 0)

    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.location = (-1400, 0)
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-1200, 100)
    nt.links.new(coord.outputs["Object"], sep.inputs["Vector"])
    nsep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nsep.location = (-1200, -300)
    nt.links.new(coord.outputs["Normal"], nsep.inputs["Vector"])
    info = nt.nodes.new("ShaderNodeObjectInfo")
    info.location = (-1200, -500)

    u = _math(nt, "DIVIDE", _math(nt, "ADD", sep.outputs["X"], sep.outputs["Y"], (-1000, 200)),
              value_b=WINDOW_W, location=(-850, 200))
    v = _math(nt, "DIVIDE", sep.outputs["Z"], value_b=WINDOW_H, location=(-850, 0))
    fu = _math(nt, "FRACT", u, location=(-700, 250))
    fv = _math(nt, "FRACT", v, location=(-700, 50))
    in_u = _math(nt, "MULTIPLY",
                 _math(nt, "GREATER_THAN", fu, value_b=0.18, location=(-550, 300)),
                 _math(nt, "LESS_THAN", fu, value_b=0.82, location=(-550, 200)), location=(-400, 250))
    in_v = _math(nt, "MULTIPLY",
                 _math(nt, "GREATER_THAN", fv, value_b=0.28, location=(-550, 100)),
                 _math(nt, "LESS_THAN", fv, value_b=0.80, location=(-550, 0)), location=(-400, 50))
    wall = _math(nt, "LESS_THAN", _math(nt, "ABSOLUTE", nsep.outputs["Z"], location=(-1000, -300)),
                 value_b=0.5, location=(-850, -300))
    window = _math(nt, "MULTIPLY", _math(nt, "MULTIPLY", in_u, in_v, location=(-250, 150)), wall,
                   location=(-100, 100))

    cell = nt.nodes.new("ShaderNodeCombineXYZ")
    cell.location = (-550, -200)
    nt.links.new(_math(nt, "FLOOR", u, location=(-700, -150)), cell.inputs["X"])
    nt.links.new(_math(nt, "FLOOR", v, location=(-700, -250)), cell.inputs["Y"])
    nt.links.new(_math(nt, "MULTIPLY", info.outputs["Random"], value_b=997.0, location=(-1000, -500)),
                 cell.inputs["Z"])
    noise = nt.nodes.new("ShaderNodeTexWhiteNoise")
    noise.noise_dimensions = "3D"
    noise.location = (-350, -200)
    nt.links.new(cell.outputs["Vector"], noise.inputs["Vector"])
    lit = _math(nt, "GREATER_THAN", noise.outputs["Value"], value_b=1.0 - lit_ratio, location=(-150, -150))
    emit = _math(nt, "MULTIPLY", window, lit, location=(50, -50))

    tint = nt.nodes.new("ShaderNodeMix")
    tint.data_type = "RGBA"
    tint.location = (300, -250)
    nt.links.new(noise.outputs["Color"], tint.inputs["Factor"])
    tint.inputs["A"].default_value = hex_to_linear_rgba("#ffb45e")
    tint.inputs["B"].default_value = hex_to_linear_rgba("#ffe0a8")
    strength = _math(nt, "MULTIPLY", emit, value_b=window_strength, location=(300, -50))

    base = nt.nodes.new("ShaderNodeMix")
    base.data_type = "RGBA"
    base.location = (300, 250)
    nt.links.new(window, base.inputs["Factor"])
    base.inputs["A"].default_value = hex_to_linear_rgba("#5a5f6b")  # concrete
    base.inputs["B"].default_value = hex_to_linear_rgba("#0d1522")  # dark glass
    rough = nt.nodes.new("ShaderNodeMapRange")
    rough.location = (300, 50)
    nt.links.new(window, rough.inputs["Value"])
    rough.inputs["To Min"].default_value = 0.85
    rough.inputs["To Max"].default_value = 0.08

    nt.links.new(base.outputs["Result"], bsdf.inputs["Base Color"])
    nt.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])
    emission_color = "Emission Color" if "Emission Color" in bsdf.inputs else "Emission"
    nt.links.new(tint.outputs["Result"], bsdf.inputs[emission_color])
    if "Emission Strength" in bsdf.inputs:
        nt.links.new(strength, bsdf.inputs["Emission Strength"])
    return mat


def build_ground_material():
    """Dark, slightly glossy asphalt for the ground plane."""
    mat = new_material("Asphalt")
    bsdf = principled(mat)
    bsdf.inputs["Base Color"].default_value = (0.018, 0.018, 0.02, 1.0)
    set_input(bsdf, "Roughness", 0.45)
    return mat


def build_sky_world(sun_elevation):
    """Physical dusk sky. Returns ``(world, sky_type)``; the type is reported."""
    world = new_world("CityWorld")
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    bg.inputs["Strength"].default_value = 0.35
    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.location = (-300, 0)
    sky_type = configure_sky(sky, elevation_deg=sun_elevation, rotation_deg=SUN_ROTATION)
    if hasattr(sky, "sun_intensity"):
        sky.sun_intensity = 0.6
    nt.links.new(sky.outputs[0], bg.inputs["Color"])
    return world, sky_type


def build_city(blocks, seed, lit_ratio=0.55, sun_elevation=3.0):
    """Create ground, sun, sky and buildings. Returns ``(buildings, sky_type)``."""
    rng = random.Random(seed)
    facade = build_facade_material(lit_ratio)
    ground_mat = build_ground_material()
    _, sky_type = build_sky_world(sun_elevation)

    grid_span = blocks * BLOCK_SIZE
    half = grid_span / 2.0

    # A ground large enough to reach the horizon, so no edge shows against the sky.
    bpy.ops.mesh.primitive_plane_add(size=GROUND_SIZE, location=(0.0, 0.0, 0.0))
    ground = bpy.context.active_object
    ground.name = "Ground"
    ground.data.materials.append(ground_mat)
    tag_role(ground, "backdrop")
    bpy.context.scene["qa_ground_z"] = 0.0

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 1.2
    sun_data.angle = math.radians(1.5)
    sun_data.color = (1.0, 0.62, 0.38)
    sun = bpy.data.objects.new("Sun", sun_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = sun_rotation_for_sky(max(sun_elevation, 8.0), SUN_ROTATION)

    buildings = []
    max_foot = BLOCK_SIZE - STREET_GAP
    for gx in range(blocks):
        for gy in range(blocks):
            cx = -half + BLOCK_SIZE * (gx + 0.5)
            cy = -half + BLOCK_SIZE * (gy + 0.5)
            width = rng.uniform(max_foot * 0.55, max_foot)
            depth = rng.uniform(max_foot * 0.55, max_foot)
            height = rng.uniform(4.0, 10.0)
            if rng.random() < 0.18:
                height *= rng.uniform(2.0, 3.6)  # occasional tower
            building = add_box(
                f"Building_{gx:02d}_{gy:02d}", width, depth, height,
                location=(cx + rng.uniform(-0.4, 0.4), cy + rng.uniform(-0.4, 0.4)),
                base_z=0.0, material=facade,
            )
            tag_role(building, "subject")
            buildings.append(building)
    return buildings, sky_type


def build_camera(buildings):
    """Camera looking over the skyline, fitted to the render aspect."""
    cam = new_camera(name="CityCamera", lens=35.0)
    cam.data.clip_end = GROUND_SIZE
    frame_objects(cam, buildings, direction=direction_from_angles(-35.0, 9.0), margin=1.05)
    return cam


def main(argv=None):
    args = parse_args(argv)
    clean_default_scene()
    scene = bpy.context.scene
    engine = apply_render_settings(scene, args)
    buildings, sky_type = build_city(args.blocks, args.seed, args.lit, args.sun_elevation)
    build_camera(buildings)
    print(f"[procedural_city] built {args.blocks}x{args.blocks} blocks (seed {args.seed}), sky={sky_type}")

    if args.save_blend:
        path = os.path.abspath(args.save_blend)
        ensure_dir(os.path.dirname(path))
        bpy.ops.wm.save_as_mainfile(filepath=path)
        print(f"[procedural_city] saved scene -> {path}")

    images = []
    if args.render:
        print(f"[procedural_city] rendering with {engine} -> {os.path.abspath(args.out)}")
        images.append(render_still(scene, args.out))
        print(f"[procedural_city] wrote {images[-1]}")
    run_qa(args, "procedural_city", images=images)


if __name__ == "__main__":
    main()
