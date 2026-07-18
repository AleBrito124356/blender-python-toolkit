"""Build a reusable .blend library of 8 procedural PBR materials.

Each material is created by a documented node-graph builder function - brushed
metal, plastic, rubber, wood, glass, emission, car paint and ceramic. Every
material gets a fake user so it survives a save even without an object using
it, which is what lets you Append or Link it into other files. Optionally adds
a row of preview spheres and renders a contact sheet.

Run headless:

    blender --background --python scripts/material_library.py -- --out material_library.blend
    blender --background --python scripts/material_library.py -- --spheres --render

Arguments (after the "--" separator):
    --out PATH     Output .blend path (default material_library.blend).
    --spheres      Add a preview sphere per material (shade-smoothed).
    --render       Render a preview to out/materials.png (implies --spheres).
    --res WxH      Preview resolution (default 1600x600).
    --samples N    Render samples (default 96).
    --cycles       Use Cycles instead of EEVEE for the preview.

Blender 4.0 renamed many Principled BSDF sockets. The _set helper below tries
several candidate socket names so the same code runs on Blender 3.x and 4.x.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    clean_default_scene,
    ensure_dir,
    hex_to_linear_rgba,
    look_at,
    new_area_light,
    new_camera,
    set_engine,
    set_samples,
    set_view_transform,
    get_argv_after_dashes,
)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="material_library.py",
        description="Build a .blend library of 8 procedural PBR materials.",
    )
    parser.add_argument("--out", default="material_library.blend", help="Output .blend path.")
    parser.add_argument("--spheres", action="store_true", help="Add preview spheres.")
    parser.add_argument("--render", action="store_true", help="Render a preview contact sheet.")
    parser.add_argument("--res", default="1600x600", help="Preview resolution WxH.")
    parser.add_argument("--samples", type=int, default=96, help="Render samples.")
    parser.add_argument("--cycles", action="store_true", help="Use Cycles for the preview.")
    return parser.parse_args(get_argv_after_dashes())


def _set(node, names, value):
    """Set the first existing input socket from ``names`` to ``value``.

    Returns True if a socket was found. Used to paper over the Principled BSDF
    socket renames between Blender 3.x and 4.x.
    """
    if isinstance(names, str):
        names = [names]
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return True
    return False


def _new_material(name):
    """Create a node-based material with a cleared tree, plus output+bsdf."""
    mat = bpy.data.materials.new(name)
    mat.use_fake_user = True  # survive save even with no user object
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    output = nt.nodes.new("ShaderNodeOutputMaterial")
    output.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (260, 0)
    nt.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    return mat, nt, bsdf, output


# ---------------------------------------------------------------------------
# Material builders. Each returns a finished bpy.types.Material.
# ---------------------------------------------------------------------------
def make_brushed_metal():
    """Anisotropic steel. A stretched Wave texture varies roughness to fake
    the directional micro-scratches of a brushed finish."""
    mat, nt, bsdf, _ = _new_material("Brushed Metal")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#b8bcc0"))
    _set(bsdf, "Metallic", 1.0)
    _set(bsdf, "Roughness", 0.35)
    _set(bsdf, ["Anisotropic", "Anisotropy"], 0.9)

    tex_coord = nt.nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-620, -60)
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-440, -60)
    mapping.inputs["Scale"].default_value = (60.0, 1.0, 1.0)  # stretch along one axis
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.location = (-240, -60)
    wave.inputs["Scale"].default_value = 4.0
    wave.inputs["Distortion"].default_value = 1.5
    ramp = nt.nodes.new("ShaderNodeMapRange")
    ramp.location = (-40, -60)
    ramp.inputs["To Min"].default_value = 0.22
    ramp.inputs["To Max"].default_value = 0.45

    nt.links.new(tex_coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
    nt.links.new(wave.outputs["Fac"], ramp.inputs["Value"])
    if "Roughness" in bsdf.inputs:
        nt.links.new(ramp.outputs["Result"], bsdf.inputs["Roughness"])
    return mat


def make_plastic():
    """Glossy injection-molded plastic: saturated base, low roughness, no metal."""
    mat, _, bsdf, _ = _new_material("Plastic")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#d81f45"))
    _set(bsdf, "Metallic", 0.0)
    _set(bsdf, "Roughness", 0.28)
    _set(bsdf, ["Specular", "Specular IOR Level"], 0.5)
    return mat


def make_rubber():
    """Matte rubber: near-black, high roughness, faint sheen. A subtle Noise
    texture perturbs roughness so it never looks perfectly uniform."""
    mat, nt, bsdf, _ = _new_material("Rubber")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#111214"))
    _set(bsdf, "Metallic", 0.0)
    _set(bsdf, "Roughness", 0.85)
    _set(bsdf, ["Specular", "Specular IOR Level"], 0.35)
    _set(bsdf, ["Sheen", "Sheen Weight"], 0.2)

    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.location = (-260, -120)
    noise.inputs["Scale"].default_value = 40.0
    rng = nt.nodes.new("ShaderNodeMapRange")
    rng.location = (-40, -120)
    rng.inputs["To Min"].default_value = 0.78
    rng.inputs["To Max"].default_value = 0.92
    nt.links.new(noise.outputs["Fac"], rng.inputs["Value"])
    if "Roughness" in bsdf.inputs:
        nt.links.new(rng.outputs["Result"], bsdf.inputs["Roughness"])
    return mat


def make_wood():
    """Procedural wood: Wave + Noise drive a warm two-tone grain into the base
    color, with roughness tracking the grain so latewood reads rougher."""
    mat, nt, bsdf, _ = _new_material("Wood")
    _set(bsdf, "Metallic", 0.0)
    _set(bsdf, "Roughness", 0.5)

    tex_coord = nt.nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-720, 0)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.location = (-520, -160)
    noise.inputs["Scale"].default_value = 3.0
    noise.inputs["Detail"].default_value = 6.0
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.location = (-320, 0)
    wave.wave_type = "RINGS"
    wave.inputs["Scale"].default_value = 2.0
    wave.inputs["Distortion"].default_value = 8.0
    wave.inputs["Detail"].default_value = 2.0

    nt.links.new(tex_coord.outputs["Object"], wave.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], wave.inputs["Distortion"])

    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.location = (-100, 0)
    ramp.color_ramp.elements[0].position = 0.3
    ramp.color_ramp.elements[0].color = hex_to_linear_rgba("#6b4324")
    ramp.color_ramp.elements[1].position = 0.7
    ramp.color_ramp.elements[1].color = hex_to_linear_rgba("#b5793f")
    nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def make_glass():
    """Clear glass: full transmission, low roughness, IOR 1.45. For EEVEE
    Legacy the material's screen-space refraction flag is enabled too."""
    mat, _, bsdf, _ = _new_material("Glass")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#ffffff"))
    _set(bsdf, "Metallic", 0.0)
    _set(bsdf, "Roughness", 0.02)
    _set(bsdf, ["Transmission", "Transmission Weight"], 1.0)
    _set(bsdf, "IOR", 1.45)
    # EEVEE Legacy needs this per-material flag to actually refract.
    if hasattr(mat, "use_screen_refraction"):
        mat.use_screen_refraction = True
    return mat


