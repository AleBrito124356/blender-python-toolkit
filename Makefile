# Convenience targets. Every target goes through the `bpt` launcher
# (python -m bpt), which finds Blender (BLENDER_EXE, PATH or the usual install
# folders) and always runs it with --background --factory-startup
# --python-exit-code 1, so a failing script fails the target.
#
# `make` is optional: every target is one `python -m bpt ...` line you can
# run directly on Windows, macOS or Linux (see README).

-include .env
export

PYTHON ?= python
BPT = $(PYTHON) -m bpt
OUT_DIR ?= out
RENDER_ENGINE ?= eevee
RENDER_RESOLUTION ?= 1600x900
RENDER_SAMPLES ?= 64
RENDER_FLAGS = --engine $(RENDER_ENGINE) --samples $(RENDER_SAMPLES)

.PHONY: help doctor city turntable turntable-video bars bars-video materials test test-unit test-blender clean-pyc

help:
	@echo "Targets:"
	@echo "  make doctor          - show the Blender install bpt will use"
	@echo "  make city            - render a procedural city to $(OUT_DIR)/city.png (+ QA report)"
	@echo "  make turntable       - render the demo product beauty still"
	@echo "  make turntable-video - render the demo turntable to $(OUT_DIR)/turntable/turntable.mp4"
	@echo "  make bars            - render the sample 3D bar chart"
	@echo "  make bars-video      - render the animated bar chart to $(OUT_DIR)/bars3d.mp4"
	@echo "  make materials       - build material_library.blend and a preview"
	@echo "  make test            - unit tests + Blender integration tests"
	@echo "  make clean-pyc       - remove __pycache__ folders"

doctor:
	$(BPT) doctor

city:
	$(BPT) city --blocks 6 --seed 7 --render --res $(RENDER_RESOLUTION) $(RENDER_FLAGS) \
		--out $(OUT_DIR)/city.png --report $(OUT_DIR)/city.qa.json

turntable:
	$(BPT) turntable --still --render $(RENDER_FLAGS) --out $(OUT_DIR)/turntable \
		--report $(OUT_DIR)/turntable.qa.json

turntable-video:
	$(BPT) turntable --frames 60 --render --video mp4 --backdrop cyclorama $(RENDER_FLAGS) \
		--out $(OUT_DIR)/turntable

bars:
	$(BPT) bars --csv data/sales.csv --title "2026 Sales" --render --res $(RENDER_RESOLUTION) \
		$(RENDER_FLAGS) --out $(OUT_DIR)/bars3d.png --report $(OUT_DIR)/bars3d.qa.json

bars-video:
	$(BPT) bars --csv data/sales.csv --title "2026 Sales" --animate --render --video mp4 \
		--res $(RENDER_RESOLUTION) $(RENDER_FLAGS) --out $(OUT_DIR)/bars3d.png

materials:
	$(BPT) materials --spheres --render $(RENDER_FLAGS) --out material_library.blend \
		--preview $(OUT_DIR)/materials.png

test: test-unit test-blender

test-unit:
	$(PYTHON) -m pytest tests/unit

test-blender:
	$(PYTHON) -m pytest tests/blender -m blender

clean-pyc:
	$(PYTHON) -c "import pathlib,shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]"
