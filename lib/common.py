"""Shared ``bpy`` helpers for the blender-python-toolkit scripts.

Every script under ``scripts/`` puts the repository root on ``sys.path`` and
imports from here. Everything works in background (headless) mode: nothing
depends on a 3D viewport, an add-on or a GPU.

Version notes (tested on Blender 5.2.1 LTS; the older branches are kept for
3.6-4.x but are not exercised by this repository's test-suite):

* Blender 4.4+ stores animation in *layered* actions, and 5.0 removed
  ``Action.fcurves``. Use :func:`action_fcurves` instead of touching
  ``action.fcurves`` directly.
* Blender 5.0 added ``ImageFormatSettings.media_type``; ``file_format`` only
  accepts the formats of the current media type, so always go through
  :func:`set_image_output` / :func:`set_video_output`.
* Blender 5.0 replaced the ``NISHITA`` sky with ``SINGLE_SCATTERING`` /
  ``MULTIPLE_SCATTERING``; :func:`configure_sky` picks whatever exists.
* The EEVEE engine id is ``BLENDER_EEVEE_NEXT`` on 4.2-4.4 and
  ``BLENDER_EEVEE`` everywhere else; :func:`set_eevee_engine` detects it.

The pure-Python parts (argument parsing, colour maths, CSV, presets, chart
layout, QA decisions) live in :mod:`lib.core` and are re-exported here so
older ``from lib.common import ...`` lines keep working.
"""

from __future__ import annotations

import math
import os

import bpy
from mathutils import Vector

from lib.core import (  # noqa: F401  (re-exported for backward compatibility)
    format_for_path,
    get_argv_after_dashes,
    hex_to_linear_rgba,
    image_extension,
    linear_to_srgb,
    parse_resolution,
    positive_int,
    ramp_color,
    resolution_type,
    srgb_to_linear,
)

try:  # numpy ships with Blender >= 2.8x; guarded so the module still imports without it
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

GEOMETRY_TYPES = {"MESH", "CURVE", "FONT", "SURFACE", "META"}


# ---------------------------------------------------------------------------
# Scene setup / teardown
# ---------------------------------------------------------------------------
def clean_default_scene():
    """Remove every object and purge unused datablocks for a clean slate.

    Objects are removed through ``bpy.data`` so there is no dependency on the
    current context or mode, which may be undefined in background mode.
    """
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.lights,
        bpy.data.cameras,
        bpy.data.images,
    ):
        for block in list(collection):
            if block.users == 0:
                collection.remove(block)


def ensure_dir(path):
    """Create ``path`` (a directory) if it does not exist and return it."""
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def new_material(name):
    """Create a node-based material on any Blender version.

    Blender 5.x always gives materials a node tree (``use_nodes`` is
    deprecated there); older versions need ``use_nodes = True``.
    """
    mat = bpy.data.materials.new(name)
    if getattr(mat, "node_tree", None) is None or bpy.app.version < (5, 0, 0):
        mat.use_nodes = True
    return mat


def new_world(name, color=(0.05, 0.05, 0.055), strength=1.0, assign=True):
    """Create a node-based world with a flat background colour."""
    world = bpy.data.worlds.new(name)
    if getattr(world, "node_tree", None) is None or bpy.app.version < (5, 0, 0):
        world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
        bg.inputs["Strength"].default_value = strength
    if assign:
        bpy.context.scene.world = world
    return world


def principled(mat):
    """Return the material's Principled BSDF node (or None)."""
    for node in mat.node_tree.nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def set_input(node, names, value):
    """Set the first existing input socket among ``names``; True if found.

    Papers over socket renames between Blender versions (for example
    ``Specular`` -> ``Specular IOR Level`` and ``Emission`` ->
    ``Emission Color`` in 4.0).
    """
    if isinstance(names, str):
        names = [names]
    for name in names:
        if name in node.inputs:
            node.inputs[name].default_value = value
            return True
    return False


