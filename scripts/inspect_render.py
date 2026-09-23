"""Audit any .blend (and optionally a rendered image) without looking at it.

Opens a .blend, measures the active camera's framing over one or more frames
and, when given, the pixels of a rendered image, then prints a pass / warn /
fail summary and writes the full JSON report. Designed for CI jobs, cron
scripts and AI agents that cannot see images: every conclusion is a number
with a threshold (see ``lib/qa.py`` and ``lib/core.py``).

Checks: an active camera exists; subjects and labels are fully inside the
frame (not cropped, not outside); nothing is behind the camera; grounded
objects rest on the ground; the subjects cover a sensible share of the frame;
labels do not overlap or hide behind other objects; the image is not black,
flat or empty and is not badly clipped.

Run headless (``--python-exit-code 1`` makes Python errors fail the process):

    blender --background --factory-startup --python-exit-code 1 \\
        --python scripts/inspect_render.py -- --input scene.blend --image out/scene.png --report out/scene.qa.json

Arguments (after the "--" separator):
    --input PATH      The .blend to inspect (required).
    --image PATH      Rendered image to analyse (repeatable).
    --render PATH     Render the current frame to PATH first, then analyse it.
    --frames LIST     Frames to check, e.g. "1,30,60" or "1-60" (default: current frame).
    --ground-z Z      Declare the ground height instead of detecting it.
    --no-ground-check Do not check ground contact.
    --res WxH         Override the resolution before measuring / rendering.
    --samples N       Override samples for --render.
    --report PATH     Write the JSON report here (default: print only the summary).
    --json            Print the full JSON report to stdout.
    --qa-strict       Exit with code 3 when any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import qa  # noqa: E402
from lib.common import (  # noqa: E402
    get_argv_after_dashes,
    positive_int,
    render_still,
    resolution_type,
    set_samples,
)
from lib.core import parse_frames  # noqa: E402


def frames_type(text):
    """``argparse`` wrapper around :func:`lib.core.parse_frames`."""
    try:
        return parse_frames(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="inspect_render.py", description="Audit a .blend and its render.")
    parser.add_argument("--input", required=True, help="The .blend to inspect.")
    parser.add_argument("--image", action="append", default=[], help="Rendered image to analyse.")
    parser.add_argument("--render", default=None, help="Render the current frame to this path first.")
    parser.add_argument("--frames", type=frames_type, default=None, help='Frames to check: "1,30" or "1-60:10".')
    parser.add_argument("--ground-z", type=float, default=None, help="Declared ground height.")
    parser.add_argument("--no-ground-check", action="store_true", help="Skip the ground-contact check.")
    parser.add_argument("--res", type=resolution_type, default=None, help="Override resolution WxH.")
    parser.add_argument("--samples", type=positive_int, default=None, help="Override samples for --render.")
    parser.add_argument("--report", default=None, help="Write the JSON report here.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("--qa-strict", action="store_true", help="Exit 3 when any check fails.")
    return parser.parse_args(get_argv_after_dashes() if argv is None else argv)


def main(argv=None):
    args = parse_args(argv)
    path = os.path.abspath(args.input)
    if not os.path.exists(path):
        raise SystemExit(f"Input .blend not found: {path}")
    bpy.ops.wm.open_mainfile(filepath=path)
    scene = bpy.context.scene
    if args.res:
        scene.render.resolution_x, scene.render.resolution_y = args.res
        scene.render.resolution_percentage = 100
    if args.samples:
        set_samples(scene, args.samples)
    if args.no_ground_check:
        for obj in scene.objects:
            obj["qa_grounded"] = False

    images = list(args.image)
    if args.render:
        if scene.camera is None:
            print("[inspect] no active camera; skipping --render")
        else:
            images.append(render_still(scene, args.render))
            print(f"[inspect] rendered {images[-1]}")

    report = qa.build_scene_report(scene, script=f"inspect:{os.path.basename(path)}", images=images,
                                   frames=args.frames, ground_z=args.ground_z)
    print(qa.format_text(report))
    if args.report:
        print(f"[inspect] report -> {qa.write_report(report, args.report)}")
    if args.json:
        print(json.dumps(report, indent=2))
    sys.stdout.flush()
    if args.qa_strict and not report["passed"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
