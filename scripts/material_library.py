"""Build a reusable .blend library of 8 procedural PBR materials.

Each material is created by a documented node-graph builder function: brushed
metal, plastic, rubber, wood, glass, emission, car paint and ceramic. Every
material gets a fake user so it survives a save even without an object using
it, which is what lets you Append or Link it into other files. Optionally adds
a row of preview spheres, fits the camera to the whole row for the preview's
aspect ratio, and renders a contact sheet with EEVEE ray tracing enabled so
the glass sphere actually refracts.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \
        --python scripts/material_library.py -- --out material_library.blend
    blender --background --factory-startup --python-exit-code 1 \
        --python scripts/material_library.py -- --spheres --render

Arguments (after the "--" separator):
    --out PATH       Output .blend path (default material_library.blend).
    --spheres        Add a preview sphere per material (shade-smoothed).
    --render         Render a preview (implies --spheres).
    --preview PATH   Preview image path (default out/materials.png).
    --res WxH        Preview resolution (default 1600x600).
    --samples N      Render samples (default 96).
    --engine NAME    eevee (default) or cycles.
    --device NAME    gpu (default, falls back to CPU) or cpu. Cycles only.
    --report PATH    Write a JSON QA report for the preview.
    --qa-strict      Exit with code 3 if any QA check fails.

Blender 4.0 renamed many Principled BSDF sockets. ``set_input`` tries several
candidate socket names so the same code runs on Blender 3.6 through 5.x.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    add_render_args,
    apply_render_settings,
    clean_default_scene,
    direction_from_angles,
    enable_eevee_raytracing,
    ensure_dir,
    frame_objects,
    get_argv_after_dashes,
    hex_to_linear_rgba,
    look_at,
    new_area_light,
    new_camera,
    new_material,
    new_world,
    render_still,
    run_qa,
    set_input,
    simple_material,
    tag_role,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="material_library.py",
        description="Build a .blend library of 8 procedural PBR materials.",
    )
    parser.add_argument("--out", default="material_library.blend", help="Output .blend path.")
    parser.add_argument("--spheres", action="store_true", help="Add preview spheres.")
    parser.add_argument("--render", action="store_true", help="Render a preview contact sheet.")
    parser.add_argument("--preview", default=os.path.join("out", "materials.png"), help="Preview image path.")
    add_render_args(parser, res="1600x600", samples=96)
    return parser.parse_args(get_argv_after_dashes() if argv is None else argv)


_set = set_input  # the builders below predate lib.common.set_input


def _new_material(name):
    """Create a node-based material with a cleared tree, plus output+bsdf."""
    mat = new_material(name)
    mat.use_fake_user = True  # survive save even with no user object
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
    # Per-material refraction flag: use_raytrace_refraction on EEVEE Next /
    # Blender 5, use_screen_refraction on EEVEE Legacy. The scene also needs
    # ray tracing turned on (see enable_eevee_raytracing).
    for flag in ("use_raytrace_refraction", "use_screen_refraction"):
        if hasattr(mat, flag):
            setattr(mat, flag, True)
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
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, segments=48, ring_count=24,
                                             location=(x0 + i * spacing, 0.0, 1.0))
        sphere = bpy.context.active_object
        sphere.name = f"Preview_{mat.name.replace(' ', '_')}"
        bpy.ops.object.shade_smooth()
        sphere.data.materials.append(mat)
        tag_role(sphere, "subject")
        spheres.append(sphere)

    bpy.ops.mesh.primitive_plane_add(size=spacing * len(materials) + 60.0, location=(0.0, 0.0, 0.0))
    plane = bpy.context.active_object
    plane.name = "PreviewFloor"
    plane.data.materials.append(simple_material("PreviewFloor", (0.12, 0.12, 0.13), roughness=0.5))
    tag_role(plane, "backdrop")
    bpy.context.scene["qa_ground_z"] = 0.0
    return spheres


def build_preview_lighting(spheres):
    """World, key/fill lights and a camera fitted to the sphere row."""
    new_world("PreviewWorld", color=(0.04, 0.04, 0.05), strength=1.0)
    width = max(len(spheres) * 2.6, 6.0)
    key = new_area_light("Key", (-width * 0.6, -width, width), energy=2500.0, size=width)
    fill = new_area_light("Fill", (width * 0.6, -width * 0.6, width * 0.7), energy=800.0, size=width)
    for light in (key, fill):
        look_at(light, (0.0, 0.0, 1.0))
    cam = new_camera(name="PreviewCamera", lens=55.0)
    # The render resolution is already set, so the fit uses the real aspect.
    frame_objects(cam, spheres, direction=direction_from_angles(0.0, 18.0), margin=1.06)
    return cam


def main(argv=None):
    args = parse_args(argv)
    clean_default_scene()
    scene = bpy.context.scene

    materials = [builder() for builder in BUILDERS]
    print(f"[materials] built {len(materials)} materials: " + ", ".join(m.name for m in materials))

    engine = apply_render_settings(scene, args)
    enable_eevee_raytracing(scene)
    if args.spheres or args.render:
        spheres = add_preview_spheres(materials)
        build_preview_lighting(spheres)

    out_path = os.path.abspath(args.out)
    ensure_dir(os.path.dirname(out_path))
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    print(f"[materials] saved library -> {out_path}")

    images = []
    if args.render:
        print(f"[materials] rendering preview with {engine} -> {os.path.abspath(args.preview)}")
        images.append(render_still(scene, args.preview))
        print(f"[materials] wrote {images[-1]}")
    if args.spheres or args.render:
        run_qa(args, "material_library", images=images)


if __name__ == "__main__":
    main()
