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
from .credentials import ProviderCredentialStore, describe_privacy, public_settings
from .generated_contracts import BatchKind
from .providers import resolve_runtime
from .capabilities import CapabilityError, get as get_capability, validate_request
from .qgis_runtime import QGISRuntime, RuntimeUnavailable, build_executor
from .features import (
    can_place,
    describe_placement,
    describe_unplaceable_geometry,
    plan_points,
    scatter_in_rectangle,
)
from .guard import describe_exception, format_traceback, guarded
from .guidance import ACTIVE_STATES, run_has_started, task_guidance
from .layout_rules import MINIMUM_WIDTH, PREFERRED_WIDTH
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
from .panel import build_companion_panel, build_thread_history_dialog
from .processing import (
    build_algorithm_parameters,
    describe_empty_input,
    describe_processing_outcome,
    operation_label,
    resolve_processing_algorithm,
)
from .qt_compat import QAction, enum_member, qgis_version
from .viewport import resolve_extent
from .results import (
    batch_is_terminal,
    batch_state,
    collect_geojson_artifact_urls,
    collect_layer_imports,
    first_batch_error,
    geojson_truncation_notice,
    review_run_ids,
    split_review_buckets,
    succeeded_run_ids,
)
from .token_store import LEGACY_TOKEN_SETTING, qgis_token_store
from .source_info import inspect_paths
from .workspace import task_workspace_path


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
    "save_settings_button", "connect_button", "disconnect_button", "workspace",
    "batch_group", "batch_title", "phase_label", "progress_bar", "guidance_label",
    "project_box", "workflow_box", "input_box", "source_summary", "run_button",
    "cancel_button", "retry_button", "import_button", "review_button",
    "open_project_button", "recent", "recent_box", "resume_button",
    "tabs",
    "nivo_context", "nivo_reply", "nivo_input", "nivo_send_button",
    "nivo_stop_button", "nivo_status",
    "nivo_new_button", "nivo_history_button",
)


