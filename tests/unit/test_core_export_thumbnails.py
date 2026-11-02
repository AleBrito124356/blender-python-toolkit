"""lib.core helpers behind bpt export / bpt thumbnails / bpt --json (no Blender needed)."""

import io
import json
import os
import struct

import pytest

from lib import core


# ---------------------------------------------------------------------------
# Synthetic files
# ---------------------------------------------------------------------------
def png_bytes(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\0\0\0"


def jpeg_bytes(width, height, sof=0xC0):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\x01\x01\0\0\x01\0\x01\0\0"
    dht = b"\xff\xc4" + struct.pack(">H", 4) + b"\0\0"  # a Huffman table must not be read as a frame
    frame = bytes([0xFF, sof]) + struct.pack(">HBHH", 17, 8, height, width) + b"\x03" + b"\x01\x22\x00" * 3
    return b"\xff\xd8" + app0 + dht + frame + b"\xff\xd9"


def webp_vp8(width, height):
    body = b"\0\0\0" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(body)) + body


def webp_vp8l(width, height):
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits)
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(body)) + body


def webp_vp8x(width, height):
    body = b"\0\0\0\0" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(body)) + body


def make_glb(doc, binary=b"", version=2):
    js = json.dumps(doc).encode("utf-8")
    js += b" " * (-len(js) % 4)
    body = struct.pack("<II", len(js), 0x4E4F534A) + js
    if binary:
        binary += b"\0" * (-len(binary) % 4)
        body += struct.pack("<II", len(binary), 0x004E4942) + binary
    return b"glTF" + struct.pack("<II", version, 12 + len(body)) + body


@pytest.mark.parametrize("data, expected", [
    (png_bytes(512, 256), ("PNG", 512, 256)),
    (jpeg_bytes(640, 480), ("JPEG", 640, 480)),
    (jpeg_bytes(33, 17, sof=0xC2), ("JPEG", 33, 17)),  # progressive
    (webp_vp8(300, 200), ("WEBP", 300, 200)),
    (webp_vp8l(1024, 1), ("WEBP", 1024, 1)),
    (webp_vp8x(4096, 2048), ("WEBP", 4096, 2048)),
    (b"GIF89a....", None),
    (b"", None),
])
def test_image_size(data, expected):
    assert core.image_size(data) == expected


# ---------------------------------------------------------------------------
# GLB reading and statistics
# ---------------------------------------------------------------------------
def sample_doc():
    accessors = [
        {"count": 24}, {"count": 36},          # 0 POSITION, 1 indices of mesh 0 (12 triangles)
        {"count": 9},                           # 2 POSITION non-indexed triangles (3)
        {"count": 4},                           # 3 POSITION lines
        {"count": 5},                           # 4 POSITION triangle strip (3)
    ]
    meshes = [
        {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]},
        {"primitives": [{"attributes": {"POSITION": 2}},
                        {"attributes": {"POSITION": 3}, "mode": 1},
                        {"attributes": {"POSITION": 4}, "mode": 5}]},
    ]
    nodes = [{"mesh": 0, "children": [1]}, {"mesh": 0}, {"mesh": 1}, {"mesh": 1}]  # node 3 is in no scene
    return {"asset": {"version": "2.0", "generator": "test"}, "scene": 0, "scenes": [{"nodes": [0, 2]}],
            "nodes": nodes, "meshes": meshes, "accessors": accessors,
            "materials": [{}], "textures": [{"source": 0}],
            "images": [{"bufferView": 0, "mimeType": "image/png", "name": "albedo"}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 33}],
            "extensionsUsed": ["KHR_draco_mesh_compression"]}


