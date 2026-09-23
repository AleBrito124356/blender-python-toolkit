"""Scene hygiene for a .blend: purge, apply transforms, fix mirrored scales, rename.

Runs a set of cleanup passes over a .blend and prints a before/after report:
    - purge orphaned datablocks,
    - apply object transforms on single-user meshes,
    - bake negative (mirrored) scales into the mesh with correct normals,
    - rename objects with a type prefix (MESH_, CAM_, LGT_, ...), collision-free.

Mirrored objects whose mesh is shared by several objects cannot be baked
without changing the other users, so they are reported and left untouched
(never silently un-mirrored).

It is DRY-RUN by default: it reports what it would do and changes nothing on
disk. Pass --execute to perform the passes; the result is written to a
"<name>_clean.blend" sibling and never overwrites the input.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/cleanup_scene.py -- --input messy.blend
    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/cleanup_scene.py -- --input messy.blend --execute

Arguments (after the "--" separator):
    --input PATH   The .blend to inspect / clean (required).
    --execute      Actually apply changes and save "<name>_clean.blend".
    --output PATH  Explicit output path for --execute (must not exist).
    --prefix       Rename objects with a per-type prefix (default on).
    --no-prefix    Skip the renaming pass.
    --json PATH    Also write the report as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import get_argv_after_dashes  # noqa: E402
from lib.core import TYPE_PREFIX, plan_prefix_renames  # noqa: E402,F401  (TYPE_PREFIX re-exported)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="cleanup_scene.py",
        description="Report and optionally apply scene-hygiene passes on a .blend.",
    )
    parser.add_argument("--input", required=True, help="Input .blend file.")
    parser.add_argument("--execute", action="store_true", help="Apply changes and save.")
    parser.add_argument("--output", default=None, help="Output path for --execute (must not exist).")
    parser.add_argument("--json", default=None, help="Also write the report as JSON.")
    parser.add_argument("--prefix", dest="prefix", action="store_true", default=True,
                        help="Rename objects with a type prefix (default on).")
    parser.add_argument("--no-prefix", dest="prefix", action="store_false",
                        help="Skip the renaming pass.")
    return parser.parse_args(get_argv_after_dashes() if argv is None else argv)


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
    """Split mirrored mesh objects into ``(fixable, shared)`` lists.

    ``fixable`` objects own their mesh, so the mirror can be baked in.
    ``shared`` objects share their mesh with other objects: baking would
    also flip the other users, so they are only reported.
    """
    fixable, shared = [], []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if any(component < 0.0 for component in obj.scale):
            (shared if obj.data.users > 1 else fixable).append(obj)
    return fixable, shared


def plan_renames(do_prefix):
    """Return a list of (object, old_name, new_name) rename operations."""
    if not do_prefix:
        return []
    objects = {obj.name: obj for obj in bpy.data.objects}
    plan = plan_prefix_renames([(obj.name, obj.type) for obj in bpy.data.objects])
    return [(objects[old], old, new) for old, new in plan]


def _apply(obj, location, rotation, scale):
    """Run transform_apply on exactly ``obj``. Returns True on success."""
    view_layer = bpy.context.view_layer
    if obj.name not in view_layer.objects:
        return False
    for other in view_layer.objects:
        other.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    try:
        bpy.ops.object.transform_apply(location=location, rotation=rotation, scale=scale)
    except RuntimeError as exc:
        print(f"[cleanup] could not apply transforms on {obj.name!r}: {exc}")
        return False
    return True


def apply_transforms():
    """Apply location/rotation/scale on every single-user mesh. Returns the count."""
    applied = 0
    for obj in list(bpy.data.objects):
        if obj.type != "MESH" or obj.data is None or obj.data.users > 1:
            continue  # multi-user data: applying would desync the other users
        if _apply(obj, True, True, True):
            applied += 1
    return applied


def fix_negative_scales(objects):
    """Bake mirrored (negative) scales into single-user meshes.

    ``transform_apply`` already flips the face winding when the determinant
    of the applied matrix is negative, so the normals stay outward. (The
    previous version flipped them a second time, which turned every mirrored
    mesh inside out.) Returns the number of objects fixed.
    """
    fixed = 0
    for obj in objects:
        if obj.data is None or obj.data.users > 1:
            continue
        if _apply(obj, False, False, True):
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


def print_report(title, before, after, renames, negatives, applied, purged, shared):
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
    print(f"mirrored, shared mesh   : {len(shared)} (left as is)")
    print(f"orphan blocks purged    : {purged}")
    print(f"objects renamed         : {len(renames)}")
    for obj in shared:
        print(f"  ! {obj.name}: scale {tuple(round(v, 4) for v in obj.scale)} shares mesh "
              f"{obj.data.name!r} with {obj.data.users - 1} other object(s); make it single-user to fix")
    if renames:
        print(line)
        for _, old, new in renames[:40]:
            print(f"  {old}  ->  {new}")
        if len(renames) > 40:
            print(f"  ... and {len(renames) - 40} more")
    print(line)


def main(argv=None):
    args = parse_args(argv)
    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        raise SystemExit(f"Input .blend not found: {input_path}")

    bpy.ops.wm.open_mainfile(filepath=input_path)
    before = count_datablocks()
    fixable, shared = find_negative_scales()
    renames = plan_renames(args.prefix)
    report = {
        "input": input_path,
        "execute": bool(args.execute),
        "before": before,
        "mirrored_fixable": [o.name for o in fixable],
        "mirrored_shared": [o.name for o in shared],
        "renames": [[old, new] for _, old, new in renames],
    }

    if not args.execute:
        print_report(
            f"DRY-RUN report for {os.path.basename(input_path)} (no changes written)",
            before, before, renames, len(fixable), 0, 0, shared,
        )
        print("Re-run with --execute to apply these passes.")
        report.update(after=before, output=None)
    else:
        # Bake mirrors first so the apply-all pass sees corrected geometry.
        fixed = fix_negative_scales(fixable)
        applied = apply_transforms()
        for obj, _old, new in renames:
            obj.name = new
        purged = purge_orphans()
        after = count_datablocks()
        print_report(f"CLEANUP report for {os.path.basename(input_path)}",
                     before, after, renames, fixed, applied, purged, shared)
        if args.output:
            out_path = os.path.abspath(args.output)
            if os.path.exists(out_path):
                raise SystemExit(f"Refusing to overwrite existing file: {out_path}")
        else:
            out_path = unique_output_path(input_path)
        bpy.ops.wm.save_as_mainfile(filepath=out_path)
        print(f"[cleanup] wrote cleaned file -> {out_path}")
        print(f"[cleanup] original untouched -> {input_path}")
        report.update(after=after, output=out_path, fixed=fixed, applied=applied, purged=purged)

    if args.json:
        path = os.path.abspath(args.json)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"[cleanup] JSON report -> {path}")


if __name__ == "__main__":
    main()
