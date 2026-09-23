"""Generate the .blend / .glb fixtures used by the Blender integration tests.

Run inside Blender (the test-suite does this into pytest's tmp_path, so no
binary fixtures are committed):

    blender --background --factory-startup --python-exit-code 1 \\
        --python tests/blender/helpers/make_fixtures.py -- OUTPUT_DIR

Files written:
    hero.blend      cube + camera + light, renders fine
    nocam.blend     same scene without a camera
    videoout.blend  hero whose output is set to FFmpeg video (Blender 5 media_type trap)
    corrupt.blend   random bytes with a .blend name
    messy.blend     mirrored single-user cube, two mirrored objects sharing one mesh,
                    an orphan mesh and an orphan material
    suzanne.glb     Suzanne rotated (0.3, 0, 0.8) rad, exported as glTF binary
    qa_crop.blend   sphere half outside the camera frame
    qa_black.blend  lit-less scene with a black world
    qa_float.blend  sphere hovering 1 unit above a ground plane
    qa_good.blend   sphere resting on a ground plane, lit and framed
"""

import math
import os
import sys

import bpy

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "fixtures"
os.makedirs(OUT, exist_ok=True)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.resolution_x = 320
    scene.render.resolution_y = 180
    try:
        scene.eevee.taa_render_samples = 4
    except AttributeError:
        pass
    return scene


def add_camera(location, target=(0.0, 0.0, 0.5), lens=50.0):
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    cam.data.lens = lens
    bpy.context.scene.collection.objects.link(cam)
    cam.location = location
    from mathutils import Vector

    direction = Vector(target) - Vector(location)
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam
    return cam


def add_light(energy=1000.0):
    light = bpy.data.objects.new("Light", bpy.data.lights.new("Light", "POINT"))
    light.data.energy = energy
    light.location = (3.0, -3.0, 5.0)
    bpy.context.scene.collection.objects.link(light)
    return light


def world(color=(0.05, 0.05, 0.06), strength=1.0):
    w = bpy.data.worlds.new("World")
    if w.node_tree is None:
        w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    bg.inputs["Color"].default_value = (*color, 1.0)
    bg.inputs["Strength"].default_value = strength
    bpy.context.scene.world = w


def save(name):
    path = os.path.join(OUT, name)
    bpy.ops.wm.save_as_mainfile(filepath=path)
    print("FIXTURE", path)


# hero / nocam / videoout -----------------------------------------------------
reset()
world()
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.5))
add_light()
add_camera((3.0, -4.0, 2.5))
save("hero.blend")
bpy.data.objects.remove(bpy.data.objects["Camera"])
save("nocam.blend")

reset()
world()
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.5))
add_light()
add_camera((3.0, -4.0, 2.5))
ims = bpy.context.scene.render.image_settings
if hasattr(ims, "media_type"):
    ims.media_type = "VIDEO"
ims.file_format = "FFMPEG"
save("videoout.blend")

with open(os.path.join(OUT, "corrupt.blend"), "wb") as fh:
    fh.write(b"BLENDER-v999 this is not a blend file " * 64)
print("FIXTURE", os.path.join(OUT, "corrupt.blend"))

# messy -----------------------------------------------------------------------
reset()
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 1.0))
cube = bpy.context.active_object
cube.name = "Cube"
cube.scale = (-1.0, 1.0, 1.0)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(4.0, 0.0, 0.5))
shared_a = bpy.context.active_object
shared_a.name = "SharedA"
shared_a.scale = (1.0, -1.0, 1.0)
shared_b = bpy.data.objects.new("SharedB", shared_a.data)
shared_b.location = (6.0, 0.0, 0.5)
bpy.context.scene.collection.objects.link(shared_b)
orphan_mesh = bpy.data.meshes.new("OrphanMesh")
orphan_mat = bpy.data.materials.new("OrphanMat")
assert orphan_mesh.users == 0 and orphan_mat.users == 0
add_camera((6.0, -10.0, 5.0), target=(2.0, 0.0, 0.5))
save("messy.blend")

# suzanne.glb -------------------------------------------------------------------
reset()
bpy.ops.mesh.primitive_monkey_add(size=2.0)
monkey = bpy.context.active_object
monkey.rotation_euler = (0.3, 0.0, 0.8)
bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, "suzanne.glb"), export_format="GLB")
print("FIXTURE", os.path.join(OUT, "suzanne.glb"))

# QA scenes ---------------------------------------------------------------------
def ground():
    bpy.ops.mesh.primitive_plane_add(size=30.0, location=(0.0, 0.0, 0.0))
    bpy.context.active_object.name = "Ground"


def sphere(z):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0.0, 0.0, z))
    bpy.context.active_object.name = "Ball"
    return bpy.context.active_object


reset()
world()
ground()
sphere(1.0)
add_light()
add_camera((0.0, -9.0, 3.0), target=(0.0, 0.0, 1.0))
save("qa_good.blend")

reset()
world()
ground()
sphere(1.0)
add_light()
add_camera((0.0, -9.0, 3.0), target=(2.6, 0.0, 1.0))  # aimed off to the side: ball crosses the edge
save("qa_crop.blend")

reset()
world(color=(0.0, 0.0, 0.0), strength=0.0)
ground()
sphere(1.0)
add_camera((0.0, -9.0, 3.0), target=(0.0, 0.0, 1.0))  # no lights at all
save("qa_black.blend")

reset()
world()
ground()
sphere(2.0)  # lowest point at z=1: floating
add_light()
add_camera((0.0, -10.0, 3.5), target=(0.0, 0.0, 1.5))
save("qa_float.blend")
