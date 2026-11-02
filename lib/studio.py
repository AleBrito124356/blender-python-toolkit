"""Model import, normalisation and the studio light rig, shared by the turntable and thumbnails.

Runs inside Blender (or a Python interpreter with the ``bpy`` module).

* :func:`import_model` imports any model format this build has an importer
  for (glTF, OBJ, FBX, STL, PLY, USD/USDA/USDC/USDZ), by extension, and
  reports a clear error for anything else or for a file the importer rejects.
* :func:`normalize_objects` centres a model, scales it into a fixed box and
  rests its lowest *evaluated* vertex on Z=0, so every model is framed and lit
  the same way.
* :func:`three_point_rig` builds the key / fill / rim area lights of the
  turntable, turned to any camera azimuth so a 3/4 thumbnail is lit like the
  turntable's front view.
"""

from __future__ import annotations

import math
import os

import bpy
from mathutils import Matrix, Vector

from lib.common import GEOMETRY_TYPES, look_at, mesh_world_bounds, new_area_light, parent_keep_transform, tag_role
from lib.core import MODEL_EXTENSIONS  # noqa: F401  (re-exported)


class ModelImportError(ValueError):
    """The file cannot be imported (unknown extension, missing importer, or the importer failed)."""


def _op(module, name):
    """``bpy.ops.<module>.<name>`` if that operator is registered, else None."""
    try:
        op = getattr(getattr(bpy.ops, module), name)
        op.get_rna_type()
    except (AttributeError, KeyError, RuntimeError):
        return None
    return op


def _importers():
    """``{extension: (operator, extra kwargs)}`` for the importers this build registers."""
    table = {}
    gltf = _op("import_scene", "gltf")
    if gltf:
        table[".glb"] = table[".gltf"] = (gltf, {})
    obj = _op("wm", "obj_import") or _op("import_scene", "obj")
    if obj:
        table[".obj"] = (obj, {})
    fbx = _op("import_scene", "fbx")
    if fbx:
        table[".fbx"] = (fbx, {})
    stl = _op("wm", "stl_import") or _op("import_mesh", "stl")
    if stl:
        table[".stl"] = (stl, {})
    ply = _op("wm", "ply_import") or _op("import_mesh", "ply")
    if ply:
        table[".ply"] = (ply, {})
    usd = _op("wm", "usd_import")
    if usd and getattr(bpy.app.build_options, "usd", True):
        for ext in (".usd", ".usda", ".usdc", ".usdz"):
            table[ext] = (usd, {"import_cameras": False, "import_lights": False})
    return table


def importable_extensions():
    """Model extensions this Blender build can import, e.g. ``['.fbx', '.glb', ...]``."""
    return sorted(_importers())


def import_model(path):
    """Import a model file by extension and return every object it created.

    Raises :class:`ModelImportError` for a missing file, an extension without
    an importer in this build, an importer error, or a file that produced no
    geometry. The caller decides what to keep (``obj.type``).
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ModelImportError(f"model not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    importers = _importers()
    if ext not in importers:
        raise ModelImportError(f"unsupported model extension {ext!r}; this Blender imports "
                               f"{', '.join(sorted(importers))}")
    op, extra = importers[ext]
    before = set(bpy.data.objects)
    try:
        result = op(filepath=path, **extra)
    except (RuntimeError, TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[-1]
        raise ModelImportError(f"{os.path.basename(path)}: the {ext} importer failed: {first}") from exc
    if isinstance(result, set) and "FINISHED" not in result:
        raise ModelImportError(f"{os.path.basename(path)}: the {ext} importer returned {sorted(result)}")
    new_objects = [o for o in bpy.data.objects if o not in before]
    if not any(o.type in GEOMETRY_TYPES for o in new_objects):
        raise ModelImportError(f"{os.path.basename(path)}: no geometry found in the file")
    return new_objects


def top_level(objects):
    """The root ancestors of ``objects`` (each once, in order)."""
    tops = []
    for obj in objects:
        top = obj
        while top.parent is not None:
            top = top.parent
        if top not in tops:
            tops.append(top)
    return tops


def normalize_objects(objects, target_size=2.0, name="ProductPivot"):
    """Centre the model on the origin, scale it into ``target_size`` and put it on Z=0.

    Everything is parented under one empty (keeping world transforms), so a
    single uniform transform moves the whole model. All measurements come from
    the evaluated vertices: object-aligned ``bound_box`` corners of a rotated
    object overestimate its extent and made imports float above the floor.
    Returns the pivot empty. Objects are tagged as QA subjects.
    """
    pivot = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(pivot)
    for top in top_level(objects):
        parent_keep_transform(top, pivot)
    bpy.context.view_layer.update()

    lo, hi, _, _ = mesh_world_bounds(objects)
    size = max(hi - lo)
    scale = target_size / size if size > 1e-9 else 1.0
    center = (lo + hi) / 2.0
    pivot.scale = (scale, scale, scale)
    pivot.location = Vector((-center.x * scale, -center.y * scale, -lo.z * scale))
    bpy.context.view_layer.update()
    for obj in objects:
        tag_role(obj, "subject")
    return pivot


# Key: bright, front-left, high. Fill: soft, front-right, low. Rim: behind.
# Offsets are in units of the model radius ``d`` for a camera at azimuth 0
# (on -Y, looking towards +Y); (offset, energy W, size).
RIG = {
    "Key": ((-3.0, -3.0, 4.0), 1200.0, 4.0),
    "Fill": ((4.0, -2.0, 2.0), 350.0, 6.0),
    "Rim": ((0.5, 4.0, 3.5), 900.0, 3.0),
}


def three_point_rig(center, d, azimuth_deg=0.0, energy_scale=1.0):
    """Create the key / fill / rim area lights around ``center`` and aim them at it.

    ``d`` scales distances and light sizes (the model radius, at least 1).
    ``azimuth_deg`` turns the whole rig about Z, so it can follow a camera
    placed with :func:`lib.common.direction_from_angles` at that azimuth.
    Returns ``(key, fill, rim)``.
    """
    rot = Matrix.Rotation(math.radians(azimuth_deg), 3, "Z")
    center = Vector(center)
    lights = []
    for name, (offset, energy, size) in RIG.items():
        location = center + (rot @ Vector(offset)) * d if azimuth_deg else center + Vector(offset) * d
        light = new_area_light(name, location, energy=energy * energy_scale, size=size * d)
        look_at(light, center)
        lights.append(light)
    return tuple(lights)
