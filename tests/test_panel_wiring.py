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
BRANDING = (PACKAGE / "branding.py").read_text(encoding="utf-8")


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
    screen.

    The reading is drawn as turns now, so the transcript renderer is what
    rebuilds it and everything that invalidates it redraws the transcript.
    """
    assert "_refresh_opening" in _calls(PLUGIN, "_render_nivo_turns"), (
        "the transcript no longer rebuilds the opening, so it goes stale"
    )
    for method in ("_refresh_nivo_context", "_refresh_ui"):
        assert "_render_nivo_turns" in _calls(PLUGIN, method), (
            "{} changes what the reading would say and does not redraw it".format(method)
        )
    assert "_nivo_turns" in _method(PLUGIN, "_refresh_opening"), (
        "the opening does not step aside once the conversation starts"
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


def test_every_compose_task_refreshes_stop_after_assignment():
    for method in ("_start_hosted_turn", "_byok_step", "_continue_objective"):
        body = _method(PLUGIN, method)
        assignment = body.index("self._nivo_compose_task = self._task(")
        refresh = body.index("self._refresh_stop_button()", assignment)
        assert refresh > assignment, "{} never reveals Stop after creating its task".format(method)


def test_connector_sign_in_is_bounded_and_user_cancellable():
    connect = _method(PLUGIN, "connect")
    work = _method(PLUGIN, "_connect_work")
    finished = _method(PLUGIN, "_connect_finished")

    assert "self._connect_cancel.set()" in connect
    assert "self._connect_task = self._task(" in connect
    assert "CONNECT_TOTAL_TIMEOUT" in work
    assert "_connect_request_timeout" in work
    assert "cancel_event=self._connect_cancel" in work
    assert "timeout=300" not in work
    assert "self._connect_task = None" in finished
    assert 'exception.code == "cancelled"' in finished
    assert "def set_label" in PANEL


def test_connector_browser_open_is_queued_to_the_qgis_ui_thread():
    work = _method(PLUGIN, "_connect_work")
    announce = _method(PLUGIN, "_announce_from_worker")

    assert "_ConnectorUiBridge(QObject)" in PLUGIN
    assert "self._connector_ui.open_url.emit(url)" in work
    assert "self._connector_ui.show_status.emit(message)" in announce
    assert "QTimer.singleShot" not in work
    assert "QDesktopServices.openUrl" not in work


def test_the_panel_has_somewhere_to_put_the_runtime_line():
    assert '"nivo_runtime": nivo_runtime' in PANEL


def test_the_reading_has_no_frame_of_its_own():
    """Everything Nivo says is a turn. A second surface above the transcript is
    the card, the modal and the dialog all over again."""
    for gone in ("nivo_reading", "mapdexFinding", "build_capabilities_dialog"):
        assert gone not in PANEL, gone
        assert gone not in PLUGIN, gone


# -- first open --------------------------------------------------------------

def test_first_open_is_derived_rather_than_stored():
    body = _method(PLUGIN, "_refresh_ui")
    assert "panel_state.is_first_open(" in body, body
    for latch in ("has_seen", "onboarded", "first_run_done"):
        assert latch not in PLUGIN, (
            "{} is a stored flag; the state has to be derived so it can return "
            "when both the session and the key are gone".format(latch)
        )


def test_asking_whether_a_provider_exists_does_not_open_the_key_store():
    """`_refresh_ui` runs on every connection change and every layer click.
    Decrypting the QGIS authentication database that often, to decide whether
    to show a screen, is not something to do."""
    body = _method(PLUGIN, "_has_provider")
    assert "load(" not in body and "_credential_store" not in body, body
    assert "mapdex/nivo/provider" in body, body


def test_the_choice_sits_above_the_reading():
    """The choice comes first, and this reverses an earlier decision.

    The old rule was that showing the choice alone is a wall in front of a
    product nobody has seen, so the reading went first and the panel said
    something true before it asked. Measured on the real dock, what that
    produced was the choice at y=602 on a first-open screen whose title ended
    at y=119, with 234 px of empty transcript in between - so the one decision
    a stranger has to make was the last thing they found, and the founder's
    words for where it had ended up are not printable here.

    Nivo introduces itself, offers the two ways it can think, and the reading
    follows as the argument for them. The reading is still first-hand and still
    needs no account; it is no longer in front of the door.
    """
    order = [PANEL.index(marker) for marker in (
        "layout.addWidget(sign_in)", "layout.addWidget(pages, 1)")]
    assert order == sorted(order), "the choice fell below the transcript again"
    # And it is left-aligned by a stretch, not by an alignment flag: given one,
    # Qt hands the widget its sizeHint instead of the available width, which
    # collapsed the reading column to ~320 px in a 1,430 px dock and clipped
    # every row mid-sentence.
    assert "AlignHCenter" not in PANEL, (
        "an alignment flag is centring a capped widget again, which starves it"
    )
    # ONE capped column owns the width. Capping each element separately gave
    # three different right edges - status in one place, transcript in another,
    # composer in a third - and a ragged edge is what reads as "not responsive".
    assert PANEL.count("setMaximumWidth(READING_WIDTH)") == 1, (
        "the width is capped in more than one place again"
    )


def test_first_open_is_only_the_two_provider_choices():
    """Onboarding must not compete with a disabled Nivo transcript.

    Once either route can answer, the normal pages return. Until then the
    connection label, short title and two actions are the whole screen.
    """
    body = _method(PLUGIN, "_refresh_ui")
    assert "(self.status, not first)" in body, body
    assert "(self.tabs, not first or self._onboarding_settings)" in body, body
    assert "(self.nivo_new_button, not first)" in body, body
    assert "(self.nivo_history_button, not first)" in body, body
    # There is no header Settings control to hide: it became a tab.
    assert "self.settings_button" not in body, body
    # And the tab bar comes back the moment the reader asks for Settings,
    # or the second onboarding route moves a stack nobody can see.
    assert "not first or self._onboarding_settings" in body, body


def test_own_model_choice_disappears_while_its_settings_are_open():
    body = _method(PLUGIN, "_refresh_ui")
    hidden_after_choice = "first and not self._onboarding_settings"

    assert body.count(hidden_after_choice) >= 3, body
    assert "self.connect_button.setVisible(not connected)" in body, body


def test_empty_chat_is_one_owned_welcome_card():
    """The fixed Nivo header owns the sender; the reading owns its action."""
    opening = _method(PLUGIN, "opening_turns")
    turn = _method(PLUGIN, "_turn_widget")
    assert 'opening=True' in opening, opening
    assert 'turns[-1]["actions"].append' in opening, opening
    assert 'See what Nivo can do' in opening, opening
    assert 'mapdexOpeningTurn' in turn, turn
    assert 'if not turn.get("opening")' in turn, turn


def test_idle_nivo_keeps_full_chat_height_without_repeating_stale_status():
    body = _method(PLUGIN, "_render_nivo_turns")

    assert "self.nivo_reply.setMaximumHeight(16777215)" in body, body
    assert body.count('enum_member(QSizePolicy, "Policy", "Expanding")') >= 2, body
    assert "setMaximumHeight(exact)" not in body, body
    assert "self.nivo_status.setVisible(has_conversation or self._busy)" in body, body


def test_the_composer_cannot_grow_into_the_empty_chat_space():
    creation = PANEL.index("composer = QFrame()")
    layout = PANEL.index("composer_row = QHBoxLayout(composer)", creation)
    block = PANEL[creation:layout]

    assert 'enum_member(QSizePolicy, "Policy", "Fixed")' in block, block


def test_the_second_option_routes_to_the_settings_that_already_exist():
    """A second provider form would be two controls for one decision."""
    body = _method(PLUGIN, "choose_own_model")
    assert "switch_page(SETTINGS_PAGE)" in body and "provider_box" in body, body
    # The flag before the refresh is the whole reason this arrives anywhere:
    # while onboarding the stack is hidden, so moving it was a no-op and
    # pressing the option did nothing at all.
    assert body.index("_onboarding_settings = True") < body.index("_refresh_ui()"), body


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
    body = _method(PLUGIN, "_say_capabilities")
    assert "for_client(CLIENT_QGIS)" in body, (
        "the outcome map must hide groups this client cannot actually execute"
    )
    assert "session_allowance" in body, (
        "account-only capabilities were not marked, so the answer to "
        '"what do I get for signing up" was missing from the one screen that asks it'
    )


def test_the_capability_answer_is_a_turn_rather_than_a_dialog():
    """A modal is the product stepping out of its own conversation to hand
    somebody a manual, over the panel it is describing."""
    body = _method(PLUGIN, "_say_capabilities")
    assert "self._say(" in body, body
    assert "exec()" not in body and "QDialog" not in body, body


def test_every_registry_domain_is_in_a_group():
    """Grouping by task reads far better than by the registry's own domain
    names, and a hand-written grouping goes stale the moment a domain is added.
    So a new domain fails here instead of vanishing from the answer."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from mapdex_qgis._vendor.nivo.capabilities import CLIENT_QGIS, for_client

    start = PLUGIN.index("CAPABILITY_GROUPS = (")
    body = PLUGIN[start:PLUGIN.index("def _say_capabilities", start)]
    grouped = set(re.findall(r'"([a-z_]+)"', body))
    live = {c.domain for c in for_client(CLIENT_QGIS)}
    assert not (live - grouped), (
        "domains missing from the grouping, so their capabilities vanish from the "
        "answer: {}".format(sorted(live - grouped))
    )


