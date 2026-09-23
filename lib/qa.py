"""Check a render without looking at it.

This module runs inside Blender. It measures facts about a scene and a
rendered image, then hands the numbers to the pure-Python rules in
:mod:`lib.core` (``evaluate_composition`` / ``evaluate_image``), which decide
pass / warn / fail. The result is a JSON report a CI job, a cron script or an
AI agent without vision can act on.

What is measured
----------------
* **Scene facts**: engine, resolution, samples, view transform, frame range,
  camera, object / light / material counts, world and output settings.
* **Composition** (``bpy_extras``-equivalent projection of every evaluated
  vertex through the active camera): which objects are cropped by the frame
  edge or entirely outside it, which are behind the camera, which float above
  the ground, how much of the frame the subjects cover (convex-hull
  silhouettes rasterised on a coarse grid) and whether labels overlap.
* **Image** (Blender's bundled numpy on the written file): mean and spread of
  display luminance, share of clipped / crushed pixels and alpha coverage,
  which catches black, blank and empty renders.

Object roles
------------
Objects may carry a ``qa_role`` custom property: ``subject``, ``annotation``,
``backdrop`` or ``ignore`` (see :func:`lib.common.tag_role`). Untagged scenes
get sensible defaults: the largest flat mesh at the bottom is the ground
(backdrop), text objects are annotations, every other renderable geometry
object is a subject that should rest on the ground.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os

import bpy
import numpy as np
from mathutils import Vector

from lib import core
from lib.common import GEOMETRY_TYPES, evaluated_world_vertices

ROLES = ("subject", "annotation", "backdrop", "ignore")
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ogv"}
GRID_W = 160


# ---------------------------------------------------------------------------
# Scene facts
# ---------------------------------------------------------------------------
def scene_facts(scene):
    """Plain-data summary of the scene settings that matter for a render."""
    r = scene.render
    engine = r.engine
    if engine == "CYCLES":
        samples = scene.cycles.samples
    else:
        samples = getattr(scene.eevee, "taa_render_samples", None)
    counts = {}
    for obj in scene.objects:
        counts[obj.type] = counts.get(obj.type, 0) + 1
    cam = scene.camera
    world = scene.world
    sky_types = []
    if world is not None and world.node_tree is not None:
        sky_types = [n.sky_type for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeTexSky"]
    ims = r.image_settings
    return {
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "engine": engine,
        "cycles_device": scene.cycles.device if engine == "CYCLES" else None,
        "resolution": [r.resolution_x, r.resolution_y],
        "resolution_percentage": r.resolution_percentage,
        "samples": samples,
        "view_transform": scene.view_settings.view_transform,
        "look": scene.view_settings.look,
        "exposure": scene.view_settings.exposure,
        "film_transparent": bool(r.film_transparent),
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "fps": r.fps / (r.fps_base or 1.0),
        "camera": None if cam is None else {
            "name": cam.name,
            "type": cam.data.type,
            "lens": round(cam.data.lens, 3),
            "location": [round(v, 4) for v in cam.matrix_world.translation],
        },
        "object_counts": counts,
        "lights": counts.get("LIGHT", 0),
        "materials": len(bpy.data.materials),
        "world": None if world is None else {"name": world.name, "sky_types": sky_types},
        "output": {
            "media_type": getattr(ims, "media_type", "IMAGE"),
            "file_format": ims.file_format,
            "color_mode": ims.color_mode,
        },
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def project_points(scene, camera, points):
    """Vectorised ``bpy_extras.object_utils.world_to_camera_view``.

    Returns an (N, 3) array: x and y in normalised frame coordinates (0..1
    inside the frame, origin bottom-left) and the depth in front of the camera
    (negative = behind). Honours lens shift, sensor fit and render aspect via
    ``Camera.view_frame(scene=...)`` exactly like the bpy_extras function.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    inv = np.array(camera.matrix_world.normalized().inverted(), dtype=np.float64)
    co = pts @ inv[:3, :3].T + inv[:3, 3]
    z = -co[:, 2]
    frame = [v.copy() for v in camera.data.view_frame(scene=scene)[:3]]
    if camera.data.type != "ORTHO":
        d = -frame[1].z
        with np.errstate(divide="ignore", invalid="ignore"):
            k = z / d
        min_x, max_x = frame[2].x * k, frame[1].x * k
        min_y, max_y = frame[1].y * k, frame[0].y * k
    else:
        min_x, max_x = frame[2].x, frame[1].x
        min_y, max_y = frame[1].y, frame[0].y
    with np.errstate(divide="ignore", invalid="ignore"):
        x = (co[:, 0] - min_x) / (max_x - min_x)
        y = (co[:, 1] - min_y) / (max_y - min_y)
    return np.stack([x, y, z], axis=1)


