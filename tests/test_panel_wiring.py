"""The panel must actually be wired to the modules that decide what it says.

`plugin.py` imports qgis at module scope and cannot be imported in CI, so these
are source-level guards in the same style as `test_panel_steps.py` and
`test_privacy_claims.py`. They exist because this plugin has repeatedly shipped
well-tested modules that nothing called: `test_module_reachability.py` was
written after 3,570 lines of capability code turned out to be reachable only
from its own tests, and `test_privacy_claims.py` after a settings panel promised
a code path `ask_nivo` never took.

A green `test_first_look.py` proves the sentences are right. It cannot prove
anybody ever reads them.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mapdex_qgis"
PLUGIN = (PACKAGE / "plugin.py").read_text(encoding="utf-8")
PANEL = (PACKAGE / "panel.py").read_text(encoding="utf-8")


def _method(source: str, name: str) -> str:
    """The body of one method, by indentation rather than by a sentinel.

    `test_panel_steps._method` slices to the next "\\n    @guarded", which stops
    early on a method that is not followed by a decorated one.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("plugin.py has no method {!r}".format(name))


def _calls(source: str, name: str) -> set[str]:
    """Every function or method name called anywhere inside one method."""
    called = set()
    for node in ast.walk(ast.parse(_method(source, name))):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                called.add(target.attr)
            elif isinstance(target, ast.Name):
                called.add(target.id)
    return called


# -- the reading reaches the screen -----------------------------------------

def test_the_opening_reading_is_built_and_rendered():
    assert "from . import first_look" in PLUGIN
    assert "first_look.opening_reading(" in PLUGIN, (
        "first_look is imported and never consulted, which is the defect class "
        "test_module_reachability.py exists for"
    )


def test_the_reading_is_refreshed_by_every_signal_that_invalidates_it():
    """It describes the active layer and the connection state, so it is stale
    the moment either changes. A reading refreshed only at start-up is a splash
    screen."""
    for method in ("_refresh_nivo_context", "_refresh_ui", "_render_nivo_turns"):
        assert "_render_first_look" in _calls(PLUGIN, method), (
            "{} changes what the reading would say and does not redraw it".format(method)
        )
    assert "_refresh_nivo_context" in _calls(PLUGIN, "_on_current_layer_changed"), (
        "clicking another layer in the Layers panel left the reading describing the old one"
    )


def test_the_reading_costs_no_features_to_produce():
    """It runs on panel open and on every layer click, so it may read metadata
    and nothing else. `featureCount()` is O(1); `getFeatures()` is not."""
    body = _method(PLUGIN, "_first_look_state")
    for forbidden in ("getFeatures", "selectByExpression", "processing.run"):
        assert forbidden not in body, (
            "{} walks the data to introduce itself; a large layer would hang the "
            "panel on every layer click".format(forbidden)
        )


def test_the_reading_does_not_reuse_the_server_context_envelope():
    """`companion_context` is versioned and is posted to Mapdex. Growing fields
    onto it for a local panel decoration is how a bounded payload stops being
    bounded."""
    # By call, not by text: the method's own docstring explains why it does not
    # use that envelope, and a substring check would read the explanation as
    # the violation.
    assert "companion_context" not in _calls(PLUGIN, "_first_look_state")


# -- the georeference measurement has one owner ------------------------------

def test_placement_is_measured_in_one_place():
    """Two copies of "valid CRS and non-empty extent" is how one of them comes
    to disagree about what placed means - and this one gates a paid workflow."""
    runtime = (PACKAGE / "qgis_runtime.py").read_text(encoding="utf-8")
    assert "def raster_is_georeferenced" in runtime
    assert "raster_is_georeferenced(layer)" in _method(PLUGIN, "_first_look_state")
    hand_rolled = re.findall(r"crs\.isValid\(\)\s*(?:and|\)?\s*and)\s*not\s+extent\.isEmpty", PLUGIN)
    assert not hand_rolled, hand_rolled


# -- which engine answers is stated where the user is -----------------------

def test_the_runtime_label_uses_the_resolution_the_turn_takes():
    """Resolving the settings a second time is exactly how the panel came to
    promise a path the turn never took (see test_privacy_claims.py)."""
    body = _method(PLUGIN, "_refresh_assistant_state")
    assert "self.assistant_runtime()" in body, body
    assert "resolve_runtime(" not in body, (
        "the Nivo header resolves the runtime independently of ask_nivo"
    )