def test_gltf_stats_counts_instances_modes_and_images():
    doc, binary = core.read_glb(make_glb(sample_doc(), png_bytes(64, 32) + b"\0" * 9))
    stats = core.gltf_stats(doc, binary)
    assert stats["mesh_instances"] == 3 and stats["primitives"] == 5
    assert stats["triangles"] == 2 * 12 + (3 + 0 + 3)  # mesh 0 twice, mesh 1 once; lines count 0
    assert stats["vertices"] == 2 * 24 + 9 + 4 + 5
    assert stats["draco"] and stats["generator"] == "test" and stats["materials"] == 1
    (image,) = stats["images"]
    assert (image["name"], image["format"], image["width"], image["height"]) == ("albedo", "PNG", 64, 32)


def test_gltf_stats_without_scenes_uses_root_nodes():
    doc = sample_doc()
    del doc["scenes"], doc["scene"]
    stats = core.gltf_stats(doc)  # roots: nodes 0, 2 and 3 (1 is a child)
    assert stats["mesh_instances"] == 4 and stats["triangles"] == 24 + 6 + 6
    assert "bytes" not in stats["images"][0]  # no binary chunk given


@pytest.mark.parametrize("data, message", [
    (b"PK\x03\x04" + b"\0" * 30, "magic"),
    (make_glb({"asset": {}}, version=1), "version 1"),
    (make_glb({"asset": {}})[:-4], "truncated"),
    (b"glTF" + struct.pack("<II", 2, 20) + struct.pack("<II", 0, 0x004E4942), "no JSON"),
])
def test_read_glb_rejects_bad_files(data, message):
    with pytest.raises(core.GltfError, match=message):
        core.read_glb(data)


# ---------------------------------------------------------------------------
# Decimation, texture sizes, round trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tris, ratio, budget, expected", [
    (1000, None, None, None),
    (1000, 0.25, None, 0.25),
    (1000, 1.0, None, None),
    (1000, None, 2000, None),     # already within budget
    (1000, None, 1000, None),
    (1000, None, 250, 0.25),
])
def test_decimate_ratio(tris, ratio, budget, expected):
    assert core.decimate_ratio(tris, ratio=ratio, max_triangles=budget) == expected


@pytest.mark.parametrize("kwargs, message", [
    ({"ratio": 0.5, "max_triangles": 10}, "not both"),
    ({"ratio": 0.0}, r"\(0, 1\]"),
    ({"ratio": 1.5}, r"\(0, 1\]"),
    ({"max_triangles": 0}, ">= 1"),
])
def test_decimate_ratio_errors(kwargs, message):
    with pytest.raises(ValueError, match=message):
        core.decimate_ratio(1000, **kwargs)


def test_refine_ratio_only_shrinks_on_overshoot():
    assert core.refine_ratio(0.5, 400, 500) == 0.5
    assert core.refine_ratio(0.5, 600, 500) == pytest.approx(0.5 * 500 / 600 * 0.98)
    assert core.refine_ratio(0.5, 0, 500) == 0.5
    assert core.refine_ratio(1e-9, 10 ** 9, 1) >= 1e-6


@pytest.mark.parametrize("size, limit, expected", [
    ((512, 256), 64, (64, 32)),
    ((256, 512), 64, (32, 64)),
    ((100, 100), 256, (100, 100)),
    ((4096, 3), 1024, (1024, 1)),
    ((3000, 2000), 1000, (1000, 667)),
])
def test_fit_size(size, limit, expected):
    assert core.fit_size(*size, limit) == expected
    with pytest.raises(ValueError):
        core.fit_size(10, 10, 0)


def box(lo, hi):
    return {"min": list(lo), "max": list(hi)}