def _hull_points(xy):
    """Reduce a big 2D point cloud to support points before the exact hull."""
    if len(xy) > 4000:
        angles = np.linspace(0.0, 2.0 * math.pi, 256, endpoint=False)
        dirs = np.stack([np.cos(angles), np.sin(angles)], axis=1)
        idx = np.unique(np.argmax(xy @ dirs.T, axis=0))
        xy = xy[idx]
    return core.convex_hull(xy.tolist())


def _rasterize_hull(hull, grid_w, grid_h):
    """Boolean (grid_h, grid_w) mask of cell centres inside a CCW convex hull."""
    if len(hull) < 3:
        return np.zeros((grid_h, grid_w), dtype=bool)
    xs = (np.arange(grid_w) + 0.5) / grid_w
    ys = (np.arange(grid_h) + 0.5) / grid_h
    gx, gy = np.meshgrid(xs, ys)
    inside = np.ones_like(gx, dtype=bool)
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        inside &= (bx - ax) * (gy - ay) - (by - ay) * (gx - ax) >= 0.0
    return inside


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def _is_renderable(obj, view_layer):
    if obj.type not in GEOMETRY_TYPES or obj.hide_render:
        return False
    try:
        return obj.visible_get(view_layer=view_layer)
    except (TypeError, RuntimeError):
        return True


def _is_flat(pts):
    ext = pts.max(axis=0) - pts.min(axis=0)
    horizontal = max(ext[0], ext[1])
    return horizontal > 0 and ext[2] <= max(1e-4, 0.01 * horizontal)


def classify_objects(scene, depsgraph, ground_z=None):
    """Return ``(items, ground_z, ground_source)`` for the renderable objects.

    Each item is ``(obj, role, grounded, own_ground_z, world_vertices)``.
    ``ground_z`` precedence: explicit argument, then the scene's
    ``qa_ground_z`` custom property, then the top of the largest flat
    untagged/backdrop mesh at the bottom of the scene, otherwise None.
    """
    view_layer = bpy.context.view_layer
    raw = []
    for obj in scene.objects:
        if not _is_renderable(obj, view_layer):
            continue
        role = obj.get("qa_role")
        if role == "ignore":
            continue
        pts = evaluated_world_vertices([obj], depsgraph)
        if len(pts) == 0:
            continue
        raw.append([obj, role if role in ROLES else None, pts])

    # The largest flat mesh at the bottom of an untagged scene is its ground:
    # it becomes a backdrop even when the ground height is declared elsewhere.
    detected_z, detected_name = None, None
    flat = [(o, p) for o, role, p in raw if role in (None, "backdrop") and _is_flat(p)]
    if flat:
        lowest = min(p[:, 2].min() for _, _, p in raw)
        candidates = [(o, p) for o, p in flat if p[:, 2].max() <= lowest + 1e-3 + 0.01 * abs(lowest)]
        if candidates:
            obj, pts = max(candidates, key=lambda op: np.ptp(op[1][:, 0]) * np.ptp(op[1][:, 1]))
            detected_z, detected_name = float(pts[:, 2].max()), obj.name
            for item in raw:
                if item[0] is obj and item[1] is None:
                    item[1] = "backdrop"

    source = None
    if ground_z is not None:
        source = "argument"
    elif "qa_ground_z" in scene:
        ground_z, source = float(scene["qa_ground_z"]), "scene property"
    elif detected_z is not None:
        ground_z, source = detected_z, f"flat object {detected_name!r}"

    items = []
    for obj, role, pts in raw:
        if role is None:
            role = "annotation" if obj.type == "FONT" else "subject"
        if role == "subject":
            grounded = bool(obj.get("qa_grounded", True))
        else:
            grounded = bool(obj.get("qa_grounded", False)) and role != "backdrop"
        own = obj.get("qa_ground_z")
        items.append((obj, role, grounded, None if own is None else float(own), pts))
    return items, ground_z, source