def test_a_state_that_cannot_ask_disables_asking():
    body = _method(PLUGIN, "_refresh_assistant_state")
    assert "can_ask" in body and "setEnabled" in body, body


def test_the_runtime_label_does_not_own_the_status_line():
    """One owner per widget. This method runs on every layer click, so a
    standing reason written into nivo_status overwrites whatever the user's
    last action reported - measured in the QGIS harness as "New chat" losing
    its own message about where the old conversation went."""
    assert "nivo_status" not in _method(PLUGIN, "_refresh_assistant_state")


def test_finishing_a_turn_asks_whether_asking_is_possible():
    """Re-enabling Ask unconditionally switched it back on in a state that
    cannot take a turn, so the button was live and the press did nothing."""
    body = _method(PLUGIN, "_set_nivo_compose_busy")
    assert "_refresh_assistant_state" in body, body
    assert "setEnabled(not busy)" not in body, body


def test_the_panel_has_somewhere_to_put_the_runtime_line():
    assert '"nivo_runtime": nivo_runtime' in PANEL
    assert '"nivo_reading": nivo_reading' in PANEL


# -- disconnect ends the session, all of it ---------------------------------

def test_disconnect_reports_what_it_did_not_stop():
    body = _method(PLUGIN, "disconnect")
    assert "panel_state.disconnect_notice" in body, (
        'disconnect still reports a bare literal above an assistant that keeps answering'
    )
    assert '_set_status("Disconnected.")' not in PLUGIN


def test_disconnect_clears_every_session_scoped_setting():
    body = _method(PLUGIN, "disconnect")
    assert "SESSION_SCOPED_SETTINGS" in body, (
        "the recent-task list survived disconnect, so Jobs kept offering Resume on "
        "batches the next token cannot read"
    )
    assert "_load_recent_tasks" in _calls(PLUGIN, "disconnect"), (
        "the stored list was cleared and the combo still showed it"
    )


def test_disconnect_does_not_touch_the_provider_key():
    """Ending a Mapdex session is not a reason to throw away the user's own
    credential for their own account somewhere else."""
    body = _method(PLUGIN, "disconnect")
    for forbidden in ("clear_assistant_key", "mapdex/nivo"):
        assert forbidden not in body, body


# -- the pages stop showing the previous session ----------------------------

def test_the_task_page_body_is_what_hides_rather_than_the_page():
    """`switch_page` shows the page it moves to, so hiding the page itself was
    overruled the moment the user clicked Task - and they got the previous
    session's project and source in greyed-out controls."""
    body = _method(PLUGIN, "_refresh_ui")
    assert "self.workspace.setVisible(connected)" not in body, (
        "the page visibility fights the segment switch again"
    )
    assert "self.workspace_body.setVisible(connected)" in body
    assert "self.workspace_locked.setVisible(not connected)" in body
    assert "self.jobs_locked.setVisible(not connected)" in body


def test_resume_is_not_offered_without_a_session():
    assert "self.api.token" in _method(PLUGIN, "_load_recent_tasks"), (
        "Resume stayed live while disconnected and submits a batch id the API "
        "answers with a tenant-safe NOT_FOUND, which reads as a lost task"
    )


# -- the capability catalogue is generated, not written ---------------------

def test_the_capability_answer_is_read_from_the_registry():
    body = _method(PLUGIN, "_open_capabilities_dialog")
    assert "for_client(CLIENT_QGIS)" in body, (
        "a hand-written list would go stale the first time a capability is added"
    )
    assert "session_allowance" in body, (
        "account-only capabilities were not marked, so the answer to "
        '"what do I get for signing up" was missing from the one screen that asks it'
    )


def test_the_dialog_renders_registry_text_as_data():
    """A summary is registry text and every other string in this panel is
    rendered as data. `setTextFormat(PlainText)` is that rule here."""
    dialog = PANEL[PANEL.index("def build_capabilities_dialog"):]
    assert "PlainText" in dialog, dialog[:400]


# -- the stored key reads as stored -----------------------------------------

def test_the_settings_panel_states_the_stored_key_as_text():
    body = _method(PLUGIN, "_refresh_assistant_privacy")
    assert "panel_state.key_state_line" in body, (
        "the stored key showed only as a grey placeholder, which reads as an "
        "empty field - the reason a user with a working key reported having none"
    )
