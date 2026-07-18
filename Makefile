# Convenience wrapper around the toolkit scripts.
#
# Reads BLENDER_EXE and defaults from .env (copy .env.example first). On
# Windows you can also just call Blender directly - see the README. These
# targets exist so you do not have to retype the long headless invocation.

-include .env
export

BLENDER_EXE ?= blender
OUT_DIR ?= out
RENDER_ENGINE ?= eevee
RENDER_RESOLUTION ?= 1600x900
RENDER_SAMPLES ?= 64

BLENDER = "$(BLENDER_EXE)" --background --python

.PHONY: help city turntable bars materials clean-pyc

help:
	@echo "Targets:"
	@echo "  make city         - render a procedural city to $(OUT_DIR)/city.png"
	@echo "  make turntable     - render the demo product turntable"
	@echo "  make bars          - render the sample 3D bar chart"
	@echo "  make materials     - build material_library.blend and a preview"
	@echo "  make clean-pyc     - remove __pycache__ folders"

city:
	$(BLENDER) scripts/procedural_city.py -- --blocks 6 --seed 7 --render \
		--res $(RENDER_RESOLUTION) --samples $(RENDER_SAMPLES) --out $(OUT_DIR)/city.png

turntable:
	$(BLENDER) scripts/product_turntable.py -- --still --render \
		--samples $(RENDER_SAMPLES) --out $(OUT_DIR)/turntable

bars:
	$(BLENDER) scripts/csv_to_bars3d.py -- --csv data/sales.csv --title "2026 Sales" \
		--render --res $(RENDER_RESOLUTION) --samples $(RENDER_SAMPLES) --out $(OUT_DIR)/bars3d.png

materials:
	$(BLENDER) scripts/material_library.py -- --spheres --render \
		--samples $(RENDER_SAMPLES) --out material_library.blend

clean-pyc:
	python -c "import pathlib,shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]"