def test_capability_discovery_is_not_a_registry_dump():
    body = _method(PLUGIN, "_say_capabilities")
    assert 'lines.extend("  " + summary' not in body, body
    assert 'actions.append({' in body
    assert 'tone": "normal"' in body
    assert 'opening=True' in body
    for choice in ("Understand a layer", "Make or refine a map", "Run a verified workflow"):
        assert choice in PLUGIN, choice


def test_choosing_a_discovery_prompt_removes_the_choice_card():
    body = _method(PLUGIN, "_first_look_action")
    ask = body[body.index("if kind == first_look.ASK:"):]
    assert 'if not turn.get("opening")' in ask
    assert ask.index('if not turn.get("opening")') < ask.index("self.ask_nivo()")


def test_all_turns_share_one_wide_screen_measure():
    body = _method(PLUGIN, "_turn_widget")
    assert "QSizePolicy," in PLUGIN[:PLUGIN.index("from qgis.core import")]
    assert "card.setMaximumWidth(BUBBLE_WIDTH)" in body, body
    assert body.index("card.setMaximumWidth(BUBBLE_WIDTH)") < body.index("if sender !="), body


def test_settings_form_is_a_named_panel_not_loose_fields():
    assert 'connection_panel.setObjectName("mapdexSettingsPanel")' in PANEL
    assert 'settings_title = QLabel("Settings")' in PANEL
    assert 'settings_title.setObjectName("mapdexSettingsTitle")' in PANEL
    assert 'settings_description.setObjectName("mapdexSettingsDescription")' in PANEL
    assert "Choose how Nivo connects" in PANEL


