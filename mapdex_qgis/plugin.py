from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from functools import partial
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt, QLocale, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsGeometry,
    QgsFeature,
    QgsCsException,
    Qgis,
    QgsMapLayer,
    QgsMessageLog,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .build_profile import (
    ALLOW_CUSTOM_ENDPOINT_SETTING,
    endpoints_unlocked,
    resolve_endpoints,
)
from .api_client import (
    MapdexAPI,
    MapdexAPIError,
    device_verification_url,
    is_transport_secure,
    normalize_api_base,
    safe_filename_part,
    same_origin,
)
from .build_version import PLUGIN_VERSION
from .connections import discover_connections, qgis_connection_names
from .credentials import (
    HOSTED_PROVIDER_NAME,
    ProviderCredentialStore,
    describe_privacy,
    public_settings,
)
from .generated_contracts import BatchKind
from ._vendor.nivo.providers import ProviderError, build_provider, default_model_for, resolve_runtime
from ._vendor.nivo.agent import AgentSession
from .byok import (
    ByokTurn,
    is_byok,
    needs_mapdex_account,
    provider_failed,
    session_allowance,
)
from ._vendor.nivo.capabilities import (
    CLIENT_QGIS,
    CapabilityError,
    for_client,
    get as get_capability,
    validate_request,
)
from .qgis_runtime import (
    PLUGIN_BOUND_CAPABILITIES,
    QGISRuntime,
    RuntimeUnavailable,
    build_executor,
    raster_is_georeferenced,
)
from ._vendor.nivo.features import (
    can_place,
    describe_placement,
    describe_unplaceable_geometry,
    plan_points,
    scatter_in_rectangle,
)
from .guard import describe_exception, format_traceback, guarded, log_debug
from .guidance import (
    ACTIVE_STATES,
    failure_next_step,
    run_has_started,
    task_guidance,
    with_failure_guidance,
)
from .layout_rules import BUBBLE_WIDTH, MINIMUM_WIDTH, PREFERRED_WIDTH
from .nivo import (
    GEOMETRY_CHOICES,
    THREAD_ID_SETTING,
    THREAD_PROJECT_SETTING,
    allowed_actions,
    companion_context,
    confirmation_actions,
    describe_thread,
    geometry_from_choice,
    remembered_thread,
    thread_is_gone,
    thread_list_items,
    thread_title,
    thread_turns,
    transition,
)
from .panel import action_row, build_companion_panel, build_thread_history_dialog
from . import branding
from . import first_look
from . import panel_state
from ._vendor.nivo.processing import (
    PROCESSING_OPERATION_CATALOG,
    PROCESSING_OUTPUT_ORDER,
    UNIT_SENSITIVE_TERRAIN,
    build_algorithm_parameters,
    describe_empty_input,
    describe_geographic_terrain_refusal,
    describe_missing_algorithm,
    describe_processing_outcome,
    operation_label,
    resolve_processing_algorithm,
)
from .qt_compat import QAction, enum_member, qgis_version
from ._vendor.nivo.viewport import resolve_extent
from .results import (
    batch_failure,
    batch_is_terminal,
    batch_state,
    collect_geojson_artifact_urls,
    collect_layer_imports,
    every_failure_needs_placement,
    first_batch_error,
    geojson_truncation_notice,
    review_run_ids,
    run_state,
    select_review_run,
    split_review_buckets,
    succeeded_run_ids,
    summarize_runs,
)
from .token_store import LEGACY_TOKEN_SETTING, qgis_token_store
from .continuation import action_result, continuation_budget, should_continue
from .plan_offer import offer_prompt, plan_offer, plan_run_report
from .trace_view import budget_label, step_rows
from .source_info import inspect_paths
from .workspace import review_workspace_path, task_workspace_path


WORKFLOWS = (
    ("Georeference maps", BatchKind.GEOREFERENCE),
    ("Digitize parcels", BatchKind.DIGITIZE_PARCELS),
    ("Validate & deliver", BatchKind.VALIDATE_DELIVER),
    ("Full pipeline", BatchKind.FULL_PIPELINE),
)

# The legacy `qgis:*` vocabulary, translated to the capability each id has
# always meant. There is one dispatch path: a canonical `domain.name@1` from the
# server and a legacy id from an older server both resolve to the same
# registered capability, are validated by the registry, and run through the same
# executor table. Before this, the legacy half was an eighteen-branch if/elif
# chain of hand-written QGIS calls that duplicated - and in places contradicted -
# the runtime it sat next to: the visibility branch would raise on a layer that
# is not in the layer tree, and nothing the chain did was recorded on the undo
# stack, so `style.undo@1` could not reverse it.
#
# This table only ever shrinks. A new capability is added to the registry and
# the executor table, never here.
LEGACY_CAPABILITY_IDS = {
    "qgis:zoom_to_layer@1": "map.zoom_layer@1",
    "qgis:zoom_to_selection@1": "map.zoom_selection@1",
    "qgis:zoom_to_extent@1": "map.zoom_extent@1",
    "qgis:refresh_canvas@1": "map.refresh@1",
    "qgis:previous_extent@1": "map.previous_extent@1",
    "qgis:add_xyz_basemap@1": "map.basemap@1",
    "qgis:inspect_layer@1": "inspect.layer@1",
    "qgis:open_attribute_table@1": "layer.attribute_table@1",
    "qgis:set_layer_visibility@1": "layer.visibility@1",
    "qgis:set_layer_opacity@1": "layer.opacity@1",
    "qgis:select_all@1": "selection.all@1",
    "qgis:clear_selection@1": "selection.clear@1",
    "qgis:invert_selection@1": "selection.invert@1",
}

# The shapes the canvas tool can collect, which is the enum draw.geometry@1
# declares. Multi-part geometries are absent on purpose: a user drawing one
# shape has drawn one shape, and offering "multipolygon" would promise a second
# part there is no gesture to start.
DRAWABLE_GEOMETRIES = ("point", "linestring", "polygon")

# The legacy ids with no registered capability behind them. Each names a method
# on the plugin rather than a branch in a conditional, so the dispatcher stays
# one lookup whether or not the registry knows the id.
PLUGIN_NATIVE_ACTIONS = {
    "qgis:add_features@1": "_run_add_features",
    "qgis:create_layer@1": "_run_create_layer",
    "qgis:open_processing@1": "_run_open_processing",
    "qgis:next_extent@1": "_run_next_extent",
}

DEFAULT_API = "https://api.mapdex.ai"
DEFAULT_WEB = "https://mapdex.ai"
# While a task waits for browser review the panel keeps a slow watch, so an
# approved result still lands in QGIS without the user pressing Resume.
REVIEW_POLL_MS = 15000


# Widget handles filled from build_companion_panel and cleared on unload, so a
# callback that outlives the panel meets None instead of a destroyed object.
PANEL_WIDGET_REFS = (
    "status", "connection_label", "api_url_input", "web_url_input",
    "save_settings_button", "settings_button", "connect_button",
    "disconnect_button", "workspace",
    "batch_group", "batch_title", "phase_label", "progress_bar", "guidance_label",
    "project_box", "workflow_box", "input_box", "source_summary", "run_button",
    "cancel_button", "retry_button", "import_button", "review_button",
    "open_project_button", "recent", "recent_box", "resume_button",
    "connect_promise", "sign_in", "tail", "body_layout",
    "first_open_prompt", "first_open_title",
    "own_model", "own_model_button", "segment_bar",
    "tabs", "workspace_body", "workspace_locked", "jobs_locked",
    "assistant_key_state",
    "nivo_context", "nivo_runtime", "composer",
    "nivo_reply", "nivo_input", "nivo_send_button",
    "nivo_stop_button", "nivo_status",
    "nivo_new_button", "nivo_history_button",
)


def transcript_turn(sender, text, steps=None, actions=None, severity="", fact=""):
    """One transcript entry, and the only place its shape is written.

    A dict rather than a tuple, learned the hard way: the entry used to be
    `(sender, text)`, grew a third element, and seven call sites went on
    writing two. plugin.py cannot be imported by the headless suite, so a shape
    mismatch here is invisible until a user hits it inside QGIS. A tuple that
    has now grown twice more - actions, a severity, a measured fact line -
    would be that bug waiting a third time.
    """
    return {
        "sender": sender,
        "text": str(text),
        "steps": list(steps or []),
        "actions": list(actions or []),
        "severity": str(severity or ""),
        "fact": str(fact or ""),
    }


def opening_turns(state):
    """The opening reading, as transcript entries.

    Module level and pure so the render harness can draw exactly what the
    panel draws. It kept its own copy of this and the two drifted within a day:
    the harness went on appending the capability chip to the last finding after
    the product had moved it to a row of its own, so the screenshots were of a
    layout nobody shipped.
    """
    reading = first_look.opening_reading(state)
    layer_id = state.get("layer_id", "")
    turns = [
        transcript_turn(
            "assistant",
            finding["detail"],
            actions=[dict(action, layer_id=layer_id) for action in finding["actions"]],
            severity=finding["severity"],
            fact=finding["headline"],
        )
        for finding in reading["findings"]
    ]
    # "What can this thing do" is the question a stranger has in front of every
    # one of these states, so it is offered from all of them - on its own row,
    # because three chips do not fit a narrow dock and Qt has no wrapping row
    # to rescue them.
    if turns:
        turns.append(transcript_turn(
            "assistant", "",
            actions=[{"label": "What can Nivo do?", "kind": "capabilities"}]))
    return turns


def plugin_icon() -> QIcon:
    """The listing mark: the indigo symbol `metadata.txt` names.

    Kept for the one surface that has no palette to read - the plugin manager
    and plugins.qgis.org - and as the last fallback for a build missing its
    assets, because a QIcon with no file draws nothing and an invisible button
    is worse than a wrongly coloured one. Inside QGIS the identity is
    `mapdex_mark_icon()`: see branding.py and MEMORY hard rule 27, which makes
    the black/white symbol the default mark and the blue one an accent variant.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), branding.LISTING_MARK)
    return QIcon(path) if os.path.isfile(path) else QIcon()


def surface_asset_icon(name: str) -> QIcon:
    """A glyph for the PANEL's surface rather than for the QGIS interface.

    `themed_asset_icon` reads the application palette, which is right for the
    toolbar - that bar is whatever colour the user's theme makes it. It is
    wrong inside this panel, which paints its own `#191919` ground whatever the
    host theme is: on a LIGHT QGIS theme it picked the ink variant and drew a
    near-black glyph on near-black, which on screen is indistinguishable from
    an icon that failed to load.

    Measured rather than reasoned: on a light interface the four panel glyphs
    came back at relative luminance 0.06 against the surface's own 0.09.

    So the panel always takes the light-coloured file - the one whose name says
    `_dark`, meaning "for a dark surface".
    """
    stem, _, extension = name.rpartition(".")
    return asset_icon("{}_dark.{}".format(stem or name, extension or "png"))


def mapdex_mark_icon() -> QIcon:
    """The Mapdex mark for the interface QGIS is currently wearing.

    Routed through `themed_asset_icon` rather than resolving the file here, so
    the mark inherits the fallback chain the tracer icon already has: the dark
    variant, then the light one, then the listing mark. One indigo picture on
    every theme is what this replaces.
    """
    return themed_asset_icon(branding.MARK_FOR_LIGHT_INTERFACE)


def asset_icon(name: str) -> QIcon:
    """A packaged icon from assets/, falling back to the Mapdex mark.

    Two buttons sharing one icon is not a toolbar, it is a guess: the tracer
    and the panel sit side by side and the user has to be able to tell which is
    which without hovering. The fallback keeps a build whose asset is missing
    usable rather than blank, which matters because a QIcon with no file draws
    nothing at all and the button becomes invisible.

    The glyph is generated by scripts/make_tracer_icon.py on a 24-unit grid,
    the size a QGIS toolbar actually draws, so it is reproducible rather than a
    binary nobody can regenerate. It deliberately does NOT reuse the Mapdex
    mark: docs/DESIGN.md forbids redrawing it, and the mark is already on the
    button next to this one.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
    return QIcon(path) if os.path.isfile(path) else plugin_icon()


def interface_is_dark() -> bool:
    """Is the QGIS window dark? Measured from the palette, not from its name.

    QGIS ships Night Mapping and Blend of Gray, users install their own, and on
    macOS and Windows the system theme can darken the application without any
    QGIS setting changing at all. Matching theme NAMES would be a hardcoded
    list that is wrong for every theme nobody thought of, which is a mistake
    this repository has paid for elsewhere and has one answer: read the value.

    The window background lightness is that value. Below the midpoint the bar
    behind the toolbar is dark, whatever anyone called the theme.
    """
    try:
        from qgis.PyQt.QtGui import QPalette  # noqa: PLC0415 - Qt-only import
        from qgis.PyQt.QtWidgets import QApplication  # noqa: PLC0415

        application = QApplication.instance()
        if application is None:
            return False
        window = application.palette().color(
            enum_member(QPalette, "ColorRole", "Window"))
        return window.lightness() < 128
    except Exception as exc:  # noqa: BLE001 - a wrong icon beats no toolbar
        log_debug("reading the interface palette", exc)
        return False


def themed_asset_icon(name: str) -> QIcon:
    """The dark variant of an asset on a dark interface, else the light one.

    Falls back through the light variant to the Mapdex mark, so a build missing
    the dark file shows a wrong-but-visible icon rather than an empty button.
    """
    if interface_is_dark():
        stem, _, extension = name.rpartition(".")
        dark = "{}_dark.{}".format(stem or name, extension or "png")
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assets", dark)
        if os.path.isfile(path):
            return QIcon(path)
    return asset_icon(name)


# --------------------------------------------------------------------------
# Reporting a capability result
# --------------------------------------------------------------------------
#
# The analytics kernel computes a mean, a frequency table, an outlier rule and
# its bounds - and the transcript used to print the server's one-line summary
# followed by the result's `kind` in brackets. The numbers were measured and
# then discarded, which is what made an assistant that can profile a cadastral
# layer read as one that does nothing useful.
#
# These functions state what was measured and nothing else. Every value comes
# out of the result object; a field the result does not carry is not mentioned
# rather than defaulted, because a fabricated zero in a statistics line is worse
# than a shorter line.

SIMPLE_RESULT_PHRASES = {
    "zoomed": "Zoomed to the layer.",
    "zoomed_to_selection": "Zoomed to the selection.",
    "zoomed_to_extent": "Moved the map to that area.",
    "refreshed": "Redrew the map.",
    "previous_extent": "Went back to the previous view.",
    "table_opened": "Opened the attribute table.",
    "layer_activated": "Made that the active layer.",
    "selected_all": "Selected every feature in the layer.",
    "selection_cleared": "Cleared the selection.",
    "selection_inverted": "Inverted the selection.",
    "filter_cleared": "Removed the filter.",
    "no_map_change": "Nothing on the map needed to change.",
    "nothing_to_undo": "There is nothing to undo.",
    "reorder_unchanged": "That layer is already in that position.",
    # Started, not finished. The run list has to be fetched before it can be
    # reported, and naming a run to review is not the same as having opened it.
    "review_requested": "Looking for the run to review…",
}


