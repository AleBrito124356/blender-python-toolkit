---
name: blender-toolkit
description: Run headless Blender jobs with the bpt launcher of blender-python-toolkit - export .blend or model files to web/AR .glb and .usdz with a verified report, render asset thumbnails and contact sheets, batch-render .blend folders, build turntables, 3D bar charts, procedural cities and material libraries, clean scenes, and check renders with numeric QA instead of looking at them. Use when a task needs Blender without a GUI (CI, containers, scripts), including machines with no Blender install (pip install bpy).
---

# blender-toolkit (bpt)

`bpt` runs the toolkit's scripts in a headless Blender and reports results as JSON. Work from the repository root (`python -m bpt ...`), or use `bpt` after `pip install -e .`.

## 1. Get a runtime

```bash
python -m bpt doctor            # which runtime, engines, FFmpeg, glTF/Draco/USD export, libEGL
```

- Blender is found via `--blender PATH`, `$BLENDER_EXE`, `blender` on PATH, the usual install folders, then a Python interpreter that can `import bpy` (`$BLENDER_PYTHON`, or the interpreter running bpt).
- No Blender? `python3.13 -m venv .venv && .venv/bin/pip install bpy` (bpy 5.x needs Python 3.13), then `BLENDER_EXE=.venv/bin/python python -m bpt doctor`. `--blender` and `$BLENDER_EXE` accept a Blender executable or such an interpreter.
- **Linux containers:** EEVEE and Workbench need libEGL even headless: `apt-get install -y libegl1 libgl1-mesa-dri` (software rendering via Mesa). Without it only Cycles renders. The `EGL Error (0x3009)` lines in the logs are harmless.
- On software EGL, EEVEE compiles shaders for each new material (about 5-10 s each; the first render of a process can take ~40 s). Use `--engine workbench` for fast thumbnails and `--engine cycles --device cpu` with low `--samples` for quick renders.

## 2. Always run with `--json`

```bash
python -m bpt --json <command> [script flags...]
```

`--json` goes **before** the command. stdout is exactly one JSON object; every log line goes to stderr. Read:

- `exit_code` and `status`: `0 ok`, `1 failed` (Python/Blender error, failed files in a batch, `--strict` thumbnails), `2 invalid_input` (missing/unsupported file, bad flag, a request this build cannot do), `3 check_failed` (files written but `--qa-strict` QA or the export round trip failed).
- `summary`: written by the script: `outputs` (absolute paths), `qa` (`passed`, `failing` check ids, one line per check), plus per-command fields below. `null` when the script crashed before writing it.
- `log_tail`: the last 40 log lines, present only when `exit_code != 0`. Read it before retrying.

`python -m bpt --dry-run <command> ...` prints the exact command without running it.

## 3. Commands

| Task | Command | Look at in `summary` |
| --- | --- | --- |
| Web / AR export | `export --input model.glb\|scene.blend [--output out/x.glb] [--usdz] [--max-triangles N \| --decimate R] [--texture-max 1024] [--image-format webp] [--draco 6]` | `export.verification.passed`, `export.export.triangles`, `export.bounds.size` (metres, X Y Z with Z up), `export.textures[].size`, `export.warnings`, `outputs` |
| Asset thumbnails | `thumbnails --input DIR [--size 256] [--engine workbench] [--background '#20242a'] [--strict]` | `counts`, `items[].status` / `error` / `warnings`, `contact_sheet`, `index` |
| Render a folder of .blend | `batch --input DIR --output out/batch [--resolution 1280x720 --samples 16]` | `counts`, `results[].status`, `results[].qa.failing` |
| Audit a scene / render | `inspect --input scene.blend [--render out/a.png] [--report r.json] [--qa-strict]` | `qa.passed`, `qa.failing` |
| Product turntable | `turntable --model m.glb --still --render --report r.json` | `outputs`, `qa` |
| CSV to 3D bars | `bars --csv data.csv --render --report r.json` | `outputs`, `qa` |
| Others | `city`, `materials`, `presets --list`, `cleanup --input x.blend [--execute]`, `run my_script.py` | |

Every script answers `--help` (`python -m bpt export --help`). Generators render only with `--render` and compute QA only with `--report PATH` or `--qa-strict`.

## 4. Reading results

- **Export:** trust the file only when `verification.passed` is true: each output was re-imported into an empty scene and matched the report's triangle count and bounds, and the .glb bytes were checked (triangles, Draco extension, texture sizes). Exit 2 with "no Draco encoder" means this Blender (e.g. the PyPI bpy wheel) cannot compress: drop `--draco`. Objects tagged `qa_role=backdrop` (floors from this toolkit) are not exported.
- **QA checks** (`qa.failing`): `in_frame` subject cropped or outside the frame; `behind_camera`; `ground_contact` floating object; `coverage` subject too small; `image_exposure` black or washed-out image (lights/world); `image_detail` flat image; `image_alpha` empty transparent render; `annotation_occluded` / `annotation_overlap` labels hidden or colliding. Fix the scene, not the threshold.
- **Thumbnails:** `status` is `ok`, `error` (could not import or render; see `error`) or `qa_failed` (empty, black or flat image). `missing_textures` lists images whose files are missing (they render magenta).

## 5. Rules

- Never pass `--cycles`; use `--engine cycles` (Blender's Cycles add-on rejects `--cycles` with exit code 2).
- Paths in flags are relative to the current directory; outputs default to `out/`.
- Inputs are never overwritten (`cleanup` and `presets --save` write new files) unless you pass `presets --in-place`.
- For your own script inside Blender: `python -m bpt run my_script.py -- --flags` and read arguments with `lib.core.get_argv_after_dashes()`; call `lib.core.write_summary({...})` to feed `--json`.
