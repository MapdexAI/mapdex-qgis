import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.layout_rules import (
    COMPACT_WIDTH,
    MINIMUM_WIDTH,
    PREFERRED_WIDTH,
    panel_layout_mode,
)


def test_panel_opens_in_the_regular_two_column_layout():
    assert panel_layout_mode(PREFERRED_WIDTH) == "regular"


def test_narrow_dock_switches_to_stacked_rows():
    assert panel_layout_mode(COMPACT_WIDTH - 1) == "compact"
    assert panel_layout_mode(MINIMUM_WIDTH) == "compact"


def test_breakpoint_is_inclusive_upwards():
    assert panel_layout_mode(COMPACT_WIDTH) == "regular"


def test_panel_can_be_dragged_narrower_than_it_opens():
    assert MINIMUM_WIDTH < COMPACT_WIDTH < PREFERRED_WIDTH


def test_nivo_is_a_primary_tab_not_an_inline_task_card():
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    assert "QTabWidget" in panel
    assert 'tabs.addTab(workspace, "Task")' in panel
    assert 'tabs.addTab(nivo, "Nivo")' in panel
    assert 'tabs.addTab(jobs, "Jobs")' in panel