def simple_material(name, color, roughness=0.5, metallic=0.0):
    """A one-node Principled material with linear RGB(A) ``color``."""
    mat = new_material(name)
    bsdf = principled(mat)
    if bsdf is not None:
        rgba = tuple(color) + (1.0,) * (4 - len(color))
        bsdf.inputs["Base Color"].default_value = rgba
        set_input(bsdf, "Roughness", roughness)
        set_input(bsdf, "Metallic", metallic)
    return mat


def tag_role(obj, role, grounded=None, ground_z=None):
    """Tag an object for the QA module (see ``lib/qa.py``).

    ``role`` is ``subject`` (must be in frame; rests on the ground unless
    ``grounded`` is False), ``annotation`` (labels/titles: must be in frame,
    may float), ``backdrop`` (floors, walls, cycloramas: ignored by framing
    checks) or ``ignore``. ``ground_z`` overrides the ground height for this
    object (used by bar charts whose zero axis is not the floor).
    """
    obj["qa_role"] = role
    if grounded is not None:
        obj["qa_grounded"] = bool(grounded)
    if ground_z is not None:
        obj["qa_ground_z"] = float(ground_z)
    return obj


# ---------------------------------------------------------------------------
# Geometry: exact boxes and evaluated bounds
# ---------------------------------------------------------------------------
def add_box(name, width, depth, height, location=(0.0, 0.0), base_z=0.0, material=None):
    """Create an exact ``width x depth x height`` box standing on ``base_z``.

    The mesh is built at its real size (no object scale to apply) with the
    origin at the centre of its base, so scaling Z grows it from the floor
    and ``Object`` texture coordinates are in metres. A negative ``height``
    builds the box downwards from ``base_z`` (used for negative bar values).
    """
    hw, hd = width / 2.0, depth / 2.0
    z0, z1 = (0.0, height) if height >= 0 else (height, 0.0)
    verts = [
        (-hw, -hd, z0), (hw, -hd, z0), (hw, hd, z0), (-hw, hd, z0),
        (-hw, -hd, z1), (hw, -hd, z1), (hw, hd, z1), (-hw, hd, z1),
    ]
    # Counter-clockwise when seen from outside -> outward normals.
    faces = [
        (0, 3, 2, 1),  # bottom
        (4, 5, 6, 7),  # top
        (0, 1, 5, 4),  # -Y
        (1, 2, 6, 5),  # +X
        (2, 3, 7, 6),  # +Y
        (3, 0, 4, 7),  # -X
    ]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = (location[0], location[1], base_z)
    if material is not None:
        mesh.materials.append(material)
    return obj


def evaluated_world_vertices(objects, depsgraph=None):
    """World-space vertices of ``objects`` after modifiers, as an (N, 3) array.

    Curves and text objects are converted through the depsgraph, so labels
    count too. Non-geometry objects are ignored.
    """
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    chunks = []
    for obj in objects:
        if obj.type not in GEOMETRY_TYPES:
            continue
        eval_obj = obj.evaluated_get(depsgraph)
        try:
            mesh = eval_obj.to_mesh()
        except RuntimeError:
            continue
        if mesh is None:
            continue
        try:
            n = len(mesh.vertices)
            if n:
                co = np.empty(n * 3, dtype=np.float64)
                mesh.vertices.foreach_get("co", co)
                co = co.reshape(n, 3)
                mw = np.array(eval_obj.matrix_world, dtype=np.float64)
                chunks.append(co @ mw[:3, :3].T + mw[:3, 3])
        finally:
            eval_obj.to_mesh_clear()
    if not chunks:
        return np.zeros((0, 3))
    return np.concatenate(chunks, axis=0)


def mesh_world_bounds(objects, depsgraph=None):
    """Exact world AABB of the evaluated vertices of ``objects``.

    Returns ``(min, max, center, radius)`` as ``Vector``s plus the radius of
    the bounding sphere around ``center``. Unlike ``obj.bound_box`` (which is
    object-aligned) this is tight for rotated objects.
    """
    pts = evaluated_world_vertices(objects, depsgraph)
    if len(pts) == 0:
        zero = Vector((0.0, 0.0, 0.0))
        return zero.copy(), zero.copy(), zero.copy(), 1.0
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    center = (lo + hi) / 2.0
    radius = float(np.sqrt(((pts - center) ** 2).sum(axis=1)).max())
    return Vector(lo), Vector(hi), Vector(center), max(radius, 1e-4)