def test_roundtrip_checks_pass_and_fail():
    expected = {"triangles": 10000, "bounds": box((0, 0, 0), (1, 2, 2))}  # diagonal 3
    ok = core.roundtrip_checks(expected, {"triangles": 9995, "bounds": box((0, 0, 0.001), (1, 2, 2.002))})
    assert [c["status"] for c in ok] == ["pass", "pass"] and ok[0]["id"] == "glb_triangles"
    bad = core.roundtrip_checks(expected, {"triangles": 9000, "bounds": box((0, 0, 0), (1, 2, 2.5))},
                                label="usdz")
    assert [(c["id"], c["status"]) for c in bad] == [("usdz_triangles", "fail"), ("usdz_bounds", "fail")]
    assert bad[1]["difference"] == pytest.approx(0.5) and "re-imported" in bad[1]["message"]
    exact = core.roundtrip_checks({"triangles": 12, "bounds": box((0, 0, 0), (1, 1, 1))},
                                  {"triangles": 11, "bounds": box((0, 0, 0), (1, 1, 1))})
    assert exact[0]["status"] == "fail"  # tolerance rounds down: 0.1% of 12 triangles is 0


def test_export_exit_code_and_text():
    report = {
        "status": "ok", "input": "/x/chair.blend",
        "source": {"triangles": 100, "vertices": 60, "area": 2.0},
        "export": {"triangles": 50, "vertices": 31, "area": 1.98, "decimate_ratio": 0.5},
        "bounds": {"size": [1.0, 0.5, 0.8]}, "materials": [{}], "textures": [],
        "outputs": [{"format": "glb", "path": "/x/chair.glb", "bytes": 2048, "draco": False}],
        "verification": {"checks": [core.make_check("glb_bounds", "pass", "ok")]},
        "warnings": ["careful"],
    }
    assert core.export_exit_code(report) == core.EXIT_OK
    text = core.format_export_text(report)
    assert "100 -> 50" in text and "2.0 KB" in text and "! careful" in text and "[PASS] glb_bounds" in text
    report["verification"]["checks"].append(core.make_check("glb_draco", "fail", "not compressed"))
    assert core.export_exit_code(report) == core.EXIT_CHECK_FAILED == 3


@pytest.mark.parametrize("n, text", [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KB"), (5 * 1024 ** 2, "5.0 MB"),
                                     (3 * 1024 ** 3, "3.0 GB")])
def test_human_bytes(n, text):
    assert core.human_bytes(n) == text


# ---------------------------------------------------------------------------
# Exit codes and summaries
# ---------------------------------------------------------------------------
def test_exit_code_constants_match_the_launcher():
    from bpt import cli

    assert (core.EXIT_OK, core.EXIT_FAILED, core.EXIT_INVALID_INPUT, core.EXIT_CHECK_FAILED) == (0, 1, 2, 3)
    assert [cli.exit_status(c) for c in range(4)] == ["ok", "failed", "invalid_input", "check_failed"]
    assert core.SUMMARY_ENV == cli.SUMMARY_ENV


def test_fail_input_exits_2_with_the_message():
    stream = io.StringIO()
    with pytest.raises(SystemExit) as info:
        core.fail_input("[x] input not found: a.glb", stream=stream)
    assert info.value.code == 2 and "input not found" in stream.getvalue()


def test_write_summary_only_when_requested(tmp_path):
    assert core.write_summary({"status": "ok"}, env={}) is None
    path = tmp_path / "s.json"
    assert core.write_summary({"status": "ok", "path": tmp_path}, env={core.SUMMARY_ENV: str(path)}) == str(path)
    data = json.loads(path.read_text())
    assert data == {"tool": "blender-python-toolkit", "status": "ok", "path": str(tmp_path)}


