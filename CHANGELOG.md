# Changelog

## 0.3.0 - 2026-11-02

Tested on `bpy` 5.2.3 LTS from PyPI (Python 3.13, Ubuntu 24.04, EEVEE and
Workbench through software EGL, Cycles on the CPU). No Blender executable was
available for this release, so the executable path of the new code is covered
by the shim's emulation of Blender's command line
(`tests/blender/test_runtime.py`), not by a run on Blender itself; 0.2.0 was
tested on Blender 5.2.1 LTS (Windows 11).

### Added

- **No Blender install needed.** `bpt` runs every command through a Python
  interpreter that can `import bpy` (`pip install bpy`) as well as through a
  Blender executable. `--blender` and `BLENDER_EXE` accept either kind
  (detected from the file name, or `PATH --version`); discovery falls back to
  `BLENDER_PYTHON` and then to the interpreter running `bpt`.
  `bpt/blender_shim.py` emulates `blender --background --factory-startup
  --python-exit-code 1 [file.blend] --python script.py -- args` on the module
  (argument order, factory settings, `.blend` loading without embedded
  scripts, `sys.argv` set before `import bpy`, exit codes). `bpt doctor`
  reports the runtime kind, glTF / Draco / USD export support and libEGL;
  `bpt --dry-run` prints the shim command.
- **`bpt export`** (`scripts/export_web.py`): a .blend or a model
  (.glb/.gltf/.obj/.fbx/.stl/.ply/.usd/.usda/.usdc/.usdz) to a +Y-up `.glb`
  and optionally a `.usdz`, with `--max-triangles N` / `--decimate RATIO`,
  `--texture-max SIZE`, `--image-format auto|jpeg|webp`, `--draco [LEVEL]`,
  `--no-apply-modifiers`, `--animations`. Writes a JSON report (triangles,
  vertices and surface area before and after, file sizes, bounds in metres,
  materials, textures and their sizes, warnings) and verifies every file by
  re-importing it into an empty scene, plus a byte-level read of the `.glb`
  (triangles, Draco extension, embedded texture sizes). Exit code 3 when a
  check fails.
- **`bpt thumbnails`** (`scripts/thumbnails.py`): a folder of models and
  .blend files to square PNG thumbnails with the turntable's normalisation and
  light rig, `--engine eevee|workbench|cycles`, transparent or solid
  `--background`, a labelled contact sheet and `index.json` (file, thumbnail,
  bounds, triangles, status, error, missing textures, image QA). Broken files
  are recorded and skipped; `--strict` exits 1.
- **`bpt --json`**: one JSON object on stdout (status, exit code, timings,
  runtime, and the script's summary: outputs, condensed QA, per-file results
  for batch / thumbnails) with all logs on stderr and the log tail included
  on failure. Every script writes its summary to `$BPT_SUMMARY`
  (`lib.core.write_summary`).
- `skills/blender-toolkit/SKILL.md`: an Agent Skill that teaches an AI agent
  when and how to use `bpt` headlessly and how to read its JSON.
- `.github/workflows/ci.yml`: unit tests on Python 3.9 and 3.13, and the
  Blender integration suite on `pip install bpy` with libEGL on Ubuntu.
- `lib/studio.py`: model import for every format the build supports,
  normalisation and the 3-point rig (now turnable to any camera azimuth),
  shared by the turntable and thumbnails. `lib.common`: `mesh_stats`,
  `bounds_dict`, `renderable_geometry`, `set_workbench_engine`,
  `object_images`. `lib.core`: GLB reading, image-size sniffing (PNG, JPEG,
  WebP), round-trip checks, exit-code constants, `fail_input`, contact-sheet
  layout and a 5x7 bitmap font.
- `product_turntable --model` also imports `.usd`, `.usda`, `.usdc` and
  `.usdz`.

### Fixed (found while building the export, each covered by a test)

- Blender's glTF exporter writes an uncompressed file, without an error,
  when its Draco library is missing (the PyPI `bpy` 5.2 wheel ships without
  `libbf_intern_draco_bridge`). `bpt export --draco` refuses with exit code 2
  on such builds and checks the extension in the written file otherwise.
- `Image.scale()` does not mark an image as modified, so the glTF exporter
  re-reads the original file and ships the full-size texture. Downscaled
  textures are written as new images instead, and the embedded sizes are
  read back from the `.glb`.
- Decimating an imported mesh deleted whole triangles (holes, half the
  surface area) because the glTF importer leaves a flat-shaded mesh as one
  island per triangle. A Weld modifier now runs before the Decimate.
- Loose vertices are not exported, so the report's bounds count only
  vertices that belong to a face.
- The USD exporter writes bevelled curves as USD curves and subdivision as a
  scheme, so a `.usdz` would not hold the triangles of the `.glb`: curves and
  text are baked to meshes first and USD tessellates subdivision.

### Changed

- **Invalid input exits with code 2** in every script (missing input file
  or folder, unsupported or unreadable model, invalid preset file, unknown
  preset, refusing to overwrite). It was 1, which is now reserved for errors
  and failed files. Exit codes: 0 ok, 1 failed, 2 invalid input, 3 a check
  failed.
- `lib.common.run_qa` takes `outputs=` (extra files the script wrote) for
  the JSON summary.
- The Blender test fixtures run against either runtime (`blender_cmd`
  fixture); video tests skip when the build has no FFmpeg.

## 0.2.0

Tested on Blender 5.2.1 LTS (Windows 11, Intel UHD 770, EEVEE and Cycles/CPU).
The version-aware branches for Blender 3.6-4.x are kept but are not exercised
by the test-suite.