def world_bounds(objects):
    """Return ``(center, radius)`` of the objects' evaluated vertices.

    Kept for backward compatibility; now vertex-exact instead of using the
    object-aligned ``bound_box`` corners.
    """
    _, _, center, radius = mesh_world_bounds(list(objects))
    return center, radius


# ---------------------------------------------------------------------------
# Camera / framing
# ---------------------------------------------------------------------------
def look_at(obj, target):
    """Rotate ``obj`` so its local -Z axis points at ``target`` (+Y up)."""
    target_loc = target.location if hasattr(target, "location") else Vector(target)
    direction = Vector(target_loc) - obj.location
    if direction.length == 0.0:
        return
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def direction_from_angles(azimuth_deg, elevation_deg):
    """Unit vector from a subject towards a camera.

    Azimuth 0 puts the camera on -Y looking towards +Y; positive azimuth
    swings it counter-clockwise seen from above. Elevation is above the
    horizon.
    """
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))


def camera_tangents(scene, cam_data):
    """``(tan_half_fov_x, tan_half_fov_y)`` for the scene's render aspect.

    Honours the camera's sensor fit (AUTO/HORIZONTAL/VERTICAL) and pixel
    aspect, which is what ``world_to_camera_view`` uses as the frame.
    """
    r = scene.render
    w = r.resolution_x * r.pixel_aspect_x
    h = r.resolution_y * r.pixel_aspect_y
    fit = cam_data.sensor_fit
    if fit == "AUTO":
        t = cam_data.sensor_width / (2.0 * cam_data.lens)
        return (t, t * h / w) if w >= h else (t * w / h, t)
    if fit == "HORIZONTAL":
        t = cam_data.sensor_width / (2.0 * cam_data.lens)
        return t, t * h / w
    t = cam_data.sensor_height / (2.0 * cam_data.lens)
    return t * w / h, t


