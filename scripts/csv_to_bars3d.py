"""Turn a CSV into an honest 3D bar chart, EEVEE-first.

Reads a CSV with a header row, picks a label column and a value column, and
builds one bar per row whose height is *proportional to the value* on a zero
baseline (a value twice as large is exactly twice as tall; negative values
hang below a zero-axis plane). A back panel carries gridlines at round tick
values with their numbers, category labels stand in front of the bars and
value labels above them, all turned to face the camera. The camera is fitted
to bars, labels and title for the render's aspect ratio. Optionally animates
a staggered, eased grow-in and renders it to PNG frames or an MP4.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/csv_to_bars3d.py -- --csv data/sales.csv --title "2026 Sales" --render
    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/csv_to_bars3d.py -- --csv data/sales.csv --animate --render --video mp4

Arguments (after the "--" separator):
    --csv PATH         CSV file with a header row (default: the repo's data/sales.csv).
    --label-col COL    Label column, by header name or 0-based index (default: first).
    --value-col COL    Value column, by name or index (default: first numeric column).
    --delimiter CHAR   Field delimiter (default: sniffed among , ; TAB |).
    --strict-csv       Exit with code 2 if any row had to be skipped.
    --sort ORDER       none (default), asc or desc.
    --title TEXT       Chart title (default "Sales").
    --render           Render to --out.
    --out PATH         Output image path (default out/bars3d.png).
    --animate          Keyframe a staggered grow-in animation.
    --frames N         Animation length in frames (default 72).
    --fps N            Frame rate (default 30).
    --video KIND       With --animate --render: encode mp4 / mkv / webm instead of PNG frames.
    --save-blend PATH  Also save the chart scene as a .blend.
    --res WxH          Render resolution (default 1600x900).
    --samples N        Render samples (default 96).
    --engine NAME      eevee (default) or cycles.
    --device NAME      gpu (default, falls back to CPU) or cpu. Cycles only.
    --report PATH      Write a JSON QA report (framing, label overlap, exposure).
    --qa-strict        Exit with code 3 if any QA check fails.
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy
from mathutils import Vector

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from lib.common import (  # noqa: E402
    action_fcurves,
    add_box,
    add_render_args,
    apply_render_settings,
    clean_default_scene,
    direction_from_angles,
    ensure_dir,
    frame_objects,
    get_argv_after_dashes,
    look_at,
    new_area_light,
    new_camera,
    new_world,
    ramp_color,
    render_still,
    run_qa,
    set_image_output,
    set_video_output,
    simple_material,
    tag_role,
)
from lib.core import (  # noqa: E402
    CsvError,
    chart_height_for,
    axis_ticks,
    chart_layout,
    format_value,
    grow_schedule,
    read_label_value_csv,
    swap_extension,
)

BAR_WIDTH = 0.8
BAR_DEPTH = 0.8
BAR_GAP = 0.5
MAX_BAR_HEIGHT = 6.0
CAMERA_AZIMUTH = -14.0   # degrees; slight three-quarter view
CAMERA_ELEVATION = 16.0  # degrees above the horizon


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="csv_to_bars3d.py",
        description="Build a 3D bar chart from a CSV.",
    )
    parser.add_argument("--csv", default=os.path.join(REPO_ROOT, "data", "sales.csv"),
                        help="CSV path (default: the bundled data/sales.csv).")
    parser.add_argument("--label-col", default=None, help="Label column (name or 0-based index).")
    parser.add_argument("--value-col", default=None, help="Value column (name or 0-based index).")
    parser.add_argument("--delimiter", default=None, help="Field delimiter (default: sniffed).")
    parser.add_argument("--strict-csv", action="store_true", help="Fail if any row is skipped.")
    parser.add_argument("--sort", choices=("none", "asc", "desc"), default="none", help="Sort bars by value.")
    parser.add_argument("--title", default="Sales", help="Chart title.")
    parser.add_argument("--render", action="store_true", help="Render the chart.")
    parser.add_argument("--out", default=os.path.join("out", "bars3d.png"), help="Output image path.")
    parser.add_argument("--animate", action="store_true", help="Animate a staggered grow-in.")
    parser.add_argument("--frames", type=int, default=72, help="Animation length in frames.")
    parser.add_argument("--fps", type=int, default=30, help="Frame rate.")
    parser.add_argument("--video", choices=("mp4", "mkv", "webm"), default=None,
                        help="With --animate --render: encode a video instead of PNG frames.")
    parser.add_argument("--save-blend", default=None, help="Save the chart scene to this .blend.")
    add_render_args(parser, res="1600x900", samples=96)
    args = parser.parse_args(get_argv_after_dashes() if argv is None else argv)
    if args.video and not args.animate:
        parser.error("--video needs --animate")
    if args.animate and args.frames < 8:
        parser.error("--frames must be >= 8 for --animate")
    if args.video and (args.res[0] % 2 or args.res[1] % 2):
        parser.error("--video needs an even width and height")
    return args


def make_text(name, body, size, material, facing, align_x="CENTER", align_y="CENTER"):
    """Extruded text whose front faces the camera direction ``facing``."""
    curve = bpy.data.curves.new(name=name, type="FONT")
    curve.body = body
    curve.size = size
    curve.extrude = size * 0.06
    curve.align_x = align_x
    curve.align_y = align_y
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(material)
    obj.rotation_euler = facing.to_track_quat("Z", "Y").to_euler()
    return obj


def fit_width(obj, max_width):
    """Shrink a text object uniformly so it is at most ``max_width`` wide."""
    bpy.context.view_layer.update()
    width = obj.dimensions.x
    if width > max_width > 0:
        k = max_width / width
        obj.scale = (obj.scale[0] * k, obj.scale[1] * k, obj.scale[2] * k)
    return obj


def build_chart(rows, title):
    """Build the chart. Returns ``(bars, value_labels, annotations, camera)``."""
    scene = bpy.context.scene
    chart_h = chart_height_for(len(rows), BAR_WIDTH, BAR_GAP, hi=MAX_BAR_HEIGHT)
    layout, scale, total_width = chart_layout(rows, chart_h, BAR_WIDTH, BAR_GAP)
    values = [b.value for b in layout]
    vmin, vmax = min(values), max(values)
    has_negative = vmin < 0
    step = BAR_WIDTH + BAR_GAP
    facing = direction_from_angles(CAMERA_AZIMUTH, CAMERA_ELEVATION)
    flat_front = Vector((0.0, -1.0, 0.0))

    text_mat = simple_material("ChartText", (0.86, 0.86, 0.9), roughness=0.4)
    muted_mat = simple_material("ChartMuted", (0.45, 0.47, 0.52), roughness=0.6)
    grid_mat = simple_material("ChartGrid", (0.16, 0.17, 0.19), roughness=0.8)
    panel_mat = simple_material("ChartPanel", (0.035, 0.037, 0.045), roughness=0.9)

    ticks = axis_ticks(vmin, vmax, target=5)
    bottom = min(min(b.bottom_z for b in layout), ticks[0] * scale)
    floor_z = 0.0 if not has_negative else bottom - 0.6
    scene["qa_ground_z"] = floor_z
    bpy.ops.mesh.primitive_plane_add(size=max(total_width * 3.0, 30.0), location=(0.0, 0.0, floor_z))
    floor = bpy.context.active_object
    floor.name = "ChartFloor"
    floor.data.materials.append(simple_material("ChartFloor", (0.07, 0.07, 0.08), roughness=0.55))
    tag_role(floor, "backdrop")

    # Back panel with gridlines at round tick values.
    tick_gap = (ticks[1] - ticks[0]) * scale if len(ticks) > 1 else 1.0
    tick_size = max(0.14, min(0.3, 0.45 * tick_gap))
    top_tick = max(ticks[-1] * scale, max(b.top_z for b in layout))
    panel_y = BAR_DEPTH / 2.0 + 0.35
    panel_w = total_width + 2.4
    panel = add_box("ChartPanel", panel_w, 0.1, top_tick + 0.5 - floor_z,
                    location=(0.0, panel_y + 0.05), base_z=floor_z, material=panel_mat)
    tag_role(panel, "backdrop")
    annotations = []
    for tick in ticks:
        z = tick * scale
        line = add_box(f"Grid_{format_value(tick)}", panel_w - 0.8, 0.02, 0.025,
                       location=(0.4, panel_y - 0.01), base_z=z - 0.0125, material=grid_mat)
        tag_role(line, "backdrop")
        # Tick numbers lie flat on the panel face so they can never sink into it.
        # Numbers sit just above their gridline (below it for negative ticks).
        label = make_text(f"Tick_{format_value(tick)}", format_value(tick), tick_size, muted_mat, flat_front,
                          align_x="RIGHT", align_y="BOTTOM" if tick >= 0 else "TOP")
        label.location = (-panel_w / 2.0 + 0.75, panel_y - 0.02, z + (0.05 if tick >= 0 else -0.05))
        tag_role(label, "annotation")
        annotations.append(label)

    if has_negative:
        axis = add_box("ZeroAxis", total_width + 0.8, BAR_DEPTH + 0.8, 0.03,
                       location=(0.0, 0.0), base_z=-0.015, material=grid_mat)
        tag_role(axis, "backdrop")

    bars, value_labels = [], []
    for bar_info in layout:
        t = (bar_info.value - vmin) / ((vmax - vmin) or 1.0)
        bar = add_box(f"Bar_{bar_info.index:02d}_{bar_info.label}"[:60], BAR_WIDTH, BAR_DEPTH, bar_info.height,
                      location=(bar_info.x, 0.0), base_z=0.0,
                      material=simple_material(f"BarMat_{bar_info.index:02d}", ramp_color(t)[:3],
                                               roughness=0.3, metallic=0.1))
        if bar_info.height >= 0:
            tag_role(bar, "subject", grounded=True, ground_z=0.0)
        else:
            tag_role(bar, "subject", grounded=False)
        bars.append(bar)

        cat = make_text(f"Label_{bar_info.index:02d}", bar_info.label, 0.36, text_mat, facing)
        fit_width(cat, step * 0.95)
        cat.location = (bar_info.x, -(BAR_DEPTH / 2.0 + 0.9), 0.24)
        tag_role(cat, "annotation")
        annotations.append(cat)

        val = make_text(f"Value_{bar_info.index:02d}", format_value(bar_info.value), 0.3, text_mat, facing)
        fit_width(val, step * 0.95)
        if bar_info.height >= 0:
            val.location = (bar_info.x, 0.0, bar_info.height + 0.32)
        else:  # in front of the bar's lower end, so the bar cannot hide it
            val.location = (bar_info.x, -(BAR_DEPTH / 2.0 + 0.2), bar_info.height - 0.3)
        tag_role(val, "annotation")
        value_labels.append(val)
        annotations.append(val)

    title_obj = make_text("Title", title, 0.8, text_mat, facing)
    fit_width(title_obj, panel_w)
    title_obj.location = (0.0, panel_y - 0.1, top_tick + 1.2)
    tag_role(title_obj, "annotation")
    annotations.append(title_obj)

    new_world("ChartWorld", color=(0.02, 0.02, 0.025), strength=1.0)
    center = Vector((0.0, 0.0, chart_h * 0.4))
    reach = max(total_width, 2.0 * chart_h)
    key = new_area_light("Key", (-reach * 0.6, -reach, chart_h * 2.2 + 2.0),
                         energy=180.0 * reach, size=reach)
    fill = new_area_light("Fill", (reach * 0.8, -reach * 0.6, chart_h + 1.0),
                          energy=60.0 * reach, size=reach * 1.4)
    for light in (key, fill):
        look_at(light, center)

    cam = new_camera(name="ChartCamera", lens=50.0)
    frame_objects(cam, bars + annotations, direction=facing, margin=1.05)
    return bars, value_labels, annotations, cam


def animate_growth(bars, value_labels, frames):
    """Staggered, eased grow-in: bars scale up from the floor, then values pop in."""
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = frames
    grow_frames = max(int(frames * 0.75), 4)
    for (start, end), bar, label in zip(grow_schedule(len(bars), grow_frames, stagger=0.4), bars, value_labels):
        bar.scale = (1.0, 1.0, 0.0)
        bar.keyframe_insert("scale", index=2, frame=start)
        bar.scale = (1.0, 1.0, 1.0)
        bar.keyframe_insert("scale", index=2, frame=end)
        for fcurve in action_fcurves(bar):
            first = fcurve.keyframe_points[0]
            first.interpolation = "CUBIC"
            first.easing = "EASE_OUT"
        pop_start = max(end - 3, start)
        final_scale = tuple(label.scale)
        label.scale = (0.0, 0.0, 0.0)
        label.keyframe_insert("scale", frame=pop_start)
        label.scale = final_scale
        label.keyframe_insert("scale", frame=end + 3)
    scene.frame_set(frames)


def main(argv=None):
    args = parse_args(argv)
    csv_path = os.path.abspath(args.csv)
    if not os.path.exists(csv_path):
        raise SystemExit(f"CSV not found: {csv_path}")
    try:
        data = read_label_value_csv(csv_path, args.label_col, args.value_col, args.delimiter)
    except CsvError as exc:
        raise SystemExit(f"[bars3d] {exc}") from None
    for line_no, reason in data.skipped:
        print(f"[bars3d] skipped line {line_no}: {reason}")
    if data.skipped and args.strict_csv:
        print(f"[bars3d] {len(data.skipped)} row(s) skipped and --strict-csv is set")
        raise SystemExit(2)
    rows = data.rows
    if args.sort != "none":
        rows = sorted(rows, key=lambda r: r[1], reverse=args.sort == "desc")

    clean_default_scene()
    scene = bpy.context.scene
    engine = apply_render_settings(scene, args)
    scene.render.fps = args.fps
    bars, value_labels, _, _ = build_chart(rows, args.title)
    print(f"[bars3d] built {len(rows)} bars from {csv_path} "
          f"(label={data.label_col!r}, value={data.value_col!r}, {len(data.skipped)} skipped)")
    if args.animate:
        animate_growth(bars, value_labels, args.frames)

    if args.save_blend:
        path = os.path.abspath(args.save_blend)
        ensure_dir(os.path.dirname(path))
        bpy.ops.wm.save_as_mainfile(filepath=path)
        print(f"[bars3d] saved scene -> {path}")

    images = []
    if args.render:
        out_path = os.path.abspath(args.out)
        ensure_dir(os.path.dirname(out_path))
        if not args.animate:
            print(f"[bars3d] rendering with {engine} -> {out_path}")
            images.append(render_still(scene, out_path))
            print(f"[bars3d] wrote {out_path}")
        elif args.video:
            ext = set_video_output(scene, args.video)
            video_path = swap_extension(out_path, ext)
            scene.render.use_file_extension = False
            scene.render.filepath = video_path
            print(f"[bars3d] rendering {args.frames} frames with {engine} -> {video_path}")
            bpy.ops.render.render(animation=True)
            print(f"[bars3d] wrote {video_path}")
            images.append(video_path)
        else:
            stem = os.path.splitext(out_path)[0] + "_"
            set_image_output(scene.render.image_settings, "PNG")
            scene.render.use_file_extension = True
            scene.render.filepath = stem
            print(f"[bars3d] rendering {args.frames} frames with {engine} -> {stem}####.png")
            bpy.ops.render.render(animation=True)
            images.append(f"{stem}{args.frames:04d}.png")
            print(f"[bars3d] wrote {args.frames} frames")
    run_qa(args, "csv_to_bars3d", images=images,
           frames=[scene.frame_end] if args.animate else None)


if __name__ == "__main__":
    main()