def occluded_fraction(scene, depsgraph, camera, obj, pts, samples=24):
    """Share of sampled vertices of ``obj`` hidden behind *another* object.

    Rays are cast from the camera towards evenly picked vertices; a vertex is
    occluded when the first hit is a different object clearly in front of it.
    Only meaningful for perspective cameras; returns None for orthographic.
    """
    if camera.data.type == "ORTHO" or len(pts) == 0:
        return None
    origin = camera.matrix_world.translation
    idx = np.linspace(0, len(pts) - 1, num=min(samples, len(pts))).astype(int)
    hidden = 0
    for i in idx:
        target = Vector(pts[i])
        ray = target - origin
        dist = ray.length
        if dist <= 1e-6:
            continue
        hit, location, _normal, _index, hit_obj, _matrix = scene.ray_cast(depsgraph, origin, ray / dist,
                                                                           distance=dist)
        in_front = hit and (location - origin).length < dist - max(1e-3, dist * 1e-4)
        if in_front and hit_obj is not None and hit_obj.original is not obj.original:
            hidden += 1
    return hidden / len(idx)


def _measure_frame(scene, camera, ground_z, thresholds, grid_w, grid_h):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    items, ground_z, source = classify_objects(scene, depsgraph, ground_z)
    near = camera.data.clip_start
    tol = thresholds["crop_tolerance"]
    mask = np.zeros((grid_h, grid_w), dtype=bool)
    objects = []
    for obj, role, grounded, own_ground, pts in items:
        proj = project_points(scene, camera, pts)
        depth = proj[:, 2]
        front = depth > near
        behind = bool((~front).any())
        xy = proj[front, :2]
        inside = ((xy >= -tol) & (xy <= 1.0 + tol)).all(axis=1) if len(xy) else np.zeros(0, bool)
        strictly_in = ((xy >= 0.0) & (xy <= 1.0)).all(axis=1) if len(xy) else np.zeros(0, bool)
        visible = bool(strictly_in.any()) or (
            len(xy) > 0 and xy[:, 0].max() >= 0 and xy[:, 0].min() <= 1
            and xy[:, 1].max() >= 0 and xy[:, 1].min() <= 1)
        cropped = bool(visible and len(xy) and not inside.all())
        bbox = None
        if len(xy):
            bbox = [float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max())]
        size = float(np.ptp(pts, axis=0).max())
        gz = own_ground if own_ground is not None else ground_z
        lowest = float(pts[:, 2].min())
        entry = {
            "name": obj.name,
            "type": obj.type,
            "role": role,
            "grounded": bool(grounded and gz is not None),
            "in_frame": bool(len(xy) == len(pts) and inside.all()),
            "visible_in_frame": bool(visible),
            "cropped": cropped,
            "behind_camera": behind,
            "bbox": [round(v, 5) for v in bbox] if bbox else None,
            "lowest_z": round(lowest, 6),
            "ground_z": None if gz is None else round(gz, 6),
            "ground_gap": None if gz is None else round(lowest - gz, 6),
            "size": round(size, 5),
            "vertices": int(len(pts)),
        }
        if role == "annotation" and visible and not behind:
            frac = occluded_fraction(scene, depsgraph, camera, obj, pts)
            entry["occluded"] = None if frac is None else round(frac, 3)
        objects.append(entry)
        if role in ("subject", "annotation") and visible and len(xy) >= 3:
            mask |= _rasterize_hull(_hull_points(xy), grid_w, grid_h)
    coverage = float(mask.mean()) if objects else 0.0
    return objects, coverage, ground_z, source