def frame_points(camera, points, direction=None, margin=1.1, scene=None):
    """Place ``camera`` so every point in ``points`` is inside the frame.

    The camera looks along ``-direction`` (a vector from the subject towards
    the camera; default: the current camera-to-subject direction) with world
    +Z up. The distance and a sideways/vertical offset are solved exactly for
    the render aspect ratio, so the content is centred and touches the frame
    only after the ``margin`` (1.1 = 10% breathing room on each side).
    Works for perspective and orthographic cameras. Returns the distance.
    """
    scene = scene or bpy.context.scene
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) == 0:
        return 0.0
    center = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
    if direction is None:
        direction = camera.location - Vector(center)
        if direction.length == 0.0:
            direction = Vector((0.0, -1.0, 0.35))
    back = Vector(direction).normalized()
    rot = (-back).to_track_quat("-Z", "Y").to_matrix()
    right, up = rot.col[0], rot.col[1]
    R = np.array([right[:], up[:], back[:]], dtype=np.float64)
    local = (pts - center) @ R.T  # columns: x (right), y (up), s (towards camera)
    x, y, s = local[:, 0], local[:, 1], local[:, 2]
    cam_data = camera.data
    tx, ty = camera_tangents(scene, cam_data)
    margin = max(float(margin), 1.0)

    if cam_data.type == "ORTHO":
        a = (x.max() + x.min()) / 2.0
        b = (y.max() + y.min()) / 2.0
        aspect = tx / ty  # frame width / frame height
        frame_w = max((x.max() - x.min()) * margin, (y.max() - y.min()) * margin * aspect, 1e-3)
        fit = cam_data.sensor_fit
        width_is_scale = fit == "HORIZONTAL" or (fit == "AUTO" and aspect >= 1.0)
        cam_data.ortho_scale = frame_w if width_is_scale else frame_w / aspect
        dist = float(s.max()) + cam_data.clip_start * 2.0 + 1.0
        far = float((dist - s).max())
        if cam_data.clip_end < far * 1.2:
            cam_data.clip_end = far * 1.5
    else:
        tx, ty = tx / margin, ty / margin
        near = cam_data.clip_start * 1.5

        def feasible(d):
            depth = d - s
            if depth.min() <= near:
                return None
            lo_x, hi_x = (x - depth * tx).max(), (x + depth * tx).min()
            lo_y, hi_y = (y - depth * ty).max(), (y + depth * ty).min()
            if lo_x > hi_x or lo_y > hi_y:
                return None
            return (lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0

        lo = float(s.max()) + near
        hi = max(lo * 2.0, 1.0)
        while feasible(hi) is None:
            hi *= 2.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if feasible(mid) is None:
                lo = mid
            else:
                hi = mid
        dist = hi
        a, b = feasible(dist)
        far = float((dist - s).max())
        if cam_data.clip_end < far * 1.2:
            cam_data.clip_end = far * 1.5

    location = Vector(center) + back * dist + right * float(a) + up * float(b)
    camera.location = location
    camera.rotation_euler = rot.to_euler()
    bpy.context.view_layer.update()  # refresh matrix_world for code that reads it next
    return dist


def frame_objects(camera, objects, margin=1.1, direction=None, scene=None):
    """Fit the evaluated vertices of ``objects`` into the camera frame.

    See :func:`frame_points`. The render resolution must already be set,
    because the fit depends on the aspect ratio.
    """
    pts = evaluated_world_vertices(list(objects))
    return frame_points(camera, pts, direction=direction, margin=margin, scene=scene)


def frame_object(camera, obj, margin=1.25):
    """Backward-compatible wrapper: fit one object or a list of objects."""
    objects = obj if isinstance(obj, (list, tuple, set)) else [obj]
    return frame_objects(camera, objects, margin=margin)


def rotated_about_z(points, pivot, angles_deg):
    """Stack copies of ``points`` rotated about a vertical axis through ``pivot``.

    Used to frame a turntable: fitting the union of every orientation keeps
    the subject in frame on every frame of the orbit.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    px, py = float(pivot[0]), float(pivot[1])
    out = []
    for ang in angles_deg:
        c, s_ = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        dx, dy = pts[:, 0] - px, pts[:, 1] - py
        out.append(np.stack([px + c * dx - s_ * dy, py + s_ * dx + c * dy, pts[:, 2]], axis=1))
    return np.concatenate(out, axis=0) if out else pts


# ---------------------------------------------------------------------------
# Render engine / colour management (version-aware)
# ---------------------------------------------------------------------------
KNOWN_ENGINES = ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES")


def render_engine_identifiers(scene=None):
    """Return the render engines that can actually be selected in this build.

    ``RenderSettings.engine`` is a dynamic enum: its static ``enum_items`` only
    list EEVEE, while Cycles and Workbench are registered at runtime. So each
    candidate (plus any add-on ``RenderEngine``) is tried and the original
    engine restored.
    """
    scene = scene or bpy.context.scene
    candidates = list(KNOWN_ENGINES)
    for cls in bpy.types.RenderEngine.__subclasses__():
        idname = getattr(cls, "bl_idname", None)
        if idname and idname not in candidates:
            candidates.append(idname)
    original = scene.render.engine
    available = []
    for engine in candidates:
        try:
            scene.render.engine = engine
        except TypeError:
            continue
        available.append(engine)
    scene.render.engine = original
    return available


def set_eevee_engine(scene):
    """Select the EEVEE engine id that exists in this build and return it.

    ``BLENDER_EEVEE_NEXT`` on 4.2-4.4, ``BLENDER_EEVEE`` before 4.2 and again
    from 5.0 on.
    """
    available = render_engine_identifiers(scene)
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if engine in available:
            scene.render.engine = engine
            return engine
    return scene.render.engine


def enable_cycles_gpu():
    """Try to enable a Cycles GPU backend. Returns the backend name or None."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except (KeyError, AttributeError):
        return None
    for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        try:
            prefs.compute_device_type = backend
        except TypeError:
            continue
        try:
            devices = prefs.get_devices_for_type(backend)
        except Exception:  # noqa: BLE001 - driver-level failures must not kill a render
            devices = []
        gpus = [d for d in devices if d.type != "CPU"]
        if gpus:
            for dev in devices:
                dev.use = dev.type != "CPU"
            return backend
    try:
        prefs.compute_device_type = "NONE"
    except TypeError:
        pass
    return None


def set_cycles_engine(scene, device="gpu"):
    """Select Cycles and a compute device. Returns ``"CYCLES/<device>"``.

    With ``device="gpu"`` the first working backend among OptiX, CUDA, HIP,
    oneAPI and Metal is enabled; without one, Cycles renders on the CPU and
    the returned label says so.
    """
    if "CYCLES" not in render_engine_identifiers(scene):
        print("[common] Cycles is not available in this build; using EEVEE")
        return set_eevee_engine(scene)
    scene.render.engine = "CYCLES"
    backend = enable_cycles_gpu() if str(device).lower() == "gpu" else None
    scene.cycles.device = "GPU" if backend else "CPU"
    if str(device).lower() == "gpu" and not backend:
        print("[common] no Cycles GPU backend available; rendering on the CPU")
    return f"CYCLES/{backend or 'CPU'}"


def set_engine(scene, name, device="gpu"):
    """Set the engine from a friendly name (``eevee`` / ``cycles``)."""
    name = (name or "eevee").strip().lower()
    if name in {"cycles", "cyc"}:
        return set_cycles_engine(scene, device=device)
    return set_eevee_engine(scene)


def set_samples(scene, samples):
    """Set the sample count on whichever engine is active."""
    samples = int(samples)
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = samples
    else:
        try:
            scene.eevee.taa_render_samples = samples
        except AttributeError:
            pass


def enable_eevee_raytracing(scene):
    """Turn on EEVEE ray tracing (screen-space refraction on EEVEE Legacy)."""
    eevee = scene.eevee
    if hasattr(eevee, "use_raytracing"):
        eevee.use_raytracing = True
        return True
    if hasattr(eevee, "use_ssr"):
        eevee.use_ssr = True
        if hasattr(eevee, "use_ssr_refraction"):
            eevee.use_ssr_refraction = True
        return True
    return False


def set_view_transform(scene):
    """Apply the default view transform for this version (AgX on 4.0+).

    Returns the transform name actually applied.
    """
    wanted = "AgX" if bpy.app.version >= (4, 0, 0) else "Filmic"
    for name in (wanted, "Standard"):
        try:
            scene.view_settings.view_transform = name
            return name
        except (TypeError, AttributeError):
            continue
    return scene.view_settings.view_transform


def set_image_output(settings, fmt="PNG", color_mode=None, color_depth=None):
    """Point ``ImageFormatSettings`` at a still-image format.

    Blender 5 only accepts ``file_format`` values of the current
    ``media_type``, so a scene saved with video output rejects ``"PNG"``
    until ``media_type`` is switched back to ``IMAGE``. Works on older
    versions too (where ``media_type`` does not exist).
    """
    fmt = str(fmt).upper()
    if hasattr(settings, "media_type"):
        wanted = "MULTI_LAYER_IMAGE" if fmt == "OPEN_EXR_MULTILAYER" else "IMAGE"
        if settings.media_type != wanted:
            settings.media_type = wanted
    settings.file_format = fmt
    if color_mode:
        try:
            settings.color_mode = color_mode
        except TypeError:
            pass
    if color_depth:
        try:
            settings.color_depth = str(color_depth)
        except TypeError:
            pass
    return fmt


VIDEO_CONTAINERS = {
    # name: (ffmpeg.format, ffmpeg.codec, extension)
    "mp4": ("MPEG4", "H264", ".mp4"),
    "mkv": ("MKV", "H264", ".mkv"),
    "webm": ("WEBM", "WEBM", ".webm"),
}


def set_video_output(scene, kind="mp4", quality="HIGH"):
    """Configure FFmpeg video output (H.264 MP4 by default). Returns the extension."""
    container, codec, ext = VIDEO_CONTAINERS[kind]
    settings = scene.render.image_settings
    if hasattr(settings, "media_type"):
        settings.media_type = "VIDEO"
    settings.file_format = "FFMPEG"
    ff = scene.render.ffmpeg
    ff.format = container
    ff.codec = codec
    try:
        ff.constant_rate_factor = quality
    except TypeError:
        pass
    try:
        ff.audio_codec = "NONE"
    except TypeError:
        pass
    if container != "WEBM":
        settings.color_mode = "RGB"
    return ext


# ---------------------------------------------------------------------------
# Animation (layered actions on Blender 4.4+/5.x)
# ---------------------------------------------------------------------------
def action_fcurves(obj):
    """Return the F-curves animating ``obj`` (an ID such as an Object).

    Uses ``action.fcurves`` where it exists (Blender < 5.0) and the layered
    API otherwise: ``action.layers[*].strips[*].channelbag(slot).fcurves``
    for the slot assigned to ``obj``.
    """
    anim = getattr(obj, "animation_data", None)
    if anim is None or anim.action is None:
        return []
    action = anim.action
    legacy = getattr(action, "fcurves", None)
    if legacy is not None and not hasattr(action, "layers"):
        return list(legacy)
    if legacy is not None and len(legacy):
        return list(legacy)
    slot = getattr(anim, "action_slot", None)
    curves = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            try:
                bag = strip.channelbag(slot) if slot is not None else None
            except (TypeError, RuntimeError):
                bag = None
            if bag is not None:
                curves.extend(bag.fcurves)
    return curves


def set_interpolation(obj, interpolation="LINEAR", easing=None, data_path=None):
    """Set interpolation (and optional easing) on every key of ``obj``."""
    count = 0
    for fcurve in action_fcurves(obj):
        if data_path and fcurve.data_path != data_path:
            continue
        for kp in fcurve.keyframe_points:
            kp.interpolation = interpolation
            if easing:
                kp.easing = easing
            count += 1
        fcurve.update()
    return count


# ---------------------------------------------------------------------------
# Sky
# ---------------------------------------------------------------------------
def configure_sky(node, elevation_deg, rotation_deg, altitude=0.0):
    """Configure a Sky Texture node with the best physical model available.

    Order of preference: ``MULTIPLE_SCATTERING`` (5.0+), ``NISHITA``
    (2.90-4.x), ``SINGLE_SCATTERING``, then ``HOSEK_WILKIE`` / ``PREETHAM``
    with an equivalent sun direction. Returns the sky type that was set, so
    callers can report it instead of failing silently.
    """
    available = [item.identifier for item in node.bl_rna.properties["sky_type"].enum_items]
    for sky_type in ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING"):
        if sky_type in available:
            node.sky_type = sky_type
            node.sun_elevation = math.radians(elevation_deg)
            node.sun_rotation = math.radians(rotation_deg)
            if hasattr(node, "altitude"):
                node.altitude = altitude
            return sky_type
    for sky_type in ("HOSEK_WILKIE", "PREETHAM"):
        if sky_type in available:
            node.sky_type = sky_type
            node.sun_direction = sky_sun_direction(elevation_deg, rotation_deg)
            return sky_type
    return node.sky_type


def sky_sun_direction(elevation_deg, rotation_deg):
    """Unit vector towards the sun of a Sky Texture with these angles.

    Measured on Blender 5.2: rotation 0 puts the sun towards +Y, rotation 90
    towards +X.
    """
    el, rot = math.radians(elevation_deg), math.radians(rotation_deg)
    return Vector((math.sin(rot) * math.cos(el), math.cos(rot) * math.cos(el), math.sin(el)))


def sun_rotation_for_sky(elevation_deg, rotation_deg):
    """Euler rotation that makes a sun lamp shine from the sky's sun position."""
    return sky_sun_direction(elevation_deg, rotation_deg).to_track_quat("Z", "Y").to_euler()


# ---------------------------------------------------------------------------
# Small object-creation conveniences
# ---------------------------------------------------------------------------
def new_camera(name="Camera", location=(0.0, -8.0, 4.0), lens=50.0):
    """Create a camera object, make it the scene camera, and return it."""
    cam_data = bpy.data.cameras.new(name)
    cam_data.lens = lens
    cam = bpy.data.objects.new(name, cam_data)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam.location = Vector(location)
    return cam


def new_sun(name="Sun", energy=3.0, angle_deg=(50.0, 0.0, 40.0), color=(1.0, 1.0, 1.0)):
    """Create a sun light, angled by the given euler degrees, and return it."""
    light_data = bpy.data.lights.new(name, type="SUN")
    light_data.energy = energy
    light_data.color = color
    sun = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = tuple(math.radians(a) for a in angle_deg)
    return sun


def new_area_light(name, location, energy, size=3.0, color=(1.0, 1.0, 1.0)):
    """Create an area light and return it (aim it later with ``look_at``)."""
    light_data = bpy.data.lights.new(name, type="AREA")
    light_data.energy = energy
    light_data.size = size
    light_data.color = color
    light = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = Vector(location)
    return light


def link_object(obj):
    """Link a loose object into the active scene collection and return it."""
    bpy.context.scene.collection.objects.link(obj)
    return obj


def parent_keep_transform(child, parent):
    """Parent ``child`` to ``parent`` without moving it in world space."""
    bpy.context.view_layer.update()
    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    child.matrix_world = world


# ---------------------------------------------------------------------------
# Shared CLI plumbing for the rendering scripts
# ---------------------------------------------------------------------------
def add_render_args(parser, res="1600x900", samples=64):
    """Add the flags every rendering script shares.

    ``--engine``/``--device`` replace the old ``--cycles`` switch: Blender's
    Cycles add-on parses the arguments after ``--`` itself, and argparse
    treats ``--cycles`` as an ambiguous prefix of its ``--cycles-device`` /
    ``--cycles-print-stats`` options, so Blender exits with code 2 before any
    script sees a ``--cycles`` flag.
    """
    parser.add_argument("--res", default=res, type=resolution_type, help=f"Render resolution WxH (default {res}).")
    parser.add_argument("--samples", type=positive_int, default=samples, help=f"Render samples (default {samples}).")
    parser.add_argument("--engine", choices=("eevee", "cycles"), default="eevee",
                        help="Render engine (default eevee).")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu",
                        help="Cycles compute device; falls back to CPU when no GPU backend works.")
    parser.add_argument("--report", default=None, metavar="PATH",
                        help="Write a JSON QA report (composition + image checks) to PATH.")
    parser.add_argument("--qa-strict", action="store_true",
                        help="Exit with code 3 when any QA check fails.")
    return parser


