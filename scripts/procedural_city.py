"""Generate a parametric city block procedurally, EEVEE-first.

Builds a seeded grid of buildings with varied heights and footprints, a
procedural facade material whose windows glow via an emission shader, a
ground plane, a sun with a physical sky, and a camera framed automatically
around the whole city. Optionally renders a still to out/city.png.

Run headless:

    blender --background --python scripts/procedural_city.py -- --blocks 6 --seed 7 --render

Arguments (after the "--" separator):
    --blocks N     Grid is N x N city blocks (default 5).
    --seed N       Random seed for reproducible layouts (default 1).
    --render       Render a still to out/city.png with EEVEE.
    --out PATH     Output image path (default out/city.png).
    --res WxH      Render resolution (default 1600x900).
    --samples N    EEVEE render samples (default 64).
    --cycles       Opt in to Cycles instead of EEVEE (slower, path-traced).
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    clean_default_scene,
    ensure_dir,
    hex_to_linear_rgba,
    look_at,
    new_camera,
    set_engine,
    set_samples,
    set_view_transform,
    get_argv_after_dashes,
)


BLOCK_SIZE = 8.0          # world units per block cell (building + street gap)
STREET_GAP = 2.2          # gap between building footprint and cell edge


def parse_args():
    parser = argparse.ArgumentParser(
        prog="procedural_city.py",
        description="Generate a seeded procedural city block.",
    )
    parser.add_argument("--blocks", type=int, default=5, help="Grid is N x N blocks.")
    parser.add_argument("--seed", type=int, default=1, help="Random seed.")
    parser.add_argument("--render", action="store_true", help="Render a still.")
    parser.add_argument("--out", default=os.path.join("out", "city.png"), help="Output image path.")
    parser.add_argument("--res", default="1600x900", help="Render resolution WxH.")
    parser.add_argument("--samples", type=int, default=64, help="EEVEE render samples.")
    parser.add_argument("--cycles", action="store_true", help="Use Cycles instead of EEVEE.")
    return parser.parse_args(get_argv_after_dashes())


def build_facade_material(rng):
    """Procedural facade: dark concrete with emissive windows via a brick grid.

    A Brick Texture node produces a regular window grid. Its mortar output is
    used as a mask: brick cells become emissive "lit windows" (with a per-cell
    random on/off feel from the brick color variation) while the mortar lines
    stay as a matte concrete BSDF. No geometry is added - the whole window
    look is shading only, which keeps the scene light for large grids.
    """
    mat = bpy.data.materials.new(name="Facade")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    output = nt.nodes.new("ShaderNodeOutputMaterial")
    output.location = (600, 0)

    mix = nt.nodes.new("ShaderNodeMixShader")
    mix.location = (380, 0)

    concrete = nt.nodes.new("ShaderNodeBsdfPrincipled")
    concrete.location = (120, -200)
    concrete.inputs["Base Color"].default_value = (0.05, 0.05, 0.06, 1.0)
    if "Roughness" in concrete.inputs:
        concrete.inputs["Roughness"].default_value = 0.9

    window_color = rng.choice([
        "#ffd9a0", "#fff0cc", "#cfe8ff", "#ffe4b3",
    ])
    emission = nt.nodes.new("ShaderNodeEmission")
    emission.location = (120, 180)
    emission.inputs["Color"].default_value = hex_to_linear_rgba(window_color)
    emission.inputs["Strength"].default_value = 6.0

    # Brick texture drives the window grid and the lit/unlit mask.
    brick = nt.nodes.new("ShaderNodeTexBrick")
    brick.location = (-260, 0)
    brick.offset = 0.0
    brick.squash = 1.0
    brick.inputs["Scale"].default_value = 6.0
    brick.inputs["Mortar Size"].default_value = 0.06
    brick.inputs["Brick Width"].default_value = 0.35
    brick.inputs["Row Height"].default_value = 0.28
    # Color1 / Color2 vary per brick -> use as a pseudo-random lit mask.
    brick.inputs["Color1"].default_value = (1.0, 1.0, 1.0, 1.0)
    brick.inputs["Color2"].default_value = (0.0, 0.0, 0.0, 1.0)
    brick.inputs["Mortar"].default_value = (0.0, 0.0, 0.0, 1.0)

    # Texture coordinates so the grid maps to the building faces.
    tex_coord = nt.nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-620, -80)
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-440, -80)
    mapping.inputs["Scale"].default_value = (1.0, 2.5, 1.0)
    nt.links.new(tex_coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], brick.inputs["Vector"])

    # brick Fac output is 1 on mortar lines, 0 on brick faces. Invert so the
    # window faces (bricks) drive the emission mix factor.
    invert = nt.nodes.new("ShaderNodeInvert")
    invert.location = (-40, 40)
    nt.links.new(brick.outputs["Fac"], invert.inputs["Color"])

    # Multiply the brick color (lit/unlit variation) with the window mask so
    # only some cells glow.
    mask = nt.nodes.new("ShaderNodeMath")
    mask.location = (160, 40)
    mask.operation = "MULTIPLY"
    nt.links.new(invert.outputs["Color"], mask.inputs[0])
    nt.links.new(brick.outputs["Color"], mask.inputs[1])

    nt.links.new(mask.outputs["Value"], mix.inputs["Fac"])
    nt.links.new(concrete.outputs["BSDF"], mix.inputs[1])
    nt.links.new(emission.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], output.inputs["Surface"])
    return mat


def build_ground_material():
    """Dark, slightly glossy asphalt for the ground plane."""
    mat = bpy.data.materials.new(name="Asphalt")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.015, 0.015, 0.017, 1.0)
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.6
    return mat


def build_sky_world():
    """Physical sky using a Nishita Sky Texture, with a dusk-ish sun angle."""
    world = bpy.data.worlds.new("CityWorld")
    bpy.context.scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()

    output = nt.nodes.new("ShaderNodeOutputWorld")
    output.location = (300, 0)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.location = (100, 0)
    bg.inputs["Strength"].default_value = 0.6

    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.location = (-160, 0)
    # Nishita is available on modern Blender; guard for older builds.
    try:
        sky.sky_type = "NISHITA"
        sky.sun_elevation = math.radians(12.0)
        sky.sun_rotation = math.radians(210.0)
    except (AttributeError, TypeError):
        pass
    nt.links.new(sky.outputs[0], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], output.inputs["Surface"])
    return world


def build_city(blocks, seed):
    """Create the ground, sun, buildings and camera. Returns the camera."""
    rng = random.Random(seed)

    facade = build_facade_material(rng)
    ground_mat = build_ground_material()
    build_sky_world()

    grid_span = blocks * BLOCK_SIZE
    half = grid_span / 2.0

    # Ground plane a bit larger than the city footprint.
    bpy.ops.mesh.primitive_plane_add(size=grid_span * 1.6, location=(0.0, 0.0, 0.0))
    ground = bpy.context.active_object
    ground.name = "Ground"
    ground.data.materials.append(ground_mat)

    # Sun light angled for long shadows.
    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 2.5
    sun_data.angle = math.radians(1.5)
    sun = bpy.data.objects.new("Sun", sun_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(58.0), 0.0, math.radians(150.0))

    max_height = 0.0
    for gx in range(blocks):
        for gy in range(blocks):
            # Cell center in world space.
            cx = -half + BLOCK_SIZE * (gx + 0.5)
            cy = -half + BLOCK_SIZE * (gy + 0.5)

            # Footprint smaller than the cell, leaving streets between blocks.
            max_foot = BLOCK_SIZE - STREET_GAP
            fw = rng.uniform(max_foot * 0.55, max_foot)
            fd = rng.uniform(max_foot * 0.55, max_foot)

            # Height: mostly low-rise with occasional towers.
            base = rng.uniform(4.0, 10.0)
            if rng.random() < 0.18:
                base *= rng.uniform(2.0, 3.6)
            height = base
            max_height = max(max_height, height)

            bpy.ops.mesh.primitive_cube_add(size=1.0)
            building = bpy.context.active_object
            building.name = f"Building_{gx:02d}_{gy:02d}"
            building.scale = (fw / 2.0, fd / 2.0, height / 2.0)
            building.location = (
                cx + rng.uniform(-0.4, 0.4),
                cy + rng.uniform(-0.4, 0.4),
                height / 2.0,
            )
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            building.data.materials.append(facade)

    # Camera framed on the whole city from a low, dramatic angle.
    cam = new_camera(name="CityCamera", lens=35.0)
    cam_distance = grid_span * 1.15
    cam.location = Vector((cam_distance, -cam_distance, max_height * 1.6 + grid_span * 0.25))
    look_at(cam, Vector((0.0, 0.0, max_height * 0.3)))
    return cam


def render(args):
    scene = bpy.context.scene
    engine = set_engine(scene, "cycles" if args.cycles else "eevee")
    set_samples(scene, args.samples)
    set_view_transform(scene)

    try:
        width, height = (int(v) for v in args.res.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--res must look like 1600x900, got {args.res!r}")
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"

    out_path = os.path.abspath(args.out)
    ensure_dir(os.path.dirname(out_path))
    scene.render.filepath = out_path

    print(f"[procedural_city] rendering with {engine} -> {out_path}")
    bpy.ops.render.render(write_still=True)
    print(f"[procedural_city] wrote {out_path}")


def main():
    args = parse_args()
    clean_default_scene()
    build_city(args.blocks, args.seed)
    print(f"[procedural_city] built {args.blocks}x{args.blocks} blocks (seed {args.seed})")
    if args.render:
        render(args)


if __name__ == "__main__":
    main()