def plugin_icon() -> QIcon:
    """The Mapdex mark, drawn from the packaged icon next to this module."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    return QIcon(path) if os.path.isfile(path) else QIcon()


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
    "reorder_applied": lambda result: "moved to {}".format(result.get("position")),
    "basemap_added": lambda result: "added {}".format(result.get("name")),
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
            persisted_token = ""  # nosec B105 - clears the stored session
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
        self.connect_button = None
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
        self.nivo_context = None
        self.nivo_reply = None
        self.nivo_status = None
        self.nivo_input = None
        self.nivo_send_button = None
        self.nivo_stop_button = None
        self.nivo_new_button = None
        self.nivo_history_button = None
        self._nivo_turns = []
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
        self.action = QAction(plugin_icon(), "Mapdex", self.iface.mainWindow())
        self.action.setToolTip("Open Mapdex for QGIS")
        self.action.triggered.connect(self.show)
        self.iface.addPluginToWebMenu("&Mapdex", self.action)
        self.iface.addToolBarIcon(self.action)
        self._install_layer_menu_actions()
        # Register the dock immediately so QGIS places it in the right rail,
        # not as a floating overlay over the menu bar.
        self._ensure_dock()
        self.dock.hide()

    def _install_layer_menu_actions(self):
        """Offer the paid work where the user already is: the layer tree.

        Georeferencing a scan by hand in QGIS is control-point placement, tens
        of minutes a sheet. That is the work worth paying to skip, and until now
        the only way to reach it was to open a panel, pick a workflow from a
        combo box and pick a source. Three steps between the user and the thing
        they came for, none of which they were thinking about: they were
        right-clicking the scan.

        The action prepares the panel and stops. It does NOT start the run.
        Starting paid work from a context menu would take the moment of consent
        away from the person paying, and the Run button is that moment.
        """
        add = getattr(self.iface, "addCustomActionForLayerType", None)
        if add is None:
            # An older or stubbed interface. The panel is still the way in.
            return
        window = self.iface.mainWindow()
        for title, kind, layer_type in self._layer_menu_entries():
            if layer_type is None:
                continue
            action = QAction(plugin_icon(), title, window)
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
        if layer is not None:
            self._set_status(
                "{} is ready to send. Press Start when you want to.".format(layer.name())
            )

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
        except Exception:
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
            except Exception:
                pass

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
        self.dock.setWidget(root)
        for key, value in refs.items():
            setattr(self, key if key != "batch" else "batch_group", value)

        self._load_connection_fields()
        self._load_assistant_fields()
        self.connect_button.clicked.connect(self.connect)
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
            try:
                self.dock.setUserVisible(True)
            except Exception:
                pass
        self.dock.show()
        self.dock.raise_()
        try:
            self.dock.activateWindow()
        except Exception:
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
        })
        self.assistant_privacy.setText(describe_privacy(runtime))

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
                self.api.token = ""  # nosec B105 - clears the rejected session
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

        self.workspace.setVisible(connected)
        self.workspace.setEnabled(connected and not self._busy)
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
        self.retry_button.setVisible(has_batch and failed > 0 and not active)
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
                "kind": "raster" if isinstance(layer, QgsRasterLayer) else "vector" if isinstance(layer, QgsVectorLayer) else "other",
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
        self.nivo_context.setText("{} · {} selected · {}".format(label, context.get("selection_count", 0), context.get("crs", "No CRS")))

    def _active_qgis_layer(self):
        layer = self.iface.activeLayer()
        if layer is not None and layer.isValid():
            return layer
        try:
            view = self.iface.layerTreeView()
        except Exception:
            view = None
        if view is not None:
            try:
                layer = view.currentLayer()
                if layer is not None and layer.isValid():
                    return layer
            except Exception:
                pass
            try:
                for candidate in view.selectedLayers():
                    if candidate is not None and candidate.isValid():
                        return candidate
            except Exception:
                pass
            try:
                node = view.currentNode()
                layer = node.layer() if node is not None and hasattr(node, "layer") else None
                if layer is not None and layer.isValid():
                    return layer
            except Exception:
                pass
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            for node in root.findLayers():
                if not node.isVisible():
                    continue
                layer = node.layer()
                if layer is not None and layer.isValid():
                    return layer
        except Exception:
            pass
        return None

    def _nivo_layer_for_action(self, target):
        if target:
            return QgsProject.instance().mapLayer(target)
        return self._active_qgis_layer()

    def _set_nivo_compose_busy(self, busy):
        if self.nivo_send_button is not None:
            self.nivo_send_button.setEnabled(not busy)
        if self.nivo_input is not None:
            self.nivo_input.setEnabled(not busy)
        if self.nivo_stop_button is not None:
            self.nivo_stop_button.setVisible(busy)
            self.nivo_stop_button.setEnabled(busy)

    @guarded
    def ask_nivo(self, *args):
        if not self.api.token or not self.project_id:
            self._set_status("Connect Mapdex and choose a project before asking Nivo.")
            return
        if self._nivo_compose_task is not None:
            self._set_status("Nivo is already working. Use Stop to cancel that request.")
            return
        message = self.nivo_input.text().strip() if self.nivo_input is not None else ""
        if not message:
            return
        self._set_nivo_compose_busy(True)
        self._nivo_state = transition(self._nivo_state, "send")
        self._nivo_turns.append(("user", message))
        self._nivo_turns.append(("assistant", "Thinking…"))
        self._render_nivo_turns()
        self.nivo_status.setText("Nivo AI is reading your map context…")
        self.nivo_input.clear()
        self._refresh_nivo_context()
        self._nivo_request_id += 1
        request_id = self._nivo_request_id
        # Build the context HERE, on the main thread. QgsTask.run() executes on a
        # worker thread, and iface.activeLayer(), the map canvas and the layer
        # tree are main-thread only: reading them from the task returned an empty
        # snapshot, so Nivo answered "no layer is active yet" while a layer was
        # plainly open. Only the HTTP call belongs in the background.
        context = companion_context(self._nivo_snapshot())
        project_id = self.project_id
        # The active project can have moved since the conversation was opened
        # (starting a task switches it). A thread from another project cannot
        # be continued here, so it is dropped rather than sent.
        thread_id = remembered_thread(self._nivo_thread_id, self._nivo_thread_project, project_id)
        title = thread_title(message)
        self._nivo_compose_task = self._task(
            "Nivo compose",
            lambda: self._compose_in_thread(project_id, message, context, thread_id, title),
            lambda exception, outcome: self._nivo_composed(request_id, exception, outcome),
            busy=False,
        )

    def _compose_in_thread(self, project_id, message, context, thread_id, title):
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
            response = self.api.compose(project_id, message, context, thread_id=thread_id)
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
                response = self.api.compose(project_id, message, context, thread_id=thread_id)
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
        if self._nivo_turns and self._nivo_turns[-1][0] == "assistant":
            self._nivo_turns[-1] = ("assistant", "Stopped.")
            self._render_nivo_turns()
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
            if self._nivo_turns and self._nivo_turns[-1][0] == "assistant":
                self._nivo_turns[-1] = ("assistant", "I couldn't complete that request.")
            self._render_nivo_turns()
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
            if self._nivo_turns and self._nivo_turns[-1][0] == "assistant":
                self._nivo_turns[-1] = ("assistant", reply)
            else:
                self._nivo_turns.append(("assistant", reply))
            self._render_nivo_turns()
        if self.nivo_status is not None:
            self.nivo_status.setText(str(outcome.get("notice") or "") or "Ready")
        for action in allowed_actions(response):
            self._nivo_state = transition(self._nivo_state, "action")
            self._apply_nivo_action(action)
        for action in confirmation_actions(response):
            self._nivo_state = transition(self._nivo_state, "confirm")
            self._confirm_nivo_action(action)

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
        for sender, text in self._nivo_turns:
            card = QWidget()
            row = QVBoxLayout(card)
            is_thinking = sender != "user" and str(text).startswith("Thinking")
            if is_thinking:
                row.setContentsMargins(2, 2, 2, 2)
                body = self._plain(QLabel(), text)
                body.setWordWrap(True)
                body.setStyleSheet("color:#8F96A8; background:transparent; border:0;")
                row.addWidget(body)
                card.setStyleSheet("background:transparent; border:0;")
                layout.addWidget(card)
                continue
            row.setContentsMargins(8, 7, 8, 7)
            row.setSpacing(3)
            header = QWidget()
            header_row = QHBoxLayout(header)
            header_row.setContentsMargins(0, 0, 0, 0)
            header_row.setSpacing(0)
            label = QLabel("You" if sender == "user" else "Nivo")
            label.setStyleSheet("color:#8F96A8; font-weight:600;" if sender != "user" else "color:#ffffff; font-weight:600;")
            header_row.addWidget(label)
            body = self._plain(QLabel(), text)
            body.setWordWrap(True)
            row.addWidget(header)
            row.addWidget(body)
            if sender == "user":
                card.setStyleSheet("background:#4F46E5; color:#ffffff; border-radius:8px;")
                body.setStyleSheet("color:#ffffff;")
            else:
                card.setStyleSheet("background:transparent; color:#F7F7F5; border:0;")
                body.setStyleSheet("color:#F7F7F5;")
            layout.addWidget(card)
        layout.addStretch(1)
        bar = self.nivo_reply.verticalScrollBar()
        bar.setValue(bar.maximum())

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
        self._nivo_turns = list(turns)
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
            params = {"bbox": resolved["bbox"], "crs": resolved["crs"]}
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
        return {
            "map.basemap@1": self._add_osm_basemap,
            "map.zoom_extent@1": self._apply_server_extent,
        }

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
        self._nivo_turns.append(("assistant", self._describe_capability_result(summary or capability_id, result)))
        self._render_nivo_turns()
        self.iface.mapCanvas().refresh()
        return True

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
            self._nivo_turns.append(("assistant", describe_unplaceable_geometry(geometry)))
            self._render_nivo_turns()
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
        self._nivo_turns.append(("assistant", describe_placement(len(features), len(positions), where)))
        self._render_nivo_turns()
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

            wanted = {
                "point": QgsWkbTypes.PointGeometry,
                "multipoint": QgsWkbTypes.PointGeometry,
                "linestring": QgsWkbTypes.LineGeometry,
                "multilinestring": QgsWkbTypes.LineGeometry,
                "polygon": QgsWkbTypes.PolygonGeometry,
                "multipolygon": QgsWkbTypes.PolygonGeometry,
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
        self._nivo_turns.append((
            "assistant",
            "Created '{}' ({}, {}). It is the active layer - toggle editing to start drawing.".format(
                name, geometry, crs or "EPSG:4326"),
        ))
        self._render_nivo_turns()
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
            self._set_status("This QGIS installation has no algorithm for the {}.".format(
                operation_label(operation)))
            self._nivo_state = transition(self._nivo_state, "error")
            return
        source_name = layer.name() if hasattr(layer, "name") else ""
        count = layer.featureCount() if hasattr(layer, "featureCount") else None
        if count == 0:
            # Only an exact zero counts as empty: several providers answer -1 for
            # "unknown". Running anyway SUCCEEDS and writes an empty layer, which
            # is how "buffer yap" ended with a new layer and no buffer in it.
            self._nivo_turns.append(("assistant", describe_empty_input(operation, source_name)))
            self._render_nivo_turns()
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
                for key in ("OUTPUT", "OUTPUT_LAYER", "OUTPUT_VECTOR", "OUTPUT_RASTER"):
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
                            except Exception:
                                pass
                        if isinstance(val, str):
                            try:
                                from qgis.core import QgsProcessingUtils
                                output_layer = QgsProcessingUtils.mapLayerFromString(val, context, True)
                                if output_layer is not None and output_layer.isValid():
                                    break
                            except Exception:
                                pass
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
            self._nivo_turns.append(("assistant", describe_processing_outcome(
                operation, output_name, produced, source_name)))
            self._render_nivo_turns()
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
        self.api.token = ""  # nosec B105 - disconnect clears the session
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
        settings.remove("mapdex/project_id")
        # The conversation belonged to the session that just ended. Keeping its
        # id would send the next connection's first question into a thread the
        # new token may not be able to see.
        self._adopt_conversation("", "")
        self._nivo_turns = []
        self._render_nivo_turns()
        self.project_box.clear()
        self._set_status("Disconnected.")
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
        if state in ("created", "queued", "pending"):
            message = "Task queued. Mapdex will start processing shortly."
        elif state == "running":
            message = "Processing in Mapdex…"
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

    def _fetch_result_files(self, detail: dict, include_review: bool = False):
        project_id = self._active_project_id()
        prepared = []
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
        path = task_workspace_path(project_id, workflow, detail)
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
        self.recent.setVisible(self.recent_box.count() > 0)

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
