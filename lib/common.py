"""Shared helpers for the blender-python-toolkit scripts.

This module is imported by every script under ``scripts/``. Because those
scripts are launched with ``blender --background --python <script>``, the
repository root is put on ``sys.path`` by each script before importing this
module, so ``from lib.common import ...`` resolves.

Everything here is pure ``bpy`` / ``mathutils`` and works in headless
(background) mode. Nothing depends on a 3D viewport, an addon, or a GPU.
"""

from __future__ import annotations

import math
import os
import sys

import bpy
from mathutils import Vector


# ---------------------------------------------------------------------------
# Argument parsing after the "--" separator
# ---------------------------------------------------------------------------
def get_argv_after_dashes():
    """Return the CLI arguments that follow the ``--`` separator.

    Blender consumes everything before ``--`` for itself, so scripts receive
    their own options after it. If ``--`` is absent, an empty list is returned
    (the script runs with all defaults).

        blender --background --python scripts/x.py -- --seed 7 --render
                                                   ^^-- returned: ["--seed", "7", "--render"]
    """
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return []


# ---------------------------------------------------------------------------
# Scene setup / teardown
# ---------------------------------------------------------------------------
def clean_default_scene():
    """Remove every object and purge unused datablocks for a clean slate.

    Deleting objects directly through ``bpy.data`` avoids any dependency on
    the current context/mode, which is important in background mode where the
    active object may be undefined.
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


# ---------------------------------------------------------------------------
# Camera / object framing
# ---------------------------------------------------------------------------
def look_at(obj, target):
    """Rotate ``obj`` so its local -Z axis points at ``target``.

    ``target`` may be another object (its ``.location`` is used) or any
    3-component sequence / ``Vector``. Cameras and lights look down their
    local -Z with +Y up, which is what ``to_track_quat('-Z', 'Y')`` encodes.
    """
    if hasattr(target, "location"):
        target_loc = target.location
    else:
        target_loc = Vector(target)

    direction = target_loc - obj.location
    if direction.length == 0.0:
        return
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def world_bounds(objects):
    """Return ``(center, radius)`` of the combined world-space bounding box.

    ``center`` is a ``Vector``; ``radius`` is the distance from the center to
    the farthest bounding-box corner (i.e. a bounding sphere).
    """
    corners = []
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "FONT", "SURFACE", "META"}:
            continue
        for corner in obj.bound_box:
            corners.append(obj.matrix_world @ Vector(corner))

    if not corners:
        return Vector((0.0, 0.0, 0.0)), 1.0

    center = sum(corners, Vector((0.0, 0.0, 0.0))) / len(corners)
    radius = max((corner - center).length for corner in corners)
    return center, max(radius, 1e-4)


def frame_object(camera, obj, margin=1.25):
    """Move ``camera`` so ``obj`` fits in view, then aim it at the object.

    The camera keeps its current viewing direction; only its distance to the
    target changes. ``margin`` > 1.0 leaves empty space around the subject.
    Works for a single object or an iterable of objects.
    """
    objects = obj if isinstance(obj, (list, tuple, set)) else [obj]
    center, radius = world_bounds(objects)

    cam_data = camera.data
    # Use the limiting (smaller) field of view so the subject fits both axes.
    half_fov = min(cam_data.angle_x, cam_data.angle_y) / 2.0
    distance = (radius * margin) / math.sin(half_fov)

    direction = camera.location - center
    if direction.length == 0.0:
        direction = Vector((0.0, -1.0, 0.4))
    direction.normalize()

    camera.location = center + direction * distance
    look_at(camera, center)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------
def srgb_to_linear(c):
    """Convert a single sRGB channel in [0, 1] to linear light."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear_rgba(hex_color, alpha=1.0):
    """Convert ``"#RRGGBB"`` (or ``"RRGGBB"``) to a linear RGBA tuple.

    Blender stores colors in linear space, so an sRGB hex picked from a design
    tool must be converted before assignment to node inputs.
    """
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected a 6-digit hex color, got {hex_color!r}")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), alpha)