def test_header_controls_use_direct_svg_icons_and_combos_keep_painted_arrow():
    assert '"lucide-new-chat.svg"' in PANEL
    assert '"lucide-history.svg"' in PANEL
    assert 'control.setIcon(QIcon(os.path.join(' in PANEL
    assert "surface_asset_path(asset_name)" not in PANEL
    assert "class _ArrowComboBox(QComboBox):" in PANEL
    assert "painter.drawLine" in PANEL
    assert PANEL.count("_elastic(_ArrowComboBox())") == 7
    assert "QComboBox::down-arrow" not in PANEL


def test_opening_metadata_failure_uses_a_local_fallback_not_global_error_banner():
    body = _method(PLUGIN, "_refresh_opening")
    assert "@guarded" not in PLUGIN[PLUGIN.rfind("\n", 0, PLUGIN.index("def _refresh_opening")):PLUGIN.index("def _refresh_opening")]
    assert 'log_debug("Could not build Nivo opening"' in body
    assert 'opening_turns({' in body
    assert "self._nivo_opening = opening" in body




def test_open_project_uses_the_unlocalized_app_route():
    body = _method(PLUGIN, "open_project")
    assert '"/workspace/{}"' in body, body
    assert "QLocale.system" not in body and 'prefix =' not in body, body
    assert 'QUrl("{}{}".format(self.web_base, path))' in body, body


