"""Turn a two-column CSV into a 3D bar-chart scene, EEVEE-first.

Reads a CSV with a header row and columns ``label,value``, builds one 3D bar
per row (height mapped to value, color mapped along a blue-to-amber ramp),
adds extruded text labels under each bar and value numbers on top, a title,
a camera framed on the chart and studio lighting, then optionally renders.

Run headless:

    blender --background --python scripts/csv_to_bars3d.py -- --csv data/sales.csv --render
    blender --background --python scripts/csv_to_bars3d.py -- --csv data/sales.csv --title "2026 Revenue" --render

Arguments (after the "--" separator):
    --csv PATH     CSV file with a header then label,value rows (default data/sales.csv).
    --title TEXT   Chart title (default "Sales").
    --render       Render a still to the output path.
    --out PATH     Output image path (default out/bars3d.png).
    --res WxH      Render resolution (default 1600x900).
    --samples N    EEVEE render samples (default 96).
    --cycles       Use Cycles instead of EEVEE.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.common import (  # noqa: E402
    clean_default_scene,
    ensure_dir,
    frame_object,
    look_at,
    new_area_light,
    new_camera,
    ramp_color,
    set_engine,
    set_samples,
    set_view_transform,
    get_argv_after_dashes,
)


BAR_WIDTH = 0.8
BAR_DEPTH = 0.8
BAR_GAP = 0.5
MAX_BAR_HEIGHT = 6.0


def parse_args():
    parser = argparse.ArgumentParser(
        prog="csv_to_bars3d.py",
        description="Build a 3D bar chart from a label,value CSV.",
    )
    parser.add_argument("--csv", default=os.path.join("data", "sales.csv"), help="CSV path.")
    parser.add_argument("--title", default="Sales", help="Chart title.")
    parser.add_argument("--render", action="store_true", help="Render a still.")
    parser.add_argument("--out", default=os.path.join("out", "bars3d.png"), help="Output image path.")
    parser.add_argument("--res", default="1600x900", help="Render resolution WxH.")
    parser.add_argument("--samples", type=int, default=96, help="Render samples.")
    parser.add_argument("--cycles", action="store_true", help="Use Cycles instead of EEVEE.")
    return parser.parse_args(get_argv_after_dashes())


def read_csv(path):
    """Read a label,value CSV into a list of (label, float) pairs."""
    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            raise SystemExit(f"CSV {path!r} is empty.")
        for row in reader:
            if len(row) < 2 or not row[0].strip():
                continue
            try:
                value = float(row[1])
            except ValueError:
                continue
            rows.append((row[0].strip(), value))
    if not rows:
        raise SystemExit(f"No usable label,value rows found in {path!r}.")
    return rows


def make_bar_material(name, t):
    """Emission-tinted PBR material colored by normalized value ``t``."""
    color = ramp_color(t)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = color
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.3
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.1
    return mat


def make_text(name, body, size=0.5, extrude=0.03, align_x="CENTER"):
    """Create an extruded 3D text object and return it."""
    curve = bpy.data.curves.new(name=name, type="FONT")
    curve.body = body
    curve.size = size
    curve.extrude = extrude
    curve.align_x = align_x
    curve.align_y = "CENTER"
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    text_mat = bpy.data.materials.new(f"{name}_mat")
    text_mat.use_nodes = True
    bsdf = text_mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.85, 0.85, 0.88, 1.0)
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.4
    obj.data.materials.append(text_mat)
    return obj


def build_chart(rows, title):
    """Build bars, labels, title, floor, lighting and camera. Returns camera."""
    values = [v for _, v in rows]
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0

    n = len(rows)
    step = BAR_WIDTH + BAR_GAP
    total_width = n * step - BAR_GAP
    x0 = -total_width / 2.0 + BAR_WIDTH / 2.0

    # Floor.
    bpy.ops.mesh.primitive_plane_add(size=max(total_width * 2.0, 20.0), location=(0.0, 0.0, 0.0))
    floor = bpy.context.active_object
    floor.name = "ChartFloor"
    floor_mat = bpy.data.materials.new("ChartFloor")
    floor_mat.use_nodes = True
    fbsdf = floor_mat.node_tree.nodes.get("Principled BSDF")
    if fbsdf is not None:
        fbsdf.inputs["Base Color"].default_value = (0.08, 0.08, 0.09, 1.0)
        if "Roughness" in fbsdf.inputs:
            fbsdf.inputs["Roughness"].default_value = 0.55
    floor.data.materials.append(floor_mat)

    bars = []
    for i, (label, value) in enumerate(rows):
        t = (value - vmin) / span
        height = 0.4 + (value - vmin) / span * (MAX_BAR_HEIGHT - 0.4)
        x = x0 + i * step

        bpy.ops.mesh.primitive_cube_add(size=1.0)
        bar = bpy.context.active_object
        bar.name = f"Bar_{i:02d}_{label}"
        bar.scale = (BAR_WIDTH / 2.0, BAR_DEPTH / 2.0, height / 2.0)
        bar.location = (x, 0.0, height / 2.0)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bar.data.materials.append(make_bar_material(f"BarMat_{i:02d}", t))
        bars.append(bar)

        # Category label under the bar, lying flat and facing the camera.
        label_obj = make_text(f"Label_{i:02d}", label, size=0.42)
        label_obj.rotation_euler = (1.5708, 0.0, 0.0)
        label_obj.location = (x, BAR_DEPTH / 2.0 + 0.35, 0.28)

        # Value number floating just above the bar.
        value_text = f"{value:g}"
        value_obj = make_text(f"Value_{i:02d}", value_text, size=0.34)
        value_obj.rotation_euler = (1.5708, 0.0, 0.0)
        value_obj.location = (x, 0.0, height + 0.4)

    # Title text behind the bars.
    title_obj = make_text("Title", title, size=1.0, extrude=0.05)
    title_obj.rotation_euler = (1.5708, 0.0, 0.0)
    title_obj.location = (0.0, -BAR_DEPTH / 2.0 - 0.6, MAX_BAR_HEIGHT + 1.4)

    # World and lighting.
    world = bpy.data.worlds.new("ChartWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.02, 0.02, 0.025, 1.0)
        bg.inputs["Strength"].default_value = 1.0
    bpy.context.scene.world = world

    key = new_area_light("Key", (-total_width, -total_width, MAX_BAR_HEIGHT * 2.2), energy=2000.0, size=total_width)
    fill = new_area_light("Fill", (total_width, -total_width * 0.6, MAX_BAR_HEIGHT), energy=700.0, size=total_width * 1.4)
    for light in (key, fill):
        look_at(light, Vector((0.0, 0.0, MAX_BAR_HEIGHT * 0.4)))

    # Camera framed on the bars.
    cam = new_camera(name="ChartCamera", lens=50.0)
    cam.location = Vector((0.0, -total_width * 1.4, MAX_BAR_HEIGHT * 1.1))
    look_at(cam, Vector((0.0, 0.0, MAX_BAR_HEIGHT * 0.4)))
    frame_object(cam, bars + [title_obj], margin=1.25)
    look_at(cam, Vector((0.0, 0.0, MAX_BAR_HEIGHT * 0.45)))
    return cam


def render(args):
    scene = bpy.context.scene
    engine = set_engine(scene, "cycles" if args.cycles else "eevee")
    set_samples(scene, args.samples)
    set_view_transform(scene)

    try:
        width, height = (int(v) for v in args.res.lower().split("x"))
    except ValueError:
        raise SystemExit(f"--res must look like 1600x900, got {args.res!r}")
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"

    out_path = os.path.abspath(args.out)
    ensure_dir(os.path.dirname(out_path))
    scene.render.filepath = out_path
    print(f"[bars3d] rendering with {engine} -> {out_path}")
    bpy.ops.render.render(write_still=True)
    print(f"[bars3d] wrote {out_path}")


def main():
    args = parse_args()
    csv_path = os.path.abspath(args.csv)
    if not os.path.exists(csv_path):
        raise SystemExit(f"CSV not found: {csv_path}")

    rows = read_csv(csv_path)
    clean_default_scene()
    build_chart(rows, args.title)
    print(f"[bars3d] built {len(rows)} bars from {csv_path}")
    if args.render:
        render(args)


if __name__ == "__main__":
    main()
