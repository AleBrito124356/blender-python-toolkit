"""Scene hygiene for a .blend: purge, apply transforms, fix scales, rename.

Runs a set of cleanup passes over a .blend and prints a before/after report:
    - purge orphaned datablocks,
    - apply object transforms on meshes,
    - fix negative (mirrored) scales and recalculate normals,
    - rename objects with a type prefix (MESH_, CAM_, LGT_, ...).

It is DRY-RUN by default: it reports what it would do and changes nothing on
disk. Pass --execute to actually perform the passes; the result is written to
a "<name>_clean.blend" sibling and never overwrites the input.

Run headless:

    blender --background --python scripts/cleanup_scene.py -- --input messy.blend
    blender --background --python scripts/cleanup_scene.py -- --input messy.blend --execute

Arguments (after the "--" separator):
    --input PATH   The .blend to inspect / clean (required).
    --execute      Actually apply changes and save "<name>_clean.blend".
    --prefix       Rename objects with a per-type prefix.
    --no-prefix    Skip the renaming pass (renaming is on by default).
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import get_argv_after_dashes  # noqa: E402


TYPE_PREFIX = {
    "MESH": "MESH_",
    "CURVE": "CRV_",
    "SURFACE": "SRF_",
    "META": "MBALL_",
    "FONT": "TXT_",
    "CAMERA": "CAM_",
    "LIGHT": "LGT_",
    "LIGHT_PROBE": "PRB_",
    "EMPTY": "EMP_",
    "ARMATURE": "ARM_",
    "LATTICE": "LAT_",
    "GPENCIL": "GP_",
    "SPEAKER": "SPK_",
    "VOLUME": "VOL_",
}


def parse_args():
    parser = argparse.ArgumentParser(
        prog="cleanup_scene.py",
        description="Report and optionally apply scene-hygiene passes on a .blend.",
    )
    parser.add_argument("--input", required=True, help="Input .blend file.")
    parser.add_argument("--execute", action="store_true", help="Apply changes and save.")
    parser.add_argument("--prefix", dest="prefix", action="store_true", default=True,
                        help="Rename objects with a type prefix (default on).")
    parser.add_argument("--no-prefix", dest="prefix", action="store_false",
                        help="Skip the renaming pass.")
    return parser.parse_args(get_argv_after_dashes())


def unique_output_path(input_path):
    """Return a '<name>_clean.blend' path that does not already exist."""
    base, _ = os.path.splitext(os.path.abspath(input_path))
    candidate = f"{base}_clean.blend"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{base}_clean_{counter:02d}.blend"
        counter += 1
    return candidate


def count_datablocks():
    """Snapshot counts of the datablock collections most affected by cleanup."""
    return {
        "objects": len(bpy.data.objects),
        "meshes": len(bpy.data.meshes),
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "orphan_meshes": sum(1 for m in bpy.data.meshes if m.users == 0),
        "orphan_materials": sum(1 for m in bpy.data.materials if m.users == 0),
    }


def find_negative_scales():
    """Return the list of mesh objects that have at least one negative scale."""
    flagged = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if any(component < 0.0 for component in obj.scale):
            flagged.append(obj)
    return flagged


def plan_renames(do_prefix):
    """Return a list of (object, old_name, new_name) rename operations."""
    if not do_prefix:
        return []
    renames = []
    for obj in bpy.data.objects:
        prefix = TYPE_PREFIX.get(obj.type, "OBJ_")
        # Strip any known prefix already present to keep renames idempotent.
        base = obj.name
        for known in TYPE_PREFIX.values():
            if base.startswith(known):
                base = base[len(known):]
                break
        else:
            if base.startswith("OBJ_"):
                base = base[len("OBJ_"):]
        new_name = prefix + base
        if new_name != obj.name:
            renames.append((obj, obj.name, new_name))
    return renames


def apply_transforms():
    """Apply location/rotation/scale on every mesh object. Returns the count."""
    applied = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        # Skip meshes whose data is shared (multi-user) - applying would desync.
        if obj.data.users > 1:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        try:
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            applied += 1
        except RuntimeError:
            continue
    return applied


def fix_negative_scales(objects):
    """Remove negative (mirrored) scales while preserving appearance.

    Applying a negative scale bakes the mirror into the geometry and leaves the
    normals inverted, so after the apply we flip normals back on any object
    with an odd number of mirrored axes. This runs before the general
    apply-transforms pass so the scale sign is corrected exactly once.
    """
    fixed = 0
    for obj in objects:
        negatives = sum(1 for component in obj.scale if component < 0.0)
        if negatives == 0:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        try:
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        except RuntimeError:
            # Multi-user or otherwise un-applyable: fall back to a sign flip.
            obj.scale = tuple(abs(component) for component in obj.scale)
        # An odd number of mirror axes inverts winding; flip normals back.
        if negatives % 2 == 1 and obj.data is not None:
            for polygon in obj.data.polygons:
                polygon.flip()
            obj.data.update()
        fixed += 1
    return fixed


def purge_orphans():
    """Remove orphaned datablocks. Returns how many were removed."""
    before = (
        len(bpy.data.meshes) + len(bpy.data.materials)
        + len(bpy.data.images) + len(bpy.data.curves)
    )
    # bpy.data.orphans_purge exists on modern Blender and needs no context.
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()
    after = (
        len(bpy.data.meshes) + len(bpy.data.materials)
        + len(bpy.data.images) + len(bpy.data.curves)
    )
    return max(before - after, 0)


def print_report(title, before, after, renames, negatives, applied, purged):
    """Print an aligned before/after hygiene report."""
    line = "-" * 58
    print("\n" + line)
    print(title)
    print(line)
    print(f"{'metric':<22}{'before':>10}{'after':>10}")
    print(line)
    for key in before:
        print(f"{key:<22}{before[key]:>10}{after[key]:>10}")
    print(line)
    print(f"transforms applied      : {applied}")
    print(f"negative scales fixed   : {negatives}")
    print(f"orphan blocks purged    : {purged}")
    print(f"objects renamed         : {len(renames)}")
    if renames:
        print(line)
        for _, old, new in renames[:40]:
            print(f"  {old}  ->  {new}")
        if len(renames) > 40:
            print(f"  ... and {len(renames) - 40} more")
    print(line)


def main():
    args = parse_args()
    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        raise SystemExit(f"Input .blend not found: {input_path}")

    bpy.ops.wm.open_mainfile(filepath=input_path)
    before = count_datablocks()

    negative_objects = find_negative_scales()
    renames = plan_renames(args.prefix)

    if not args.execute:
        # Dry-run: report the plan without touching anything.
        after = before  # nothing changed
        print_report(
            f"DRY-RUN report for {os.path.basename(input_path)} (no changes written)",
            before, after, renames, len(negative_objects), 0, 0,
        )
        print("Re-run with --execute to apply these passes.")
        return

    # Execute passes. Fix mirrored scales first so the subsequent
    # apply-transforms pass sees already-corrected geometry.
    fixed = fix_negative_scales(negative_objects)
    applied = apply_transforms()
    for obj, _old, new in renames:
        obj.name = new
    purged = purge_orphans()

    after = count_datablocks()
    print_report(
        f"CLEANUP report for {os.path.basename(input_path)}",
        before, after, renames, fixed, applied, purged,
    )

    out_path = unique_output_path(input_path)
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    print(f"[cleanup] wrote cleaned file -> {out_path}")
    print(f"[cleanup] original untouched -> {input_path}")


if __name__ == "__main__":
    main()
