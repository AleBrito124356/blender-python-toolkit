"""Run scripts/export_web.py with a re-import that reports the wrong bounds.

Used to prove that a round trip which disagrees with the report fails the run
(exit code 3) without adding a test hook to the script itself:

    blender -b --factory-startup --python-exit-code 1 --python tests/blender/helpers/export_with_fault.py -- <export args>
"""

import os
import pathlib
import sys

ROOT = os.environ.get("BPT_TOOLKIT_ROOT") or str(pathlib.Path(__file__).resolve().parents[3])
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import export_web  # noqa: E402

_measure = export_web.measure_reimport


def measure_with_a_drift(path):
    measured = _measure(path)
    measured["bounds"]["max"][2] += 0.25  # as if the file came back 25 cm taller
    return measured


export_web.measure_reimport = measure_with_a_drift
export_web.main()
