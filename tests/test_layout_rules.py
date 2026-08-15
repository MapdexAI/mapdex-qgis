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
    assert "QStackedWidget" in panel
    assert "QTabWidget," not in panel
    assert 'page_order = (nivo, workspace, jobs)' in panel
    assert 'for index, title in enumerate(("Nivo AI", "Task", "Jobs"))' in panel
    assert 'page.setVisible(index == target)' in panel


def test_nivo_panel_exposes_a_visible_turn_status():
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    assert '"nivo_status": nivo_status' in panel


def test_nivo_transcript_is_native_widgets_not_model_html():
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "QTextBrowser" not in panel
    assert "setHtml(" not in plugin
    assert "QScrollArea" in panel