### Fixed

- **`--cycles` never worked.** Blender's Cycles add-on parses the arguments
  after `--` itself and rejects `--cycles` as an ambiguous prefix of
  `--cycles-device` / `--cycles-print-stats`, so Blender exited with code 2
  before any script ran. Replaced by `--engine {eevee,cycles}` and
  `--device {gpu,cpu}` in every rendering script.
- **Blender 5.x support.** The turntable animation crashed on the removed
  `Action.fcurves` (now `lib.common.action_fcurves`, layered-action aware);
  `batch_render` and `render_presets` crashed on files saved with video output
  because `image_settings.media_type` was never set (now
  `set_image_output` / `set_video_output`); the dusk sky silently fell back to
  daylight because `NISHITA` no longer exists (now `configure_sky`, which picks
  `MULTIPLE_SCATTERING` and reports what it chose); render engines registered
  at runtime (Cycles, Workbench) were invisible to engine detection.
- **Failures now fail.** Every documented command uses
  `--python-exit-code 1`; `batch_render` exits 1 when a file errors (and, with
  `--strict`, when one is skipped).
- **Floating, half-size geometry.** Buildings and bars were built from a
  `size=1` cube scaled by half their dimensions and placed at half height.
  They are now exact boxes (`add_box`) standing on the ground.
- **Misleading bar chart.** Heights were min-max normalised (Dec/Jan rendered
  15x for a 2.86x ratio). Heights are now proportional to the value on a zero
  baseline, negative values hang below a zero axis, category labels sit in
  front of the bars (they were behind them), value labels sit above them.
- **No window grid.** The facade used a 2D brick texture on object XY, which
  streaked on two walls and was flat on the other two. The new facade projects
  a window grid onto all four walls, lights windows individually and keeps
  roofs bare.
- **Turntable framing.** Floor contact and framing used object-aligned
  `bound_box` corners, so rotated imports floated (Suzanne 0.043 above the
  floor) and filled ~15% of the frame. Both now use evaluated vertices, and the
  camera is fitted to the model over the whole orbit.
- **Cropped material preview.** The camera is fitted to the sphere row for the
  real render aspect; EEVEE ray tracing is enabled so glass refracts.
- **Inside-out normals.** `cleanup_scene --execute` flipped the faces of
  mirrored meshes a second time after `transform_apply` had already fixed
  them. Mirrored objects sharing a mesh are now reported and left alone
  instead of being silently un-mirrored with `abs()`.
- **Presets were never saved.** `render_presets` without `--render` changed
  nothing on disk.

### Added

- `bpt` launcher (`pip install -e .`, or `python -m bpt`): Blender
  auto-discovery, `bpt doctor`, one sub-command per script, `--dry-run`, and
  safe flags on every run.
- Render QA (`lib/qa.py`): `--report PATH` / `--qa-strict` on every rendering
  script, per-file reports in `batch_render`'s `summary.json`, and
  `scripts/inspect_render.py` to audit any `.blend` and image. Checks framing,
  cropping, objects behind the camera, ground contact, coverage, label overlap
  and occlusion, and black / flat / empty / clipped images.
- `lib/core.py`: all bpy-free logic (argument, CSV, number and preset parsing,
  preset validation, chart layout, rename planning, QA rules) so it is unit
  tested without Blender.
- Tests: 167 unit tests on system Python and 42 integration tests that run
  every script in a headless Blender and measure the result. Against the
  previous release the integration suite fails (see the PR for the counts).
- Exit code 3 for `--qa-strict` failures, distinct from errors (1) and
  usage problems (2).
- `docs/gallery/`: renders produced by the scripts, and a 0.1-vs-0.2
  before/after comparison.
- `product_turntable`: `--video mp4|mkv|webm` (FFmpeg output), `--fps`,
  `--backdrop floor|cyclorama|none`, `--elevation`, `.stl`/`.ply` import, a
  light rig that orbits with the camera.
- `csv_to_bars3d`: gridlines with round tick values, `--label-col`,
  `--value-col`, `--delimiter`, thousands separators / decimal commas,
  `--strict-csv`, `--sort`, `--animate` (staggered eased grow-in) with PNG or
  video output.
- `procedural_city`: `--lit`, `--sun-elevation`.
- `batch_render`: `summary.json` / `summary.csv`, `--strict`, `--no-qa`,
  `--qa-strict`, `--device`.
- `render_presets`: `--save` (new `<name>_<preset>.blend`, never overwrites),
  `--in-place`, schema validation with every problem listed, output extension
  follows the preset format.
- `cleanup_scene`: `--output`, `--json`, collision-free renames.
- `procedural_city`, `product_turntable`, `csv_to_bars3d`: `--save-blend PATH`.
- `pyproject.toml` with the `bpt` console script and pytest configuration.

### Changed

- `--cycles` is gone (it could not be used anyway): use `--engine cycles`.
- The turntable demo subject is a lathed bottle with a metal cap instead of a
  bevelled cube (which looked identical every 90 degrees).
- `csv_to_bars3d --csv` defaults to the repository's `data/sales.csv`
  regardless of the working directory; chart height adapts to the bar count.
- `material_library` writes its preview to `--preview` (default
  `out/materials.png`, as before).
- `lib.common.world_bounds` / `frame_object` now measure evaluated vertices
  (tighter, rotation-correct) instead of `bound_box` corners.
- The Makefile delegates to `python -m bpt`; `requirements.txt` now lists the
  development dependencies of the system Python (Blender never saw it).
