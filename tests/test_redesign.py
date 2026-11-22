"""The 0.14.0 redesign: theme, icons, Settings fields and the words the panel says.

The panel itself is Qt and cannot be imported here; what can be, is. Every rule
the redesign introduced that is not a layout lives in a plain module, and these
are its tests. The rendered result is checked by the QGIS harness and by
screenshots, not here.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import (  # noqa: E402
    confirm_card, icons, job_items, nivo_examples, panel_state, task_price, task_sources, theme,
    workspace,
)

PANEL = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
UI = (ROOT / "mapdex_qgis" / "ui.py").read_text(encoding="utf-8")
THEME = (ROOT / "mapdex_qgis" / "theme.py").read_text(encoding="utf-8")


# -- theme -------------------------------------------------------------------

def test_the_theme_follows_the_qgis_window_lightness():
    assert theme.theme_for_lightness(30)["name"] == "dark"
    assert theme.theme_for_lightness(127)["name"] == "dark"
    assert theme.theme_for_lightness(128)["name"] == "light"
    assert theme.theme_for_lightness(240)["name"] == "light"


def test_both_themes_define_the_same_tokens():
    assert set(theme.DARK) == set(theme.LIGHT)


def test_the_stylesheet_uses_every_placeholder_it_names():
    # A token the sheet asks for and the dict lacks is a KeyError at runtime,
    # in QGIS, the first time the panel opens.
    for tokens in (theme.DARK, theme.LIGHT):
        sheet = theme.stylesheet(tokens)
        assert "{" not in sheet.replace("{{", "").replace("}}", "") or "}" in sheet
        assert tokens["primary"] in sheet


def test_one_filled_blue_and_no_purple():
    for tokens in (theme.DARK, theme.LIGHT):
        assert tokens["primary"] == "#2563EB"
        assert tokens["primary_text"] == "#FFFFFF"
    # No purple or indigo anywhere in the panel's palette (DESIGN.md 4.1).
    for banned in ("#4F46E5", "#4338CA", "#5B5BD6", "#6366F1", "#7C3AED", "#8B5CF6"):
        assert banned.lower() not in THEME.lower(), banned
        assert banned.lower() not in PANEL.lower(), banned
        assert banned.lower() not in UI.lower(), banned


def test_status_tones_are_a_closed_set_with_a_colour_each():
    for tokens in (theme.DARK, theme.LIGHT):
        for tone in theme.TONES:
            assert theme.tone_colour(tokens, tone).startswith("#")
            assert theme.tone_colour(tokens, tone, for_text=True).startswith("#")


def test_one_radius():
    assert theme.RADIUS == 6
    # No second radius for surfaces and controls. The exceptions are genuine
    # circles and pills (the count badge, the avatar) and the thin progress
    # and scroll tracks, whose rounding is half their own thickness.
    allowed = ("Badge", "Avatar", "QProgressBar", "QScrollBar", "border-radius: 0")
    previous = ""
    for line in THEME.splitlines():
        if re.search(r"border-radius:\s*[1-9]", line):
            context = previous + line
            assert any(word in context for word in allowed), line.strip()
        previous = line


def test_no_hex_colour_outside_the_token_module():
    # Colours are tokens, in one place. Panel and widgets ask the theme.
    for name, source in (("panel.py", PANEL), ("ui.py", UI)):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not re.search(r'"#[0-9A-Fa-f]{6}"', stripped), "{}: {}".format(name, stripped)


# -- icons -------------------------------------------------------------------

def test_every_named_icon_is_vendored_with_its_licence():
    for name in icons.NAMES:
        assert pathlib.Path(icons.lucide_path(name)).is_file(), name
    assert (pathlib.Path(icons.LUCIDE_DIR) / "LICENSE.txt").is_file()


def test_icons_take_the_theme_colour_and_never_currentcolor():
    svg = icons.lucide_svg("history", "#123456").decode("utf-8")
    assert "#123456" in svg
    assert "currentColor" not in svg


def test_a_missing_icon_fails_loudly():
    try:
        icons.lucide_svg("no-such-icon", "#000000")
    except FileNotFoundError:
        return
    raise AssertionError("a missing icon rendered as nothing")


def test_status_icons_have_a_distinct_shape_per_state_kind():
    # Done, waiting/failed, working and idle each have their own shape. Warn
    # and err share the exclamation mark, as on the web app; their WORD
    # ("Needs you", "Failed") tells them apart, never the colour alone.
    shapes = {tone: icons.status_svg(tone, "#000000") for tone in ("ok", "warn", "working", "neutral")}
    assert len(set(shapes.values())) == len(shapes)
    assert icons.status_svg("err", "#000000") == icons.status_svg("warn", "#000000")


# -- Settings: which fields each model choice shows ----------------------------

def test_mapdex_shows_no_model_fields():
    assert panel_state.model_choice_for("") == "mapdex"
    assert panel_state.model_fields_for("") == {
        "vendor": False, "model": False, "endpoint": False, "key": False}


def test_own_key_shows_vendor_model_and_key_but_no_endpoint():
    assert panel_state.model_choice_for("openai") == "own"
    assert panel_state.model_fields_for("openai") == {
        "vendor": True, "model": True, "endpoint": False, "key": True}


def test_a_compatible_endpoint_also_asks_for_its_address():
    assert panel_state.model_fields_for("openai_compatible")["endpoint"] is True


def test_a_local_model_needs_no_key():
    assert panel_state.model_choice_for("ollama") == "local"
    assert panel_state.model_fields_for("ollama") == {
        "vendor": False, "model": True, "endpoint": True, "key": False}


def test_the_three_choices_and_the_vendor_menu():
    assert [choice[0] for choice in panel_state.MODEL_CHOICES] == ["mapdex", "own", "local"]
    assert "ollama" not in [value for _label, value in panel_state.KEY_PROVIDER_CHOICES]


# -- Nivo start screen ---------------------------------------------------------

def _state(**layer):
    return {"layer": layer}


def test_polygon_examples_are_three_and_name_the_layer():
    rows = nivo_examples.examples(_state(name="parcels", kind="vector", geometry="Polygon", crs="EPSG:2154"))
    assert len(rows) == 3
    assert all("parcels" in row["label"] for row in rows)


def test_no_layer_still_offers_something_to_do():
    assert nivo_examples.examples({})


def test_an_unplaced_scan_is_a_blocking_finding_in_amber_not_red():
    state = {"layer": {"name": "scan", "kind": "raster", "georeferenced": False}}
    finding = nivo_examples.blocking_finding(state)
    if finding is not None:
        assert finding["tone"] == "warn"


def test_the_context_line_states_count_and_crs():
    line = nivo_examples.context_line({"name": "polygon1", "kind": "vector", "feature_count": 24,
                                       "crs": "EPSG:32635"})
    assert line == "polygon1 · 24 features · EPSG:32635"
    assert nivo_examples.context_line({"name": "x", "kind": "vector", "feature_count": 1}).endswith("No CRS")
    assert nivo_examples.context_line(None) == "No active layer"


def test_measured_is_said_only_for_a_measurement():
    measured = nivo_examples.turn_status({"capability": "analytics.geometry@1"})
    assert measured["word"] == "Measured" and measured["tone"] == "ok"
    assert nivo_examples.turn_status({"text": "Hello"}) is None
    failed = nivo_examples.turn_status({"steps": [{"status": "failed", "title": "Buffer"}]})
    assert failed["tone"] == "err"


def test_steps_summary_counts_steps_and_time():
    assert nivo_examples.steps_summary([]) == ""
    summary = nivo_examples.steps_summary([{"duration_ms": 310}, {"duration_ms": 90}])
    assert summary.startswith("2 steps")


# -- the confirmation card ---------------------------------------------------------

def test_a_buffer_card_states_input_distance_and_result():
    card = confirm_card.processing_card(
        {"params": {"operation": "buffer", "distance": 25}},
        {"name": "line1", "count": 3, "family": "line"},
        {"strategy": "reproject", "crs": "EPSG:32635"},
    )
    labels = [label for label, _value in card["rows"]]
    assert labels == ["Input", "Distance", "Result"]
    assert dict(card["rows"])["Distance"] == "25 m, measured in EPSG:32635"
    assert card["title"].endswith("?")
    assert card["confirm"] == "Create layer" and card["cancel"] == "Cancel"


# -- Task: the button counts sheets and carries its price -----------------------

def test_the_start_button_counts_sheets():
    sources = [task_sources.file_source("/tmp/a.tif"), task_sources.file_source("/tmp/b.tif")]
    assert task_sources.start_action(sources, "georeference") == "Place 2 sheets"
    assert task_sources.start_action([], "georeference")


def test_the_button_price_is_no_charge_a_figure_or_nothing():
    kinds = [{"kind": "georeference", "cents": 0, "leave_cents": 200},
             {"kind": "digitize_parcels", "cents": 500}]
    assert task_price.button_price(kinds, "georeference", 2) == "No charge"
    assert task_price.button_price(kinds, "digitize_parcels", 2).startswith("$")
    # Unknown is never zero.
    assert task_price.button_price([], "digitize_parcels", 2) == ""


# -- Jobs: the summary leads with what needs a person ------------------------------

def test_the_jobs_headline_leads_with_what_needs_you():
    items = [{"state": "succeeded"}, {"state": "needs_review"}, {"state": "failed"}, {"state": "running"}]
    headline = job_items.jobs_headline(items)
    assert headline.startswith("4 sheets")
    assert "need" in headline


# -- workspace: no project selector -----------------------------------------------

def test_tasks_go_to_the_workspace_default_project():
    projects = [{"id": "p1", "name": "A"}, {"id": "p2", "name": "Default Project", "is_default": True}]
    assert workspace.default_project_id(projects) == "p2"
    assert workspace.default_project_id([]) == ""


def test_a_label_renamed_after_creation_keeps_a_style():
    # Measured on QGIS 3.34 / Qt 5: a label made as "mapdexMeta" and then
    # renamed for a test handle lost its size and rendered in the body voice,
    # which a larger default font made obvious. Every such rename is styled.
    renamed = re.findall(
        r'(\w+) = ui\.label\([^\n]*\)\n\s+\1\.setObjectName\("(mapdex\w+)"\)', PANEL)
    assert renamed, "the pattern this guards no longer matches anything"
    sheet = theme.stylesheet(theme.DARK)
    missing = [name for _var, name in renamed if "#" + name not in sheet]
    assert not missing, missing


# -- icons: whole Lucide glyphs, one per source kind ---------------------------

def _svg_elements(name):
    svg = (ROOT / "mapdex_qgis" / "assets" / "lucide" / "{}.svg".format(name)).read_text(encoding="utf-8")
    return svg, re.findall(r"<(path|rect|circle|line|polyline|polygon|ellipse)\b", svg)


def test_every_icon_is_a_whole_lucide_glyph_with_its_version():
    for name in icons.NAMES:
        svg, elements = _svg_elements(name)
        assert svg.startswith("<!-- Lucide 1.17.0 (ISC)"), name
        assert elements, name


def test_source_kind_icons_carry_every_part_of_the_glyph():
    # "layers" is three stacked sheets; a copy with one path drew a single
    # diamond, which read as a different symbol.
    assert _svg_elements("layers")[1].count("path") == 3
    assert sorted(_svg_elements("image")[1]) == ["circle", "path", "rect"]
    assert _svg_elements("file-text")[1].count("path") >= 4


def test_a_source_row_icon_follows_its_kind():
    start = PANEL.index("def _icon_for")
    body = PANEL[start:PANEL.index("def _build_row", start)]
    assert '.endswith(".pdf")' in body and '"file-text"' in body
    assert '"vector"' in body and '"layers"' in body
    assert '"raster"' in body and '"image"' in body


# -- the panel's own font ------------------------------------------------------

def test_geist_is_bundled_with_its_licence():
    fonts = ROOT / "mapdex_qgis" / "assets" / "fonts"
    for name in ("Geist-Regular.ttf", "Geist-Medium.ttf", "Geist-SemiBold.ttf"):
        data = (fonts / name).read_bytes()
        assert data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"), name
    assert "SIL OPEN FONT LICENSE" in (fonts / "OFL.txt").read_text(encoding="utf-8").upper()


def test_the_font_applies_inside_the_panel_only():
    sheet = theme.stylesheet(theme.DARK, "Geist")
    assert sheet.lstrip().startswith('QWidget { font-family: "Geist"; }')
    assert "font-family" not in theme.stylesheet(theme.DARK).split("}")[0]
    # The QGIS window keeps its own font: nothing in the plugin sets the
    # application font.
    call = re.compile(r"(QApplication|QGuiApplication|qApp)(\.instance\(\))?\.setFont\(")
    for path in (ROOT / "mapdex_qgis").rglob("*.py"):
        assert not call.search(path.read_text(encoding="utf-8")), path


# -- Jobs: one state, a real clock, a fraction only when there is one ------------

def test_one_state_for_the_job():
    assert job_items.batch_status("queued", {"total": 1}, True)["word"] == "Queued"
    assert job_items.batch_status("running", {"total": 1}, True)["word"] == "Working"
    assert job_items.batch_status("", {}, True, uploading=True)["word"] == "Uploading"
    running = job_items.batch_status("running", {"total": 1}, True)
    assert running["tone"] == "working"


def test_a_bar_only_for_a_real_fraction():
    assert job_items.sheet_fraction({"total": 1, "succeeded": 0}) is None
    fraction = job_items.sheet_fraction({"total": 4, "succeeded": 1, "failed": 1})
    assert fraction == {"done": 2, "total": 4, "text": "2 of 4 sheets"}
    assert "%" not in fraction["text"]


def test_the_clock_reads_like_a_clock():
    assert job_items.elapsed_text(12) == "12 s"
    assert job_items.elapsed_text(72) == "1 min 12 s"
    assert job_items.elapsed_text(3780) == "1 h 3 min"
    assert job_items.elapsed_text("x") == ""


def test_the_running_count_and_the_server_clock():
    assert job_items.running_count({"total": 4, "succeeded": 1}) == 3
    assert job_items.running_count({"total": 1}) == 1
    assert job_items.created_at_seconds("1970-01-01T00:01:00Z") == 60.0
    assert job_items.created_at_seconds("") == 0.0
    assert job_items.created_at_seconds("not a date") == 0.0


# -- Nivo: a trace only when something ran ----------------------------------------

def test_a_plain_reply_shows_no_trace():
    assert not nivo_examples.trace_worth_showing([])
    assert not nivo_examples.trace_worth_showing([
        {"capability": "nivo:classify", "status": "succeeded"},
        {"capability": "nivo:compose", "status": "succeeded"},
    ])
    assert nivo_examples.trace_worth_showing([
        {"capability": "nivo:classify", "status": "succeeded"},
        {"capability": "spatial:measure@1", "status": "succeeded"},
    ])
    assert nivo_examples.trace_worth_showing([{"capability": "nivo:plan", "status": "failed"}])


# -- Settings: contact, and versions only on the clipboard -------------------------

def test_copy_details_carries_versions_only():
    text = panel_state.support_details("0.14.0", "4.0.2-Norrköping", "6.11.0", "Windows-11")
    lines = text.split("\n")
    assert lines == [
        "Mapdex for QGIS 0.14.0", "QGIS 4.0.2-Norrköping", "Qt 6.11.0", "System Windows-11"]
    assert "@" not in text and "token" not in text.lower()


def test_the_footer_offers_contact_not_an_issue_tracker():
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert 'CONTACT_URL = "https://mapdex.ai/contact"' in plugin
    assert "Contact us" in PANEL and "Copy details" in PANEL
    assert "Report a problem" not in PANEL


# -- answers can be copied -------------------------------------------------------

def test_an_answer_can_be_selected_with_the_mouse():
    # People copy measured numbers out of answers: the answer body is a
    # selectable label, by mouse and by keyboard, and never a painted widget.
    body = UI[UI.index("class ProseLabel(QLabel):"):UI.index("def hline():")]
    assert '"TextSelectableByMouse"' in body
    assert '"TextSelectableByKeyboard"' in body
    assert "paintEvent" not in body
    assert "html.escape(" in body, "model text must be escaped before it is shown as rich text"
    assert "line-height:" in body
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert 'ui.ProseLabel(str(text), "mapdexTurnBody")' in plugin


# -- one filled blue: send is lit only with a question in the field ---------------

def test_send_is_lit_only_with_a_question_to_send():
    assert panel_state.send_enabled(True, False, "Total area of polygon1?")
    assert not panel_state.send_enabled(True, False, "")
    assert not panel_state.send_enabled(True, False, "   ")
    assert not panel_state.send_enabled(True, False, None)
    assert not panel_state.send_enabled(True, True, "Total area?")
    assert not panel_state.send_enabled(False, False, "Total area?")
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    assert "self.nivo_input.textChanged.connect(self._refresh_send_button)" in plugin


# -- a confirmation card ends in what happened --------------------------------------

def test_an_answered_card_ends_in_an_outcome_with_icon_and_word():
    tones = set(theme.TONES)
    for state in ("pending", "running", "done", "empty", "not_run", "failed", "cancelled", "approved"):
        tone, word, _detail = confirm_card.outcome_status(state)
        assert tone in tones and word, state
    assert confirm_card.outcome_status("done", "added roads buffer, 3 features") == (
        "ok", "Done", "added roads buffer, 3 features")
    assert confirm_card.outcome_status("not_run", "the layer is no longer in the project")[:2] == ("err", "Not run")
    assert confirm_card.outcome_status("failed", "QGIS stopped the buffer")[:2] == ("err", "Failed")
    assert confirm_card.outcome_status("cancelled") == ("neutral", "Cancelled", "nothing was changed")
    # A card saved by an earlier build never claims it is still running.
    assert confirm_card.outcome_status("approved")[1:] == ("Approved", "the outcome was not recorded")


def test_the_card_states_the_result_or_its_absence():
    assert confirm_card.done_detail("roads buffer", 3) == "added roads buffer, 3 features"
    assert confirm_card.done_detail("roads buffer", 1) == "added roads buffer, 1 feature"
    assert confirm_card.done_detail("roads buffer", None) == "added roads buffer"
    assert confirm_card.done_detail("", None) == "nothing was produced"
    assert confirm_card.check_outcome(True, 0) == ("done", "no invalid shapes")
    assert confirm_card.check_outcome(True, 2) == ("done", "2 invalid shapes")
    assert confirm_card.check_outcome(True, None) == ("done", "the report layer is in the project")
    # No report layer at all: the check finished, but nothing is known. Never Done.
    state, detail = confirm_card.check_outcome(False, None)
    assert state == "empty" and "no report" in detail


def _method_body(name):
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    start = plugin.index("    def {}(".format(name))
    end = plugin.find("\n    @guarded\n    def ", start + 1)
    return plugin[start:end]


def test_every_exit_of_a_confirmed_run_settles_its_card():
    body = _method_body("_run_processing_operation")
    # One place guarantees a terminal state: the wrapper runs the steps inside
    # Settlement.guarding() and closes the body; the callback does the same.
    wrapper = body[:body.index("    def _run_processing_steps(")]
    assert "with settlement.guarding():\n            self._run_processing_steps(action, settlement)" in wrapper
    assert "settlement.close_body()" in wrapper
    assert "with settlement.guarding():\n                    finish(successful, results)" in body
    assert "settlement.close_callback()" in body
    assert 'report_unexpected(self, "the Processing result", exc)' in body
    # addTask answers 0 when QGIS did not take the task; that must settle too.
    assert "accepted = QgsApplication.taskManager().addTask(task)" in body
    assert 'settlement.settle("not_run", "QGIS did not accept the task")' in body
    assert "settlement.layer_may_exist = True\n                QgsProject.instance().addMapLayer(output_layer)" in body
    # The settlement writes to the card, and nothing settles it around the object.
    assert "lambda state, detail: self._settle_confirmation(action, state, detail)" in wrapper
    assert "self._settle_confirmation(action" not in body.replace(
        "lambda state, detail: self._settle_confirmation(action, state, detail)", "")
    # Layer gone, no algorithm, empty input, the task failing, and both
    # successes each settle the card; every other refusal goes through not_run.
    assert 'settlement.settle("not_run", "the layer is no longer in the project")' in body
    assert 'settlement.settle("not_run", missing)' in body
    assert body.count("self._action_failed(") == 1, "a refusal bypasses not_run and leaves the card running"
    assert '"failed", "QGIS stopped the {} before it finished"' in body
    assert '"done" if (output_name and produced != 0) else "empty"' in body
    assert "settlement.settle(*confirm_card.check_outcome(output_layer is not None, produced))" in body


# -- statuses: outcomes stay, only progress notes fade -------------------------------

def test_an_outcome_refusal_or_error_status_does_not_clear_itself():
    for tone in ("info", "ok", "warn", "err"):
        assert not panel_state.status_fades(tone, False), tone
    assert not panel_state.status_fades("warn", True)
    assert not panel_state.status_fades("err", True)
    assert panel_state.status_fades("info", True)
    panel = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
    assert "panel_state.status_fades(tone, transient)" in panel
    plugin = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    for sentence in ("QGIS rejected the new features.", "Approve code {user_code} in your browser"):
        line = next(line for line in plugin.splitlines() if sentence in line)
        assert "transient=True" not in line, sentence


# -- tooltips: names are text, never markup ------------------------------------------

def test_markup_in_a_name_is_shown_as_characters_in_a_tooltip():
    import html as _html

    name = 'Atlas <b>Survey</b> & <img src="x"> Co.'
    tip = panel_state.plain_tooltip(name)
    assert tip.startswith("<qt>") and tip.endswith("</qt>")
    assert "<b>" not in tip and "<img" not in tip
    assert _html.unescape(tip[len("<qt>"):-len("</qt>")]) == name
    assert panel_state.plain_tooltip("") == ""
    assert panel_state.plain_tooltip(None) == ""
    # Every tooltip that can carry a workspace, layer, file or account name.
    start = UI.index("class ElidedLabel(QLabel):")
    elided = UI[start:UI.index("def paintEvent", start)]
    assert elided.count("self.setToolTip(plain_tooltip(self._full))") == 2
    assert "self.setToolTip(self._full)" not in UI
    assert "self.setToolTip(hint)" not in UI
    # An icon button's tooltip can name a file or layer ("Remove <file> from this task").
    assert "self.setToolTip(tooltip)" not in UI
    assert "self.setToolTip(plain_tooltip(tooltip))" in UI
    remove = panel_state.plain_tooltip("Remove {} from this task".format("<b>plan</b>.pdf"))
    assert "<b>" not in remove and "&lt;b&gt;plan&lt;/b&gt;.pdf" in remove
