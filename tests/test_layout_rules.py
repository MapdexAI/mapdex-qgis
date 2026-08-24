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
    assert 'page_order = (nivo, workspace, jobs, settings_page)' in panel
    assert 'enumerate(("Nivo AI", "Task", "Jobs", "Settings"))' in panel
    assert 'page.setVisible(index == target)' in panel


def test_settings_is_a_page_and_not_a_panel_that_expands_the_column():
    # It used to be a disclosure in the middle of the column, so opening it
    # pushed everything below it down by the height of a nine-field form -
    # which meant pressing "use my own model" answered the question by shoving
    # the choice it belonged to off the screen.
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    assert "settings_page_layout.addWidget(connection_panel)" in panel
    # Anchored on the indent, because the page's own line ends in the same
    # characters: `settings_page_layout.addWidget(connection_panel)` contains
    # `layout.addWidget(connection_panel)`, so a bare substring check here
    # fails against the correct code.
    assert "\n    layout.addWidget(connection_panel)" not in panel, (
        "the settings form is back in the scrolling column"
    )
    assert "settings_button.toggled.connect(connection_panel.setVisible)" not in panel


def test_the_settings_page_index_has_one_owner():
    # The tab bar, the header control and the first-open route into the
    # provider fields all have to agree on which page Settings is, and three
    # copies of a 3 is how they come to disagree.
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "SETTINGS_PAGE = 3" in panel
    assert "SETTINGS_PAGE," in plugin, "plugin.py does not import the shared index"
    assert "self.switch_page(SETTINGS_PAGE)" in plugin


def test_choosing_your_own_model_lands_on_the_settings_page():
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    start = plugin.index("def choose_own_model")
    body = plugin[start : plugin.index("\n    def ", start + 1)]
    assert "switch_page(SETTINGS_PAGE)" in body
    assert "provider_box.setFocus()" in body, (
        "it arrives at the page without putting the caret in the first field"
    )


def test_a_demoted_connect_row_keeps_a_container_and_changes_its_sentence():
    # Reported as "the background is broken": Connect Mapdex rendered as indigo
    # text floating on the panel with no container, directly above a bordered
    # card, while still carrying the word "Recommended". Losing a rank means
    # losing the FILL, not losing the object - and not keeping a sentence that
    # tells somebody who has already started that they should start.
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    # The standalone rule, not the shared `QFrame#mapdexRow, ...Primary,
    # ...Quiet { border-radius }` one that also contains this selector. Anchored
    # on the line start so the two cannot be confused.
    quiet = panel[panel.index("\n        QFrame#mapdexRowQuiet {") :]
    quiet = quiet[: quiet.index("}")]
    assert "border: 1px solid" in quiet, "the demoted rank has no container"

    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "CONNECT_PROMISE_UPGRADE" in plugin
    assert "set_sublabel(" in plugin


def test_nivo_panel_exposes_a_visible_turn_status():
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    assert '"nivo_status": nivo_status' in panel


def test_nivo_transcript_is_native_widgets_not_model_html():
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "QTextBrowser" not in panel
    assert "setHtml(" not in plugin
    assert "QScrollArea" in panel
