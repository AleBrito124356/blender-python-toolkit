"""In-Blender measurement probes used by the integration tests.

Chained after a toolkit script in the same Blender process:

    blender -b --factory-startup --python scripts/x.py --python tests/blender/helpers/probe.py -- ...

or run on a saved file:

    blender -b file.blend --factory-startup --python tests/blender/helpers/probe.py

The probe to run and the JSON output path come from the environment
(``BPT_PROBE`` = comma-separated probe names, ``BPT_PROBE_OUT`` = path),
because everything after ``--`` belongs to the toolkit script. Measurements
deliberately use Blender's own APIs (``bpy_extras.object_utils.
world_to_camera_view``, the render pipeline, ``MovieClip``) rather than the
toolkit's helpers, so the tests check the toolkit instead of trusting it.
"""

import json
import math
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


def evaluated_vertices(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        return [eval_obj.matrix_world @ v.co for v in mesh.vertices]
    finally:
        eval_obj.to_mesh_clear()


def probe_lowest_z():
    out = {}
    for obj in bpy.context.scene.objects:
        if obj.type in {"MESH", "FONT", "CURVE"} and not obj.hide_render:
            verts = evaluated_vertices(obj)
            if verts:
                out[obj.name] = min(v.z for v in verts)
    return out


def probe_normals():
    """Outward / inward face counts per mesh (relative to the mesh centre)."""
    out = {}
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        mw = obj.matrix_world
        normal_matrix = mw.to_3x3().inverted().transposed()
        verts = [mw @ v.co for v in obj.data.vertices]
        center = sum(verts, Vector()) / max(len(verts), 1)
        outward = inward = 0
        for poly in obj.data.polygons:
            n = (normal_matrix @ poly.normal).normalized()
            c = mw @ poly.center
            if (c - center).dot(n) > 0:
                outward += 1
            else:
                inward += 1
        out[obj.name] = {
            "scale": [round(v, 5) for v in obj.scale],
            "determinant": round(mw.to_3x3().determinant(), 5),
            "outward": outward,
            "inward": inward,
            "mesh": obj.data.name,
            "mesh_users": obj.data.users,
        }
    return out


def action_fcurves(obj):
    anim = obj.animation_data
    if anim is None or anim.action is None:
        return []
    action = anim.action
    if hasattr(action, "layers") and getattr(anim, "action_slot", None) is not None:
        curves = []
        for layer in action.layers:
            for strip in layer.strips:
                bag = strip.channelbag(anim.action_slot)
                if bag is not None:
                    curves.extend(bag.fcurves)
        return curves
    return list(action.fcurves)


def probe_keyframes():
    out = {}
    for obj in bpy.data.objects:
        curves = action_fcurves(obj)
        if curves:
            out[obj.name] = [
                {"path": fc.data_path, "index": fc.array_index,
                 "keys": [[kp.co[0], kp.co[1], kp.interpolation, kp.easing] for kp in fc.keyframe_points]}
                for fc in curves
            ]
    return out


def probe_projection():
    """Projected x/y range of every renderable object via bpy_extras."""
    scene = bpy.context.scene
    cam = scene.camera
    out = {"camera": cam.name if cam else None, "resolution": [scene.render.resolution_x,
                                                               scene.render.resolution_y], "objects": {}}
    if cam is None:
        return out
    for obj in scene.objects:
        if obj.type not in {"MESH", "FONT", "CURVE"} or obj.hide_render:
            continue
        verts = evaluated_vertices(obj)
        if not verts:
            continue
        pts = [world_to_camera_view(scene, cam, v) for v in verts]
        out["objects"][obj.name] = {
            "role": obj.get("qa_role"),
            "x": [min(p.x for p in pts), max(p.x for p in pts)],
            "y": [min(p.y for p in pts), max(p.y for p in pts)],
            "z_min": min(p.z for p in pts),
        }
    return out


def probe_bars():
    """Bar heights, and camera-space depth of labels vs their bar's front face."""
    scene = bpy.context.scene
    cam = scene.camera
    cam_inv = cam.matrix_world.inverted()
    bars, labels = {}, {}
    for obj in scene.objects:
        if obj.name.startswith("Bar_"):
            verts = evaluated_vertices(obj)
            zs = [v.z for v in verts]
            front_y = min(v.y for v in verts)
            front_depth = min(-(cam_inv @ v).z for v in verts)
            bars[obj.name] = {"height": max(zs) - min(zs), "top": max(zs), "bottom": min(zs),
                              "front_y": front_y, "front_depth": front_depth,
                              "index": int(obj.name.split("_")[1])}
        elif obj.name.startswith(("Label_", "Value_")):
            verts = evaluated_vertices(obj)
            labels[obj.name] = {"max_depth": max(-(cam_inv @ v).z for v in verts),
                                "max_y": max(v.y for v in verts),
                                "min_z": min(v.z for v in verts), "max_z": max(v.z for v in verts)}
    return {"bars": bars, "labels": labels, "camera_y": cam.matrix_world.translation.y}


def probe_dimensions():
    bpy.context.view_layer.update()
    return {o.name: list(o.dimensions) for o in bpy.context.scene.objects if o.type == "MESH"}


def probe_materials():
    return {m.name: {"fake_user": m.use_fake_user, "users": m.users,
                     "raytrace_refraction": getattr(m, "use_raytrace_refraction", None)}
            for m in bpy.data.materials}


def probe_movie():
    """Frame count and size of the video named by BPT_PROBE_MOVIE (decoded by Blender)."""
    clip = bpy.data.movieclips.load(os.environ["BPT_PROBE_MOVIE"])
    return {"frames": clip.frame_duration, "size": list(clip.size)}


def probe_scene():
    scene = bpy.context.scene
    r = scene.render
    samples = scene.cycles.samples if r.engine == "CYCLES" else scene.eevee.taa_render_samples
    ims = r.image_settings
    world = scene.world
    skies, sky_elevations = [], []
    if world is not None and world.node_tree is not None:
        for node in world.node_tree.nodes:
            if node.bl_idname == "ShaderNodeTexSky":
                skies.append(node.sky_type)
                sky_elevations.append(math.degrees(node.sun_elevation))
    return {
        "resolution": [r.resolution_x, r.resolution_y, r.resolution_percentage],
        "engine": r.engine,
        "samples": samples,
        "file_format": ims.file_format,
        "media_type": getattr(ims, "media_type", None),
        "film_transparent": r.film_transparent,
        "view_transform": scene.view_settings.view_transform,
        "frame_range": [scene.frame_start, scene.frame_end],
        "sky_types": skies,
        "sky_sun_elevations": sky_elevations,
        "eevee_raytracing": getattr(scene.eevee, "use_raytracing", None),
        "camera": scene.camera.name if scene.camera else None,
        "objects": sorted(o.name for o in scene.objects),
    }


def probe_camera_path():
    """Camera world position on a few frames (checks that the orbit moves)."""
    scene = bpy.context.scene
    out = {}
    for frame in sorted({scene.frame_start, (scene.frame_start + scene.frame_end) // 2, scene.frame_end}):
        scene.frame_set(frame)
        out[str(frame)] = list(scene.camera.matrix_world.translation)
    return out


def probe_facade():
    """Render each wall of one building head-on and measure the window grid.

    For every wall an orthographic camera fills the frame with that wall only;
    the rendered luminance is reduced to a column profile and a row profile.
    A window grid varies along both; the old XY brick pattern produced
    streaks (variation along one axis only) or flat colour.
    """
    import numpy as np

    scene = bpy.context.scene
    building = next(o for o in scene.objects if o.name.startswith("Building_"))
    verts = evaluated_vertices(building)
    lo = Vector((min(v.x for v in verts), min(v.y for v in verts), min(v.z for v in verts)))
    hi = Vector((max(v.x for v in verts), max(v.y for v in verts), max(v.z for v in verts)))
    center = (lo + hi) / 2
    size = hi - lo
    for obj in scene.objects:  # only the probed building remains visible
        if obj.type == "MESH" and obj is not building:
            obj.hide_render = True
    cam_data = bpy.data.cameras.new("ProbeCam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("ProbeCam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.eevee.taa_render_samples = 1
    ims = scene.render.image_settings
    if hasattr(ims, "media_type"):
        ims.media_type = "IMAGE"
    ims.file_format = "PNG"
    out = {}
    walls = {
        "-Y": (Vector((0, -1, 0)), size.x), "+Y": (Vector((0, 1, 0)), size.x),
        "-X": (Vector((-1, 0, 0)), size.y), "+X": (Vector((1, 0, 0)), size.y),
    }
    tmp = os.environ.get("BPT_PROBE_OUT", "probe.json") + ".facade.png"
    for name, (normal, width) in walls.items():
        side = min(width, size.z) * 0.8
        cam_data.ortho_scale = side
        target = center.copy()
        target.z = lo.z + min(size.z * 0.5, side)
        cam.location = target + normal * (max(size) + 5.0)
        cam.rotation_euler = (-normal).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = tmp
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(tmp, check_existing=False)
        px = np.empty(len(img.pixels), dtype=np.float32)
        img.pixels.foreach_get(px)
        bpy.data.images.remove(img)
        px = px.reshape(96, 96, 4)
        lum = 0.2126 * px[..., 0] + 0.7152 * px[..., 1] + 0.0722 * px[..., 2]
        out[name] = {
            "row_profile_std": float(lum.mean(axis=1).std()),
            "col_profile_std": float(lum.mean(axis=0).std()),
            "bright_fraction": float((lum > 0.5).mean()),
            "dark_fraction": float((lum < 0.2).mean()),
        }
    return out


PROBES = {name[len("probe_"):]: fn for name, fn in globals().items() if name.startswith("probe_")}


def main():
    names = [n.strip() for n in os.environ.get("BPT_PROBE", "scene").split(",") if n.strip()]
    bpy.context.view_layer.update()  # matrix_world is stale until the depsgraph is evaluated
    result = {name: PROBES[name]() for name in names}
    path = os.environ.get("BPT_PROBE_OUT")
    text = json.dumps(result, indent=1, default=lambda o: list(o) if hasattr(o, "__len__") else str(o))
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    sys.stdout.flush()


main()
