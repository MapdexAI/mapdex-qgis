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


def test_the_onboarding_route_to_settings_reveals_the_stack_it_moves():
    # Reported: pressing "use my own model" at first open did nothing at all.
    # The tab bar and the page stack are hidden while nothing has been chosen -
    # Task and Jobs cannot do anything yet - so switching the stack to Settings
    # was a move nobody could see.
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "(self.tabs, not first or self._onboarding_settings)" in plugin
    assert "(self.segment_bar, not first or self._onboarding_settings)" in plugin
    # And the forced reset to the conversation has to respect it, or the very
    # next repaint undoes the move.
    assert "if first and not self._onboarding_settings and self.switch_page" in plugin


def test_connecting_does_no_network_work_on_the_click_handler():
    # Reported after a disconnect: Connect Mapdex did nothing. It had grown
    # three network round trips - discovery, the resource document, the
    # pre-flight - before the first status line, so it sat silent for seconds
    # and read as a dead button. Everything slow belongs on the worker.
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    start = plugin.index("    def connect(self, *args):")
    body = plugin[start : plugin.index("\n    def ", start + 1)]
    for slow in ("discover_endpoints", "server_knows_this_client",
                 "loopback_available", "_connector_resource"):
        assert slow not in body, "{} still runs on the click handler".format(slow)
    assert "_set_status(" in body, "the click gives no immediate feedback"
    assert "self._task(" in body


def test_the_header_settings_control_is_gone():
    # Two controls for one destination, and the header one named itself but not
    # where it would appear.
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "settings_button = QToolButton()" not in panel
    assert '"settings_button": settings_button' not in panel
    assert "self.settings_button" not in plugin
    # The one that stayed is a different widget: Save settings, inside the form.
    assert "save_settings_button" in panel


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
