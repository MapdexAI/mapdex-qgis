"""The format tables and the byte-count wording.

The compatibility tests that used to live here moved to
`test_task_sources.py` with `inspect_paths`, which `task_sources.summary`
superseded: the question is now asked of a list of SOURCES, which may hold open
QGIS layers as well as files.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.source_info import human_size


def test_human_size():
    assert human_size(1536) == "1.5 KB"