def test_open_project_uses_the_shared_secondary_button_style():
    creation = PANEL.index('open_project_button = QPushButton("Open in Mapdex")')
    placement = PANEL.index("workspace_layout.addLayout", creation)
    block = PANEL[creation:placement]

    assert 'open_project_button.setObjectName("mapdexSecondaryButton")' in block


def test_discovery_stays_at_the_top_after_a_tall_resize():
    body = _method(PLUGIN, "_render_nivo_turns")
    assert 'has_conversation = any(not turn.get("opening")' in body, body
    assert 'bar.maximum() if has_conversation else bar.minimum()' in body, body


# -- the stored key reads as stored -----------------------------------------

def test_the_settings_panel_states_the_stored_key_as_text():
    body = _method(PLUGIN, "_refresh_assistant_privacy")
    assert "panel_state.key_state_line" in body, (
        "the stored key showed only as a grey placeholder, which reads as an "
        "empty field - the reason a user with a working key reported having none"
    )


# -- one owner for the page ---------------------------------------------------

def test_the_page_is_changed_through_the_panels_own_switcher():
    """A page change owns three things at once: the stack index, the visibility
    of every page, and which segment button is checked.

    `setCurrentIndex` sets one of them. Pressing a Mapdex action therefore moved
    the stack to Task while the Nivo page stayed visible and the tab bar stayed
    on Nivo - the founder's "navigation is broken, the tab looks like Task and
    the content is not".
    """
    # Narrowed to the page stack: the combo boxes in this method legitimately
    # use setCurrentIndex, and a check that forbids the whole name would be a
    # guard nobody can satisfy.
    assert "self.tabs.setCurrentIndex" not in PLUGIN, (
        "the page is moved behind the switcher's back again"
    )
    assert "self.switch_page(" in _method(PLUGIN, "_preselect_workflow")
    assert '"switch_page": switch_page' in PANEL, "the switcher is not handed to the plugin"
    switcher = PANEL[PANEL.index("def switch_page(target):"):PANEL.index("for index, title in enumerate")]
    for owned in ("setCurrentIndex", "setVisible", "setChecked"):
        assert owned in switcher, "the switcher stopped owning {}".format(owned)


def test_the_tracer_is_withdrawn_unless_it_is_asked_for():
    """Not good enough on a real sheet, so it is not on the toolbar. Withdrawn
    rather than deleted: the live wire, the follow and their harnesses all stay
    and all still run, behind the same QSettings key the vectorizer branch uses
    so the two cannot end up with two switches for one decision."""
    assert "def tracing_enabled" in PLUGIN
    assert "mapdex/tracer/beta" in PLUGIN, "a second switch for one decision"
    install = _method(PLUGIN, "_install_vectorize_action")
    assert "if not self.tracing_enabled():" in install, install[:200]
    assert "self._vectorize_action = None" in install
    assert "if self._vectorize_action is not None:" in _method(PLUGIN, "initGui"), (
        "initGui adds a toolbar action that may not exist"
    )