def ramp_color(t):
    """Map ``t`` in [0, 1] to an RGBA color along a blue -> teal -> amber ramp.

    A small hand-built gradient so scripts can color things by value without
    creating a ColorRamp node. Returns a linear RGBA tuple.
    """
    t = max(0.0, min(1.0, float(t)))
    stops = [
        (0.00, (0.15, 0.35, 0.90)),  # blue
        (0.50, (0.10, 0.70, 0.65)),  # teal
        (0.80, (0.95, 0.72, 0.15)),  # amber
        (1.00, (0.92, 0.30, 0.20)),  # warm red
    ]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            span = (t1 - t0) or 1.0
            k = (t - t0) / span
            r = c0[0] + (c1[0] - c0[0]) * k
            g = c0[1] + (c1[1] - c0[1]) * k
            b = c0[2] + (c1[2] - c0[2]) * k
            return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), 1.0)
    r, g, b = stops[-1][1]
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), 1.0)


# ---------------------------------------------------------------------------
# Render engine / color management (version-aware)
# ---------------------------------------------------------------------------
def render_engine_identifiers(scene=None):
    """Return the render-engine enum identifiers registered in this build."""
    scene = scene or bpy.context.scene
    prop = scene.render.bl_rna.properties["engine"]
    return [item.identifier for item in prop.enum_items]


def set_eevee_engine(scene):
    """Select the EEVEE engine that exists in this Blender version.

    Blender 4.2 renamed the realtime engine to ``BLENDER_EEVEE_NEXT`` while
    older releases use ``BLENDER_EEVEE``. Returns the identifier actually set.
    """
    available = render_engine_identifiers(scene)
    if bpy.app.version >= (4, 2, 0):
        preference = ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
    else:
        preference = ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"]
    for engine in preference:
        if engine in available:
            scene.render.engine = engine
            return engine
    return scene.render.engine


def set_cycles_engine(scene, device="GPU"):
    """Select Cycles if available and try to enable a GPU compute device.

    Cycles is opt-in in this toolkit: EEVEE is the default because it is fast
    and stable headless. Returns the engine identifier that was set.
    """
    available = render_engine_identifiers(scene)
    if "CYCLES" not in available:
        return set_eevee_engine(scene)

    scene.render.engine = "CYCLES"
    scene.cycles.device = device
    if device == "GPU":
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            for backend in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
                try:
                    prefs.compute_device_type = backend
                except TypeError:
                    continue
                prefs.get_devices()
                enabled = False
                for dev in prefs.devices:
                    if dev.type != "CPU":
                        dev.use = True
                        enabled = True
                if enabled:
                    break
        except Exception:
            # No GPU backend available; Cycles falls back to CPU.
            scene.cycles.device = "CPU"
    return "CYCLES"


def set_engine(scene, name, device="GPU"):
    """Set the render engine from a friendly name: ``"eevee"`` or ``"cycles"``."""
    name = (name or "eevee").strip().lower()
    if name in {"cycles", "cyc"}:
        return set_cycles_engine(scene, device=device)
    return set_eevee_engine(scene)


def set_samples(scene, samples):
    """Set the sample count on whichever engine is active."""
    samples = int(samples)
    engine = scene.render.engine
    if engine == "CYCLES":
        scene.cycles.samples = samples
    else:
        # Both EEVEE and EEVEE Next expose taa_render_samples.
        try:
            scene.eevee.taa_render_samples = samples
        except AttributeError:
            pass


def set_view_transform(scene):
    """Apply the default view transform for this version.

    Blender 4.0 switched the default from Filmic to AgX. We follow that so
    renders look consistent with the interactive default of each release.
    Returns the transform name that was applied.
    """
    wanted = "AgX" if bpy.app.version >= (4, 0, 0) else "Filmic"
    try:
        scene.view_settings.view_transform = wanted
    except (TypeError, AttributeError):
        wanted = "Standard"
        try:
            scene.view_settings.view_transform = "Standard"
        except (TypeError, AttributeError):
            pass
    return wanted


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


def new_sun(name="Sun", energy=3.0, angle_deg=(50.0, 0.0, 40.0)):
    """Create a sun light and return it, angled by the given euler degrees."""
    light_data = bpy.data.lights.new(name, type="SUN")
    light_data.energy = energy
    sun = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(sun)
    sun.rotation_euler = tuple(math.radians(a) for a in angle_deg)
    return sun


def new_area_light(name, location, energy, size=3.0):
    """Create an area light and return it (aimed later with ``look_at``)."""
    light_data = bpy.data.lights.new(name, type="AREA")
    light_data.energy = energy
    light_data.size = size
    light = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = Vector(location)
    return light


def link_object(obj):
    """Link a loose object into the active scene collection and return it."""
    bpy.context.scene.collection.objects.link(obj)
    return obj