def make_emission():
    """Pure emitter for signage / lights. Uses the Principled emission inputs
    when present, falling back to a dedicated Emission shader on old builds."""
    mat, nt, bsdf, output = _new_material("Emission")
    color = hex_to_linear_rgba("#1ec8ff")
    got_color = _set(bsdf, ["Emission Color", "Emission"], color)
    got_strength = _set(bsdf, "Emission Strength", 8.0)
    if not (got_color and got_strength):
        # Very old Principled BSDF: swap in a standalone Emission node.
        emission = nt.nodes.new("ShaderNodeEmission")
        emission.location = (260, -220)
        emission.inputs["Color"].default_value = color
        emission.inputs["Strength"].default_value = 8.0
        nt.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def make_car_paint():
    """Metallic-flake car paint approximated with a colored metallic base and a
    strong clear coat layer for the wet, deep-gloss look."""
    mat, _, bsdf, _ = _new_material("Car Paint")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#0b2a6b"))
    _set(bsdf, "Metallic", 0.9)
    _set(bsdf, "Roughness", 0.28)
    # "Clearcoat" became "Coat Weight" / "Coat Roughness" in Blender 4.0.
    _set(bsdf, ["Coat Weight", "Clearcoat"], 1.0)
    _set(bsdf, ["Coat Roughness", "Clearcoat Roughness"], 0.03)
    return mat