def measure_composition(scene, camera=None, frames=None, ground_z=None, thresholds=None):
    """Measure framing facts, over one or several frames.

    With several ``frames`` an object counts as cropped / behind / floating
    if it is on *any* sampled frame, and coverage is the minimum over frames,
    so a turntable is checked on its whole orbit.
    """
    th = dict(core.DEFAULT_THRESHOLDS, **(thresholds or {}))
    camera = camera or scene.camera
    if camera is None:
        return {"camera": None, "objects": [], "coverage": None, "ground_z": None, "frames": []}
    frames = list(frames) if frames else [scene.frame_current]
    original = scene.frame_current
    w = scene.render.resolution_x * scene.render.pixel_aspect_x
    h = scene.render.resolution_y * scene.render.pixel_aspect_y
    grid_w = GRID_W
    grid_h = max(8, int(round(GRID_W * h / w)))
    merged, coverages, ground_used, source = {}, [], None, None
    try:
        for frame in frames:
            if frame != scene.frame_current:
                scene.frame_set(frame)
            objects, cov, ground_used, source = _measure_frame(scene, camera, ground_z, th, grid_w, grid_h)
            coverages.append(cov)
            for entry in objects:
                prev = merged.get(entry["name"])
                if prev is None:
                    entry["problem_frames"] = []
                    merged[entry["name"]] = prev = entry
                else:
                    prev["cropped"] |= entry["cropped"]
                    prev["behind_camera"] |= entry["behind_camera"]
                    prev["visible_in_frame"] &= entry["visible_in_frame"]
                    prev["in_frame"] &= entry["in_frame"]
                    if entry["ground_gap"] is not None and prev["ground_gap"] is not None:
                        prev["ground_gap"] = max(prev["ground_gap"], entry["ground_gap"])
                    prev["bbox"] = entry["bbox"]  # overlap is judged on the last frame
                    if entry.get("occluded") is not None:
                        prev["occluded"] = max(prev.get("occluded") or 0.0, entry["occluded"])
                if entry["cropped"] or entry["behind_camera"] or not entry["visible_in_frame"]:
                    prev["problem_frames"].append(frame)
    finally:
        if scene.frame_current != original:
            scene.frame_set(original)
    return {
        "camera": camera.name,
        "frames": frames,
        "ground_z": ground_used,
        "ground_source": source,
        "coverage": min(coverages) if coverages else None,
        "coverage_by_frame": [round(c, 4) for c in coverages],
        "objects": list(merged.values()),
    }


# ---------------------------------------------------------------------------
# Image statistics
# ---------------------------------------------------------------------------
def _linear_to_display(rgb):
    rgb = np.clip(rgb, 0.0, 1.0)
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1.0 / 2.4) - 0.055)


def image_stats(path):
    """Luminance / clipping / alpha statistics of an image file on disk."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return {"error": f"file not found: {path}"}
    try:
        img = bpy.data.images.load(path, check_existing=False)
    except RuntimeError as exc:
        return {"error": str(exc)}
    try:
        width, height = img.size
        channels = img.channels
        if width == 0 or height == 0:
            return {"error": "image has no pixels"}
        px = np.empty(width * height * channels, dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(-1, channels)
        if channels >= 3:
            rgb = px[:, :3]
        else:
            rgb = np.repeat(px[:, :1], 3, axis=1)
        if img.is_float:
            rgb = _linear_to_display(rgb)
        alpha = px[:, 3] if channels == 4 else np.ones(len(px), dtype=np.float32)
        has_alpha = bool(channels == 4 and (alpha < 0.999).any())
        opaque = alpha > 0.5
        lum = 0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]
        sel = lum[opaque] if has_alpha and opaque.any() else lum
        return {
            "width": int(width),
            "height": int(height),
            "channels": int(channels),
            "float": bool(img.is_float),
            "has_alpha": has_alpha,
            "alpha_coverage": round(float(opaque.mean()), 5),
            "mean_luminance": round(float(sel.mean()), 5),
            "std_luminance": round(float(sel.std()), 5),
            "clipped": round(float((sel >= 0.995).mean()), 5),
            "crushed": round(float((sel <= 0.01).mean()), 5),
            "mean_rgb": [round(float(v), 4) for v in rgb.mean(axis=0)],
        }
    finally:
        bpy.data.images.remove(img)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_scene_report(scene, script="scene", images=(), frames=None, ground_z=None, thresholds=None):
    """Measure the scene (and any rendered images) and evaluate every check."""
    comp = measure_composition(scene, frames=frames, ground_z=ground_z, thresholds=thresholds)
    checks = core.evaluate_composition(comp, thresholds)
    image_entries = []
    for path in images or ():
        entry = {"path": os.path.abspath(path)}
        if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS:
            entry["skipped"] = "video files are not decoded; check a PNG render instead"
            image_entries.append(entry)
            continue
        stats = image_stats(path)
        entry.update(stats)
        img_checks = core.evaluate_image(stats, thresholds)
        for c in img_checks:
            c["image"] = os.path.basename(path)
        checks.extend(img_checks)
        image_entries.append(entry)
    extra = {"created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    return core.build_report(script, scene_facts(scene), comp, image_entries, checks, extra=extra)


def write_report(report, path):
    """Write ``report`` as pretty JSON and return the absolute path."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return path


def format_text(report):
    """Human/agent-readable summary (see :func:`lib.core.format_report_text`)."""
    return core.format_report_text(report)
