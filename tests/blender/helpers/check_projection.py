"""Compare lib.qa.project_points with bpy_extras world_to_camera_view.

Writes the maximum absolute error per camera setup to $PROJ_OUT as JSON.
"""

import json
import os
import pathlib
import random
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

ROOT = os.environ.get("BPT_TOOLKIT_ROOT") or str(pathlib.Path(__file__).resolve().parents[3])
sys.path.insert(0, ROOT)
from lib.qa import project_points  # noqa: E402

scene = bpy.context.scene
cam = scene.camera
rng = random.Random(4)
points = [Vector((rng.uniform(-6, 6), rng.uniform(-6, 6), rng.uniform(-2, 6))) for _ in range(300)]
cases = {
    "landscape": dict(res=(1600, 900), shift=(0.0, 0.0), fit="AUTO", ortho=False, aspect=(1, 1)),
    "portrait_shift": dict(res=(600, 1000), shift=(0.2, -0.1), fit="AUTO", ortho=False, aspect=(1, 1)),
    "vertical_fit": dict(res=(1280, 720), shift=(0.0, 0.3), fit="VERTICAL", ortho=False, aspect=(1, 1)),
    "pixel_aspect": dict(res=(1000, 1000), shift=(0.0, 0.0), fit="AUTO", ortho=False, aspect=(2, 1)),
    "ortho": dict(res=(800, 600), shift=(0.1, 0.0), fit="AUTO", ortho=True, aspect=(1, 1)),
}
result = {}
for name, c in cases.items():
    scene.render.resolution_x, scene.render.resolution_y = c["res"]
    scene.render.pixel_aspect_x, scene.render.pixel_aspect_y = c["aspect"]
    cam.data.shift_x, cam.data.shift_y = c["shift"]
    cam.data.sensor_fit = c["fit"]
    cam.data.type = "ORTHO" if c["ortho"] else "PERSP"
    bpy.context.view_layer.update()
    ours = project_points(scene, cam, [tuple(p) for p in points])
    worst = 0.0
    for p, q in zip(points, ours):
        ref = world_to_camera_view(scene, cam, p)
        if ref.z <= 0:
            continue
        worst = max(worst, abs(ref.x - q[0]), abs(ref.y - q[1]), abs(ref.z - q[2]))
    result[name] = worst
with open(os.environ["PROJ_OUT"], "w", encoding="utf-8") as fh:
    json.dump(result, fh)