def make_ceramic():
    """Glazed ceramic: bright off-white base, low roughness, slight subsurface
    for the soft, waxy depth of porcelain."""
    mat, _, bsdf, _ = _new_material("Ceramic")
    _set(bsdf, "Base Color", hex_to_linear_rgba("#f3efe7"))
    _set(bsdf, "Metallic", 0.0)
    _set(bsdf, "Roughness", 0.12)
    _set(bsdf, ["Specular", "Specular IOR Level"], 0.6)
    _set(bsdf, ["Subsurface", "Subsurface Weight"], 0.08)
    if not _set(bsdf, "Subsurface Radius", (0.6, 0.5, 0.45)):
        pass
    return mat


BUILDERS = [
    make_brushed_metal,
    make_plastic,
    make_rubber,
    make_wood,
    make_glass,
    make_emission,
    make_car_paint,
    make_ceramic,
]


def add_preview_spheres(materials):
    """Lay out one shaded sphere per material in a row and return them."""
    spheres = []
    spacing = 2.6
    x0 = -spacing * (len(materials) - 1) / 2.0
    for i, mat in enumerate(materials):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(x0 + i * spacing, 0.0, 1.0))
        sphere = bpy.context.active_object
        sphere.name = f"Preview_{mat.name.replace(' ', '_')}"
        bpy.ops.object.shade_smooth()
        sphere.data.materials.append(mat)
        spheres.append(sphere)

    # Neutral pedestal plane.
    bpy.ops.mesh.primitive_plane_add(size=spacing * len(materials) + 6.0, location=(0.0, 0.0, 0.0))
    plane = bpy.context.active_object
    plane.name = "PreviewFloor"
    floor_mat = bpy.data.materials.new("PreviewFloor")
    floor_mat.use_nodes = True
    fbsdf = floor_mat.node_tree.nodes.get("Principled BSDF")
    if fbsdf is not None:
        fbsdf.inputs["Base Color"].default_value = (0.12, 0.12, 0.13, 1.0)
    plane.data.materials.append(floor_mat)
    return spheres


def build_preview_lighting(spheres):
    """World, key/fill lights and a camera framing the sphere row."""
    world = bpy.data.worlds.new("PreviewWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.04, 0.04, 0.05, 1.0)
        bg.inputs["Strength"].default_value = 1.0
    bpy.context.scene.world = world

    width = max(len(spheres) * 2.6, 6.0)
    key = new_area_light("Key", (-width * 0.6, -width, width), energy=2500.0, size=width)
    fill = new_area_light("Fill", (width * 0.6, -width * 0.6, width * 0.7), energy=800.0, size=width)
    for light in (key, fill):
        look_at(light, Vector((0.0, 0.0, 1.0)))

    cam = new_camera(name="PreviewCamera", lens=55.0)
    cam.location = Vector((0.0, -width * 1.35, width * 0.55))
    look_at(cam, Vector((0.0, 0.0, 1.0)))
    return cam


def main():
    args = parse_args()
    clean_default_scene()

    materials = [builder() for builder in BUILDERS]
    print(f"[materials] built {len(materials)} materials: " + ", ".join(m.name for m in materials))

    want_spheres = args.spheres or args.render
    if want_spheres:
        spheres = add_preview_spheres(materials)
        build_preview_lighting(spheres)

    out_path = os.path.abspath(args.out)
    ensure_dir(os.path.dirname(out_path))
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    print(f"[materials] saved library -> {out_path}")

    if args.render:
        scene = bpy.context.scene
        engine = set_engine(scene, "cycles" if args.cycles else "eevee")
        set_samples(scene, args.samples)
        set_view_transform(scene)
        try:
            width, height = (int(v) for v in args.res.lower().split("x"))
        except ValueError:
            raise SystemExit(f"--res must look like 1600x600, got {args.res!r}")
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        preview_path = os.path.abspath(os.path.join("out", "materials.png"))
        ensure_dir(os.path.dirname(preview_path))
        scene.render.filepath = preview_path
        print(f"[materials] rendering preview with {engine} -> {preview_path}")
        bpy.ops.render.render(write_still=True)
        print(f"[materials] wrote {preview_path}")


if __name__ == "__main__":
    main()
