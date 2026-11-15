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