def test_condense_report():
    report = {"passed": False, "status": "fail", "counts": {"pass": 1, "fail": 1},
              "checks": [{"id": "in_frame", "status": "fail", "message": "cropped", "objects": ["A"]},
                         {"id": "image_exposure", "status": "pass", "message": "ok", "image": "a.png", "value": 1}],
              "composition": {"big": "data"}}
    short = core.condense_report(report)
    assert short["failing"] == ["in_frame"] and "composition" not in short
    assert short["checks"][1] == {"id": "image_exposure", "status": "pass", "message": "ok", "image": "a.png"}
    assert core.condense_report(None) is None


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------
def test_find_inputs(tmp_path):
    for rel in ("b.GLB", "a.obj", "notes.txt", ".hidden.glb", "scene.blend", "scene.blend1", "sub/c.stl",
                ".git/d.glb"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    exts = core.MODEL_EXTENSIONS + core.SCENE_EXTENSIONS
    flat = [os.path.relpath(p, tmp_path) for p in core.find_inputs(str(tmp_path), exts)]
    assert flat == ["a.obj", "b.GLB", "scene.blend"]
    deep = [os.path.relpath(p, tmp_path) for p in core.find_inputs(str(tmp_path), exts, recursive=True)]
    assert deep == ["a.obj", "b.GLB", "scene.blend", os.path.join("sub", "c.stl")]


def test_thumbnail_names_are_unique_and_readable():
    paths = ["/a/chair.glb", "/a/chair.obj", "/a/lamp shade.fbx", "/a/sub/chair.glb", "/a/Table.blend"]
    names = core.thumbnail_names(paths, root="/a")
    assert names["/a/chair.glb"] == "chair_glb.png" and names["/a/chair.obj"] == "chair_obj.png"
    assert names["/a/lamp shade.fbx"] == "lamp_shade.png" and names["/a/Table.blend"] == "Table.png"
    assert names["/a/sub/chair.glb"] == "sub__chair_glb.png"
    assert len(set(names.values())) == len(paths)
    no_root = core.thumbnail_names(["/x/a.glb", "/y/a.glb"])
    assert sorted(no_root.values()) == ["a_glb.png", "a_glb_01.png"]


@pytest.mark.parametrize("text, expected", [
    ("transparent", None), ("NONE", None), ("#ffffff", (1.0, 1.0, 1.0)), ("000", (0.0, 0.0, 0.0)),
    ("white", (1.0, 1.0, 1.0)), ("#204060", (0x20 / 255, 0x40 / 255, 0x60 / 255)),
])
def test_parse_background(text, expected):
    assert core.parse_background(text) == expected


def test_parse_background_rejects_garbage():
    with pytest.raises(ValueError, match="transparent"):
        core.parse_background("#12345")


def test_contact_sheet_layout():
    layout = core.contact_sheet_layout(5, 64, pad=8, label_height=10)
    assert (layout["columns"], layout["rows"]) == (3, 2)
    assert layout["width"] == 3 * 64 + 4 * 8 and layout["height"] == 2 * (64 + 10) + 3 * 8
    assert layout["cells"][0] == (8, 8) and layout["cells"][3] == (8, 8 + 64 + 10 + 8)
    assert core.contact_sheet_layout(4, 10, columns=10)["columns"] == 4  # never wider than the item count
    empty = core.contact_sheet_layout(0, 10)
    assert empty["cells"] == [] and empty["columns"] == 1


def test_text_bitmap_and_elide():
    rows = core.text_bitmap("Ab")
    assert len(rows) == core.GLYPH_H and all(len(r) == 11 for r in rows)
    assert rows[0][:5] == [0, 1, 1, 1, 0]  # top of the A
    assert rows[6][6:11] == [1, 1, 1, 1, 0]  # bottom of the B
    assert core.text_bitmap("~") == core.text_bitmap("?")
    assert core.text_bitmap("") == [[] for _ in range(core.GLYPH_H)]
    assert core.elide_middle("short.glb", 20) == "short.glb"
    long = core.elide_middle("a_very_long_model_name.glb", 12)
    assert len(long) == 12 and long.startswith("a_ver") and long.endswith(".glb") and ".." in long


def test_thumbnails_exit_code():
    ok, bad = {"status": "ok"}, {"status": "error"}
    assert core.thumbnails_exit_code([ok, bad]) == 0
    assert core.thumbnails_exit_code([ok, bad], strict=True) == 1
    assert core.thumbnails_exit_code([ok, {"status": "qa_failed"}], strict=True) == 1
    assert core.thumbnails_exit_code([ok], strict=True) == 0
    assert core.thumbnails_exit_code([], strict=True) == 1