def _pretty_number(value):
    """A readable number, or the value unchanged when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return str(value)
    magnitude = abs(number)
    if magnitude and (magnitude < 0.001 or magnitude >= 1e12):
        return "{:.4g}".format(number)
    if number.is_integer():
        return "{:,}".format(int(number))
    return "{:,.4f}".format(number).rstrip("0").rstrip(".")


def _joined(parts):
    return " · ".join(part for part in parts if part)


def _describe_numeric(result):
    if not result.get("usable"):
        return "no usable numbers in that field ({} empty)".format(_pretty_number(result.get("nulls") or 0))
    parts = [
        "{} values".format(_pretty_number(result.get("usable"))),
        "mean {}".format(_pretty_number(result.get("mean"))),
        "median {}".format(_pretty_number(result.get("median"))),
        "min {}".format(_pretty_number(result.get("min"))),
        "max {}".format(_pretty_number(result.get("max"))),
    ]
    if result.get("nulls"):
        parts.append("{} empty".format(_pretty_number(result["nulls"])))
    if result.get("scope") and result["scope"] != "all":
        parts.append("scope: {}".format(result["scope"]))
    if result.get("truncated"):
        parts.append("the read stopped at the row limit, so this describes part of the layer")
    return _joined(parts)


def _describe_categorical(result):
    categories = result.get("categories") or []
    if not categories:
        return "no values to count in that field"
    head = ", ".join(
        "{} ({})".format(entry.get("value"), _pretty_number(entry.get("count")))
        for entry in categories[:5]
    )
    parts = ["{} distinct values in {}".format(
        _pretty_number(result.get("distinct")), _pretty_number(result.get("usable"))), head]
    if result.get("truncated"):
        parts.append("more categories not shown")
    if result.get("nulls"):
        parts.append("{} empty".format(_pretty_number(result["nulls"])))
    return _joined(parts)


def _describe_histogram(result):
    bins = result.get("bins") or []
    if not bins:
        return "no numeric values to bin"
    return "{} bins from {} to {}".format(
        len(bins), _pretty_number(bins[0].get("min")), _pretty_number(bins[-1].get("max")))


def _describe_top_n(result):
    rows = result.get("rows") or []
    parts = ["{} of {} by {}".format(
        _pretty_number(result.get("matched")), _pretty_number(result.get("considered")), result.get("field"))]
    if rows:
        parts.append("top values " + ", ".join(_pretty_number(row.get("value")) for row in rows[:5]))
    return _joined(parts)


def _describe_outliers(result):
    if not result.get("matched"):
        if result.get("reason") == "not_enough_values":
            return "too few values to judge an outlier"
        return "no outliers under {}".format(result.get("rule") or result.get("method"))
    return _joined([
        "{} of {} flagged".format(
            _pretty_number(result.get("matched")), _pretty_number(result.get("considered"))),
        "rule: {}".format(result.get("rule") or result.get("method")),
        "outside {} to {}".format(
            _pretty_number(result.get("lower_bound")), _pretty_number(result.get("upper_bound"))),
    ])


def _describe_group(result):
    groups = result.get("groups") or []
    if not groups:
        return "nothing to group"
    head = ", ".join(
        "{} {}".format(entry.get("group"), _pretty_number(entry.get("value"))) for entry in groups[:5])
    label = result.get("statistic") or "count"
    if result.get("value_field"):
        label = "{} of {}".format(label, result["value_field"])
    parts = ["{} by {}".format(label, result.get("group_field")),
             "{} groups".format(_pretty_number(result.get("group_count"))), head]
    if result.get("skipped_null_values"):
        parts.append("{} rows had no value".format(_pretty_number(result["skipped_null_values"])))
    return _joined(parts)


def _describe_comparison(result):
    left = result.get("left") or {}
    right = result.get("right") or {}
    delta = result.get("delta") or {}
    parts = ["mean {} vs {}".format(_pretty_number(left.get("mean")), _pretty_number(right.get("mean")))]
    if isinstance(delta.get("mean_percent"), (int, float)):
        parts.append("{}% difference".format(_pretty_number(delta["mean_percent"])))
    parts.append("{} vs {} values".format(
        _pretty_number(left.get("usable")), _pretty_number(right.get("usable"))))
    return _joined(parts)


def _describe_breaks(result):
    return "{} {} classes from {} to {}".format(
        _pretty_number(result.get("classes")), result.get("method"),
        _pretty_number(result.get("min")), _pretty_number(result.get("max")))


def _describe_layer_profile(result):
    parts = [str(result.get("name") or ""), str(result.get("type") or "")]
    if result.get("type") == "vector":
        parts.append("{} features".format(_pretty_number(result.get("feature_count"))))
        parts.append("{} fields".format(len(result.get("fields") or [])))
        if result.get("selected"):
            parts.append("{} selected".format(_pretty_number(result["selected"])))
    else:
        parts.append("{}x{} px".format(_pretty_number(result.get("width")), _pretty_number(result.get("height"))))
        parts.append("{} bands".format(_pretty_number(result.get("bands"))))
        if result.get("georeferenced") is False:
            parts.append("not georeferenced")
    parts.append(result.get("crs") or "no CRS")
    return _joined(parts)


def _describe_field_profile(result):
    fields = result.get("fields") or []
    numeric = [field["name"] for field in fields if field.get("numeric")]
    parts = ["{} fields over {} features".format(len(fields), _pretty_number(result.get("features")))]
    if numeric:
        parts.append("numeric: " + ", ".join(numeric[:8]))
    return _joined(parts)


def _describe_jobs(result):
    """The run list, stating both what matched and what the project holds.

    "3 runs" and "3 of 47 runs" answer different questions, and the second is
    the one a user filtering by state asked. A listing truncated to fit the
    transcript says so, because a silently cut list reads as a complete one.
    """
    matched = int(result.get("matched") or 0)
    total = int(result.get("total") or 0)
    state = str(result.get("state") or "all")
    if not total:
        return "this project has no Mapdex runs yet"
    if not matched:
        return "none of the {} runs in this project are {}".format(_pretty_number(total), state)
    rows = result.get("runs") or []
    head = ("{} of {} runs".format(_pretty_number(matched), _pretty_number(total))
            if state != "all" else "{} runs".format(_pretty_number(total)))
    parts = [head]
    for row in rows:
        line = "{} {}".format(row.get("state") or "unknown", row.get("title") or row.get("run_id") or "")
        failure = str(row.get("error") or "")
        parts.append("{} - {}".format(line.strip(), failure) if failure else line.strip())
    if matched > len(rows):
        parts.append("{} more not listed".format(_pretty_number(matched - len(rows))))
    return _joined(parts)


def _describe_jobs_requested(result):
    state = str(result.get("state") or "all")
    return "reading the run list" if state == "all" else "reading the {} runs".format(state)


def _describe_review_opened(result):
    return "opened the review for {} ({})".format(
        result.get("run_id") or "that run", result.get("state") or "unknown")


RESULT_DESCRIBERS = {
    "numeric": _describe_numeric,
    "categorical": _describe_categorical,
    "histogram": _describe_histogram,
    "top_n": _describe_top_n,
    "outliers": _describe_outliers,
    "group_aggregate": _describe_group,
    "comparison": _describe_comparison,
    "breaks": _describe_breaks,
    "layer_profile": _describe_layer_profile,
    "field_profile": _describe_field_profile,
    "project_profile": lambda result: "{} layers in the project".format(len(result.get("layers") or [])),
    "selection_applied": lambda result: "selected {} of {} features".format(
        _pretty_number(result.get("selected")), _pretty_number(result.get("requested"))),
    "visibility_applied": lambda result: "layer {}".format("shown" if result.get("visible") else "hidden"),
    "opacity_applied": lambda result: "opacity {}%".format(_pretty_number(result.get("opacity"))),
    "style_applied": lambda result: "{} style with {} classes".format(
        result.get("style"), _pretty_number(result.get("classes"))),
    "labels_applied": lambda result: "labelled by {}".format(result.get("field")),
    "filter_applied": lambda result: "{} matched {} features".format(
        result.get("expression"), _pretty_number(result.get("matched"))),
    "measurement": lambda result: "{} m ({})".format(
        _pretty_number(result.get("metres")), result.get("method") or "measured"),
    "export": lambda result: "wrote {} features to {}".format(
        _pretty_number(result.get("features")), result.get("path")),
    "field_calculated": lambda result: "added '{}' over {} rows, {} empty".format(
        result.get("field"), _pretty_number(result.get("rows")), _pretty_number(result.get("nulls"))),
    "field_preview": lambda result: "preview only: {} rows, {} empty".format(
        _pretty_number(result.get("rows")), _pretty_number(result.get("nulls"))),
    "jobs": _describe_jobs,
    "jobs_requested": _describe_jobs_requested,
    "review_opened": _describe_review_opened,
    "reorder_applied": lambda result: "moved to {}".format(result.get("position")),
    "basemap_added": lambda result: "added {}".format(result.get("name")),
    # The CRS is in the line on purpose. A drawn shape is data, and a layer whose
    # frame the user cannot see is one they cannot check against anything else.
    "geometry_drawn": lambda result: "drew a {} into '{}' from {} point(s), in {}".format(
        result.get("geometry"), result.get("layer_name"),
        _pretty_number(result.get("vertices")), result.get("crs")),
    "undone": lambda result: "undid the last {} change".format(result.get("change")),
    "undo_failed": lambda result: "could not undo that: {}".format(result.get("reason")),
}


def describe_capability_result(summary, result):
    """One line for the transcript, built only from what the result carries."""
    headline = str(summary or "").strip()
    if not isinstance(result, dict):
        return headline or "Done."
    # An analysis that was also drawn on the map arrives wrapped; report the
    # measurement, which is the part the user asked about.
    if "analysis" in result and isinstance(result["analysis"], dict):
        detail = describe_capability_result("", result["analysis"])
        applied = result.get("map") if isinstance(result.get("map"), dict) else {}
        if applied.get("kind") == "style_applied":
            detail = _joined([detail, "shown on the map"])
        elif applied.get("kind") == "selection_applied":
            detail = _joined([detail, "selected on the map"])
        return _joined([headline, detail]) if headline else detail
    if "layer" in result and "fields" in result and isinstance(result["layer"], dict):
        detail = _joined([
            describe_capability_result("", result["layer"]),
            describe_capability_result("", result["fields"]),
        ])
        return _joined([headline, detail]) if headline else detail
    kind = str(result.get("kind") or "")
    phrase = SIMPLE_RESULT_PHRASES.get(kind)
    if phrase:
        return phrase
    describer = RESULT_DESCRIBERS.get(kind)
    if describer is None:
        # An unrecognised kind must not be dressed up as a measurement.
        return headline or (kind.replace("_", " ") if kind else "Done.")
    try:
        detail = describer(result)
    except Exception:  # noqa: BLE001 - a transcript line must never break a turn
        detail = ""
    if not detail:
        return headline or kind.replace("_", " ")
    return "{}: {}".format(headline, detail) if headline else detail


class _WorkTask(QgsTask):
    """Small QgsTask wrapper — more reliable than QgsTask.fromFunction across builds."""

    def __init__(self, description: str, work: Callable):
        flags = enum_member(QgsTask, "Flag", "CanCancel")
        super().__init__(description, flags)
        self._work = work
        self.result = None
        self.error: Optional[BaseException] = None

    def run(self):
        try:
            self.result = self._work()
            return True
        except BaseException as exc:  # noqa: BLE001 — surface to UI callback
            self.error = exc
            return False


class MapdexPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        settings = QSettings()
        self.token_store = qgis_token_store(settings)
        persisted_token = self.token_store.load() if self.token_store is not None else ""
        # Never continue using the historical plaintext setting. Existing users
        # reconnect once and receive encrypted QGIS Authentication DB storage.
        settings.remove(LEGACY_TOKEN_SETTING)
        # A released build is pinned to the hosted service; only a development
        # build (or an explicitly unlocked install) may point somewhere else.
        self._endpoints_unlocked = endpoints_unlocked(
            settings.value(ALLOW_CUSTOM_ENDPOINT_SETTING, "")
        )
        api_base, web_base, stale_endpoint = resolve_endpoints(
            str(settings.value("mapdex/base_url", "") or ""),
            str(settings.value("mapdex/web_base", "") or ""),
            self._endpoints_unlocked,
        )
        if stale_endpoint:
            # The stored session belongs to another deployment.
            # Clearing the stored session, not assigning a password.
            persisted_token = ""  # nosec B105
            if self.token_store is not None:
                self.token_store.clear()
            settings.setValue("mapdex/base_url", api_base)
            settings.setValue("mapdex/web_base", web_base)
        self.api = MapdexAPI(api_base, persisted_token)
        self.web_base = web_base.rstrip("/")
        self.device_code = ""
        self.batch_id = ""
        self.project_id = str(settings.value("mapdex/project_id", "") or "")
        self.imported_layer_ids = set()  # type: set[str]
        self.selected_paths = []  # type: list[str]
        # The resolved assistant runtime, held so the Nivo header does not open
        # the encrypted authentication database on every layer click.
        self._assistant_runtime_cache = None
        self._last_batch: Optional[dict] = None
        self._source_label = ""
        self._pending_is_batch = False
        self._busy = False
        self._panel_root = None
        # Background tasks in flight; QGIS crashes if Python collects one early.
        self._tasks = []
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_token)
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self._poll_batch)
        self.progress_pending = False
        # A plan the panel ran directly. It belongs to no batch, so it needs its
        # own follow: without one, a turn that needed a Run showed the work,
        # started it, and then went quiet until the user thought to look in the
        # browser.
        self.plan_timer = QTimer()
        self.plan_timer.timeout.connect(self._poll_plan_run)
        self.plan_run_id = ""
        self.plan_run_pending = False
        # Whether the server has actually started the child run, and since when
        # it has been waiting. None means "not reported"; the panel then keeps
        # its neutral wording instead of guessing.
        self._backend_started: Optional[bool] = None
        self._waiting_since = 0.0
        self._start_probe_countdown = 0
        # Which terminal beat has already been announced in the QGIS message bar.
        self._announced_state = ""
        # Widget refs filled in _ensure_dock
        self.status = None
        self.connection_label = None
        self.api_url_input = None
        self.web_url_input = None
        self.save_settings_button = None
        self.settings_button = None
        self.connect_button = None
        self.connect_promise = None
        self.sign_in = None
        self.tail = None
        self.body_layout = None
        self.first_open_prompt = None
        self.first_open_title = None
        self.own_model = None
        self.own_model_button = None
        self.segment_bar = None
        self.disconnect_button = None
        self.workspace = None
        self.batch_group = None
        self.batch_title = None
        self.phase_label = None
        self.progress_bar = None
        self.guidance_label = None
        self.project_box = None
        self.workflow_box = None
        self._layer_menu_actions = []
        self._measure_action = None
        self._measure_tool = None
        self._draw_action = None
        self._draw_tool = None
        self._previous_map_tool = None
        self.input_box = None
        self.source_summary = None
        self.run_button = None
        self.cancel_button = None
        self.retry_button = None
        self.import_button = None
        self.review_button = None
        self.recent = None
        self.recent_box = None
        self.resume_button = None
        self.tabs = None
        self.workspace_body = None
        self.workspace_locked = None
        self.jobs_locked = None
        self.assistant_key_state = None
        self.nivo_context = None
        self.nivo_runtime = None
        self.nivo_reply = None
        self.nivo_status = None
        self.nivo_input = None
        self.nivo_send_button = None
        self.composer = None
        self.nivo_stop_button = None
        self.nivo_new_button = None
        self.nivo_history_button = None
        self._nivo_turns = []
        # The opening reading, rebuilt on every draw. Kept apart from the
        # conversation because it describes the layer that is active now,
        # and because clearing the transcript must bring it back.
        self._nivo_opening = []
        # A multi-step objective: what was asked, what the actions reported, and
        # how many round trips it has taken.
        self._nivo_objective = ""
        self._nivo_action_results = []
        self._nivo_round_trips = 0
        self._nivo_state = "idle"
        self._executed_nivo_actions = set()
        self._nivo_compose_task = None
        self._nivo_request_id = 0
        # The conversation this panel is continuing. Restored only when it was
        # opened in the project we are about to talk to; threads are
        # project-scoped on the server, so carrying one across would ask the
        # API to continue something that project cannot see.
        self._nivo_thread_id = remembered_thread(
            settings.value(THREAD_ID_SETTING, ""),
            settings.value(THREAD_PROJECT_SETTING, ""),
            self.project_id,
        )
        self._nivo_thread_project = self.project_id if self._nivo_thread_id else ""
        # The History dialog's widget handles while it is open, and the thread
        # rows currently listed in it, in the order they are displayed.
        self._history_refs = None
        self._history_rows = []
        self._history_request_id = 0

    @guarded
    def initGui(self):
        self.action = QAction(mapdex_mark_icon(), "Mapdex", self.iface.mainWindow())
        self.action.setToolTip("Open Mapdex for QGIS")
        self.action.triggered.connect(self.show)
        self.iface.addPluginToWebMenu("&Mapdex", self.action)
        self._install_vectorize_action()
        # Mapdex owns a toolbar rather than one icon on the shared Plugins bar.
        # The two things on it are separate products to the person using them:
        # the panel is where you ask Mapdex to do something, and the tracer is a
        # canvas tool you hold down and work with for an hour. One icon cannot
        # carry both, and a user who wants the tracer on screen with the panel
        # out of the way can now have exactly that. QGIS shows and hides it as a
        # unit under View > Toolbars.
        self.toolbar = self.iface.addToolBar("Mapdex")
        self.toolbar.setObjectName("MapdexToolbar")
        self.toolbar.addAction(self.action)
        self.toolbar.addAction(self._vectorize_action)
        self._install_measure_action()
        self._install_draw_action()
        self._install_layer_menu_actions()
        # Register the dock immediately so QGIS places it in the right rail,
        # not as a floating overlay over the menu bar.
        self._ensure_dock()
        self.dock.hide()

    def _install_vectorize_action(self):
        """Trace a drawn line instead of clicking along it.

        A second first-class tool rather than a menu entry, because that is what
        it is. The competitor this answers sends the raster around your cursor to
        its own servers; this reads the layer already open in QGIS and searches
        it here, so it works on an archive that is not allowed to leave the
        building and costs nothing per stroke.
        """
        self._vectorize_action = QAction(
            themed_asset_icon("icon_vectorize.png"), "Vectorize with Mapdex",
            self.iface.mainWindow())
        self._vectorize_action.setToolTip(
            "Trace lines and areas on a scanned map: click the line and the tool follows it")
        self._vectorize_action.setCheckable(True)
        self._vectorize_action.triggered.connect(self._toggle_vectorize_tool)
        self.iface.addPluginToWebMenu("&Mapdex", self._vectorize_action)

        self._vectorize_settings_action = QAction(
            "Tracer settings...", self.iface.mainWindow())
        self._vectorize_settings_action.setToolTip(
            "How the tracer reads a scan: snapping, gap bridging, smoothing")
        self._vectorize_settings_action.triggered.connect(self._edit_tracer_settings)
        self.iface.addPluginToWebMenu("&Mapdex", self._vectorize_settings_action)

    def _tracer_options(self):
        """The saved tracer settings, clamped.

        Read per activation rather than cached: a user changing a setting is
        usually mid-sheet and expects the next stroke to use it.
        """
        from .livewire import TraceOptions  # noqa: PLC0415 - Qt-only import

        settings = QSettings()
        prefix = "mapdex/tracer/"

        def number(key, fallback, cast=float):
            try:
                return cast(settings.value(prefix + key, fallback))
            except (TypeError, ValueError):
                return fallback

        defaults = TraceOptions()
        raw_dark = str(settings.value(prefix + "dark_ink", "true")).lower()
        return TraceOptions(
            max_ink_fraction=number("max_ink_fraction", defaults.max_ink_fraction),
            min_ink_contrast=number("min_ink_contrast", defaults.min_ink_contrast),
            bridge_px=number("bridge_px", defaults.bridge_px, int),
            snap_px=number("snap_px", defaults.snap_px, int),
            paper_penalty=number("paper_penalty", defaults.paper_penalty),
            simplify_px=number("simplify_px", defaults.simplify_px),
            dark_ink=raw_dark not in ("false", "0", "no"),
        ).clamped()

    @guarded
    def _edit_tracer_settings(self):
        """One dialog, in the tool own words rather than the algorithm ones."""
        from qgis.PyQt.QtWidgets import (  # noqa: PLC0415 - Qt-only import
            QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QSpinBox,
        )

        current = self._tracer_options()
        dialog = QDialog(self.iface.mainWindow())
        dialog.setWindowTitle("Mapdex tracer")
        form = QFormLayout(dialog)

        snap = QSpinBox(dialog)
        snap.setRange(0, 64)
        snap.setValue(int(current.snap_px))
        form.addRow("Snap a click onto a line within (px)", snap)

        bridge = QSpinBox(dialog)
        bridge.setRange(0, 6)
        bridge.setValue(int(current.bridge_px))
        form.addRow("Bridge gaps in a broken line up to (px)", bridge)

        smooth = QDoubleSpinBox(dialog)
        smooth.setRange(0.0, 20.0)
        smooth.setSingleStep(0.5)
        smooth.setValue(float(current.simplify_px))
        form.addRow("Smooth the traced line by (px)", smooth)

        contrast = QDoubleSpinBox(dialog)
        contrast.setRange(0.0, 255.0)
        contrast.setValue(float(current.min_ink_contrast))
        form.addRow("Treat a scan as blank below this ink contrast", contrast)

        dark = QCheckBox("Dark ink on light paper", dialog)
        dark.setChecked(bool(current.dark_ink))
        form.addRow(dark)

        buttons = QDialogButtonBox(
            enum_member(QDialogButtonBox, "StandardButton", "Ok")
            | enum_member(QDialogButtonBox, "StandardButton", "Cancel"), dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        accepted = dialog.exec()
        if accepted:
            settings = QSettings()
            prefix = "mapdex/tracer/"
            settings.setValue(prefix + "snap_px", int(snap.value()))
            settings.setValue(prefix + "bridge_px", int(bridge.value()))
            settings.setValue(prefix + "simplify_px", float(smooth.value()))
            settings.setValue(prefix + "min_ink_contrast", float(contrast.value()))
            settings.setValue(prefix + "dark_ink", "true" if dark.isChecked() else "false")
            self._set_status("Tracer settings saved.")

    def _raster_for_tracing(self):
        """The scan to trace, chosen the way the user would expect.

        The active layer wins when it is a raster, because selecting it is how a
        person says which sheet they mean. Otherwise the topmost visible raster,
        because that is the one they can see and are pointing at.
        """
        from qgis.core import QgsProject  # noqa: PLC0415 - Qt-only import

        from .qt_compat import layer_type  # noqa: PLC0415 - Qt-only import

        active = self.iface.activeLayer()
        if active is not None and active.type() == layer_type("RasterLayer"):
            return active
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            layer = node.layer()
            if (layer is not None and node.isVisible()
                    and layer.type() == layer_type("RasterLayer")):
                return layer
        return None

    @guarded
    def _toggle_vectorize_tool(self, checked=True):
        if not checked:
            self._restore_map_tool()
            return
        if self._raster_for_tracing() is None:
            self._restore_map_tool()
            self._set_status(
                "Tracing needs a scanned map. Add the raster to the project, or "
                "select it in the Layers panel, and try again.")
            return

        geometry = self._resolve_trace_target()
        if not geometry:
            self._restore_map_tool()
            self._set_status("Nivo did not start tracing.")
            return
        from .vectorize import VectorizeMapTool  # noqa: PLC0415 - Qt-only import

        self._restore_map_tool()
        canvas = self.iface.mapCanvas()
        self._previous_map_tool = canvas.mapTool()
        self._vectorize_tool = VectorizeMapTool(
            canvas, self._raster_for_tracing, geometry, self._traced_geometry,
            self._tracer_options(), self._set_status, self._trace_cancelled,
            trace_existing=self._layers_to_follow)
        canvas.setMapTool(self._vectorize_tool)
        if self._vectorize_action is not None:
            self._vectorize_action.setChecked(True)

    def _resolve_trace_target(self):
        """The active layer, or the reason it cannot take a traced shape.

        Deliberately no picker and no scratch layer. The traced shape belongs
        in the layer the user is working in, which is the one they selected;
        QGIS's own Add Feature tool works exactly this way and nobody has to be
        told how. An earlier version asked which geometry to trace when no
        layer could answer, and put the result in a layer it invented. Both
        were extra steps in front of the obvious answer, and the invented layer
        was somewhere the user had not chosen to look.

        Editing is started rather than demanded. Picking this tool over a layer
        you have selected is not ambiguous, and QGIS will still ask before
        anything is written to disk when the session is closed.

        Returns the geometry to trace, or "" with the reason already said.
        """
        from .qt_compat import geometry_type, layer_type  # noqa: PLC0415 - Qt-only import

        layer = self.iface.activeLayer()
        if layer is None:
            self._set_status(
                "Select the layer the traced shapes should go into, in the "
                "Layers panel, then start the tracer.")
            return ""
        if layer.type() != layer_type("VectorLayer"):
            self._set_status(
                "{} is not a vector layer, so a traced shape cannot go into "
                "it. Select the line or area layer you are digitizing "
                "into.".format(layer.name()))
            return ""
        try:
            kind = layer.geometryType()
        except (AttributeError, TypeError):
            kind = None
        if kind == geometry_type("PolygonGeometry"):
            geometry = "polygon"
        elif kind == geometry_type("LineGeometry"):
            geometry = "line"
        else:
            self._set_status(
                "{} holds points, and the tracer follows lines and areas. Use "
                "Draw with Mapdex to place points.".format(layer.name()))
            return ""

        if not layer.isEditable():
            if not layer.startEditing():
                self._set_status(
                    "{} cannot be edited, so traced shapes cannot go into it. "
                    "Check the layer is writable.".format(layer.name()))
                return ""
            self._set_status(
                "Editing {}. Click a drawn line to start tracing.".format(layer.name()))
            return geometry

        self._set_status(
            "Tracing into {}. Click a drawn line to start.".format(layer.name()))
        return geometry

    def _layers_to_follow(self):
        """Vector layers whose drawn boundaries the tracer may reuse.

        The layer being edited first, because on a cadastral sheet the boundary
        worth reusing is almost always the parcel traced a minute ago. Other
        VISIBLE vector layers follow, which is the set QGIS's own tracing uses:
        a layer the user has turned off is one they have decided not to work
        against, and following it would attach their new boundary to geometry
        they cannot even see.
        """
        from qgis.core import QgsProject  # noqa: PLC0415 - Qt-only import
        from .qt_compat import layer_type  # noqa: PLC0415 - Qt-only import

        layers = []
        active = self.iface.activeLayer()
        if active is not None and active.type() == layer_type("VectorLayer"):
            layers.append(active)
        try:
            for node in QgsProject.instance().layerTreeRoot().findLayers():
                layer = node.layer()
                if (layer is not None and node.isVisible()
                        and layer.type() == layer_type("VectorLayer")
                        and layer not in layers):
                    layers.append(layer)
        except (AttributeError, RuntimeError) as exc:
            log_debug("listing layers the tracer may follow", exc)
        return layers

    @guarded
    def _traced_geometry(self, geometry, points, crs):
        """Keep the traced shape, in the place the user is already working.

        There is one destination: the layer being edited. The shape belongs
        beside the ones traced before it, in the undo stack the user already
        has. Creating a layer of its own instead was an answer to a question
        nobody asked, and it put the work somewhere they had not chosen.

        The tool STAYS ARMED. Digitizing a sheet is a hundred shapes in a row,
        and the first version put the tracer away after each one because it was
        modelled on the draw tool, which makes a layer and is finished. Having
        to re-arm between parcels is the difference between a tool someone works
        with for an hour and one they try once.
        """
        if not crs:
            # Refused here rather than at the registry, which could only say
            # "crs is required". The reason is the project, not the request.
            self._set_status(
                "This map is in a coordinate system with no authority code, so "
                "Mapdex cannot state where that shape is. Set a project CRS such "
                "as EPSG:4326 and trace it again.")
            return
        self._append_traced_feature(geometry, points)

    def _append_traced_feature(self, geometry, points):
        """Add the shape to the layer being edited. False when there is not one.

        The vertices arrive in the CANVAS crs and the layer has its own, so they
        are transformed before they are stored. Skipping that is how a traced
        boundary lands in the right place on screen and the wrong place in the
        file.
        """
        from qgis.core import (  # noqa: PLC0415 - Qt-only import
            QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
        )
        from .qt_compat import geometry_type, layer_type  # noqa: PLC0415 - Qt-only import

        layer = self.iface.activeLayer()
        if layer is None or layer.type() != layer_type("VectorLayer"):
            return False
        if not layer.isEditable():
            return False
        wants_polygon = geometry == "polygon"
        try:
            is_polygon = layer.geometryType() == geometry_type("PolygonGeometry")
        except (AttributeError, TypeError):
            return False
        if wants_polygon != is_polygon:
            self._set_status(
                "The layer being edited holds {}, and this trace is {}. Finish it "
                "into a new layer or switch layers.".format(
                    "areas" if is_polygon else "lines",
                    "an area" if wants_polygon else "a line"))
            return False

        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        target_crs = layer.crs()
        vertices = [QgsPointXY(float(x), float(y)) for x, y in points]
        if canvas_crs != target_crs and canvas_crs.isValid() and target_crs.isValid():
            transform = QgsCoordinateTransform(canvas_crs, target_crs, QgsProject.instance())
            vertices = [transform.transform(point) for point in vertices]

        if wants_polygon:
            # Close the ring explicitly. QGIS will close it anyway, but doing it
            # here means the vertex count the user is told about is the one the
            # layer stores.
            if vertices and vertices[0] != vertices[-1]:
                vertices = vertices + [vertices[0]]
            shape = QgsGeometry.fromPolygonXY([vertices])
        else:
            shape = QgsGeometry.fromPolylineXY(vertices)
        if shape is None or shape.isEmpty():
            self._set_status("That trace did not make a usable shape.")
            return False

        feature = self._traced_feature(layer, shape)
        # One edit command, so the user Ctrl+Z removes the whole traced shape.
        # Without it the feature lands outside the layer undo stack and their
        # undo does something else, or nothing, at the moment they most expect
        # it to work.
        layer.beginEditCommand("Trace with Mapdex")
        try:
            if not self._confirm_traced_attributes(layer, feature):
                layer.destroyEditCommand()
                self._set_status("Traced shape discarded at the attribute form.")
                return True
            if not layer.addFeature(feature):
                layer.destroyEditCommand()
                self._set_status("The layer refused the traced feature.")
                return False
        except Exception:
            layer.destroyEditCommand()
            raise
        layer.endEditCommand()
        layer.triggerRepaint()
        self._set_status(
            "Traced {} added to {}. Click a line to start the next one; save "
            "the layer to keep them.".format(
                "area" if wants_polygon else "line", layer.name()))
        return True

    def _traced_feature(self, layer, shape):
        """A feature with the layer own defaults already applied.

        A traced parcel that arrives with every field empty ignores defaults the
        user set up for exactly this, and QgsVectorLayerUtils is what every
        other digitizing path in QGIS uses to honour them.
        """
        from qgis.core import QgsFeature  # noqa: PLC0415 - Qt-only import

        try:
            from qgis.core import QgsVectorLayerUtils  # noqa: PLC0415

            feature = QgsVectorLayerUtils.createFeature(layer)
        except Exception as exc:  # noqa: BLE001 - defaults are a nicety, not the shape
            log_debug("applying layer defaults to a traced feature", exc)
            feature = QgsFeature(layer.fields())
        feature.setGeometry(shape)
        return feature

    def _confirm_traced_attributes(self, layer, feature) -> bool:
        """Show the attribute form, unless this layer or QGIS says not to.

        Someone digitizing cadastre types the parcel number as they go, and a
        tool that stores a shape with no attributes makes them find it again
        afterwards. The suppression setting is respected because a user who
        turned the form off did so to trace faster, which is this tool whole
        point.

        Returning True with no form shown is the normal path; False means the
        user cancelled the form and the shape should not be stored.
        """
        from qgis.core import QgsEditFormConfig  # noqa: PLC0415 - Qt-only import

        try:
            suppress = layer.editFormConfig().suppress()
            if suppress == enum_member(QgsEditFormConfig, "FeatureFormSuppress", "SuppressOn"):
                return True
            if suppress == enum_member(QgsEditFormConfig, "FeatureFormSuppress", "SuppressDefault"):
                setting = QSettings().value(
                    "qgis/digitizing/disable_enter_attribute_values_dialog", False)
                if str(setting).lower() in ("true", "1"):
                    return True
            return bool(self.iface.openFeatureForm(layer, feature))
        except Exception as exc:  # noqa: BLE001 - never lose a trace to the form
            log_debug("showing the attribute form for a traced feature", exc)
            return True

    @guarded
    def _trace_cancelled(self, discarded):
        self._restore_map_tool()
        self._set_status(
            "Tracing cancelled; {} stretch(es) discarded.".format(discarded)
            if discarded else "Tracing cancelled.")

    def _install_measure_action(self):
        """Let a person point at two places, rather than already know them.

        `measure.distance@1` takes a pair of coordinates, so until this action
        existed the capability could be invoked by an agent holding numbers and
        by nobody else. `MeasureMapTool` was written for exactly this and was
        never connected to anything: measuring by pointing is how measuring is
        done, and it was the one canvas-native interaction the plugin had.

        The tool is checkable and puts the previous tool back when the
        measurement finishes, because a modal state the user has to remember to
        leave is a trap: the next click on the map would otherwise start a
        measurement they did not ask for.
        """
        self._measure_action = QAction(mapdex_mark_icon(), "Measure with Mapdex", self.iface.mainWindow())
        self._measure_action.setToolTip("Click two points on the map to measure the distance between them")
        self._measure_action.setCheckable(True)
        self._measure_action.triggered.connect(self._toggle_measure_tool)
        self.iface.addPluginToWebMenu("&Mapdex", self._measure_action)

    @guarded
    def _toggle_measure_tool(self, checked=True):
        if not checked:
            self._restore_map_tool()
            return
        from .maptools import MeasureMapTool  # noqa: PLC0415 - Qt-only import

        # Put any Mapdex tool away first, so "previous" is the tool the user
        # chose rather than our other one. Without it, starting a measurement
        # while drawing makes the draw tool the thing measuring returns to, and
        # the user is left in a mode whose menu item reads unchecked.
        self._restore_map_tool()
        canvas = self.iface.mapCanvas()
        self._previous_map_tool = canvas.mapTool()
        self._measure_tool = MeasureMapTool(canvas, self._measured_two_points, self._set_status)
        canvas.setMapTool(self._measure_tool)
        if self._measure_action is not None:
            self._measure_action.setChecked(True)
        self._set_status("Click the first point to measure from.")

    @guarded
    def _measured_two_points(self, from_lon, from_lat, to_lon, to_lat):
        """Hand the two clicked positions to the registry-validated capability.

        Deliberately routed through `_run_capability` rather than calling the
        runtime directly: a measurement started from the canvas and one asked
        for in words must produce the same validated request, the same ellipsoid
        decision and the same transcript line.
        """
        self._restore_map_tool()
        self._run_capability(
            "measure.distance@1",
            {"from_lon": from_lon, "from_lat": from_lat, "to_lon": to_lon, "to_lat": to_lat},
            "Measure between two clicked points",
        )

    def _install_draw_action(self):
        """Let a person draw the shape, rather than describe it.

        `features.py` will place points and refuses to invent a polygon, which
        is the right refusal - corners nobody supplied are fabricated geometry -
        but it left the product unable to accept a boundary either. Asking for
        an area of interest created an empty layer and told the user to toggle
        editing, so the third release-gate scenario, draw an AOI then analyse it,
        had no first step.

        Checkable and self-restoring for the same reason the measure action is:
        a modal canvas state the user has to remember to leave turns their next
        click into a vertex of a shape they were not drawing.
        """
        self._draw_action = QAction(mapdex_mark_icon(), "Draw with Mapdex", self.iface.mainWindow())
        self._draw_action.setToolTip(
            "Draw a point, line or area on the map and keep it as a layer")
        self._draw_action.setCheckable(True)
        self._draw_action.triggered.connect(self._toggle_draw_tool)
        self.iface.addPluginToWebMenu("&Mapdex", self._draw_action)

    @guarded
    def _toggle_draw_tool(self, checked=True):
        if not checked:
            self._restore_map_tool()
            return
        # The geometry cannot be defaulted: someone drawing parcels gets nothing
        # from a point layer. The picker is the existing one, so "what kind of
        # shape" is asked the same way whether a layer is being created or drawn.
        geometry = self._ask_geometry_type()
        if not geometry or geometry not in DRAWABLE_GEOMETRIES:
            self._restore_map_tool()
            self._set_status("Nivo did not start drawing.")
            return
        from .maptools import DrawMapTool  # noqa: PLC0415 - Qt-only import

        # As the measure action: put any Mapdex tool away first so "previous" is
        # the tool the user picked, not our other one.
        self._restore_map_tool()
        canvas = self.iface.mapCanvas()
        self._previous_map_tool = canvas.mapTool()
        self._draw_tool = DrawMapTool(
            canvas, geometry, self._drew_geometry, self._draw_cancelled, self._set_status)
        canvas.setMapTool(self._draw_tool)
        if self._draw_action is not None:
            self._draw_action.setChecked(True)
        self._set_status(
            "Click to place points for the {}. Right-click or press Enter to finish, "
            "Esc to cancel.".format(geometry))

    @guarded
    def _drew_geometry(self, geometry, vertices, crs):
        """Hand the drawn shape to the registry-validated capability.

        Routed through `_run_capability` for the reason the measure tool is: a
        shape drawn on the canvas and one arriving from the server must be
        validated by the same registry and produce the same transcript line.
        """
        self._restore_map_tool()
        if not crs:
            # Refused here rather than at the registry, which would only be able
            # to say "crs is required". The user needs the reason, and the reason
            # is their project, not their request.
            self._set_status(
                "This map is in a coordinate system with no authority code, so Mapdex "
                "cannot state where that shape is. Set a project CRS such as EPSG:4326 "
                "and draw it again.")
            return
        self._run_capability(
            "draw.geometry@1",
            {"geometry": geometry, "vertices": vertices, "crs": crs},
            "Draw {} on the map".format("an area" if geometry == "polygon" else "a " + geometry),
        )

    @guarded
    def _draw_cancelled(self, discarded):
        self._restore_map_tool()
        self._set_status(
            "Drawing cancelled; {} point(s) discarded.".format(discarded) if discarded
            else "Drawing cancelled.")

    def _draw_geometry_capability(self, params):
        """Turn validated vertices into a real layer, in the CRS they were drawn in.

        The registry has already proved the vertices are pairs of finite numbers
        and bounded in count. What it cannot check is whether that many of them
        make the requested shape, because the answer depends on a second
        parameter - so `refusal_for` is asked here, and a request that arrived
        from the capability channel with two corners for a polygon is refused
        with the reason rather than stored as a shape with no area.

        A memory layer, deliberately: it appears immediately and needs no path or
        format decision from someone who is in the middle of drawing. Nothing the
        user already had is touched, which is why this is safe rather than
        consequential.
        """
        from .maptools import refusal_for  # noqa: PLC0415 - keeps the import graph honest

        geometry = str(params.get("geometry") or "")
        vertices = list(params.get("vertices") or [])
        authid = str(params.get("crs") or "").strip()
        refusal = refusal_for(geometry, vertices)
        if refusal:
            raise CapabilityError(refusal)
        crs = QgsCoordinateReferenceSystem(authid)
        if not crs.isValid():
            raise CapabilityError("{} is not a coordinate reference system QGIS knows".format(
                authid or "that CRS"))
        name = str(params.get("name") or "").strip()[:120] or self._unique_layer_name(geometry)
        layer = QgsVectorLayer("{}?crs={}&index=yes".format(geometry, authid), name, "memory")
        if not layer.isValid():
            raise CapabilityError("QGIS could not create a {} layer in {}".format(geometry, authid))
        feature = QgsFeature(layer.fields())
        feature.setGeometry(self._drawn_geometry(geometry, vertices))
        added, _features = layer.dataProvider().addFeatures([feature])
        if not added:
            raise CapabilityError("QGIS rejected the drawn geometry")
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.iface.setActiveLayer(layer)
        layer.triggerRepaint()
        # The assistant has to learn about the layer in this turn, or the next
        # question ("how big is it?") is asked about a layer it cannot see.
        self._refresh_nivo_context()
        return {
            "kind": "geometry_drawn",
            "layer_id": layer.id(),
            "layer_name": layer.name(),
            "geometry": geometry,
            "crs": authid,
            "vertices": len(vertices),
        }

    @staticmethod
    def _drawn_geometry(geometry, vertices):
        """Build the QGIS geometry, closing a polygon ring if the user did not.

        A ring left open is the ordinary case: the user right-clicks to finish
        rather than clicking exactly on their first corner, and a ring whose
        ends do not meet is not a polygon.
        """
        points = [QgsPointXY(float(x), float(y)) for x, y in vertices]
        if geometry == "point":
            return QgsGeometry.fromPointXY(points[0])
        if geometry == "linestring":
            return QgsGeometry.fromPolylineXY(points)
        ring = list(points)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return QgsGeometry.fromPolygonXY([ring])

    def _restore_map_tool(self):
        canvas = self.iface.mapCanvas()
        for attribute in ("_measure_tool", "_draw_tool", "_vectorize_tool"):
            tool = getattr(self, attribute, None)
            if tool is None:
                continue
            try:
                canvas.unsetMapTool(tool)
            except (AttributeError, RuntimeError):
                pass
            setattr(self, attribute, None)
        if self._previous_map_tool is not None:
            try:
                canvas.setMapTool(self._previous_map_tool)
            except (AttributeError, RuntimeError):
                pass
            self._previous_map_tool = None
        for attribute in ("_measure_action", "_draw_action", "_vectorize_action"):
            action = getattr(self, attribute, None)
            if action is not None:
                action.setChecked(False)

    def _install_layer_menu_actions(self):
        """Offer the paid work where the user already is: the layer tree.

        Georeferencing a scan by hand in QGIS is control-point placement, tens
        of minutes a sheet. That is the work worth paying to skip, and until now
        the only way to reach it was to open a panel, pick a workflow from a
        combo box and pick a source. Three steps between the user and the thing
        they came for, none of which they were thinking about: they were
        right-clicking the scan.

        The action starts the work. Clicking a menu item that says
        "Georeference with Mapdex" on a named layer IS the consent: the panel it
        used to open only restated what the menu item already said, so the
        second click bought no information and cost the user a step. Cancel
        after the fact is the safeguard, not confirm before it, because cancel
        does not tax the case that goes right.

        It falls back to preparing and stopping when the run genuinely cannot
        start — no session, no project, something already running — because
        those need a decision the menu cannot make.
        """
        add = getattr(self.iface, "addCustomActionForLayerType", None)
        if add is None:
            # An older or stubbed interface. The panel is still the way in.
            return
        window = self.iface.mainWindow()
        for title, kind, layer_type in self._layer_menu_entries():
            if layer_type is None:
                continue
            action = QAction(mapdex_mark_icon(), title, window)
            action.triggered.connect(partial(self._prepare_from_layer_menu, kind))
            try:
                add(action, "Mapdex", layer_type, True)
            except (AttributeError, TypeError):
                continue
            self._layer_menu_actions.append(action)

    def _layer_menu_entries(self):
        """The workflows worth a right-click, against the layer type each needs.

        Deliberately not all four. A context menu earns its place by being
        short, and offering a raster workflow on a vector layer teaches the user
        that the menu does not know what they clicked.
        """
        raster = getattr(QgsMapLayer, "RasterLayer", None)
        vector = getattr(QgsMapLayer, "VectorLayer", None)
        return (
            ("Georeference with Mapdex", BatchKind.GEOREFERENCE, raster),
            ("Digitize parcels with Mapdex", BatchKind.DIGITIZE_PARCELS, raster),
            ("Validate and deliver with Mapdex", BatchKind.VALIDATE_DELIVER, vector),
        )

    @guarded
    def _prepare_from_layer_menu(self, kind, *_args):
        """Open the panel with the clicked layer and workflow already chosen."""
        layer = self.iface.activeLayer()
        self.show()
        if self.workflow_box is not None:
            index = self.workflow_box.findData(kind)
            if index >= 0:
                self.workflow_box.setCurrentIndex(index)
        if self.input_box is not None:
            index = self.input_box.findData("active_layer")
            if index >= 0:
                self.input_box.setCurrentIndex(index)
        name = layer.name() if layer is not None else ""
        # Only start when the Start button itself would have been available.
        # That check already knows about the session, the busy flag and a run
        # in flight, so reusing it keeps one answer to "can this go now".
        can_start = self.run_button is not None and self.run_button.isEnabled()
        if not can_start:
            self._set_status(
                "{} is ready to send. Press Start when you want to.".format(name)
                if name else "Ready to send. Press Start when you want to."
            )
            return
        if name:
            self._set_status("Sending {}. Press Cancel to stop it.".format(name))
        self.run_input()

    @guarded
    def _on_current_layer_changed(self, _layer=None):
        """React to the QGIS active layer only while this instance is alive.

        QGIS keeps interface-level connections across a plugin reload, so this
        can still fire for an unloaded instance whose panel is destroyed.
        Touching those widgets raises RuntimeError, which QGIS shows as a
        Python error on every layer click.
        """
        if self.dock is None or self.input_box is None:
            return
        try:
            if self.input_box.currentData() == "active_layer":
                self._summarize_active_layer()
            # The reading describes the ACTIVE layer, so it is stale the moment
            # that changes. This is the signal that makes it feel like a
            # companion rather than a splash screen.
            self._refresh_nivo_context()
        except RuntimeError:
            # The panel this instance owned is gone; stay quiet.
            return

    @guarded
    def unload(self):
        # Take the context-menu entries back off first. QGIS keeps them on the
        # interface, not on the plugin, so a reload without this leaves a second
        # "Georeference with Mapdex" behind on every reload.
        remove = getattr(self.iface, "removeCustomActionForLayerType", None)
        if remove is not None:
            for action in self._layer_menu_actions:
                try:
                    remove(action)
                except (AttributeError, TypeError, RuntimeError):
                    pass
        self._layer_menu_actions.clear()
        # A map tool outlives the plugin that set it: leaving it active means
        # clicking the canvas after an unload calls into a dead plugin.
        self._restore_map_tool()
        for attribute in ("_measure_action", "_draw_action", "_vectorize_action",
                          "_vectorize_settings_action"):
            action = getattr(self, attribute, None)
            if action is not None:
                self.iface.removePluginWebMenu("&Mapdex", action)
                setattr(self, attribute, None)
        # The toolbar is ours, so it goes with us. QGIS keeps it on the main
        # window otherwise, and every reload leaves another empty Mapdex bar
        # behind, the same way the layer context entries did.
        toolbar = getattr(self, "toolbar", None)
        if toolbar is not None:
            toolbar.setParent(None)
            self.toolbar = None
        self.poll_timer.stop()
        self.progress_timer.stop()
        for task in list(self._tasks):
            try:
                task.cancel()
            except RuntimeError:
                pass
        self._tasks.clear()
        try:
            self.iface.currentLayerChanged.disconnect(self._on_current_layer_changed)
        except (AttributeError, TypeError, RuntimeError):
            # Never connected, already gone, or an interface without the signal.
            pass
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action:
            self.iface.removePluginWebMenu("&Mapdex", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        # Drop every widget handle: the C++ objects go away with the dock, and
        # a leftover callback must find None rather than a dead wrapper.
        self._panel_root = None
        for name in PANEL_WIDGET_REFS:
            setattr(self, name, None)

    def _report_unexpected(self, action, exc):
        """The error boundary's reporter: log fully, tell the user plainly.

        Never re-raises. An exception escaping a Qt slot aborts QGIS under
        PyQt5.5+/PyQt6, so this is the last line before the user loses their
        session.
        """
        try:
            QgsMessageLog.logMessage(
                "Nivo action '{}' failed:\n{}".format(action, format_traceback(exc)),
                "Mapdex",
                enum_member(Qgis, "MessageLevel", "Critical"),
            )
        except Exception:  # nosec B110
            # The log is where a failure would be recorded, so there is nowhere
            # left to record this one. The user channels below still run.
            pass
        # Reporting must not depend on our own panel. When the panel is what
        # failed, telling the user through it silently reports nothing - which
        # is how "the menu does nothing when I click it" happened: the boundary
        # caught the real error and then had nowhere to put it. Each channel is
        # tried in turn, independently, and the last one needs no plugin state.
        summary = "Mapdex could not finish '{}': {}".format(action, describe_exception(exc))
        delivered = False
        try:
            self._set_status("Mapdex hit a problem and stopped safely. See the Mapdex log for details.")
            self._announce(summary, level=2)
            delivered = True
        except Exception:
            delivered = False
        if not delivered:
            # QGIS' own message bar: alive even when our dock never built.
            try:
                self.iface.messageBar().pushMessage("Mapdex", summary, level=2, duration=10)
                delivered = True
            except Exception:
                delivered = False
        if not delivered:
            # Last resort. A modal is intrusive, but silence during a failed
            # start-up leaves the user clicking a menu entry that does nothing.
            try:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Mapdex",
                    summary + "\n\nQGIS is unaffected. The full details are in the Mapdex log panel.",
                )
            except Exception:  # nosec B110
                # The last of three independent channels has failed, and this is
                # already the handler for a failure. There is nothing further to
                # try and nothing that may be allowed to escape from here.
                pass

    @guarded
    def _reicon_actions(self):
        """Re-choose every Mapdex mark after the interface palette changed.

        Guarded and tolerant: this runs from a Qt event, the actions may have
        been removed by an unload already, and a wrong icon must never be able
        to take QGIS down with it.
        """
        mark = mapdex_mark_icon()
        for action in (self.action, self._measure_action, self._draw_action,
                       *(self._layer_menu_actions or [])):
            if action is not None:
                action.setIcon(mark)
        # The tracer has its own glyph, and it is themed by the same rule.
        # getattr: unlike the others this one is never initialised to None, it
        # is created by _install_vectorize_action, so a palette change arriving
        # before or after that must meet None rather than AttributeError.
        tracer = getattr(self, "_vectorize_action", None)
        if tracer is not None:
            tracer.setIcon(themed_asset_icon("icon_vectorize.png"))

    def _ensure_dock(self):
        if self.dock is not None:
            return
        self.dock = QDockWidget("Mapdex for QGIS", self.iface.mainWindow())
        self.dock.setObjectName("MapdexForQgisDock")
        left = enum_member(Qt, "DockWidgetArea", "LeftDockWidgetArea")
        right = enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea")
        self.dock.setAllowedAreas(left | right)
        self.dock.setMinimumWidth(MINIMUM_WIDTH)
        self.dock.setFeatures(
            enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetMovable")
            | enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetFloatable")
            | enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetClosable")
        )

        root, refs = build_companion_panel(
            WORKFLOWS, endpoint_settings=self._endpoints_unlocked
        )
        # Hand the whole tree to the dock before touching any of it: with the
        # dock as the C++ owner, no widget can be collected while the panel is
        # still being wired up.
        self._panel_root = root
        # The mark on the toolbar and the menus is chosen from the palette, so
        # it has to be re-chosen when the palette moves. The actions are not
        # children of this panel, but the panel is a widget and receives the
        # application palette change, which is the signal the actions need.
        root.on_palette_change = self._reicon_actions
        self.dock.setWidget(root)
        for key, value in refs.items():
            setattr(self, key if key != "batch" else "batch_group", value)

        self._load_connection_fields()
        self._load_assistant_fields()
        self.connect_button.clicked.connect(self.connect)
        self.own_model_button.clicked.connect(self.choose_own_model)
        self.disconnect_button.clicked.connect(self.disconnect)
        self.save_settings_button.clicked.connect(self.save_connection_settings)
        self.provider_box.currentIndexChanged.connect(self._assistant_provider_changed)
        self.clear_key_button.clicked.connect(self.clear_assistant_key)
        self.run_button.clicked.connect(self.run_input)
        self.input_box.currentIndexChanged.connect(self._source_changed)
        self.workflow_box.currentIndexChanged.connect(self._workflow_changed)
        self.cancel_button.clicked.connect(self.cancel_batch)
        self.retry_button.clicked.connect(self.retry_failed)
        self.import_button.clicked.connect(self.import_results)
        self.review_button.clicked.connect(self.open_review)
        if hasattr(self, "open_project_button") and self.open_project_button:
            self.open_project_button.clicked.connect(self.open_project)
        self.resume_button.clicked.connect(self.resume_recent)
        self.nivo_send_button.clicked.connect(self.ask_nivo)
        self.nivo_stop_button.clicked.connect(self.stop_nivo)
        self.nivo_input.returnPressed.connect(self.ask_nivo)
        self.nivo_new_button.clicked.connect(self.new_nivo_task)
        self.nivo_history_button.clicked.connect(self.open_nivo_history)
        self._workflow_changed(self.workflow_box.currentIndex())
        self._load_recent_tasks()
        # A bound method, never a lambda: unload() has to be able to take this
        # connection back off the QGIS interface. A lambda cannot be
        # disconnected reliably, so every reload used to leave one more
        # connection pointing at a destroyed panel.
        try:
            self.iface.currentLayerChanged.connect(self._on_current_layer_changed)
        except AttributeError:
            pass

        self.iface.addDockWidget(
            enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea"),
            self.dock,
        )
        self._apply_preferred_dock_width()
        if self.api.token:
            self._set_status("Connected. Loading projects…")
            self._refresh_ui()
            self._load_projects()
        else:
            self._set_status("Not connected. Click Connect in browser to link this QGIS session.")
            self._refresh_ui()

    def _apply_preferred_dock_width(self):
        """Open the dock wide enough to use without a manual resize.

        QGIS restores a remembered width when the user has already sized the
        dock, so this only widens a dock that is still at its default.
        """
        if self.dock is None or self.dock.width() >= PREFERRED_WIDTH:
            return
        window = self.iface.mainWindow()
        try:
            window.resizeDocks(
                [self.dock],
                [PREFERRED_WIDTH],
                enum_member(Qt, "Orientation", "Horizontal"),
            )
        except (AttributeError, TypeError):
            # Older Qt without resizeDocks: the widget size hint still applies.
            self.dock.resize(PREFERRED_WIDTH, self.dock.height())

    @guarded
    def show(self, _checked: bool = False):
        self._ensure_dock()
        if hasattr(self.dock, "setUserVisible"):
            # Same narrow set as resizeDocks above: a missing or re-signatured
            # method, or a wrapper whose C++ dock has already gone. `show` is
            # guarded, so anything else still reaches the error boundary instead
            # of being swallowed here.
            try:
                self.dock.setUserVisible(True)
            except (AttributeError, RuntimeError, TypeError):
                pass
        self.dock.show()
        self.dock.raise_()
        try:
            self.dock.activateWindow()
        except (AttributeError, RuntimeError):
            pass
        # If QGIS restored it as floating over the chrome, re-dock on the right.
        if self.dock.isFloating():
            self.dock.setFloating(False)
            self.iface.addDockWidget(
                enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea"),
                self.dock,
            )

    def _set_status(self, text: str, toast: bool = False):
        if self.status is not None:
            self.status.setText(text)
        if toast:
            self._announce(text)

    def _announce(self, text: str, level: int = 0, duration: int = 6) -> None:
        """Say it in the QGIS message bar, not only inside our dock.

        A finished task is invisible when the panel is closed or behind the
        map, so every terminal beat is announced where QGIS users already look.
        Levels follow Qgis.MessageLevel: 0 info, 1 warning, 3 success.
        """
        try:
            self.iface.messageBar().pushMessage("Mapdex", text, level=level, duration=duration)
        except (AttributeError, RuntimeError, TypeError) as error:
            # No message bar (unloaded plugin, headless run). A notice must
            # never break the task, but it should not vanish either.
            QgsMessageLog.logMessage(
                "{} [message bar unavailable: {}]".format(text, error), "Mapdex"
            )

    def _load_connection_fields(self):
        if self.api_url_input is None:
            return
        self.api_url_input.setEditText(self.api.base_url or DEFAULT_API)
        self.web_url_input.setEditText(self.web_base or DEFAULT_WEB)

    # -- Nivo assistant runtime (hosted by default, BYOK when a key is set) --

    def _credential_store(self):
        """The encrypted key store, created lazily so start-up stays cheap."""
        if getattr(self, "_credentials", None) is None:
            try:
                manager = QgsApplication.authManager()
            except Exception:
                manager = None
            self._credentials = ProviderCredentialStore(auth_manager=manager)
        return self._credentials

    def assistant_settings(self) -> dict:
        """Non-secret assistant preferences, plus the key for the transport.

        The key is read here and handed straight to the provider. It is never
        stored on the plugin object, put in a companion context, or logged.
        """
        settings = QSettings()
        stored = {
            "provider": str(settings.value("mapdex/nivo/provider", "") or ""),
            "model": str(settings.value("mapdex/nivo/model", "") or ""),
            "base_url": str(settings.value("mapdex/nivo/base_url", "") or ""),
            "auth_config_id": str(settings.value("mapdex/nivo/auth_config_id", "") or ""),
            # The vendored package deliberately knows no vendor name, so the host
            # names the service a hosted turn would actually reach.
            "hosted_provider_name": HOSTED_PROVIDER_NAME,
        }
        if stored["provider"]:
            stored["api_key"] = self._credential_store().load(stored["auth_config_id"])
        return stored

    @guarded
    def _load_assistant_fields(self):
        if getattr(self, "provider_box", None) is None:
            return
        stored = QSettings()
        provider = str(stored.value("mapdex/nivo/provider", "") or "")
        index = self.provider_box.findData(provider)
        self.provider_box.setCurrentIndex(index if index >= 0 else 0)
        self.model_input.setText(str(stored.value("mapdex/nivo/model", "") or ""))
        self.base_url_input.setText(str(stored.value("mapdex/nivo/base_url", "") or ""))
        # The key field is deliberately left blank even when one is stored: a
        # secret is written, never read back into the interface.
        self.api_key_input.clear()
        if provider and str(stored.value("mapdex/nivo/auth_config_id", "") or ""):
            self.api_key_input.setPlaceholderText("A key is stored. Type a new one to replace it.")
        self._assistant_provider_changed()

    @guarded
    def _assistant_provider_changed(self, _index=None):
        if getattr(self, "provider_box", None) is None:
            return
        provider = self.provider_box.currentData() or ""
        needs_endpoint = provider in {"openai_compatible", "ollama"}
        self.base_url_input.setEnabled(bool(provider))
        self.api_key_input.setEnabled(bool(provider) and provider != "ollama")
        self.model_input.setEnabled(bool(provider))
        # Name the model a blank field actually resolves to. `build_provider`
        # substitutes it, so "Provider default" was describing a real value the
        # user could not see - and for a custom gateway there is no default at
        # all, which is a thing to say before the first call fails on it.
        default_model = default_model_for(provider)
        if provider:
            self.model_input.setPlaceholderText(
                default_model or "Required: this endpoint has no default model"
            )
        else:
            self.model_input.setPlaceholderText("Provider default")
        if needs_endpoint and not self.base_url_input.text().strip() and provider == "ollama":
            self.base_url_input.setPlaceholderText("http://127.0.0.1:11434")
        self._refresh_assistant_privacy()

    @guarded
    def _refresh_assistant_privacy(self):
        """State where this install currently sends the assistant turn."""
        if getattr(self, "assistant_privacy", None) is None:
            return
        provider = self.provider_box.currentData() or ""
        typed_key = self.api_key_input.text().strip() if self.api_key_input is not None else ""
        stored_key = str(QSettings().value("mapdex/nivo/auth_config_id", "") or "")
        runtime = resolve_runtime({
            "provider": provider,
            "api_key": typed_key or stored_key,
            "base_url": self.base_url_input.text().strip() if self.base_url_input is not None else "",
            "hosted_provider_name": HOSTED_PROVIDER_NAME,
        })
        self.assistant_privacy.setText(describe_privacy(runtime))
        # The stored key as TEXT. It showed only in the field's placeholder,
        # which is grey and reads as "this is empty, type here" - which is why
        # a user with a working stored key reported configuring no provider.
        if self.assistant_key_state is not None:
            self.assistant_key_state.setText(
                panel_state.key_state_line(provider, bool(typed_key or stored_key))
            )
        # Remove had nothing to remove for most of its life on screen. getattr,
        # because this handle is not in PANEL_WIDGET_REFS and so is not nulled
        # on unload; a late callback must meet None, not a dead wrapper.
        clear_button = getattr(self, "clear_key_button", None)
        if clear_button is not None:
            clear_button.setVisible(bool(stored_key))
        # This function is the last act of both save and clear, so it is the
        # one place the runtime decision can have changed. Drop the cache, then
        # restate the engine in the Nivo header from the fresh answer.
        self._forget_assistant_runtime()
        self._refresh_assistant_state()

    @guarded
    def save_assistant_settings(self) -> bool:
        """Persist assistant preferences; store any new key in the auth DB."""
        if getattr(self, "provider_box", None) is None:
            return True
        provider = str(self.provider_box.currentData() or "")
        settings = QSettings()
        if not provider:
            # Back to the hosted path: forget the key rather than leaving a
            # secret behind for a provider that is no longer in use.
            self.clear_assistant_key(announce=False)
            settings.setValue("mapdex/nivo/provider", "")
            self._refresh_assistant_privacy()
            return True
        base_url = self.base_url_input.text().strip()
        if provider in {"openai_compatible"} and not base_url:
            QMessageBox.warning(
                self.iface.mainWindow(), "Mapdex",
                "An OpenAI-compatible provider needs an endpoint URL.",
            )
            return False
        typed_key = self.api_key_input.text().strip()
        stored_id = str(settings.value("mapdex/nivo/auth_config_id", "") or "")
        if typed_key:
            try:
                result = self._credential_store().store(provider, typed_key)
            except Exception as exc:
                self._show_error("Nivo could not store that key", exc)
                return False
            stored_id = result.get("auth_config_id", "")
            if result.get("storage") == "session":
                self._announce(
                    "QGIS has no unlocked authentication database, so the key is kept for this "
                    "session only and is not written to disk.",
                    level=1,
                )
            self.api_key_input.clear()
            self.api_key_input.setPlaceholderText("A key is stored. Type a new one to replace it.")
        elif provider != "ollama" and not stored_id:
            QMessageBox.warning(
                self.iface.mainWindow(), "Mapdex",
                "Paste an API key for {}, or choose Mapdex (hosted).".format(provider),
            )
            return False
        clean = public_settings({
            "provider": provider,
            "model": self.model_input.text().strip(),
            "base_url": base_url,
            "auth_config_id": stored_id,
        })
        for key in ("provider", "model", "base_url", "auth_config_id"):
            settings.setValue("mapdex/nivo/" + key, clean.get(key, ""))
        self._refresh_assistant_privacy()
        return True

    @guarded
    def clear_assistant_key(self, *args, announce: bool = True):
        settings = QSettings()
        self._credential_store().clear(str(settings.value("mapdex/nivo/auth_config_id", "") or ""))
        settings.setValue("mapdex/nivo/auth_config_id", "")
        if getattr(self, "api_key_input", None) is not None:
            self.api_key_input.clear()
            self.api_key_input.setPlaceholderText("Paste a key to use your own provider")
        self._refresh_assistant_privacy()
        if announce:
            self._set_status("Removed the stored provider key. Nivo will use your Mapdex plan.")

    def _reject_insecure_endpoint(self, url: str) -> bool:
        """Block a plaintext endpoint that is not this machine."""
        if is_transport_secure(url):
            return False
        QMessageBox.warning(
            self.iface.mainWindow(),
            "Mapdex",
            "Use an https:// address. Mapdex sends your access token with every "
            "request, so plain http is only allowed for a local install on this "
            "computer.",
        )
        return True

    def _point_at_endpoint(self, api_url: str) -> None:
        """Switch endpoints, dropping the session issued by the previous one.

        A token belongs to the deployment that issued it. Carrying it to a new
        address would hand a Mapdex session to whoever runs that address.
        """
        if same_origin(api_url, self.api.base_url):
            self.api.base_url = api_url
            QSettings().setValue("mapdex/base_url", api_url)
            return
        had_token = bool(self.api.token)
        self.api.base_url = api_url
        QSettings().setValue("mapdex/base_url", api_url)
        if had_token:
            self.disconnect()
            self._set_status("Endpoint changed. Connect again to authorize this QGIS.")

    @guarded
    def save_connection_settings(self, *args):
        # One Save button covers both sections. Assistant settings are saved
        # even in a released build, where the endpoint fields are pinned.
        if not self.save_assistant_settings():
            return
        if not self._endpoints_unlocked:
            self._set_status("Saved Nivo assistant settings.")
            return
        api_url = normalize_api_base(self.api_url_input.currentText())
        web_url = (self.web_url_input.currentText() or DEFAULT_WEB).strip().rstrip("/")
        if not api_url:
            QMessageBox.warning(self.iface.mainWindow(), "Mapdex", "API URL is required.")
            return
        if self._reject_insecure_endpoint(api_url) or self._reject_insecure_endpoint(web_url):
            return
        self._point_at_endpoint(api_url)
        self.web_base = web_url
        QSettings().setValue("mapdex/web_base", web_url)
        self._set_status("Saved. API = {api} · Web = {web}".format(api=api_url, web=web_url))

    def _apply_connection_settings_from_fields(self) -> bool:
        """Read current URL fields before connect (even if Save was not clicked)."""
        if not self._endpoints_unlocked:
            return True  # pinned to the hosted service; the fields are not shown
        api_url = normalize_api_base(self.api_url_input.currentText())
        web_url = (self.web_url_input.currentText() or DEFAULT_WEB).strip().rstrip("/")
        if api_url and self._reject_insecure_endpoint(api_url):
            return False
        if web_url and self._reject_insecure_endpoint(web_url):
            return False
        if api_url:
            self._point_at_endpoint(api_url)
        if web_url:
            self.web_base = web_url
            QSettings().setValue("mapdex/web_base", web_url)
        return True

    def _show_error(self, title: str, exc: BaseException):
        detail = str(exc)
        if isinstance(exc, MapdexAPIError):
            bits = [detail]
            if exc.url:
                bits.append("\n\n{method} {url}".format(method=exc.method or "HTTP", url=exc.url))
            if exc.correlation_id:
                bits.append("\ncorrelation_id: {cid}".format(cid=exc.correlation_id))
            if exc.status == 404 and "api.mapdex.ai" in (exc.url or ""):
                bits.append(
                    "\n\nProduction may not expose device auth yet. "
                    "Set API URL to http://127.0.0.1:8080 while the local API is running."
                )
            detail = "".join(bits)
            if exc.status == 401:
                # Clearing the rejected session, not assigning a password.
                self.api.token = ""  # nosec B105
                if self.token_store is not None:
                    self.token_store.clear()
                detail += "\n\nYour Mapdex session expired. Connect again to continue."
                self._refresh_ui()
        self._set_status(str(exc).split("\n")[0])
        QMessageBox.warning(self.iface.mainWindow(), title, detail)

    @guarded
    def _refresh_ui(self):
        # Nothing to refresh once the panel is gone; a late callback must not
        # walk destroyed widgets.
        if self.dock is None or self.connect_button is None:
            return
        connected = bool(self.api.token)
        has_batch = bool(self.batch_id)
        state = batch_state(self._last_batch or {}) if has_batch else ""
        counts = (self._last_batch or {}).get("counts") or {}
        active = state in ACTIVE_STATES
        failed = int(counts.get("failed", 0) or 0)
        succeeded = int(counts.get("succeeded", 0) or 0)
        needs_review = int(counts.get("needs_review", 0) or 0)
        total = int(counts.get("total", 0) or 0)
        completed = succeeded + needs_review + failed + int(counts.get("cancelled", 0) or 0)

        if self.connection_label is not None:
            self.connection_label.setText("Connected to Mapdex" if connected else "Not connected")

        self.connect_button.setVisible(not connected)
        self.connect_button.setEnabled(not self._busy)
        self.disconnect_button.setVisible(connected)
        self.disconnect_button.setEnabled(connected and not self._busy)

        # First open: neither a session nor a provider, so nothing can be asked
        # yet and the panel is nine controls a stranger cannot rank. Derived,
        # never a stored flag - a flag goes stale and, once spent, cannot come
        # back. The reading above it needed neither, which is what lets this
        # screen say something true before it asks for anything.
        has_provider = self._has_provider()
        first = panel_state.is_first_open(connected, has_provider)
        if self.connect_button is not None and hasattr(self.connect_button, "set_tone"):
            # Connect is the one filled action while nothing has been chosen.
            # To somebody already answering from their own model it is an
            # upgrade, not the thing to press, so it drops a rank rather than
            # sitting under their composer as a permanent indigo block.
            self.connect_button.set_tone("primary" if first else "quiet")
        for widget, shown in (
            (self.sign_in, not connected),
            (self.connect_promise, not connected),
            # Only on first open: afterwards the settings panel owns the
            # provider choice and repeating it here is a second control for one
            # decision.
            (self.first_open_prompt, first),
            (self.first_open_title, first),
            (self.own_model, first),
            # Nothing can be asked yet, and a disabled field above the choice
            # is dead weight where the eye lands last.
            (self.composer, not first),
            (self.nivo_status, not first),
            (self.tail, first),
            # One page and no tab bar while nothing has been chosen: Task and
            # Jobs are Mapdex session surfaces and cannot do anything yet.
            (self.segment_bar, not first),
        ):
            if widget is not None:
                widget.setVisible(shown)
        if first and self.tabs is not None:
            self.tabs.setCurrentIndex(0)
        if self.body_layout is not None and self.tabs is not None:
            # Stop the transcript grabbing the spare height while it holds
            # only a reading; the tail below takes it instead.
            self.body_layout.setStretchFactor(self.tabs, 0 if first else 1)
        if self.nivo_reply is not None:
            # With no stretch the transcript falls back to its minimum, which
            # put a scrollbar on a reading that had room to sit whole. Give it
            # the height its content asks for, bounded so a long reading still
            # leaves the choice on screen.
            content = self.nivo_reply.widget()
            wanted = content.sizeHint().height() + 12 if content is not None else 0
            if first:
                # Fixed to its content, so a reading that fits shows whole. A
                # minimum alone left the scroll area at that minimum and put a
                # scrollbar on a reading with room to sit.
                exact = min(max(wanted, 120), 520)
                self.nivo_reply.setMinimumHeight(exact)
                self.nivo_reply.setMaximumHeight(exact)
            else:
                self.nivo_reply.setMinimumHeight(170)
                self.nivo_reply.setMaximumHeight(16777215)

        # The page stays mounted so its segment button is never a dead end;
        # the BODY and the notice swap. Setting the page itself invisible was
        # fought by the segment switch, which shows the page it moves to, so a
        # disconnected user clicking Task got the previous session's project,
        # workflow and source in greyed-out controls - a signed-out product
        # reading as a broken one.
        self.workspace.setVisible(True)
        self.workspace.setEnabled(not self._busy)
        if self.workspace_body is not None:
            self.workspace_body.setVisible(connected)
            self.workspace_body.setEnabled(connected and not self._busy)
        if self.workspace_locked is not None:
            self.workspace_locked.setVisible(not connected)
        if self.jobs_locked is not None:
            self.jobs_locked.setVisible(not connected)
        self.run_button.setEnabled(connected and not self._busy and not active)

        # History needs a session and a project to list anything; New chat is
        # local and stays available so a transcript can always be cleared.
        if self.nivo_history_button is not None:
            self.nivo_history_button.setEnabled(connected and bool(self.project_id))
        if self.nivo_new_button is not None:
            self.nivo_new_button.setEnabled(True)

        self.batch_group.setVisible(connected and has_batch)
        self.cancel_button.setVisible(active)
        self.cancel_button.setEnabled(active and not self._busy)
        # Retry re-sends the identical request, so it is hidden when every
        # failure was a refusal that request cannot satisfy. The route to the
        # prerequisite takes its place on the action button below.
        unsatisfiable = every_failure_needs_placement(self._last_batch or {})
        self.retry_button.setVisible(has_batch and failed > 0 and not active and not unsatisfiable)
        self.retry_button.setEnabled(not self._busy)
        self.retry_button.setText(
            "Retry failed item" if failed == 1 else "Retry {} failed items".format(failed)
        )
        # Pulling the result is always the user's move, including while review
        # is still open: the button then offers the draft instead of hiding.
        self.import_button.setVisible(has_batch and (succeeded > 0 or needs_review > 0) and not active)
        self.import_button.setEnabled(not self._busy)
        if succeeded > 0:
            self.import_button.setText(
                "Get result from Mapdex"
                if succeeded == 1
                else "Get {} results from Mapdex".format(succeeded)
            )
            self.import_button.setToolTip("Add the approved Mapdex result to this project.")
        else:
            self.import_button.setText("Get result from Mapdex")
            self.import_button.setToolTip(
                "Review is still open in Mapdex. Mapdex asks before handing you the "
                "draft, split into review layers."
            )
        if has_batch:
            guidance = task_guidance(
                state,
                counts,
                backend_started=self._backend_started,
                waiting_seconds=self._waiting_seconds(),
                failure_code=batch_failure(self._last_batch or {}).get("code", ""),
            )
            self.review_button.setText(guidance["action"])
            self.review_button.setVisible(bool(guidance["action"]))
            self.review_button.setEnabled(not self._busy)
            self.phase_label.setText(guidance["phase"])
            self.guidance_label.setText(guidance["hint"])
            if guidance["busy"]:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(guidance["progress"])
        if self.batch_title is not None:
            if active and total > 1:
                self.batch_title.setText("Processing {} of {} files".format(completed, total))
            else:
                self.batch_title.setText("Task in progress" if active else "Latest task")
        if self.recent is not None and self.recent_box is not None:
            self.recent.setVisible(self.recent_box.count() > 0 and connected)
        # Which engine answers, and the reading of the open project, are both
        # functions of the connection state that just changed.
        self._refresh_assistant_state()
        self._render_nivo_turns()

    def _task(self, description: str, work: Callable, done: Callable, busy: bool = True):
        if busy and self._busy:
            self._set_status("Please wait — Mapdex is still working…")
            return
        if busy:
            self._busy = True
            self._refresh_ui()
        task = _WorkTask(description, work)
        # Hold a strong reference until QGIS is finished with the task. The
        # task manager owns it on the C++ side, but a task collected by Python
        # while it is still queued takes QGIS down with an access violation.
        self._tasks.append(task)
        finished_once = {"done": False}

        def finished():
            if finished_once["done"]:
                return
            finished_once["done"] = True
            if busy:
                self._busy = False
            if task in self._tasks:
                self._tasks.remove(task)
            # The panel can be gone (plugin unloaded or reloaded) by the time a
            # background task reports back. Touching destroyed widgets from
            # this callback is what crashed QGIS, so stop here instead.
            if self.dock is None:
                return
            try:
                if task.error is not None:
                    done(task.error, None)
                else:
                    done(None, task.result)
            except RuntimeError:
                return
            finally:
                if busy and self.dock is not None:
                    try:
                        self._refresh_ui()
                    except RuntimeError:
                        pass

        task.taskCompleted.connect(finished)
        task.taskTerminated.connect(finished)
        QgsApplication.taskManager().addTask(task)
        return task

    def _active_project_id(self) -> str:
        data = self.project_box.currentData() if self.project_box is not None else None
        return str(data or self.project_id or "")

    @guarded
    def _source_changed(self, _index):
        mode = self.input_box.currentData()
        if mode != "file":
            self.selected_paths = []
            if mode == "active_layer":
                self._summarize_active_layer()
            elif self.source_summary is not None:
                self.source_summary.setText("No source selected")
            return
        # Let the combo popup close before opening the native Windows dialog.
        # Opening it synchronously from currentIndexChanged can leave it behind
        # the QGIS window on some Qt5 builds.
        QTimer.singleShot(0, lambda selected_mode=mode: self._choose_source(selected_mode))

    def _choose_source(self, mode):
        if self.input_box.currentData() != mode:
            return
        file_filter = (
            "Spatial files (*.gpkg *.geojson *.json *.shp *.tif *.tiff *.pdf);;"
            "All files (*.*)"
        )
        # A file input is the archive/batch entry point.  Keep the active-layer
        # route deliberately single-source (exporting an arbitrary QGIS layer
        # collection would make a surprising, potentially huge upload), but
        # let a user select several compatible files here and create one real
        # server-side Batch with per-item Run/retry/review state.
        paths, _ = QFileDialog.getOpenFileNames(
            self.iface.mainWindow(), "Choose spatial files", "", file_filter
        )
        paths = [path for path in paths if path]
        if not paths:
            self.input_box.blockSignals(True)
            self.input_box.setCurrentIndex(0)
            self.input_box.blockSignals(False)
            self.selected_paths = []
            return
        self.selected_paths = list(paths)
        label = (
            os.path.basename(paths[0])
            if len(paths) == 1
            else "{} files selected".format(len(paths))
        )
        self.input_box.setItemText(self.input_box.currentIndex(), label)
        self._source_label = label
        report = inspect_paths(self.selected_paths, str(self.workflow_box.currentData() or ""))
        self.source_summary.setText(
            report["summary"] if report["valid"] else "{} · {}".format(report["summary"], report["error"])
        )
        self._set_status(
            "Ready to start a task with {}.".format(label)
            if len(paths) == 1
            else "Ready to start a batch with {} files.".format(len(paths))
        )

    def _summarize_active_layer(self):
        layer = self._active_qgis_layer()
        if layer is None or not layer.isValid():
            self.source_summary.setText("No valid active QGIS layer")
            return
        if isinstance(layer, QgsRasterLayer):
            kind = "Raster"
        elif isinstance(layer, QgsVectorLayer):
            kind = "Vector"
        else:
            kind = "Unsupported"
        crs = layer.crs().authid() if hasattr(layer, "crs") and layer.crs().isValid() else "No CRS"
        self._source_label = layer.name()
        self.source_summary.setText("{} · {} · {}".format(layer.name(), kind, crs))
        self._refresh_nivo_context()

    def _nivo_snapshot(self):
        """Return only measured QGIS metadata for the untrusted context envelope."""
        layer = self._active_qgis_layer()
        active = {}
        if layer is not None and layer.isValid():
            active = {
                "id": layer.id(),
                "name": layer.name(),
                "kind": (
                    "raster" if isinstance(layer, QgsRasterLayer)
                    else "vector" if isinstance(layer, QgsVectorLayer)
                    else "other"
                ),
                "crs": layer.crs().authid() if layer.crs().isValid() else "",
                "feature_count": layer.featureCount() if isinstance(layer, QgsVectorLayer) else 0,
                "geometry_type": layer.wkbType() if isinstance(layer, QgsVectorLayer) else "raster",
                "fields": [field.name() for field in layer.fields()] if isinstance(layer, QgsVectorLayer) else [],
            }
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        project = QgsProject.instance()
        return {
            "bbox": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
            "crs": canvas.mapSettings().destinationCrs().authid(),
            "active_layer": active,
            "selection_count": layer.selectedFeatureCount() if isinstance(layer, QgsVectorLayer) else 0,
            "visible_layer_count": len([item for item in project.layerTreeRoot().findLayers() if item.isVisible()]),
            # Name-only references. The projection in connections.py has no
            # field for a host, database, user, password or authcfg id, so a
            # DSN cannot reach a compose payload even by accident.
            "connections": discover_connections(qgis_connection_names),
            "qgis_version": qgis_version(QgsApplication, Qgis),
            "plugin_version": PLUGIN_VERSION,
        }

    @guarded
    def _refresh_nivo_context(self):
        if self.nivo_context is None:
            return
        context = companion_context(self._nivo_snapshot())
        layer = context.get("active_layer") or {}
        label = layer.get("name") or layer.get("id") or "No active QGIS layer"
        self.nivo_context.setText("{} · {} selected · {}".format(
            label, context.get("selection_count", 0), context.get("crs", "No CRS")
        ))
        self._refresh_assistant_state()
        self._render_nivo_turns()

    # ----------------------------------------------------------------------
    # The opening reading: what Nivo says before it is asked anything
    # ----------------------------------------------------------------------

    def _first_look_state(self):
        """Measured QGIS metadata for `first_look`, read in O(1).

        Deliberately NOT `companion_context`: that envelope is versioned, is
        posted to a server and is bounded for that reason. This one never
        leaves the machine, and it needs two things that envelope has no field
        for - whether a raster is placed, and which layers disagree with the
        project CRS.

        Nothing here walks a feature. Anything that would (validity, field
        profiles) is offered as a one-click action instead, so introducing
        itself cannot make the panel hang on a large layer.
        """
        from qgis.core import QgsProject

        project = QgsProject.instance()
        layer = self._active_qgis_layer()
        described = {}
        if layer is not None and layer.isValid():
            is_vector = isinstance(layer, QgsVectorLayer)
            described = {
                "name": layer.name(),
                "kind": "vector" if is_vector else "raster" if isinstance(layer, QgsRasterLayer) else "other",
                "crs": layer.crs().authid() if layer.crs().isValid() else "",
                "feature_count": layer.featureCount() if is_vector else 0,
            }
            if described["kind"] == "raster":
                described["width"] = getattr(layer, "width", lambda: 0)()
                described["height"] = getattr(layer, "height", lambda: 0)()
                described["georeferenced"] = raster_is_georeferenced(layer)
                # Which provider drew it. A tile service has a valid CRS and a
                # real extent exactly like a placed scan does, so without this
                # the reading offered to digitize parcels out of an
                # OpenStreetMap basemap that Nivo had added a turn earlier.
                described["provider"] = str(getattr(layer, "providerType", lambda: "")() or "")

        # Layers QGIS is silently reprojecting on the fly. The map looks right
        # and every measurement crossing them is not, which is the most common
        # invisible defect in a working project and free to detect.
        project_crs = project.crs().authid() if project.crs().isValid() else ""
        mismatched = []
        layers = list(project.mapLayers().values())
        for candidate in layers:
            try:
                if not candidate.isValid():
                    continue
                authid = candidate.crs().authid() if candidate.crs().isValid() else ""
            except (AttributeError, RuntimeError):
                continue
            if project_crs and authid and authid != project_crs:
                mismatched.append(candidate.name())
        return {
            "layer": described,
            "layer_id": layer.id() if layer is not None and layer.isValid() else "",
            "layer_count": len(layers),
            "crs_mismatch": mismatched,
        }

    @guarded
    def _refresh_opening(self):
        """Rebuild the opening reading as turns, ahead of the conversation.

        Not appended to `_nivo_turns`: it describes the layer that is active
        NOW, so it is recomputed on every draw rather than accumulated. Once
        the user has said something it steps aside entirely - an opening is not
        a status bar, and on a 396 px dock it would push the answer they are
        reading off the top.
        """
        self._nivo_opening = []
        if self._nivo_turns:
            return
        self._nivo_opening = opening_turns(self._first_look_state())

    def _action_chip(self, action):
        """One thing to do, attached to the turn that earned it.

        The panel's one row shape, so a choice here looks like a choice
        anywhere else in it. Free work and Mapdex work are told apart by the
        glyph and by the tone, not by inventing a second control language:
        two chips in one strip used to come out 364 px and 280 px wide with
        different heights, which is most of what read as unfinished.
        """
        paid = action.get("kind") == first_look.MAPDEX
        icon = self._action_icon(str(action.get("workflow") or "")) if paid else None
        button = action_row(
            str(action.get("label") or ""),
            icon=icon,
            tone="normal" if paid else "quiet",
        )
        if action.get("promise"):
            button.setToolTip(str(action["promise"]))
        button.clicked.connect(
            lambda _checked=False, chosen=dict(action):
            self._first_look_action(chosen, str(chosen.get("layer_id") or "")))
        return button

    def _severity_icon(self, severity):
        """The state glyph, from QGIS's own theme set.

        Measured in QGIS 4.0.2: all four paint at 24 px and follow the user's
        theme without any work from us, which is also why the panel looks like
        part of the host rather than a web card dropped into it.
        """
        name = branding.SEVERITY_ICONS.get(str(severity or ""), "")
        return QgsApplication.getThemeIcon(name) if name else QIcon()

    def _action_icon(self, workflow):
        """The glyph for one piece of Mapdex work, or None if it has none.

        QGIS has no equivalent that paints - mIconGeoreferencer.svg resolves to
        an empty pixmap here - so these four are ours, generated on the same
        24-unit grid by scripts/make_panel_icons.py.

        Chosen for the panel's surface, not for the interface: this chip is
        drawn on #191919 whatever theme QGIS is wearing.
        """
        asset = branding.ACTION_ICONS.get(str(workflow or ""), "")
        return surface_asset_icon(asset) if asset else None

    @guarded
    def _first_look_action(self, action, layer_id):
        """Act on a finding.

        Three kinds, and the difference between them is the whole design: a
        local capability runs here and now for free, a question goes to
        whichever engine is configured, and Mapdex work is routed rather than
        pretended.
        """
        kind = str(action.get("kind") or "")
        if kind == "capabilities":
            self._say_capabilities()
            return
        if kind == "connect":
            self.connect()
            return
        if kind == first_look.LOCAL:
            capability = str(action.get("capability") or "")
            params = {}
            if "layer_id" in (get_capability(capability).params if get_capability(capability) else {}):
                if not layer_id:
                    self._set_status("Select a layer first.")
                    return
                params["layer_id"] = layer_id
            self._run_capability(capability, params, summary=str(action.get("label") or ""))
            self._render_nivo_turns()
            return
        if kind == first_look.ASK:
            if self.nivo_input is not None:
                self.nivo_input.setText(str(action.get("prompt") or ""))
                self.ask_nivo()
            return
        if kind == first_look.MAPDEX:
            self._offer_mapdex_work(action)

    @guarded
    def _offer_mapdex_work(self, action):
        """Route a measured finding to the Mapdex work that answers it.

        Connected, this preselects the workflow on the Task page so the user
        lands on a form already filled in rather than on three empty
        dropdowns. Disconnected, it states what the work would do and offers
        the connection, because a button that silently does nothing is worse
        than an honest ask.
        """
        workflow = str(action.get("workflow") or "")
        promise = str(action.get("promise") or "")
        if not self.api.token:
            # A turn, not a dialog. The offer was a QMessageBox that covered
            # the panel, asked once and took its reason away with it when
            # dismissed - so the argument for connecting was gone the moment
            # somebody said "not now". In the transcript it stays on screen.
            self._say(
                promise or "This part runs on Mapdex.",
                fact="Needs a Mapdex account. Creating one is free.",
                actions=[
                    {"label": "Create an account", "kind": "connect"},
                    {"label": "I already have one", "kind": "connect"},
                ],
            )
            return
        self._preselect_workflow(workflow)

    def _preselect_workflow(self, workflow):
        """Open the Task page with this workflow and the active layer chosen."""
        if self.workflow_box is None or self.tabs is None:
            return
        for index in range(self.workflow_box.count()):
            data = self.workflow_box.itemData(index)
            if str(getattr(data, "value", data) or "") == workflow:
                self.workflow_box.setCurrentIndex(index)
                break
        if self.input_box is not None:
            target = self.input_box.findData("active_layer")
            if target >= 0:
                self.input_box.setCurrentIndex(target)
        self.tabs.setCurrentIndex(1)

    # What a person wants to do, mapped onto the registry's own domains.
    # Grouping by task reads far better than "analytics / geoprocessing /
    # inspect / selection / field / filter", but a hand-written grouping goes
    # stale the moment a domain is added - so this is a MAP and a test asserts
    # every domain is in it. A new domain fails the build instead of vanishing
    # from the answer.
    CAPABILITY_GROUPS = (
        ("Measure and analyse", ("analytics", "measure", "inspect", "spatial", "field", "filter")),
        ("Survey computation", ("survey", "coordinates", "crs")),
        ("Reshape and process", ("geoprocessing", "processing", "terrain", "draw")),
        ("Map and style", ("map", "style", "layer", "selection", "sheet")),
        ("Databases", ("postgis",)),
        ("Deliver", ("export", "report", "publish")),
        ("Mapdex work", ("mapdex",)),
    )

    @guarded
    def _say_capabilities(self):
        """Answer "what can Nivo do?" as a turn, from the registry.

        Not a dialog. A modal here is the product stepping out of its own
        conversation to hand somebody a manual, and it covers the panel it is
        describing. A turn scrolls, it stays, and the next question is already
        in the box below it.

        Generated, so a capability added to the registry cannot go missing from
        the answer, and the account-only ones are marked rather than hidden: a
        person deciding whether to sign up is entitled to see what signing up
        is for.
        """
        offline = session_allowance(CLIENT_QGIS)
        by_domain = {}
        for capability in for_client(CLIENT_QGIS):
            by_domain.setdefault(capability.domain, []).append(
                (capability.summary, capability.id not in offline))
        lines = []
        for title, domains in self.CAPABILITY_GROUPS:
            rows = sorted(row for domain in domains for row in by_domain.get(domain, []))
            if not rows:
                continue
            needs_account = all(paid for _summary, paid in rows)
            lines.append("{}{}".format(title, " - needs a connection" if needs_account else ""))
            lines.extend("  " + summary for summary, _paid in rows)
            lines.append("")
        self._say(
            "\n".join(lines).rstrip(),
            fact="Everything below runs here in QGIS unless it says otherwise.",
            actions=[{"label": "What can you do with this layer?", "kind": first_look.ASK,
                      "prompt": "What can you do with the layer I have open?"}],
        )

    @guarded
    def _refresh_assistant_state(self):
        """State which engine answers the next turn, where the user is standing.

        One resolution, shared with the turn itself. The panel used to hold
        this fact only inside a settings section that is collapsed by default,
        so a disconnected session answering from the user's own key read as a
        leak rather than as the design it is.
        """
        if self.nivo_runtime is None:
            return
        engine = panel_state.assistant_engine(
            self.assistant_runtime(), bool(self.api.token), self.project_id
        )
        self.nivo_runtime.setText(engine["line"])
        if self.nivo_send_button is not None:
            self.nivo_send_button.setEnabled(engine["can_ask"] and not self._busy)
        if self.nivo_input is not None:
            self.nivo_input.setEnabled(engine["can_ask"])
        # Deliberately does NOT write nivo_status. That line is a transient
        # reply to what the user just did, and this method runs on every layer
        # click and every connection refresh; writing a standing reason there
        # overwrote whatever the last action had reported - measured, it was
        # "New chat" losing its own "the old conversation is in History"
        # message. The reason lives in the header line above instead, which is
        # why `assistant_engine` states it there in every blocked state.

    def _active_qgis_layer(self):
        layer = self.iface.activeLayer()
        if layer is not None and layer.isValid():
            return layer
        try:
            view = self.iface.layerTreeView()
        except Exception:
            view = None
        # Four independent routes to "the layer the user means". Each may be
        # absent or broken on a given QGIS build, so a failure moves to the next
        # one - but it is recorded, because "Nivo says no layer is active" with
        # a layer plainly selected is otherwise unanswerable.
        if view is not None:
            try:
                layer = view.currentLayer()
                if layer is not None and layer.isValid():
                    return layer
            except Exception as exc:  # noqa: BLE001 - the layer tree may raise anything
                log_debug("reading the current layer from the layer tree", exc)
            try:
                for candidate in view.selectedLayers():
                    if candidate is not None and candidate.isValid():
                        return candidate
            except Exception as exc:  # noqa: BLE001 - the layer tree may raise anything
                log_debug("reading the selected layers from the layer tree", exc)
            try:
                node = view.currentNode()
                layer = node.layer() if node is not None and hasattr(node, "layer") else None
                if layer is not None and layer.isValid():
                    return layer
            except Exception as exc:  # noqa: BLE001 - the layer tree may raise anything
                log_debug("reading the current layer tree node", exc)
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            for node in root.findLayers():
                if not node.isVisible():
                    continue
                layer = node.layer()
                if layer is not None and layer.isValid():
                    return layer
        except Exception as exc:  # noqa: BLE001 - the project tree may raise anything
            log_debug("scanning the project for a visible layer", exc)
        return None

    def _nivo_layer_for_action(self, target):
        if target:
            return QgsProject.instance().mapLayer(target)
        return self._active_qgis_layer()

    def _set_nivo_compose_busy(self, busy):
        """Busy always disables; not busy ASKS whether this state can ask.

        Two owners of one control disagree eventually. Re-enabling
        unconditionally meant that finishing a turn switched Ask back on in a
        disconnected hosted session that cannot take one, so the button was
        live and the next press produced only a status line.
        """
        if busy:
            if self.nivo_send_button is not None:
                self.nivo_send_button.setEnabled(False)
            if self.nivo_input is not None:
                self.nivo_input.setEnabled(False)
        else:
            self._refresh_assistant_state()
        self._refresh_stop_button()

    def _refresh_stop_button(self):
        """Stop is offered whenever there is something to stop.

        Two things now qualify: a compose stream, and a plan run this panel
        started. They used to be one, and the run begins after the stream ends,
        so the control vanished at exactly the moment a person watching credits
        drain would reach for it.
        """
        if self.nivo_stop_button is None:
            return
        stoppable = self._nivo_compose_task is not None or bool(self.plan_run_id)
        self.nivo_stop_button.setVisible(stoppable)
        self.nivo_stop_button.setEnabled(stoppable)

    def assistant_runtime(self) -> dict:
        """Which runtime this turn will actually take, from stored settings.

        One resolution for the label and for the turn. Reading it twice from
        two different places is how the panel came to promise a direct path
        that the turn never took.

        Cached, because `assistant_settings` loads the stored key and that
        opens the encrypted QGIS authentication database. Once per turn is
        right; once per layer click - which is how often the header line now
        asks - is not. It is dropped at the only two places the answer can
        change, so it is never stale rather than merely usually fresh.
        """
        if getattr(self, "_assistant_runtime_cache", None) is None:
            self._assistant_runtime_cache = resolve_runtime(self.assistant_settings())
        return self._assistant_runtime_cache

    def _has_provider(self):
        """Is a model provider configured at all?

        Presence, from the non-secret preference, so this never opens the
        encrypted authentication database - `_refresh_ui` runs on every
        connection change and every layer click.
        """
        settings = QSettings()
        return bool(str(settings.value("mapdex/nivo/provider", "") or "").strip())

    @guarded
    def choose_own_model(self, *args):
        """Open the settings panel on the provider fields.

        The first-open screen offers the choice; the panel that already exists
        is where it is made. A second provider form would be two controls for
        one decision, and they would disagree eventually.
        """
        if self.settings_button is not None:
            self.settings_button.setChecked(True)
        if self.provider_box is not None:
            self.provider_box.setFocus()
        self._set_status(
            "Choose a provider and paste its key, then Save settings. "
            "Nivo answers from your own model after that."
        )

    def _forget_assistant_runtime(self):
        """Drop the cached decision after the settings behind it moved."""
        self._assistant_runtime_cache = None

    @guarded
    def ask_nivo(self, *args):
        if self._nivo_compose_task is not None:
            self._set_status("Nivo is already working. Use Stop to cancel that request.")
            return
        # One resolution, used for the gate and for the branch below. A hosted
        # turn IS a Mapdex call, so it needs a Mapdex session; a BYOK turn
        # reaches the user's own provider and runs only capabilities that
        # execute on this machine, so requiring an account for it is what would
        # make the feature unreachable for exactly the people it is offered to.
        runtime = self.assistant_runtime()
        if needs_mapdex_account(runtime) and (not self.api.token or not self.project_id):
            # The same sentence the header already shows, from the same place,
            # so the reason a press did nothing matches the reason on screen.
            self._set_status(panel_state.assistant_engine(
                runtime, bool(self.api.token), self.project_id
            )["blocked_reason"])
            return
        message = self.nivo_input.text().strip() if self.nivo_input is not None else ""
        if not message:
            return
        self._set_nivo_compose_busy(True)
        self._nivo_state = transition(self._nivo_state, "send")
        self._nivo_turns.append(transcript_turn("user", message))
        self._say("Thinking…")
        self.nivo_status.setText("Nivo AI is reading your map context…")
        self.nivo_input.clear()
        self._refresh_nivo_context()
        self._nivo_request_id += 1
        request_id = self._nivo_request_id
        # Build the context HERE, on the main thread. QgsTask.run() executes on a
        # worker thread, and iface.activeLayer(), the map canvas and the layer
        # tree are main-thread only: reading them from the task returned an empty
        # snapshot, so Nivo answered "no layer is active yet" while a layer was
        # plainly open. Only the HTTP call belongs in the background. The BYOK
        # loop obeys the same rule for the same reason - it reads the same
        # canvas and runs capabilities against the same layer tree.
        # The objective a continuation re-sends. A continuation is the same
        # question one step on, so re-deriving it from the input box would send
        # whatever the user has since typed there.
        self._nivo_objective = message
        self._nivo_round_trips = 0
        context = companion_context(self._nivo_snapshot())
        if is_byok(runtime):
            self._start_byok_turn(message, context, request_id)
            return
        self._start_hosted_turn(message, context, request_id)

    def _start_hosted_turn(self, message, context, request_id):
        """Ask Mapdex: one HTTP round trip, on a worker thread."""
        project_id = self.project_id
        # The active project can have moved since the conversation was opened
        # (starting a task switches it). A thread from another project cannot
        # be continued here, so it is dropped rather than sent.
        thread_id = remembered_thread(self._nivo_thread_id, self._nivo_thread_project, project_id)
        title = thread_title(message)
        self._set_nivo_compose_busy(True)
        self._nivo_compose_task = self._task(
            "Nivo compose",
            lambda: self._compose_in_thread(project_id, message, context, thread_id, title),
            lambda exception, outcome: self._nivo_composed(request_id, exception, outcome),
            busy=False,
        )

    # -- the BYOK turn -----------------------------------------------------

    def _start_byok_turn(self, message, context, request_id):
        """Ask the user's own provider, and run the answer's steps here.

        The provider is built and the session is constructed at this line
        deliberately: this is where a turn stops being a Mapdex request, so it
        is where the decision should be readable. The capability allowance is
        the offline subset - a turn that never contacts Mapdex cannot perform a
        Mapdex Run, so georeference, digitization, validation, batch and review
        stay account-gated rather than being offered and then refused.

        Each turn gets a fresh session, so a BYOK conversation has no memory of
        earlier turns the way the hosted path does through its Mapdex thread.
        That is a stated limitation, not an oversight: `AgentSession` counts its
        step budget and its repeat-call guard per instance, so simply keeping
        one across turns would leave the second question with a spent budget.
        Carrying `history` forward without those two is the change, and it is
        not this one.
        """

        def retry_hosted():
            self._start_hosted_turn(message, context, request_id)

        try:
            provider = build_provider(self.assistant_settings())
            session = AgentSession(
                provider,
                self._capability_executor(),
                client=CLIENT_QGIS,
                confirm=self._confirm_byok_step,
                allowed=session_allowance(CLIENT_QGIS),
            )
        except ProviderError as error:
            self._byok_failed(request_id, error, retry_hosted)
            return
        except Exception as error:  # noqa: BLE001 - the host must survive anything
            self._byok_failed(request_id, error, retry_hosted)
            return
        turn = ByokTurn(session, self._offer_hosted_path, retry_hosted).start(message, context)
        if self.nivo_status is not None:
            # Deliberately not the vendor's name. Which company answers a turn
            # is our routing, not the user's business, and printing it here
            # reads as Mapdex announcing where the question was sent. The one
            # routing fact a user does need - that this turn did NOT go to
            # Mapdex - is a settings-panel claim, not a per-turn caption.
            self.nivo_status.setText("Nivo is working on that…")
        self._byok_step(turn, request_id)

    def _byok_step(self, turn, request_id):
        """Hand the next model call to a worker thread, and only that."""
        prompt = turn.next_prompt()
        if prompt is None:
            self._byok_finished(request_id, turn.exhausted())
            return
        system, messages = prompt
        self._nivo_compose_task = self._task(
            "Nivo (your provider)",
            lambda: turn.provider_reply(system, messages),
            lambda exception, reply: self._byok_replied(turn, request_id, exception, reply),
            busy=False,
        )

    @guarded
    def _byok_replied(self, turn, request_id, exception, reply):
        """Back on the main thread: run what the model chose, then loop."""
        if request_id != self._nivo_request_id:
            return
        self._nivo_compose_task = None
        if exception is not None:
            self._byok_failed(request_id, exception, turn.run_hosted, turn=turn)
            return
        try:
            outcome = turn.deliver(reply)
        except Exception as error:  # noqa: BLE001 - the host must survive anything
            self._byok_failed(request_id, error, turn.run_hosted, turn=turn)
            return
        if outcome is None:
            self._byok_step(turn, request_id)
            return
        self._byok_finished(request_id, outcome)

    @guarded
    def _byok_failed(self, request_id, error, retry_hosted, turn=None):
        """A provider that could not answer ends the turn here.

        It must not quietly become a Mapdex request: that would upload the map
        context the settings panel had just promised to keep off Mapdex. The
        hosted path is offered through one function that always asks first.
        """
        if request_id != self._nivo_request_id:
            return
        self._nivo_compose_task = None
        self._nivo_state = transition(self._nivo_state, "error")
        if turn is not None:
            outcome = turn.provider_failed(error)
        else:
            outcome = provider_failed(error, self._offer_hosted_path, retry_hosted)
        self._replace_last_assistant_turn(outcome["message"])
        if outcome["handed_to_mapdex"]:
            # The hosted turn owns the busy state and the transcript from here.
            if self.nivo_status is not None:
                self.nivo_status.setText("Asking Mapdex instead…")
            return
        self._set_nivo_compose_busy(False)
        if self.nivo_status is not None:
            self.nivo_status.setText("Your provider could not answer")
        self._set_status(outcome["message"])

    @guarded
    def _byok_finished(self, request_id, outcome):
        """Render a terminal BYOK outcome. Capabilities have already run."""
        if request_id != self._nivo_request_id:
            return
        self._nivo_compose_task = None
        self._set_nivo_compose_busy(False)
        outcome = outcome if isinstance(outcome, dict) else {}
        kind = str(outcome.get("kind") or "")
        self._replace_last_assistant_turn(str(outcome.get("message") or "Nivo returned no message."))
        if kind == "clarify":
            self._nivo_state = transition(self._nivo_state, "clarify")
            status = "Waiting for your answer"
        elif kind == "confirmation_required":
            self._nivo_state = transition(self._nivo_state, "confirm")
            status = "Not run — confirmation declined"
        elif kind == "incomplete":
            self._nivo_state = transition(self._nivo_state, "error")
            status = "Ran out of steps"
        else:
            status = "Ready"
        if self.nivo_status is not None:
            self.nivo_status.setText(status)
        if outcome.get("map_effects"):
            self.iface.mapCanvas().refresh()

    def _confirm_byok_step(self, decision):
        """The registry's confirmation gate, asked from inside the loop.

        The model's own view of risk is not consulted: the decision carries the
        capability id and the registry decides whether it needs a person.
        """
        return self._confirm_capability(str((decision or {}).get("capability") or ""))

    def _offer_hosted_path(self, notice):
        """Ask whether to send this question to Mapdex after a provider failure.

        Returns False without a dialog when there is no Mapdex session to offer:
        a question promising a fallback that cannot happen is worse than none.
        """
        if not self.api.token or not self.project_id:
            return False
        yes = enum_member(QMessageBox, "StandardButton", "Yes")
        no = enum_member(QMessageBox, "StandardButton", "No")
        answer = QMessageBox.question(
            self.iface.mainWindow(),
            "Ask Mapdex instead?",
            "{}\n\nNothing has been sent to Mapdex. Asking Mapdex sends your bounded map "
            "context to Mapdex and uses your plan. Ask Mapdex now?".format(notice),
            yes | no,
            no,
        )
        return answer == yes

    def _replace_last_assistant_turn(self, text, steps=None):
        """Overwrite the pending "Thinking…" bubble, or add one.

        Four call sites wrote this by hand, each reading the entry by position,
        which is how they all went on building tuples after the entry became a
        dict. One helper, and `steps` is a parameter because the composed reply
        brings its own while a stop or a failure keeps whatever was there.
        """
        if self._nivo_turns and self._nivo_turns[-1]["sender"] == "assistant":
            last = self._nivo_turns[-1]
            self._nivo_turns[-1] = transcript_turn(
                "assistant", text, steps if steps is not None else last["steps"],
                last["actions"], last["severity"], last["fact"])
            self._render_nivo_turns()
            return
        self._say(str(text), steps)

    def _compose_in_thread(self, project_id, message, context, thread_id, title, companion_results=None):
        """One conversational turn. Network only — runs on a worker thread.

        Both calls belong here rather than in the caller: opening a
        conversation is an HTTP round trip, and doing it on the main thread
        would freeze QGIS before the question was even sent.

        A remembered thread the server no longer has answers 404. That is not
        an error the user can act on — they never knew the conversation had an
        id — so it opens a fresh one and asks the question again. The turn
        succeeds; only the memory of earlier turns is lost, which is already
        true whatever we do.
        """
        notice = ""
        if not thread_id:
            thread_id, notice = self._open_conversation(project_id, title)
        try:
            response = self.api.compose(
                project_id, message, context, thread_id=thread_id,
                companion_results=companion_results)
        except MapdexAPIError as exc:
            if not thread_id or not thread_is_gone(exc.status):
                # A conversation we just opened is still ours even though this
                # turn failed. Carrying it back on the exception keeps the next
                # attempt in the same thread instead of leaving an orphan
                # behind and opening another one.
                exc.mapdex_thread_id = thread_id
                raise
            thread_id, notice = self._open_conversation(project_id, title)
            try:
                response = self.api.compose(
                    project_id, message, context, thread_id=thread_id,
                    companion_results=companion_results)
            except MapdexAPIError as retry_error:
                retry_error.mapdex_thread_id = thread_id
                raise
        return {"thread_id": thread_id, "response": response, "notice": notice}

    def _open_conversation(self, project_id, title):
        """Open a thread, or report why this turn has no memory. Worker thread.

        A deployment whose thread store is unavailable must still be able to
        answer a question. Continuity degrades, and the panel says so rather
        than leaving the user to discover that follow-ups stopped working.
        """
        try:
            created = self.api.create_thread(project_id, title)
        except MapdexAPIError:
            return "", "Answered without conversation history: Mapdex did not open a conversation."
        thread_id = str((created or {}).get("id") or "")
        if not thread_id:
            return "", "Answered without conversation history: Mapdex did not open a conversation."
        return thread_id, ""

    @guarded
    def stop_nivo(self, *args):
        if self.plan_run_id:
            self._cancel_plan_run()
            return
        if self._nivo_compose_task is None:
            return
        self._nivo_request_id += 1
        try:
            self._nivo_compose_task.cancel()
        except RuntimeError:
            pass
        self._nivo_compose_task = None
        self._set_nivo_compose_busy(False)
        self._nivo_state = transition(self._nivo_state, "error")
        self._replace_last_assistant_turn("Stopped.")
        if self.nivo_status is not None:
            self.nivo_status.setText("Stopped")
        self._set_status("Nivo request stopped.")

    @guarded
    def _nivo_composed(self, request_id, exception, outcome):
        if request_id != self._nivo_request_id:
            return
        self._nivo_compose_task = None
        self._set_nivo_compose_busy(False)
        if exception:
            self._nivo_state = transition(self._nivo_state, "error")
            # Adopt only a conversation this turn actually opened. Re-stamping
            # the remembered one would bind it to whatever project is active
            # now, which is not necessarily the project it belongs to.
            carried = getattr(exception, "mapdex_thread_id", "")
            if carried:
                self._adopt_conversation(carried)
            self._replace_last_assistant_turn("I couldn't complete that request.")
            if self.nivo_status is not None:
                self.nivo_status.setText("Request failed")
            self._show_error("Nivo could not compose a response", exception)
            return
        outcome = outcome if isinstance(outcome, dict) else {}
        response = outcome.get("response")
        # Adopt whichever conversation actually carried this turn: the
        # remembered one, or the replacement opened after the remembered one
        # turned out to be gone.
        self._adopt_conversation(outcome.get("thread_id") or "")
        if not isinstance(response, dict):
            self._nivo_state = transition(self._nivo_state, "error")
            self._show_error("Nivo could not compose a response", RuntimeError("Invalid compose response"))
            return
        if self.nivo_reply is not None:
            reply = str(response.get("text") or response.get("message") or "Nivo returned no message.")
            # The steps are what the turn actually did. Rendering only the reply
            # is what made a turn that ran four capabilities and a turn that
            # answered from memory look identical.
            steps = step_rows(response)
            self._replace_last_assistant_turn(reply, steps)
        if self.nivo_status is not None:
            notice = str(outcome.get("notice") or "")
            budget = budget_label(response)
            self.nivo_status.setText(notice or budget or "Ready")
        # A computed answer whose output is POSITIONS goes on the canvas. A
        # bearing is a number and stays in the reply; a traverse is a walk
        # between stations, and handing somebody six coordinate pairs in a chat
        # bubble makes them copy the work in by hand, which is the work this
        # plugin exists to remove.
        self._draw_survey_result(response)
        self._nivo_action_results = []
        for action in allowed_actions(response):
            self._nivo_state = transition(self._nivo_state, "action")
            self._apply_nivo_action(action)
        for action in confirmation_actions(response):
            self._nivo_state = transition(self._nivo_state, "confirm")
            self._confirm_nivo_action(action)
        self._continue_objective(response)
        # Last, because everything above is what the turn ALREADY did and this
        # is what it is asking to do next. Offering first would put a dialog in
        # front of an answer the person has not read.
        self._offer_plan_run(response)

    @guarded
    def _draw_survey_result(self, response):
        """Put a survey answer's positions on the canvas as memory layers.

        Everything that can be wrong with the payload was decided in
        `survey_drawing`, which is pure and tested; what is left here is
        creating the layers. A memory layer, deliberately, for the same reason
        a drawn shape uses one: it appears immediately and asks nobody for a
        path or a format while they are in the middle of a question.

        Nothing the user already had is touched, so this is safe rather than
        consequential and needs no confirmation.
        """
        from .survey_drawing import layer_specs  # noqa: PLC0415

        if not isinstance(response, dict):
            return
        self._create_survey_layers(layer_specs(response.get("spatial_tool_result")))

    @guarded
    def _create_survey_layers(self, specs):
        """Create one memory layer per geometry type a survey answer produced.

        Shared by the two ways an answer arrives: a conversational turn and a
        planned run. A user who asks for a traverse should get the same picture
        whichever way they asked, and one creation path is how that stays true.
        """
        from qgis.core import (  # noqa: PLC0415 - Qt-only import
            QgsFeature,
            QgsField,
            QgsGeometry,
            QgsJsonUtils,
            QgsProject,
            QgsVectorLayer,
        )
        from qgis.PyQt.QtCore import QVariant  # noqa: PLC0415 - Qt-only import

        from .survey_drawing import attribute_names  # noqa: PLC0415

        if not specs:
            return

        created = []
        for spec in specs:
            layer = QgsVectorLayer(
                "{}?crs=EPSG:4326&index=yes".format(spec.geometry_type),
                self._unique_layer_name(spec.name), "memory")
            if not layer.isValid():
                continue
            names = attribute_names(spec.features)
            if names:
                # Declared before the features go in, and from the UNION of the
                # properties: a traverse labels its stations unevenly, and
                # taking the first feature's keys would drop a label.
                #
                # Every property is carried as text. A station number is a
                # number and a label is not, and guessing per column would make
                # the schema depend on which answer arrived first.
                text_type = enum_member(QVariant, "Type", "String")
                layer.dataProvider().addAttributes(
                    [QgsField(name, text_type) for name in names])
                layer.updateFields()
            features = []
            for entry in spec.features:
                geometry = QgsGeometry.fromWkt(
                    QgsJsonUtils.geometryFromGeoJson(json.dumps(entry["geometry"])).asWkt())
                if geometry.isEmpty():
                    continue
                feature = QgsFeature(layer.fields())
                feature.setGeometry(geometry)
                for name in names:
                    value = entry["properties"].get(name)
                    feature.setAttribute(name, "" if value is None else str(value))
                features.append(feature)
            if not features:
                continue
            layer.dataProvider().addFeatures(features)
            layer.updateExtents()
            QgsProject.instance().addMapLayer(layer)
            layer.triggerRepaint()
            created.append(layer)

        if not created:
            return
        self.iface.setActiveLayer(created[-1])
        # The assistant has to learn about the layers in this turn, or the next
        # question is asked about something it cannot see.
        self._refresh_nivo_context()

    @guarded
    def _continue_objective(self, response):
        """Tell the server what the actions did, so the objective can go on.

        This is the client half of a multi-step turn. Without it a desktop
        objective ends after one action however many the question needed, which
        is what taught users to type one small command at a time.

        The user's message is re-sent unchanged: the objective has not changed,
        only what is known about it. Nothing here decides anything about GIS -
        the server chooses the next capability and the registry still validates
        it before it runs.
        """
        results = list(getattr(self, "_nivo_action_results", []))
        if not should_continue(response, results, getattr(self, "_nivo_round_trips", 0)):
            self._nivo_round_trips = 0
            return
        self._nivo_round_trips = getattr(self, "_nivo_round_trips", 0) + 1
        message = self._nivo_objective
        if not message:
            self._nivo_round_trips = 0
            return
        used, total = continuation_budget(response)
        if self.nivo_status is not None and total:
            self.nivo_status.setText("Step {} of {}".format(used + 1, total))
        context = companion_context(self._nivo_snapshot())
        self._nivo_request_id += 1
        request_id = self._nivo_request_id
        project_id = self._active_project_id()
        thread_id = self._nivo_thread_id
        self._set_nivo_compose_busy(True)

        def work():
            return self._compose_in_thread(
                project_id, message, context, thread_id, message, companion_results=results)

        self._nivo_compose_task = self._task(
            "Nivo is continuing", work,
            lambda error, outcome: self._nivo_composed(request_id, error, outcome))

    @staticmethod
    def _plain(label, text):
        """Put text into a QLabel as text, not as markup.

        QLabel defaults to Qt::AutoText, which sniffs the string and renders
        anything that looks like HTML as HTML. Assistant replies and stored
        history are data: a `<b>` in a layer name, or an `<img>` in a message
        replayed from the server, must appear as those characters.
        """
        label.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        label.setText(str(text))
        return label

    def _step_widget(self, row_data):
        """One step of the turn: what ran, with what, and how it ended.

        Status is a word, never a colour on its own: the panel is themed by
        QGIS and a reader on a monochrome theme or a screen reader must get the
        same answer as everyone else.
        """
        line = "{}  {}. {}".format(row_data.get("marker", "+"), row_data.get("index", 1), row_data.get("title", ""))
        tail = [part for part in (row_data.get("status_label"), row_data.get("duration")) if part]
        if tail:
            line += "  (" + ", ".join(tail) + ")"
        detail = row_data.get("detail") or ""
        params = row_data.get("params") or ""
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(2, 1, 2, 1)
        column.setSpacing(1)
        head = self._plain(QLabel(), line)
        head.setWordWrap(True)
        tone = row_data.get("tone")
        head.setStyleSheet(
            "color:#F2B8B5;" if tone == "danger" else "color:#8F96A8;"
        )
        column.addWidget(head)
        if detail:
            body = self._plain(QLabel(), "     " + detail)
            body.setWordWrap(True)
            body.setStyleSheet("color:#A9B0C0;")
            column.addWidget(body)
        if params:
            # The arguments are the half of "what did it do" that nothing used
            # to record. They are shown small rather than hidden, because the
            # panel has no room for a disclosure control per step.
            argument_line = self._plain(QLabel(), "     " + params)
            argument_line.setWordWrap(True)
            argument_line.setStyleSheet("color:#6F7688; font-family:monospace; font-size:10px;")
            column.addWidget(argument_line)
        widget.setStyleSheet("background:transparent; border:0;")
        return widget

    @guarded
    def _render_nivo_turns(self):
        """Render sender-distinct native widget bubbles; no model HTML."""
        if self.nivo_reply is None:
            return
        transcript = self.nivo_reply.widget()
        layout = transcript.layout() if transcript is not None else None
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        # The opening reading is drawn as turns, ahead of the conversation, and
        # is recomputed rather than accumulated - it describes the layer that is
        # active NOW. Once the user says something it steps aside, because on a
        # narrow dock it would push the thing they are reading off the top.
        self._refresh_opening()
        for turn in list(self._nivo_opening) + list(self._nivo_turns):
            layout.addWidget(self._turn_widget(turn))
        layout.addStretch(1)
        bar = self.nivo_reply.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _turn_widget(self, turn):
        """One transcript entry: a measured line, what was said, what to do."""
        sender = turn.get("sender")
        text = turn.get("text", "")
        card = QWidget()
        row = QVBoxLayout(card)

        if sender != "user" and str(text).startswith("Thinking"):
            card.setObjectName("mapdexTurn")
            row.setContentsMargins(2, 2, 2, 2)
            body = self._plain(QLabel(), text)
            body.setObjectName("mapdexTurnThinking")
            body.setWordWrap(True)
            row.addWidget(body)
            return card

        row.setContentsMargins(8, 7, 8, 7)
        row.setSpacing(3)
        if not text and not turn.get("fact"):
            # A turn that carries only actions: no name, no empty body line.
            card.setObjectName("mapdexTurn")
            strip = QVBoxLayout()
            strip.setContentsMargins(0, 0, 0, 0)
            strip.setSpacing(5)
            for action in turn.get("actions") or []:
                strip.addWidget(self._action_chip(action))
            row.addLayout(strip)
            return card
        label = QLabel("You" if sender == "user" else "Nivo")
        label.setObjectName("mapdexTurnWhoUser" if sender == "user" else "mapdexTurnWho")
        row.addWidget(label)

        # The measurement, set apart from the sentence about it. Mono, because
        # a file name, a pixel size and a CRS code are data and read as data.
        if turn.get("fact"):
            fact = self._plain(QLabel(), turn["fact"])
            fact.setObjectName("mapdexTurnFact")
            fact.setWordWrap(True)
            row.addWidget(fact)

        body = self._plain(QLabel(), text)
        body.setWordWrap(True)

        severity = turn.get("severity")
        if severity and sender != "user":
            # Icon AND wording, never colour alone: DESIGN.md section 8, and
            # the coloured stripe this replaces broke exactly that rule.
            said = QWidget()
            said_row = QHBoxLayout(said)
            said_row.setContentsMargins(0, 0, 0, 0)
            said_row.setSpacing(7)
            glyph = QLabel()
            glyph.setFixedSize(16, 16)
            glyph.setPixmap(self._severity_icon(severity).pixmap(15, 15))
            said_row.addWidget(glyph, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
            said_row.addWidget(body, 1)
            row.addWidget(said)
        else:
            row.addWidget(body)

        if sender == "user":
            card.setObjectName("mapdexTurnUser")
            card.setMaximumWidth(BUBBLE_WIDTH)
            body.setObjectName("mapdexTurnBodyUser")
        else:
            card.setObjectName("mapdexTurn")
            body.setObjectName("mapdexTurnBody")
            for row_data in turn.get("steps") or []:
                row.addWidget(self._step_widget(row_data))
            actions = turn.get("actions") or []
            if actions:
                strip = QVBoxLayout()
                strip.setContentsMargins(0, 5, 0, 0)
                strip.setSpacing(5)
                for action in actions:
                    strip.addWidget(self._action_chip(action))
                row.addLayout(strip)
        return card

    # ----------------------------------------------------------------------
    # Conversations: New chat, History, and the thread the panel is continuing
    # ----------------------------------------------------------------------

    def _adopt_conversation(self, thread_id, project_id=None):
        """Make this the conversation the next question continues.

        Persisted with the project it belongs to, so a QGIS restart resumes it
        and a different project does not.
        """
        project = self.project_id if project_id is None else project_id
        thread_id = str(thread_id or "")
        self._nivo_thread_id = thread_id
        self._nivo_thread_project = project if thread_id else ""
        settings = QSettings()
        if thread_id:
            settings.setValue(THREAD_ID_SETTING, thread_id)
            settings.setValue(THREAD_PROJECT_SETTING, self._nivo_thread_project)
        else:
            settings.remove(THREAD_ID_SETTING)
            settings.remove(THREAD_PROJECT_SETTING)

    @guarded
    def new_nivo_task(self, *args):
        """Clear the transcript and start a fresh conversation on the next turn.

        Deliberately local: nothing is deleted on the server, so the
        conversation being cleared away is still in History. The thread is
        dropped rather than replaced because an empty conversation nobody ever
        used is noise in that list - the next question opens one.
        """
        if self._nivo_compose_task is not None:
            self._set_status("Nivo is still working. Use Stop before starting a new chat.")
            return
        self._nivo_turns = []
        self._render_nivo_turns()
        self._adopt_conversation("")
        if self.nivo_status is not None:
            self.nivo_status.setText("New chat. The previous conversation is in History.")
        self._refresh_nivo_context()

    @guarded
    def open_nivo_history(self, *args):
        """Open the History dialog for this project's conversations."""
        if not self.api.token or not self.project_id:
            self._set_status("Connect Mapdex and choose a project to see earlier conversations.")
            return
        if self._history_refs is not None:
            return
        dialog, refs = build_thread_history_dialog(self.iface.mainWindow())
        self._history_refs = refs
        self._history_rows = []
        refs["retry_button"].clicked.connect(self._load_thread_history)
        refs["open_button"].clicked.connect(self._open_selected_thread)
        refs["delete_button"].clicked.connect(self._delete_selected_thread)
        refs["close_button"].clicked.connect(dialog.reject)
        # Lambdas are safe here in a way they are not on `iface`: these
        # connections die with the dialog, which this method owns end to end.
        refs["list"].itemDoubleClicked.connect(lambda _item: self._open_selected_thread())
        refs["list"].currentRowChanged.connect(lambda _row: self._refresh_history_buttons())
        self._load_thread_history()
        # PyQt6 dropped `exec_`; PyQt5 (QGIS 3) has both. Resolved by name so
        # the dialog opens on either binding rather than raising the first time
        # a user clicks History.
        run_modal = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
        try:
            run_modal()
        finally:
            self._history_refs = None
            self._history_rows = []
            dialog.deleteLater()

    @guarded
    def _load_thread_history(self, *args):
        """Fetch this project's conversations and show the loading state."""
        refs = self._history_refs
        if refs is None:
            return
        project_id = self.project_id
        self._history_request_id += 1
        request_id = self._history_request_id
        refs["state_label"].setVisible(True)
        refs["state_label"].setText("Loading conversations…")
        refs["list"].setVisible(False)
        # Empty the row model BEFORE the widget: clear() emits
        # currentRowChanged, and the handler that fires would otherwise read
        # the rows of the listing that has just gone.
        self._history_rows = []
        refs["list"].clear()
        refs["retry_button"].setVisible(False)
        self._refresh_history_buttons()
        self._task(
            "Load Nivo conversations",
            lambda: self.api.list_threads(project_id),
            lambda exception, payload: self._thread_history_loaded(request_id, exception, payload),
            busy=False,
        )

    @guarded
    def _thread_history_loaded(self, request_id, exception, payload):
        refs = self._history_refs
        if refs is None or request_id != self._history_request_id:
            # The dialog was closed, or a reload has already superseded this.
            return
        if exception:
            refs["state_label"].setVisible(True)
            refs["state_label"].setText(
                "Could not load conversations. {}".format(describe_exception(exception))
            )
            refs["retry_button"].setVisible(True)
            self._refresh_history_buttons()
            return
        refs["retry_button"].setVisible(False)
        self._history_rows = thread_list_items(payload)
        if not self._history_rows:
            refs["state_label"].setVisible(True)
            refs["state_label"].setText(
                "No earlier conversations in this project yet. Ask Nivo something and it will "
                "appear here."
            )
            refs["list"].setVisible(False)
            self._refresh_history_buttons()
            return
        refs["state_label"].setVisible(False)
        refs["list"].setVisible(True)
        for row in self._history_rows:
            # Plain list text. A title is the user's own words returned by the
            # server, and this transcript never renders such text as markup.
            refs["list"].addItem(describe_thread(row))
        current = 0
        for index, row in enumerate(self._history_rows):
            if row["id"] == self._nivo_thread_id:
                current = index
                break
        refs["list"].setCurrentRow(current)
        self._refresh_history_buttons()

    def _refresh_history_buttons(self):
        refs = self._history_refs
        if refs is None:
            return
        try:
            selected = 0 <= refs["list"].currentRow() < len(self._history_rows)
        except RuntimeError:
            return
        refs["open_button"].setEnabled(selected)
        refs["delete_button"].setEnabled(selected)

    def _selected_thread(self):
        refs = self._history_refs
        if refs is None:
            return None
        row = refs["list"].currentRow()
        if 0 <= row < len(self._history_rows):
            return self._history_rows[row]
        return None

    @guarded
    def _open_selected_thread(self, *args):
        thread = self._selected_thread()
        refs = self._history_refs
        if thread is None or refs is None:
            return
        refs["dialog"].accept()
        project_id = self.project_id
        thread_id = thread["id"]
        if self.nivo_status is not None:
            self.nivo_status.setText("Loading that conversation…")
        self._task(
            "Load Nivo conversation",
            lambda: self.api.thread_messages(thread_id, project_id),
            lambda exception, payload: self._thread_opened(
                thread_id, project_id, exception, payload
            ),
            busy=False,
        )

    @guarded
    def _thread_opened(self, thread_id, project_id, exception, payload):
        if exception:
            if isinstance(exception, MapdexAPIError) and thread_is_gone(exception.status):
                # Deleted between listing it and opening it. Say so; do not
                # adopt a conversation the server does not have.
                if self.nivo_status is not None:
                    self.nivo_status.setText("That conversation is no longer available.")
                self._set_status("That Nivo conversation is no longer available.")
                return
            if self.nivo_status is not None:
                self.nivo_status.setText("Could not open that conversation.")
            self._show_error("Could not open that conversation", exception)
            return
        turns = thread_turns(payload)
        # A replayed conversation carries no execution trace: the server stores
        # the messages, not the steps. Restoring it with an empty step list says
        # "not recorded" rather than "nothing ran", which are different claims.
        self._nivo_turns = [transcript_turn(sender, text) for sender, text in turns]
        self._render_nivo_turns()
        self._adopt_conversation(thread_id, project_id)
        if self.nivo_status is not None:
            if turns:
                self.nivo_status.setText(
                    "Continuing this conversation ({} messages).".format(len(turns))
                )
            else:
                self.nivo_status.setText("That conversation has no messages yet.")

    @guarded
    def _delete_selected_thread(self, *args):
        thread = self._selected_thread()
        refs = self._history_refs
        if thread is None or refs is None:
            return
        yes = enum_member(QMessageBox, "StandardButton", "Yes")
        no = enum_member(QMessageBox, "StandardButton", "No")
        answer = QMessageBox.question(
            refs["dialog"],
            "Delete conversation",
            "Delete “{}” from Mapdex?\n\nThis cannot be undone.".format(thread["title"]),
            yes | no,
            no,
        )
        if answer != yes:
            return
        project_id = self.project_id
        thread_id = thread["id"]
        refs["state_label"].setVisible(True)
        refs["state_label"].setText("Deleting…")
        self._task(
            "Delete Nivo conversation",
            lambda: self.api.delete_thread(thread_id, project_id),
            lambda exception, _result: self._thread_deleted(thread_id, exception),
            busy=False,
        )

    @guarded
    def _thread_deleted(self, thread_id, exception):
        refs = self._history_refs
        if exception:
            if refs is not None:
                refs["state_label"].setVisible(True)
                refs["state_label"].setText(
                    "Could not delete that conversation. {}".format(describe_exception(exception))
                )
                refs["retry_button"].setVisible(True)
            return
        if thread_id == self._nivo_thread_id:
            # The conversation the panel was continuing no longer exists. The
            # transcript stays readable; only the memory is dropped, so the
            # next question opens a new conversation instead of a 404.
            self._adopt_conversation("")
            if self.nivo_status is not None:
                self.nivo_status.setText("Conversation deleted. The next question starts a new one.")
        if refs is not None:
            self._load_thread_history()

    def _say(self, text, steps=None, actions=None, severity="", fact=""):
        """Append one assistant turn and draw it."""
        self._nivo_turns.append(
            transcript_turn("assistant", text, steps, actions, severity, fact))
        self._render_nivo_turns()

    @guarded
    def _apply_nivo_action(self, action):
        """Dispatch one compose action through the capability registry.

        There is a single path. A canonical `domain.name@1` id and the legacy
        `qgis:*` id that means the same thing both resolve to one registered
        capability, are validated by the registry, and are executed by the same
        executor table - so an action behaves identically however the server
        chose to spell it, and a consequential one is gated by the registry's own
        risk metadata rather than by whichever branch happened to handle it.

        Only the four legacy ids with no registered capability keep a
        plugin-native handler, and those are a table lookup too.
        """
        action_id = action.get("id")
        if not action_id or action_id in self._executed_nivo_actions:
            self._set_status("Nivo ignored a duplicate or malformed action.")
            return
        self._executed_nivo_actions.add(action_id)
        self._nivo_state = transition(self._nivo_state, "execute")
        tool = action["tool"]
        target = action.get("target")
        layer = self._nivo_layer_for_action(target)
        if target and (layer is None or not layer.isValid()):
            self._set_status("Nivo did not run the action because its target layer is no longer available.")
            return

        try:
            resolved = self._capability_request(tool, action, layer)
        except CapabilityError as error:
            self._set_status("Nivo could not run that: {}".format(error))
            return
        if resolved is not None:
            capability_id, params = resolved
            if self._run_capability(capability_id, params, action.get("summary") or tool):
                self._nivo_state = transition(self._nivo_state, "done")
            return

        handler = PLUGIN_NATIVE_ACTIONS.get(tool)
        if handler is None:
            # Filter/style/review/result commands the server can name but this
            # build has no capability for. Never turn free-form model params
            # into QGIS calls just because the id looked familiar.
            self._set_status("Nivo prepared a confirmation-required action: {}".format(
                action.get("summary") or tool))
            return
        if not getattr(self, handler)(action):
            return
        self._nivo_state = transition(self._nivo_state, "done")

    def _capability_request(self, tool, action, layer):
        """Resolve an action to a validated-capability request, or None.

        Translation happens here and execution does not: a legacy id names the
        same operation as its canonical capability, and routing both through the
        registry is precisely what makes them behave the same. Returns None when
        no registered capability covers the id, so the caller can fall back to
        the plugin-native table.
        """
        capability_id = LEGACY_CAPABILITY_IDS.get(tool, tool)
        capability = get_capability(capability_id)
        if capability is None:
            return None
        params = dict(action.get("params") or {})
        if capability_id == "map.zoom_extent@1":
            # The legacy payload may carry a centre and a zoom level rather than
            # a box. Resolving it here is what lets one registry-validated
            # capability serve both shapes.
            resolved = resolve_extent(params)
            if resolved is None:
                raise CapabilityError("that map extent had no usable bounding box or centre")
            # Emitted under the capability's declared name, which is the
            # server's. The resolver above is what accepts whichever name
            # arrived; validation below only ever sees the canonical one.
            params = {"bounds": resolved["bbox"], "crs": resolved["crs"]}
        if "layer_id" in capability.params and "layer_id" not in params:
            layer_id = str(action.get("target") or "") or (layer.id() if layer is not None else "")
            if layer_id:
                params["layer_id"] = layer_id
        return capability_id, params

    def _confirm_capability(self, capability_id, summary=""):
        """Ask before running a capability the registry marks consequential.

        The registry decides this, not the model and not the server. A compose
        response that simply omits `requires_confirmation` must not be able to
        make `field.calculate@1` write a column into the user's own data
        silently - that write cannot be undone from here.
        """
        capability = get_capability(capability_id)
        question = summary or (capability.summary if capability is not None else capability_id)
        yes = enum_member(QMessageBox, "StandardButton", "Yes")
        no = enum_member(QMessageBox, "StandardButton", "No")
        answer = QMessageBox.question(
            self.iface.mainWindow(),
            "Confirm Nivo action",
            "{}\n\nThis changes your data and cannot be undone from Mapdex. Run it?".format(question),
            yes | no,
            no,
        )
        return answer == yes

    def _plugin_capability_handlers(self):
        """Capabilities the QGIS runtime cannot own because they need the plugin.

        `qgis_runtime` binds the effects that need only the project and the
        canvas. A basemap layer and a server-supplied extent need this plugin's
        own viewport resolution, so they are chained onto the runtime table
        rather than duplicated inside it.
        """
        handlers = {
            "map.basemap@1": self._add_osm_basemap,
            "map.zoom_extent@1": self._apply_server_extent,
            # Processing needs this plugin's async task runner and its output
            # loading, so it cannot live in the runtime. It was reachable only
            # through the legacy `qgis:processing_operation@1` id, which left
            # the canonical capability refusing an operation its own legacy
            # spelling performed.
            "processing.run@1": self._run_processing_capability,
            "processing.discover@1": self._discover_processing,
            # The named operations. Each closes over its own operation name and
            # reuses the one runner, so there is a single place that decides
            # how a Processing algorithm is resolved, validated and loaded.
            #
            # Written out rather than comprehended from
            # NAMED_GEOPROCESSING_OPERATIONS: the parity test reads this table
            # STATICALLY, and a key it cannot read is a binding hidden from the
            # guard that exists to check it.
            "geoprocessing.buffer@1": self._named_processing_capability("buffer"),
            "geoprocessing.clip@1": self._named_processing_capability("clip"),
            "geoprocessing.intersection@1": self._named_processing_capability("intersection"),
            "geoprocessing.union@1": self._named_processing_capability("union"),
            "geoprocessing.difference@1": self._named_processing_capability("difference"),
            "geoprocessing.dissolve@1": self._named_processing_capability("dissolve"),
            "geoprocessing.merge@1": self._named_processing_capability("merge"),
            "geoprocessing.centroid@1": self._named_processing_capability("centroid"),
            "geoprocessing.convex_hull@1": self._named_processing_capability("convex_hull"),
            "geoprocessing.reproject@1": self._named_processing_capability("reproject"),
            "geoprocessing.spatial_join@1": self._named_processing_capability("spatial_join"),
            "geoprocessing.simplify@1": self._named_processing_capability("simplify"),
            "geoprocessing.repair@1": self._named_processing_capability("repair"),
            "geoprocessing.validate@1": self._named_processing_capability("validate"),
            "geoprocessing.split@1": self._named_processing_capability("split"),
            "geoprocessing.zonal_statistics@1": self._named_processing_capability("zonal_statistics"),
            # Terrain, through the same Processing task. Three of them refuse on
            # a grid measured in degrees before the algorithm is reached.
            "terrain.slope@1": self._named_processing_capability("slope"),
            "terrain.aspect@1": self._named_processing_capability("aspect"),
            "terrain.hillshade@1": self._named_processing_capability("hillshade"),
            "terrain.ruggedness@1": self._named_processing_capability("ruggedness"),
            "terrain.roughness@1": self._named_processing_capability("roughness"),
            "terrain.contours@1": self._named_processing_capability("contours"),
            "terrain.flow_accumulation@1": self._named_processing_capability("flow_accumulation"),
            "terrain.watershed@1": self._named_processing_capability("watershed"),
            "terrain.viewshed@1": self._named_processing_capability("viewshed"),
            # Both read the project's runs through this plugin's API client, so
            # neither can live in the runtime, which has no session and no
            # project. Until they were bound, seeing a job list or opening a
            # review meant leaving QGIS for a browser and finding the run again
            # by hand.
            "mapdex.jobs@1": self._list_mapdex_runs,
            "mapdex.open_review@1": self._open_mapdex_review,
            # The shape a person drew on the canvas becoming a layer. It needs
            # the project, the active-layer change and the assistant context
            # refresh, so it belongs on this side rather than in the runtime.
            "draw.geometry@1": self._draw_geometry_capability,
        }
        # The declaration and the table must not drift: an id advertised here
        # and missing from the table is the "Nivo prepared an action" and a
        # canvas that never moves failure this whole surface exists to avoid.
        #
        # A raise rather than an assert, because assert statements are removed
        # entirely under `python -O`. QGIS does not run optimised today, but an
        # invariant that quietly stops being checked depending on how the host
        # was started is not an invariant.
        declared = set(PLUGIN_BOUND_CAPABILITIES)
        bound = set(handlers)
        if bound != declared:
            raise RuntimeError(
                "the plugin-bound declaration and its handler table disagree: "
                "declared-not-bound {}, bound-not-declared {}".format(
                    sorted(declared - bound), sorted(bound - declared)
                )
            )
        return handlers

    def _named_processing_capability(self, operation):
        """A handler for one named operation.

        The capability id IS the operation, so the caller never sends one and
        cannot send a different one: `geoprocessing.buffer@1` can only buffer.
        That is the difference from the generic bridge, which takes the
        operation as an argument and therefore has to ask before every run.
        """

        def run(params):
            request = dict(params)
            request["operation"] = operation
            # `other_layer_id` is what the capability declares, because that is
            # the name every other two-layer capability on this surface uses;
            # `target_layer` is what the Processing request carries. Translating
            # here keeps one vocabulary facing the user and one facing QGIS.
            if request.get("other_layer_id") and not request.get("target_layer"):
                request["target_layer"] = request["other_layer_id"]
            return self._run_processing_capability(request)

        return run

    def _run_processing_capability(self, params):
        """Start a validated Processing operation.

        Confirmation already happened: the registry marks this consequential and
        `_run_capability` asks before it calls anything here. Asking twice for
        one action buys no information and costs the user a step.
        """
        operation = str(params.get("operation") or "")
        # A slope, aspect or hillshade on a grid measured in degrees is a ratio
        # of metres to degrees, which is not a slope. QGIS computes it anyway
        # and says nothing: a 10% grade reads as 89.99 degrees, measured in the
        # Workspace's own terrain tests. Refused here, before the algorithm, and
        # the refusal names the reprojection this same assistant can perform.
        if operation in UNIT_SENSITIVE_TERRAIN:
            layer = self._nivo_layer_for_action(params.get("layer_id") or "")
            crs = layer.crs() if layer is not None else None
            if crs is not None and crs.isValid() and crs.isGeographic():
                raise CapabilityError(
                    describe_geographic_terrain_refusal(operation, crs.authid()))
        action = {
            "target": params.get("layer_id") or "",
            "params": dict(params),
            "summary": "Run the {}".format(operation_label(operation)),
        }
        self._run_processing_operation(action)
        # Deliberately not a result. The algorithm runs as a QGIS task and
        # reports when it finishes; claiming an outcome here would describe work
        # that has not happened yet.
        return {"kind": "processing_started", "operation": operation}

    def _discover_processing(self, params):
        """Which of the allowlisted operations this QGIS can actually run.

        Answers from the live registry rather than from the catalog, because an
        operation whose algorithm is not installed is not available however
        confidently the allowlist names it.
        """
        available = []
        for operation in PROCESSING_OPERATION_CATALOG:
            _identifier, algorithm = resolve_processing_algorithm(
                QgsApplication.processingRegistry(), operation
            )
            if algorithm is not None:
                available.append({"operation": operation, "label": operation_label(operation)})
        return {
            "kind": "processing_catalog",
            "objective": str(params.get("objective") or ""),
            "available": available,
            "unavailable": len(PROCESSING_OPERATION_CATALOG) - len(available),
        }

    def _require_mapdex_session(self):
        """The project the server-side capabilities act on, or a refusal.

        Refusing here names the missing precondition. Calling `/v1/runs` without
        a session answers 401, which reaches the user as an HTTP failure for a
        question whose real answer is "connect first".
        """
        if not self.api.token:
            raise CapabilityError("connect to Mapdex first - these runs live in your workspace")
        project_id = self._active_project_id()
        if not project_id:
            raise CapabilityError("choose a Mapdex project first")
        return project_id

    def _list_mapdex_runs(self, params):
        """Start the run listing. The answer arrives in the transcript.

        `/v1/runs` is a network round trip and this executes on the Qt main
        thread, so the list is fetched as a task exactly the way Processing is
        run. Returning a summary here would be a listing of runs nobody has
        fetched yet.
        """
        project_id = self._require_mapdex_session()
        state = str(params.get("state") or "all")
        self._task(
            "List Mapdex runs",
            lambda: self.api.runs(project_id),
            lambda exception, payload: self._mapdex_runs_listed(state, exception, payload),
            busy=False,
        )
        return {"kind": "jobs_requested", "state": state}

    @guarded
    def _mapdex_runs_listed(self, state, exception, payload):
        if exception:
            self._nivo_report("Nivo could not read the run list: {}".format(describe_exception(exception)))
            return
        self._nivo_report(
            describe_capability_result(
                "", with_failure_guidance(summarize_runs(payload, state))
            )
        )

    def _open_mapdex_review(self, params):
        """Start resolving which run to review, then open it in the browser.

        The run has to be fetched even when its id was supplied: a review link
        is built from the run's SOURCE file, and the id alone cannot produce
        one. Opening `/review/<run id>` resolves no run, so the Studio closes
        the cockpit and returns the reviewer to the project map.
        """
        project_id = self._require_mapdex_session()
        run_id = str(params.get("run_id") or "").strip()
        self._task(
            "Open Mapdex review",
            lambda: self.api.runs(project_id),
            lambda exception, payload: self._mapdex_review_resolved(
                project_id, run_id, exception, payload
            ),
            busy=False,
        )
        return {"kind": "review_requested", "run_id": run_id}

    @guarded
    def _mapdex_review_resolved(self, project_id, run_id, exception, payload):
        if exception:
            self._nivo_report("Nivo could not reach the run list: {}".format(describe_exception(exception)))
            return
        run = select_review_run(payload, run_id)
        if run is None:
            # Two different absences, and the user can act on each: a named run
            # that is not in this project, or a project with nothing waiting.
            self._nivo_report(
                "Mapdex has no run {} in this project.".format(run_id) if run_id
                else "Nothing in this project is waiting for review."
            )
            return
        locale = QLocale.system().name().split("_")[0]
        prefix = "" if locale == "en" else "/{}".format(locale)
        path = review_workspace_path(project_id, run)
        QDesktopServices.openUrl(QUrl("{}{}{}".format(self.web_base, prefix, path)))
        self._nivo_report(describe_capability_result("", {
            "kind": "review_opened",
            "run_id": str(run.get("id") or ""),
            "state": run_state(run),
        }))

    def _nivo_report(self, line):
        """Put a line from a finished background task into the transcript.

        The turn that started the task already ended, so this also closes the
        state machine: leaving it in `executing` wedges every later turn.
        """
        self._say(line)
        self._set_status(line)
        self._nivo_state = transition(self._nivo_state, "done")

    def _add_osm_basemap(self, params):
        provider = str(params.get("provider") or "osm")
        if provider != "osm":
            raise CapabilityError("{} is not a basemap this build can add".format(provider))
        uri = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19"
        basemap = QgsRasterLayer(uri, "OpenStreetMap", "wms")
        if not basemap.isValid():
            raise CapabilityError("QGIS could not create the OpenStreetMap XYZ layer")
        QgsProject.instance().addMapLayer(basemap)
        return {"kind": "basemap_added", "name": basemap.name()}

    def _apply_server_extent(self, params):
        if not self._zoom_to_server_extent(params):
            raise CapabilityError("that location could not be placed on the current map")
        return {"kind": "zoomed_to_extent"}

    def _run_add_features(self, action):
        return bool(self._add_features(action.get("params") or {}, action.get("id") or ""))

    def _run_create_layer(self, action):
        return bool(self._create_scratch_layer(action.get("params") or {}))

    def _run_open_processing(self, _action):
        self.iface.showProcessingAlgorithmDialog("", {})
        return True

    def _run_next_extent(self, _action):
        self.iface.mapCanvas().zoomToNextExtent()
        return True

    def _capability_executor(self):
        """The executor table, built on first use.

        Built lazily because it binds to the live project, and a plugin that
        constructs it at load time fails to load at all when anything in that
        chain raises. The plugin's own handlers are chained in front of the
        runtime table so both halves answer to one `execute(request)` call.
        """
        executor = getattr(self, "_nivo_executor", None)
        if executor is None:
            from qgis.core import QgsProject

            runtime = build_executor(QGISRuntime(self.iface, QgsProject.instance(), self._set_status))
            local = self._plugin_capability_handlers()

            def execute(request):
                handler = local.get(str(request.get("capability") or ""))
                if handler is not None:
                    return handler(dict(request.get("params") or {}))
                return runtime(request)

            execute.capabilities = frozenset(runtime.capabilities) | frozenset(local)
            self._nivo_executor = execute
            executor = execute
        return executor

    def _run_capability(self, capability_id, params, summary=""):
        """Validate a capability request and run it through the executor table.

        Every failure becomes a status message. This runs inside QGIS, where an
        escaping exception is not a stack trace in a log but a broken host
        application, which is why the plugin grew an error boundary in the first
        place. Returns True when the capability actually ran.
        """
        try:
            request = validate_request(capability_id, params)
        except CapabilityError as error:
            return self._capability_refused(error)
        if request.get("requires_confirmation") and not self._confirm_capability(capability_id, summary):
            self._set_status("Nivo did not run {}.".format(summary or capability_id))
            self._nivo_state = transition(self._nivo_state, "done")
            return False
        try:
            result = self._capability_executor()(request)
        except CapabilityError as error:
            return self._capability_refused(error)
        except RuntimeUnavailable as error:
            # A missing precondition ("nothing is selected on that layer") is a
            # refusal the user can act on, not a malfunction.
            return self._capability_refused(error)
        except Exception as error:  # noqa: BLE001 - the host must survive anything
            self._set_status("Nivo failed to run {}: {}".format(capability_id, describe_exception(error)))
            self._nivo_state = transition(self._nivo_state, "error")
            return False
        described = self._describe_capability_result(summary or capability_id, result)
        self._say(described)
        self._report_action_result(capability_id, True, summary=described, result=result)
        self.iface.mapCanvas().refresh()
        return True

    def _report_action_result(self, capability_id, ok, summary="", error="", result=None):
        """Record what an action did, for the continuation to report.

        Kept per turn rather than per session: a new question starts a new
        objective, and carrying the previous one's outcomes into it would tell
        the server work had just happened that had not.
        """
        if not hasattr(self, "_nivo_action_results"):
            self._nivo_action_results = []
        self._nivo_action_results.append(action_result(
            capability_id, ok, summary=summary, error=str(error or ""),
            result=result if isinstance(result, dict) else None))

    def _capability_refused(self, error):
        """A refusal is a finished turn, not a wedged one.

        The turn state machine has no transition out of `executing` except done
        or error, so returning early without one leaves every later turn stuck
        in `executing`.
        """
        self._set_status("Nivo could not run that: {}".format(error))
        self._nivo_state = transition(self._nivo_state, "done")
        return False

    @staticmethod
    def _describe_capability_result(summary, result):
        """One line for the transcript, stating what the capability measured."""
        return describe_capability_result(summary, result)

    GEOMETRY_TYPES = ("point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon")

    @guarded
    def _add_features(self, params, seed=""):
        """Place real features on a layer, creating one only if needed.

        Asking for points used to resolve to "create an empty layer", which is
        worse than refusing: the layer appeared, so the request looked handled.
        """
        geometry = str(params.get("geometry") or "point").strip().lower()
        if geometry not in self.GEOMETRY_TYPES:
            geometry = "point"
        if not can_place(geometry):
            # Refused BEFORE a layer exists. Honouring the requested type for the
            # layer while always building point geometry made QGIS reject every
            # feature and left an empty polygon layer behind - a request that
            # looked answered, plus a raw provider error in the message bar.
            self._say(describe_unplaceable_geometry(geometry))
            self._set_status("Nivo did not place features.")
            self._nivo_state = transition(self._nivo_state, "done")
            return False
        layer = self._layer_for_features(geometry, params)
        if layer is None:
            return False
        target_crs = layer.crs()

        if str(params.get("area") or "") == "viewport":
            # The user meant what they can see. The canvas extent is already in
            # the map CRS, so this needs no geocoding and no guessing.
            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            positions = scatter_in_rectangle(
                [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
                params.get("count") or 1,
                seed,
            )
            source_crs = canvas.mapSettings().destinationCrs()
            where = "the current view"
        else:
            plan = plan_points(params, seed)
            if not plan.get("points"):
                self._set_status("Nivo had no area to place features in.")
                return False
            positions = plan["points"]
            source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            where = str(params.get("place") or "")

        transform = None
        if source_crs.isValid() and target_crs.isValid() and source_crs != target_crs:
            try:
                transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
            except Exception:
                self._set_status("Nivo could not place those features in the layer's CRS.")
                return False

        features = []
        for x, y in positions:
            point = QgsPointXY(float(x), float(y))
            if transform is not None:
                try:
                    point = transform.transform(point)
                except QgsCsException:
                    continue
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            features.append(feature)
        if not features:
            self._set_status("Nivo could not place those features in the layer's CRS.")
            return False

        ok, _added = layer.dataProvider().addFeatures(features)
        if not ok:
            self._set_status("QGIS rejected the new features.")
            self._nivo_state = transition(self._nivo_state, "error")
            return False
        layer.updateExtents()
        layer.triggerRepaint()
        self.iface.setActiveLayer(layer)
        self._zoom_to_layers([layer])
        self.iface.mapCanvas().refresh()
        self._say(describe_placement(len(features), len(positions), where))
        self._refresh_nivo_context()
        self._set_status("Nivo added {} feature(s) to '{}'.".format(len(features), layer.name()))
        return True

    def _layer_for_features(self, geometry, params):
        """Reuse a compatible editable layer, or make one for the features."""
        active = self._active_qgis_layer()
        if isinstance(active, QgsVectorLayer) and active.isValid():
            # Only an in-memory scratch layer is written to without asking; a
            # file or database layer is the user's data, not ours to append to.
            if active.dataProvider().name() == "memory" and self._geometry_matches(active, geometry):
                return active
        created = self._create_scratch_layer({
            "geometry": geometry,
            "crs": params.get("layer_crs") or "",
        })
        if not created:
            return None
        return self._active_qgis_layer()

    def _geometry_matches(self, layer, geometry):
        try:
            from qgis.core import QgsWkbTypes

            from .qt_compat import enum_member

            # PyQt6 requires the scoped ``GeometryType`` path; PyQt5 accepts
            # the flat form.
            point = enum_member(QgsWkbTypes, "GeometryType", "PointGeometry")
            line = enum_member(QgsWkbTypes, "GeometryType", "LineGeometry")
            polygon = enum_member(QgsWkbTypes, "GeometryType", "PolygonGeometry")
            wanted = {
                "point": point,
                "multipoint": point,
                "linestring": line,
                "multilinestring": line,
                "polygon": polygon,
                "multipolygon": polygon,
            }.get(geometry)
            return wanted is not None and layer.geometryType() == wanted
        except Exception:
            return False

    @guarded
    def _create_scratch_layer(self, params):
        """Create a new empty editable layer and make it the active one.

        A scratch (memory) layer is the right default: it appears immediately,
        needs no path or format decision, and the user can save it wherever they
        want afterwards. Writing a file without being asked would put data on
        their disk that they never chose a location for.
        """
        geometry = str(params.get("geometry") or "").strip().lower()
        if geometry not in self.GEOMETRY_TYPES:
            # The one decision that cannot be defaulted safely: a point layer is
            # useless to someone who wanted to draw parcels. Ask with a picker,
            # not a chat message - a question in the transcript has nowhere to
            # go, because the user's answer starts a fresh turn where "polygon"
            # is a bare word carrying no intent.
            geometry = self._ask_geometry_type()
            if not geometry:
                self._set_status("Nivo did not create a layer.")
                self._nivo_state = transition(self._nivo_state, "done")
                return False
        crs = str(params.get("crs") or "").strip()
        if not crs:
            crs = self.iface.mapCanvas().mapSettings().destinationCrs().authid()
        name = str(params.get("name") or "").strip()[:120] or self._unique_layer_name(geometry)
        uri = "{}?crs={}&index=yes".format(geometry, crs or "EPSG:4326")
        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            self._set_status("QGIS could not create that layer.")
            self._nivo_state = transition(self._nivo_state, "error")
            return False
        QgsProject.instance().addMapLayer(layer)
        self.iface.setActiveLayer(layer)
        self.iface.mapCanvas().refresh()
        self._say("Created '{}' ({}, {}). It is the active layer - toggle editing to start drawing.".format(
            name, geometry, crs or "EPSG:4326"))
        self._refresh_nivo_context()
        self._set_status("Nivo created the layer '{}'.".format(name))
        return True

    @guarded
    def _ask_geometry_type(self):
        """Offer the geometry choice as a picker; "" when the user cancels."""
        labels = [label for label, _geometry in GEOMETRY_CHOICES]
        choice, accepted = QInputDialog.getItem(
            self.iface.mainWindow(),
            "New layer",
            "What kind of layer should Nivo create?",
            labels,
            2,      # Polygon: the most common answer when drawing areas
            False,  # not editable - only the closed set may be chosen
        )
        if not accepted:
            return ""
        return geometry_from_choice(choice)

    def _unique_layer_name(self, geometry):
        existing = {layer.name() for layer in QgsProject.instance().mapLayers().values()}
        base = "New {} layer".format(geometry)
        if base not in existing:
            return base
        for index in range(2, 100):
            candidate = "{} {}".format(base, index)
            if candidate not in existing:
                return candidate
        return base

    @guarded
    def _zoom_to_server_extent(self, params):
        """Move the canvas to a server-supplied extent, converting its CRS.

        The server geocodes in WGS84; this canvas is usually EPSG:3857 or a
        national grid. Setting those degrees directly is what put "zoom to
        Istanbul" a few metres from null island instead of on Istanbul.
        """
        resolved = resolve_extent(params)
        if resolved is None:
            self._set_status("Nivo rejected an invalid map extent.")
            return False
        canvas = self.iface.mapCanvas()
        target = canvas.mapSettings().destinationCrs()
        source = QgsCoordinateReferenceSystem(resolved["crs"])
        if not source.isValid():
            self._set_status("Nivo rejected an extent with an unknown CRS.")
            return False
        rectangle = QgsRectangle(*resolved["bbox"])
        if target.isValid() and source != target:
            try:
                rectangle = QgsCoordinateTransform(
                    source, target, QgsProject.instance()
                ).transformBoundingBox(rectangle)
            except QgsCsException:
                self._set_status("Nivo could not place that location in the current map CRS.")
                return False
        if rectangle.isEmpty():
            self._set_status("Nivo received an empty map extent.")
            return False
        canvas.setExtent(rectangle)
        canvas.refresh()
        return True

    @guarded
    def _confirm_nivo_action(self, action):
        action_id = action.get("id")
        if not action_id or action_id in self._executed_nivo_actions:
            self._set_status("Nivo ignored a duplicate or malformed confirmation.")
            return
        operation = action.get("params", {}).get("operation")
        message = "{}\n\nTarget layer: {}\nOperation: {}".format(
            action.get("summary") or "Nivo prepared a QGIS Processing operation.",
            action.get("target") or "active layer",
            operation_label(operation),
        )
        answer = QMessageBox.question(
            self.iface.mainWindow(),
            "Confirm Nivo action",
            message,
            enum_member(QMessageBox, "StandardButton", "Yes") | enum_member(QMessageBox, "StandardButton", "No"),
            enum_member(QMessageBox, "StandardButton", "No"),
        )
        if answer != enum_member(QMessageBox, "StandardButton", "Yes"):
            self._set_status("Nivo action cancelled.")
            self._nivo_state = transition(self._nivo_state, "done")
            return
        self._executed_nivo_actions.add(action_id)
        self._nivo_state = transition(self._nivo_state, "apply")
        self._run_processing_operation(action)

    @guarded
    def _run_processing_operation(self, action):
        target = action.get("target")
        layer = self._nivo_layer_for_action(target)
        if layer is None or not layer.isValid():
            self._set_status("Nivo did not run Processing because the target layer is no longer available.")
            self._nivo_state = transition(self._nivo_state, "error")
            return
        operation = action.get("params", {}).get("operation")
        _algorithm_id, algorithm = resolve_processing_algorithm(QgsApplication.processingRegistry(), operation)
        if algorithm is None:
            # Names the missing provider and the route round it. "This
            # installation has no algorithm" named nothing and offered no next
            # move, which is the same failure as an unexplained error code.
            self._set_status(describe_missing_algorithm(operation))
            self._nivo_state = transition(self._nivo_state, "error")
            return
        source_name = layer.name() if hasattr(layer, "name") else ""
        count = layer.featureCount() if hasattr(layer, "featureCount") else None
        if count == 0:
            # Only an exact zero counts as empty: several providers answer -1 for
            # "unknown". Running anyway SUCCEEDS and writes an empty layer, which
            # is how "buffer yap" ended with a new layer and no buffer in it.
            self._say(describe_empty_input(operation, source_name))
            self._set_status("Nivo did not run the {}.".format(operation_label(operation)))
            self._nivo_state = transition(self._nivo_state, "done")
            return
        try:
            parameters = build_algorithm_parameters(algorithm, operation, layer, action.get("params", {}))
        except Exception as exc:
            self._show_error("Nivo Processing validation failed", exc)
            self._nivo_state = transition(self._nivo_state, "error")
            return
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()
        task = QgsProcessingAlgRunnerTask(algorithm, parameters, context, feedback)
        task._context = context
        task._feedback = feedback
        task._algorithm = algorithm
        self._tasks.append(task)

        @guarded
        def completed(successful, results):
            if task in self._tasks:
                self._tasks.remove(task)
            if self.dock is None:
                return
            if not successful:
                self._set_status("Nivo Processing task failed or was cancelled.")
                self._nivo_state = transition(self._nivo_state, "error")
                return
            output_layer = None
            if isinstance(results, dict):
                # From the shared order rather than a copy of it: this list and
                # the one the parameters are built from have to name the same
                # keys, or an output the algorithm was asked to write is never
                # looked for.
                for key in PROCESSING_OUTPUT_ORDER:
                    if key in results:
                        val = results[key]
                        if hasattr(val, "isValid") and val.isValid():
                            output_layer = val
                            break
                        if isinstance(val, str) and hasattr(context, "takeResultLayer"):
                            try:
                                output_layer = context.takeResultLayer(val)
                                if output_layer is not None and output_layer.isValid():
                                    break
                            except Exception as exc:  # noqa: BLE001 - Processing may raise anything
                                # The second route below still resolves most
                                # outputs. Recorded because the visible symptom
                                # of losing both is "the algorithm ran and
                                # nothing appeared", with no other trace.
                                log_debug("taking the Processing result layer for {}".format(key), exc)
                        if isinstance(val, str):
                            try:
                                from qgis.core import QgsProcessingUtils
                                output_layer = QgsProcessingUtils.mapLayerFromString(val, context, True)
                                if output_layer is not None and output_layer.isValid():
                                    break
                            except Exception as exc:  # noqa: BLE001 - Processing may raise anything
                                log_debug("resolving the Processing output {}".format(key), exc)
            output_name = ""
            produced = None
            if output_layer is not None and output_layer.isValid():
                QgsProject.instance().addMapLayer(output_layer)
                self.iface.setActiveLayer(output_layer)
                output_name = output_layer.name()
                counter = getattr(output_layer, "featureCount", None)
                if counter is not None:
                    try:
                        produced = int(counter())
                    except Exception:
                        produced = None
                    if produced is not None and produced < 0:
                        produced = None
            # An algorithm that finished is not the same as a result the user can
            # see: "completed" over an empty or missing output is a claim the map
            # contradicts. The algorithm id stays out of this line entirely.
            self._say(describe_processing_outcome(
                operation, output_name, produced, source_name))
            self.iface.mapCanvas().refresh()
            self._nivo_state = transition(self._nivo_state, "done")
            if output_name and produced != 0:
                self._set_status("Nivo finished the {}.".format(operation_label(operation)))
            else:
                self._set_status("The {} finished without producing anything.".format(
                    operation_label(operation)))

        task.executed.connect(completed)
        QgsApplication.taskManager().addTask(task)
        self._set_status("Nivo is running the {} on '{}'.".format(
            operation_label(operation), source_name))

    @guarded
    def _workflow_changed(self, _index):
        kind = self.workflow_box.currentData()
        current = self.input_box.currentData()
        self.input_box.blockSignals(True)
        self.input_box.clear()
        self.input_box.addItem("Select source…", "")
        self.input_box.addItem("Active QGIS layer", "active_layer")
        self.input_box.addItem("Choose a file…", "file")
        target = self.input_box.findData(current)
        self.input_box.setCurrentIndex(target if target >= 0 else 0)
        self.input_box.blockSignals(False)
        self.selected_paths = []
        self._source_label = ""
        self.source_summary.setText("No source selected")
        if kind == BatchKind.VALIDATE_DELIVER:
            self._set_status("Use the active vector layer or choose one file.")
        else:
            self._set_status("Use the active raster layer or choose one file.")

    @guarded
    def connect(self, *args):
        if not self._apply_connection_settings_from_fields():
            return
        self._set_status("Starting browser connection via {url}…".format(url=self.api.base_url))
        self._task("Mapdex device authorization", self.api.authorize_device, self._authorization_created)

    @guarded
    def disconnect(self, *args):
        self.poll_timer.stop()
        self.progress_timer.stop()
        # Disconnecting while the browser approval is still outstanding leaves a
        # pending authorization on the server that this plugin has stopped
        # polling for. Cancel it, so a code the user abandoned cannot be
        # approved afterwards by anyone who saw it on screen.
        #
        # Best effort: the session is ending either way, and a failure here must
        # not leave the plugin half-disconnected with a token it has stopped
        # using. Once a token has been issued the authorization is already
        # `consumed` and there is nothing left to cancel, so this only matters
        # for the pending case -- which is exactly when `device_code` is set.
        if self.device_code:
            try:
                self.api.revoke_device(self.device_code)
            except Exception as exc:  # noqa: BLE001 - disconnect must always complete
                # Recorded rather than swallowed: the disconnect proceeds either
                # way, but a code that could not be cancelled is worth knowing
                # about, because it stays approvable until it expires.
                QgsMessageLog.logMessage(
                    "Could not cancel the pending device authorization: {}".format(exc),
                    "Mapdex",
                    enum_member(Qgis, "MessageLevel", "Warning"),
                )
        # Disconnecting clears the session, it does not assign a password.
        self.api.token = ""  # nosec B105
        self.device_code = ""
        self.batch_id = ""
        self.project_id = ""
        self._last_batch = None
        self.imported_layer_ids.clear()
        settings = QSettings()
        if self.token_store is not None:
            self.token_store.clear()
        else:
            settings.remove(LEGACY_TOKEN_SETTING)
        # Everything that belonged to the session, not only its token. The
        # recent-task list used to survive, so the Jobs page kept offering
        # Resume on batches the next token cannot read - and after connecting a
        # different account, on another workspace's tasks. The provider key is
        # deliberately not in this list: it is the user's own credential for
        # their own account elsewhere.
        for key in panel_state.SESSION_SCOPED_SETTINGS:
            settings.remove(key)
        # The conversation belonged to the session that just ended. Keeping its
        # id would send the next connection's first question into a thread the
        # new token may not be able to see.
        self._adopt_conversation("", "")
        self._nivo_turns = []
        self._render_nivo_turns()
        self.project_box.clear()
        # The Task page's own selections are session state too: a project's
        # file left selected there reads as ready to submit when it is not.
        self.selected_paths = []
        if self.input_box is not None:
            self.input_box.blockSignals(True)
            self.input_box.setCurrentIndex(0)
            self.input_box.blockSignals(False)
        if self.source_summary is not None:
            self.source_summary.setText("No source selected")
        self._load_recent_tasks()
        # Reported before the refresh, so the panel and the sentence agree.
        # "Disconnected." full stop, above an assistant that keeps answering
        # from the user's own provider, is what read as a defect.
        self._set_status(panel_state.disconnect_notice(self.assistant_runtime()))
        self._refresh_ui()

    @guarded
    def _authorization_created(self, exception, response):
        if exception:
            self._show_error("Mapdex connection failed", exception)
            return
        if not isinstance(response, dict):
            self._show_error("Mapdex connection failed", RuntimeError("Empty authorization response"))
            return
        self.device_code = response.get("device_code") or ""
        user_code = response.get("user_code") or ""
        if not self.device_code or not user_code:
            self._show_error("Mapdex connection failed", RuntimeError("Authorization response missing codes"))
            return
        self._set_status(f"Approve code {user_code} in your browser, then return to QGIS.")
        verify = device_verification_url(
            response.get("verification_uri") or "", self.web_base, self.api.base_url
        )
        QDesktopServices.openUrl(QUrl(f"{verify}?code={user_code}"))
        interval = max(5, int(response.get("interval") or 5))
        self.poll_timer.start(interval * 1000)

    @guarded
    def _poll_token(self):
        if not self.device_code:
            return
        try:
            response = self.api.poll_device_token(self.device_code)
        except MapdexAPIError as exc:
            if exc.status in (400, 428):
                # Still pending user approval.
                return
            if exc.status == 429:
                delay = max(5, exc.retry_after or 30)
                self.poll_timer.setInterval(delay * 1000)
                self._set_status("Mapdex asked QGIS to slow down. Retrying in {} seconds…".format(delay))
                return
            self.poll_timer.stop()
            self._show_error("Mapdex connection failed", exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.poll_timer.stop()
            self._show_error("Mapdex connection failed", exc)
            return
        self.poll_timer.stop()
        token = (response or {}).get("access_token")
        if not token:
            self._show_error("Mapdex connection failed", RuntimeError("No access token returned"))
            return
        self.api.token = token
        self.project_id = str((response or {}).get("project_id") or self.project_id or "")
        settings = QSettings()
        persisted = self.token_store is not None and self.token_store.save(self.api.token)
        if self.project_id:
            settings.setValue("mapdex/project_id", self.project_id)
        if persisted:
            self._set_status("Connected securely to Mapdex.")
        else:
            self._set_status(
                "Connected for this QGIS session. Unlock the QGIS Authentication Database "
                "to keep the connection after restart."
            )
        self._refresh_ui()
        self._load_projects()

    def _load_projects(self):
        self._task("Load Mapdex projects", self.api.projects, self._projects_loaded)

    @guarded
    def _projects_loaded(self, exception, projects):
        if exception:
            self._show_error("Could not load projects", exception)
            return
        self.project_box.clear()
        items = projects if isinstance(projects, list) else []
        for project in items:
            if not isinstance(project, dict):
                continue
            name = project.get("name") or project.get("title") or "Project"
            project_id = project.get("id") or ""
            if project_id:
                self.project_box.addItem(str(name), project_id)
        if self.project_id:
            index = self.project_box.findData(self.project_id)
            if index >= 0:
                self.project_box.setCurrentIndex(index)
        if self.project_box.count() == 0:
            self._set_status(
                "Connected, but no projects were returned. "
                "If the API requires admin scope for /v1/projects, use the project_id from the token."
            )
        else:
            self._set_status("Connected. Choose a project and send work.")

    @guarded
    def run_input(self, *args):
        project_id = self._active_project_id()
        if not project_id:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Mapdex",
                "Select a Mapdex project first.",
            )
            return
        mode = self.input_box.currentData()
        if not mode:
            QMessageBox.information(
                self.iface.mainWindow(), "Mapdex", "Choose a source file first."
            )
            return
        temp_dir = tempfile.mkdtemp(prefix="mapdex-qgis-")
        paths = []
        try:
            if mode == "file":
                if not self.selected_paths:
                    self._choose_source(mode)
                if not self.selected_paths:
                    return
                paths = list(self.selected_paths)
            else:
                layer = self.iface.activeLayer()
                if layer is None or not layer.isValid():
                    QMessageBox.information(
                        self.iface.mainWindow(),
                        "Mapdex",
                        "Select a valid layer in the QGIS Layers panel first.",
                    )
                    return
                kind = self.workflow_box.currentData()
                if isinstance(layer, QgsVectorLayer):
                    if kind != BatchKind.VALIDATE_DELIVER:
                        raise RuntimeError(
                            "This workflow needs a raster image. Select an open raster layer "
                            "or choose an image file."
                        )
                    path = os.path.join(temp_dir, "active-layer.gpkg")
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    options.layerName = "active_layer"
                    result = QgsVectorFileWriter.writeAsVectorFormatV3(
                        layer, path, QgsProject.instance().transformContext(), options
                    )
                    if result[0] != enum_member(QgsVectorFileWriter, "WriterError", "NoError"):
                        raise RuntimeError("Could not export the active layer to GeoPackage.")
                elif isinstance(layer, QgsRasterLayer):
                    if kind == BatchKind.VALIDATE_DELIVER:
                        raise RuntimeError(
                            "Validate & deliver needs a vector layer. Select an open vector layer."
                        )
                    source = str(layer.source() or "").split("|", 1)[0]
                    if source.startswith("file:"):
                        source = QUrl(source).toLocalFile()
                    path = os.path.normpath(source)
                    if not os.path.isfile(path):
                        raise RuntimeError(
                            "The active raster is remote or has no local source file. "
                            "Save it locally first, then choose that file."
                        )
                else:
                    raise RuntimeError("The active QGIS layer type is not supported.")
                paths = [path]
        except Exception as exc:  # noqa: BLE001
            self._show_error("Could not prepare input", exc)
            return

        kind = self.workflow_box.currentData()
        report = inspect_paths(paths, str(kind or ""))
        self.source_summary.setText(report["summary"])
        if not report["valid"]:
            QMessageBox.warning(self.iface.mainWindow(), "Source is not compatible", report["error"])
            return
        self.imported_layer_ids.clear()
        self._announced_state = ""
        self._pending_is_batch = len(paths) > 1
        self._set_status("Uploading to Mapdex…")
        self.batch_id = "uploading"
        self._last_batch = {"status": "created", "counts": {"total": 1}}
        self._refresh_ui()
        self._task(
            "Send layer to Mapdex",
            lambda: self._upload_and_run(paths, project_id, kind),
            self._run_started,
        )

    def _upload_and_run(self, paths, project_id, kind):
        file_ids = []
        for path in paths:
            uploaded = self.api.upload_file(path, project_id)
            file_id = uploaded.get("id") or uploaded.get("source_file_id")
            if not file_id:
                raise RuntimeError("Upload succeeded but no file id was returned.")
            file_ids.append(file_id)
        return {"batch": self.api.start_batch(project_id, file_ids, kind), "file_ids": file_ids}

    @guarded
    def _run_started(self, exception, response):
        if exception:
            self.batch_id = ""
            self._last_batch = None
            self._show_error("Send to Mapdex failed", exception)
            return
        payload = response or {}
        batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else payload
        file_ids = payload.get("file_ids") or []
        self.batch_id = (batch or {}).get("id") or ""
        if not self.batch_id:
            self._show_error("Send to Mapdex failed", RuntimeError("No batch id returned"))
            return
        self.project_id = self._active_project_id()
        self._remember_task(
            self.batch_id,
            self.project_id,
            str(self.workflow_box.currentData() or ""),
            self._source_label or "QGIS source",
            file_ids[0] if file_ids else "",
        )
        QSettings().setValue("mapdex/project_id", self.project_id)
        self._set_status(
            "Task started. Mapdex is working in the background…"
        )
        self._refresh_ui()
        self.progress_timer.start(3000)

    @guarded
    def _offer_plan_run(self, response):
        """Ask whether to run the plan this turn proposed, and then run it.

        The desktop could not do this at all. A compose turn needing a Run came
        back as a description of the work with no way to start it, so a
        traverse, a resection, a geoid height or validating a layer was
        understood, planned, and then sat in the panel. See the `run a server
        plan` row in the capability matrix.

        Nothing here reads the plan. It travels to the server exactly as it
        arrived, with the hash that proves it: a client that rebuilds a plan
        from tool names runs something nobody approved.
        """
        offer = plan_offer(response)
        if offer is None:
            return
        if self.plan_run_id:
            # One plan at a time. Starting a second while the first is running
            # spends credits on work whose result the panel cannot attribute.
            self._say("There is already a plan running. I will offer this one when it finishes.")
            return
        try:
            project_id = self._require_mapdex_session()
        except CapabilityError as error:
            self._say(str(error))
            return

        yes = enum_member(QMessageBox, "StandardButton", "Yes")
        no = enum_member(QMessageBox, "StandardButton", "No")
        answer = QMessageBox.question(
            self.iface.mainWindow(), "Run this in Mapdex?",
            offer_prompt(offer), yes | no, no,
        )
        if answer != yes:
            self._say("Not run. The plan is still here if you change your mind.")
            return

        self._nivo_state = transition(self._nivo_state, "action")
        text = str((response or {}).get("text") or "")
        plan, plan_hash = offer["plan"], offer["plan_hash"]
        self._set_status("Running in Mapdex…")
        self._task(
            "Run Mapdex plan",
            lambda: self.api.create_run(project_id, text, plan, plan_hash),
            self._plan_run_created,
        )

    @guarded
    def _plan_run_created(self, exception, payload):
        # The request has been made, so the turn is executing whatever the
        # answer is. The closed state table has no edge from `action_ready` to
        # `error`, so reporting a failure without this step leaves the turn
        # sitting in `action_ready` while the panel says the run failed.
        self._nivo_state = transition(self._nivo_state, "execute")
        if exception is not None:
            self._nivo_state = transition(self._nivo_state, "error")
            self._say("Mapdex could not start that run: {}".format(describe_exception(exception)))
            self._set_status("The run did not start")
            return
        run_id = str((payload or {}).get("id") or "")
        if not run_id:
            # A created run with no id cannot be followed, and reporting it as
            # started would leave the user watching a run nobody can find.
            self._nivo_state = transition(self._nivo_state, "error")
            self._say("Mapdex accepted the plan but returned no run to follow.")
            return
        self.plan_run_id = run_id
        self._refresh_stop_button()
        self._say("Running it in Mapdex now. I will tell you when it finishes.")
        self.plan_timer.start(3000)

    @guarded
    def _cancel_plan_run(self):
        """Ask Mapdex to stop the run this panel started.

        Nothing is reported as stopped before the server says so. Announcing it
        locally and then discovering the run had already finished would tell
        somebody their work was thrown away when it was delivered.
        """
        run_id = self.plan_run_id
        if not run_id:
            return
        project_id = self._active_project_id()
        self._set_status("Asking Mapdex to stop the run…")
        self._task(
            "Cancel Mapdex run",
            lambda: self.api.cancel_run(project_id, run_id),
            self._plan_run_cancelled,
            busy=False,
        )

    @guarded
    def _plan_run_cancelled(self, exception, _payload):
        if exception is not None:
            # A run that reached a terminal state a moment before the request
            # answers 409. That is the ordinary race between pressing Stop and
            # the run finishing, not a fault, and the next poll reports the
            # real outcome, so nothing is said here beyond letting it land.
            self._set_status("The run had already finished; showing its result.")
            return
        # The poll owns the transition to terminal, so the state is not written
        # twice from two places.
        self._say("Cancelling that run in Mapdex.")

    @guarded
    def _poll_plan_run(self):
        if self.plan_run_pending or not self.plan_run_id:
            return
        self.plan_run_pending = True
        project_id = self._active_project_id()
        run_id = self.plan_run_id
        self._task(
            "Follow Mapdex run", lambda: self.api.run(run_id, project_id),
            self._plan_run_polled, busy=False,
        )

    @guarded
    def _plan_run_polled(self, exception, payload):
        self.plan_run_pending = False
        if exception is not None:
            # One failed poll is a network blip, not a failed run. Stopping
            # here would report a run as lost while it is still executing.
            return
        report = plan_run_report(payload)
        if not report["terminal"]:
            return
        self.plan_timer.stop()
        finished_run = self.plan_run_id
        self.plan_run_id = ""
        self._refresh_stop_button()
        self._say(report["message"])
        self._set_status("Ready")
        self._nivo_state = transition(
            self._nivo_state, "error" if report["state"] in {"failed", "cancelled"} else "done")
        if not report["layers"]:
            return
        # The result is the point. Announcing a finished run and leaving its
        # layers in the browser is the same silence this whole path removes.
        self._set_status("Bringing the result into QGIS…")
        self._task(
            "Import Mapdex results into QGIS",
            lambda: self._fetch_result_files({}, only_runs=(finished_run,)),
            self._results_imported,
        )

    @guarded
    def _poll_batch(self):
        if self.progress_pending or not self.batch_id or self._busy:
            return
        self.progress_pending = True
        project_id = self._active_project_id()
        probe_run = ""
        # Occasionally ask whether the run really started. Every poll would be
        # a second request every few seconds for a fact that changes once.
        if self._start_probe_countdown <= 0 and self._backend_started is not True:
            probe_run = self._active_run_id()
            self._start_probe_countdown = 5
        else:
            self._start_probe_countdown -= 1

        def work():
            detail = self.api.batch(project_id, self.batch_id)
            started = None
            if probe_run:
                try:
                    started = run_has_started(self.api.run(probe_run, project_id))
                except MapdexAPIError:
                    started = None
            return {"detail": detail, "started": started, "probed": bool(probe_run)}

        self._task("Refresh Mapdex batch", work, self._batch_updated, busy=False)

    def _note_backend_start(self, started) -> None:
        """Record whether the server has begun executing the active run."""
        if started is None:
            self._backend_started = None
            return
        if started:
            self._backend_started = True
            self._waiting_since = 0.0
            return
        if self._backend_started is not False:
            self._waiting_since = time.monotonic()
        self._backend_started = False

    def _waiting_seconds(self) -> int:
        if self._backend_started is not False or not self._waiting_since:
            return 0
        return int(max(0.0, time.monotonic() - self._waiting_since))

    def _active_run_id(self) -> str:
        """The run id of the first item that has not reached a terminal state."""
        for item in (self._last_batch or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "").lower()
            if state in {"succeeded", "needs_review", "failed", "cancelled"}:
                continue
            run_id = str(item.get("run_id") or "")
            if run_id:
                return run_id
        return ""

    @guarded
    def _batch_updated(self, exception, payload):
        self.progress_pending = False
        if exception:
            if isinstance(exception, MapdexAPIError) and exception.status == 429:
                delay = max(3, exception.retry_after or 15)
                self.progress_timer.setInterval(delay * 1000)
                self._set_status("Rate limit reached. Retrying task status in {} seconds…".format(delay))
                return
            self._show_error("Batch status failed", exception)
            return
        response = payload
        if isinstance(payload, dict) and "detail" in payload and "started" in payload:
            response = payload.get("detail")
            if payload.get("probed"):
                self._note_backend_start(payload.get("started"))
        self._last_batch = response
        state = batch_state(response)
        counts = (response or {}).get("counts") or {}
        ok = int(counts.get("succeeded", 0) or 0)
        review = int(counts.get("needs_review", 0) or 0)
        failed = int(counts.get("failed", 0) or 0)
        next_step = failure_next_step(batch_failure(response or {}).get("code"))
        if state in ("created", "queued", "pending"):
            message = "Task queued. Mapdex will start processing shortly."
        elif state == "running":
            message = "Processing in Mapdex…"
        elif failed and not ok and not review and next_step:
            # A refusal that names a next step is not a failure report. The scan
            # is fine and already uploaded; saying so and offering the route is
            # the whole difference between this and the generic sentence below,
            # which left the user holding a rejected upload.
            message = next_step["hint"]
            if self._announced_state != "needs_placement":
                self._announced_state = "needs_placement"
                self._announce(message, level=1, duration=10)
        elif failed and not ok and not review:
            message = first_batch_error(response or {}) or "Task failed. Retry it, or open Mapdex for details."
        elif failed:
            message = "Task finished with {} failed item(s). {} result(s) are ready.".format(failed, ok)
        elif review:
            message = "{} item(s) need review before they can be added to QGIS.".format(review)
        else:
            message = "Task complete. {} result(s) are ready to add to QGIS.".format(ok)
        self._set_status(message)
        self._refresh_ui()
        if batch_is_terminal(response or {}):
            if succeeded_run_ids(response or {}):
                self.progress_timer.stop()
                self._task(
                    "Import Mapdex results into QGIS",
                    lambda: self._fetch_result_files(response),
                    self._results_imported,
                )
            elif review_run_ids(response or {}):
                # Review is a browser round trip, but the user should not have
                # to come back and press Resume to find out it finished: keep a
                # slow watch so the approved layer lands in QGIS on its own.
                self.progress_timer.start(REVIEW_POLL_MS)
                if self._announced_state != "review":
                    self._announced_state = "review"
                    self._announce(
                        "Mapdex needs your review in the browser. QGIS adds the layer "
                        "automatically once you approve it.",
                        level=1,
                    )
                self._set_status(
                    "Waiting for your review in the browser. Approve the result there and "
                    "it lands in QGIS by itself."
                )
            else:
                self.progress_timer.stop()

    @guarded
    def import_results(self, *args):
        """Pull the result into QGIS, saying plainly when it is still a draft."""
        if not self.batch_id:
            return
        project_id = self._active_project_id()
        detail = self._last_batch or {}
        approved = bool(succeeded_run_ids(detail))
        pending_review = bool(review_run_ids(detail))
        include_review = False

        if not approved and pending_review:
            yes = enum_member(QMessageBox, "StandardButton", "Yes")
            no = enum_member(QMessageBox, "StandardButton", "No")
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Mapdex",
                "Review is not finished in Mapdex yet.\n\n"
                "Bring the draft into QGIS anyway? It arrives split into review "
                "layers (invalid geometry, needs review, clean) so you can work "
                "through it here. Nothing is approved by importing it.",
                yes | no,
                no,
            )
            if answer != yes:
                self._set_status(
                    "Left in Mapdex for review. Open Review, approve it there, and the "
                    "approved layer arrives here on its own."
                )
                return
            include_review = True

        def work():
            fresh = self._last_batch or self.api.batch(project_id, self.batch_id)
            self._last_batch = fresh
            return self._fetch_result_files(fresh, include_review=include_review)

        self._set_status("Fetching the draft from Mapdex…" if include_review else "Fetching Mapdex results…")
        self._task("Import Mapdex results into QGIS", work, self._results_imported)

    def _draft_review_layers(self, raw: bytes, layer: dict, result_dir: str, safe_id: str):
        """Write one GeoJSON per validator verdict so review can happen in QGIS.

        The extractor reports `quality_status: uncalibrated`, so there is no
        calibrated confidence to grade features by. The split therefore uses
        what the validators did report: invalid geometry, review required, and
        the rest. A draft is never recorded as imported, so the approved layer
        can still arrive later.
        """
        try:
            geojson = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            geojson = {}
        buckets = split_review_buckets(geojson)
        if not buckets:
            return []
        prepared = []
        for bucket in buckets:
            path = os.path.join(result_dir, "{}-{}.geojson".format(safe_id, bucket["key"]))
            payload = {"type": "FeatureCollection", "features": bucket["features"]}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            prepared.append(
                {
                    "path": path,
                    "name": "Mapdex · {} · {} ({}) · draft".format(
                        layer["name"], bucket["label"], len(bucket["features"])
                    ),
                    # Drafts are not the approved result: keep them out of the
                    # imported set so approval still delivers the real layer.
                    "layer_id": "",
                    "kind": "vector",
                }
            )
        return prepared

    def _fetch_result_files(self, detail: dict, include_review: bool = False,
                            only_runs: tuple[str, ...] = ()):
        """Bring the results of a batch, or of named runs, into QGIS.

        `only_runs` is the second entry point: a plan the panel ran directly is
        one run and belongs to no batch, and giving it its own copy of this
        method would leave two places that decide how a raster result differs
        from a vector one and what a draft review layer looks like.
        """
        project_id = self._active_project_id()
        prepared = []
        if only_runs:
            run_ids = list(only_runs)
            # A named run is imported as it stands. `include_review` exists to
            # decide whether to reach past a batch's approved items into its
            # unfinished ones; there is nothing to reach past here, and a run
            # the caller asked for by id is not a draft it did not ask for.
            draft_runs: set[str] = set()
        else:
            run_ids = list(succeeded_run_ids(detail))
            draft_runs = set()
            if include_review:
                for run_id in review_run_ids(detail):
                    if run_id not in run_ids:
                        run_ids.append(run_id)
                        draft_runs.add(run_id)
        for run_id in run_ids:
            draft = run_id in draft_runs
            run = self.api.run(run_id, project_id)
            # A survey computation's positions are not a Layer and never will
            # be: nothing is materialized on the server, because the answer
            # belongs to the turn rather than to the project. They arrive with
            # the step that produced them, so they are drawn here rather than
            # imported below.
            from .survey_drawing import drawings_from_run  # noqa: PLC0415

            self._create_survey_layers(drawings_from_run(run))
            for layer in collect_layer_imports(run):
                layer_id = layer["layer_id"]
                if layer_id in self.imported_layer_ids:
                    continue
                metadata = self.api.layer(layer_id, project_id)
                geometry_type = str(
                    metadata.get("geometry_type") or layer.get("geometry_type") or ""
                ).lower()
                result_dir = tempfile.mkdtemp(prefix="mapdex-qgis-result-")
                # The id comes from the server and is used as a file name.
                safe_id = safe_filename_part(layer_id, "layer")
                if geometry_type == "raster":
                    file_id = str(metadata.get("source_file_id") or "")
                    if not file_id:
                        raise RuntimeError(
                            "Raster result {} has no downloadable source file.".format(layer_id)
                        )
                    raw = self.api.file_bytes(file_id, project_id)
                    path = os.path.join(result_dir, "{}.tif".format(safe_id))
                    kind = "raster"
                else:
                    raw = self.api.layer_geojson(layer_id, project_id)
                    try:
                        notice = geojson_truncation_notice(json.loads(raw), layer["name"])
                    except (ValueError, TypeError):
                        notice = ""
                    if notice:
                        self._announce(notice, level=1, duration=10)
                    if draft:
                        # An unreviewed draft is worth reviewing IN QGIS, so it
                        # arrives as one layer per validator verdict instead of
                        # a single blob the user has to filter by hand.
                        prepared.extend(
                            self._draft_review_layers(raw, layer, result_dir, safe_id)
                        )
                        continue
                    path = os.path.join(result_dir, "{}.geojson".format(safe_id))
                    kind = "vector"
                with open(path, "wb") as handle:
                    handle.write(raw)
                prepared.append(
                    {
                        "path": path,
                        "name": "Mapdex · {}".format(layer["name"]),
                        "layer_id": layer_id,
                        "kind": kind,
                    }
                )
            for artifact in collect_geojson_artifact_urls(run):
                key = artifact["url"]
                if key in self.imported_layer_ids:
                    continue
                raw = self.api.download_bytes(artifact["url"], project_id)
                path = os.path.join(
                    tempfile.mkdtemp(prefix="mapdex-qgis-result-"),
                    "artifact.geojson",
                )
                with open(path, "wb") as handle:
                    handle.write(raw)
                prepared.append(
                    {
                        "path": path,
                        "name": "Mapdex · {}".format(artifact["name"]),
                        "layer_id": key,
                        "kind": "vector",
                    }
                )
        return {"files": prepared, "batch": detail}

    @guarded
    def _results_imported(self, exception, payload):
        if exception:
            self._show_error("Could not import results", exception)
            return
        files = (payload or {}).get("files") or []
        detail = (payload or {}).get("batch") or self._last_batch or {}
        added = 0
        imported = []
        for item in files:
            if item.get("kind") == "raster":
                layer = QgsRasterLayer(item["path"], item["name"])
            else:
                layer = QgsVectorLayer(item["path"], item["name"], "ogr")
            if not layer.isValid():
                continue
            QgsProject.instance().addMapLayer(layer)
            if item.get("layer_id"):
                self.imported_layer_ids.add(item["layer_id"])
            added += 1
            imported.append(layer)
        self._zoom_to_layers(imported)
        review_n = len(review_run_ids(detail))
        if added:
            msg = "Added {} Mapdex result layer(s) to the project.".format(added)
            if review_n:
                msg += " {} item(s) still need browser Review.".format(review_n)
            self._set_status(msg)
            self.iface.mapCanvas().refresh()
            # The layer is in the tree and the canvas moved to it, but say so
            # in the message bar too: the task finishes long after the user
            # stopped watching this panel.
            self._announced_state = "imported"
            self._announce(msg, level=3)
        elif review_n:
            self._set_status("Open Review — no approved vector layers are ready to import yet.")
        else:
            self._set_status("Task finished, but no importable result layers were available to add.")
            self._announce("Mapdex task finished with no importable layer.", level=1)

    def _zoom_to_layers(self, layers) -> None:
        """Frame the imported layers, in the canvas CRS.

        `QgsMapCanvas.setExtent` expects the canvas projection, while
        `layer.extent()` is in the layer's own. Mapdex results arrive as
        EPSG:4326 GeoJSON, so handing that rectangle straight to a Web
        Mercator canvas dropped the view near 0°/0° instead of the data.
        """
        canvas = self.iface.mapCanvas()
        target_crs = canvas.mapSettings().destinationCrs()
        combined = QgsRectangle()
        combined.setMinimal()
        for layer in layers:
            extent = layer.extent()
            if extent is None or extent.isEmpty():
                continue
            source_crs = layer.crs()
            if source_crs.isValid() and target_crs.isValid() and source_crs != target_crs:
                transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
                try:
                    extent = transform.transformBoundingBox(extent)
                except QgsCsException:
                    # An unprojectable result is better left where the user is
                    # than thrown at a wrong place on the map.
                    continue
            combined.combineExtentWith(extent)
        if combined.isEmpty():
            return
        try:
            combined.scale(1.05)  # a little air around the result
        except (AttributeError, TypeError):
            pass
        canvas.setExtent(combined)
        canvas.refresh()

    @guarded
    def cancel_batch(self, *args):
        if not self.batch_id:
            return
        project_id = self._active_project_id()

        def done(exception, _response):
            if exception:
                self._show_error("Cancel failed", exception)
                return
            self._set_status("Cancel requested. Refreshing batch…")
            self.progress_timer.start(2000)

        self._task(
            "Cancel Mapdex batch",
            lambda: self.api.cancel_batch(project_id, self.batch_id),
            done,
        )

    @guarded
    def retry_failed(self, *args):
        if not self.batch_id:
            return
        project_id = self._active_project_id()

        def done(exception, response):
            if exception:
                self._show_error("Retry failed", exception)
                return
            retried = (response or {}).get("retried", 0)
            self._set_status("Retried {} failed item(s). Watching progress…".format(retried))
            self.imported_layer_ids.clear()
            self._announced_state = ""
            self.progress_timer.start(3000)

        self._task(
            "Retry failed Mapdex items",
            lambda: self.api.retry_failed(project_id, self.batch_id),
            done,
        )

    @guarded
    def open_review(self, *args):
        if not self.batch_id:
            return
        locale = QLocale.system().name().split("_")[0]
        prefix = "" if locale == "en" else "/{}".format(locale)
        project_id = self._active_project_id()
        workflow = str(self.workflow_box.currentData() or "") if self.workflow_box else ""
        fallback_file_id = ""
        for task in self._recent_tasks():
            if task.get("batch_id") == self.batch_id:
                workflow = str(task.get("workflow") or workflow)
                fallback_file_id = str(task.get("file_id") or "")
                break
        detail = self._last_batch
        if not detail and fallback_file_id:
            detail = {"items": [{"file_id": fallback_file_id, "state": "running"}]}
        # A refusal that names a prerequisite outranks the workflow that was
        # submitted: the destination is where the user satisfies it, not the desk
        # for the work that was already refused. The file is the one Mapdex
        # already holds, so this is a handoff and not a second upload.
        failure = batch_failure(detail or {})
        route = failure_next_step(failure.get("code")).get("route")
        path = task_workspace_path(
            project_id,
            route or workflow,
            detail,
            file_id=failure.get("file_id", "") if route else "",
        )
        QDesktopServices.openUrl(
            QUrl("{}{}{}".format(self.web_base, prefix, path))
        )

    @guarded
    def open_project(self, *args):
        project_id = self._active_project_id()
        locale = QLocale.system().name().split("_")[0]
        prefix = "" if locale == "en" else "/{}".format(locale)
        path = "/workspace/{}".format(project_id) if project_id else "/workspace"
        QDesktopServices.openUrl(
            QUrl("{}{}{}".format(self.web_base, prefix, path))
        )

    def _recent_tasks(self):
        raw = str(QSettings().value("mapdex/recent_tasks", "[]") or "[]")
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    def _remember_task(self, batch_id, project_id, workflow, source, file_id=""):
        tasks = [item for item in self._recent_tasks() if item.get("batch_id") != batch_id]
        tasks.insert(
            0,
            {
                "batch_id": batch_id,
                "project_id": project_id,
                "workflow": workflow,
                "source": source,
                "file_id": file_id,
            },
        )
        QSettings().setValue("mapdex/recent_tasks", json.dumps(tasks[:5]))
        self._load_recent_tasks()

    def _load_recent_tasks(self):
        if self.recent_box is None:
            return
        self.recent_box.clear()
        for item in self._recent_tasks():
            label = "{} · {}".format(item.get("workflow") or "Task", item.get("source") or "Source")
            self.recent_box.addItem(label, item)
        # A Resume needs a session: without one it submits a batch id the API
        # answers with a tenant-safe NOT_FOUND, which reads as a lost task.
        self.recent.setVisible(self.recent_box.count() > 0 and bool(self.api.token))

    @guarded
    def resume_recent(self, *args):
        item = self.recent_box.currentData()
        if not isinstance(item, dict):
            return
        self.batch_id = str(item.get("batch_id") or "")
        self.project_id = str(item.get("project_id") or self.project_id)
        project_index = self.project_box.findData(self.project_id)
        if project_index >= 0:
            self.project_box.setCurrentIndex(project_index)
        self._last_batch = None
        self._announced_state = ""
        self._set_status("Refreshing the selected Mapdex task…")
        self._refresh_ui()
        self._poll_batch()
        self.progress_timer.start(3000)


# Keep optional debug import available without binding into runtime paths.
_ = traceback