def apply_render_settings(scene, args, transparent=False):
    """Engine, samples, colour management and resolution from parsed args."""
    engine = set_engine(scene, args.engine, device=args.device)
    set_samples(scene, args.samples)
    set_view_transform(scene)
    width, height = args.res
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = bool(transparent)
    return engine


def render_still(scene, path, fmt=None, color_mode=None):
    """Render the current frame to ``path`` and return the absolute path.

    The image format follows the file extension (``.png``, ``.jpg``,
    ``.exr``, ...) unless ``fmt`` is given.
    """
    path = os.path.abspath(path)
    ensure_dir(os.path.dirname(path))
    fmt = fmt or format_for_path(path)
    if fmt == "JPEG" and color_mode == "RGBA":
        color_mode = "RGB"
    set_image_output(scene.render.image_settings, fmt, color_mode=color_mode)
    scene.render.use_file_extension = False
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return path


def run_qa(args, script, images=(), frames=None, scene=None):
    """Build, print and optionally write the QA report; honour ``--qa-strict``.

    Returns the report dict (or None when neither ``--report`` nor
    ``--qa-strict`` was requested).
    """
    if not (getattr(args, "report", None) or getattr(args, "qa_strict", False)):
        return None
    from lib import qa  # local import: qa pulls in bpy_extras

    scene = scene or bpy.context.scene
    report = qa.build_scene_report(scene, script=script, images=images, frames=frames)
    print(qa.format_text(report))
    if args.report:
        path = qa.write_report(report, args.report)
        print(f"[{script}] QA report -> {path}")
    if args.qa_strict and not report["passed"]:
        print(f"[{script}] QA failed and --qa-strict is set; exiting with code 3")
        raise SystemExit(3)
    return report

