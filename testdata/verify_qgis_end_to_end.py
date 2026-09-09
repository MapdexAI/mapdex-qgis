"""Drive the Mapdex QGIS companion end to end against real QGIS 4 / PyQt6.

`verify_in_qgis.py` next to this file imports every module and calls runtime
functions. That catches an enum mistake at module scope and proves the analytics
answers, and it is where the export `AttributeError` was found. What it never
does is build the panel or press anything, so a PyQt6 mistake inside a slot -
the ones that only run when a user clicks - stays invisible to it, and an action
the server can name but the dispatcher quietly drops looks exactly like an
action that worked.

This script covers what a human tester would cover that a harness can:

  Part 1  build the companion panel and assert its structure.
  Part 2  load the fixture and check every answer in testdata/README.md.
  Part 3  construct the plugin against a real dock and press its controls.
  Part 4  dispatch every capability this build advertises, from a simulated
          compose response, and classify each as executed, refused by name, or
          silently dropped.

Everything below runs against the real QGIS libraries. Two boundaries are
stubbed, and only two: the HTTP client (`plugin.api`) and the modal question
box, because a network call and a blocking prompt cannot be answered by a
script. Every widget, every enum, every layer and every capability executor is
the real one.

Run it (Windows, from a NATIVE path - cmd.exe refuses a UNC working directory
and a WSL share silently resolves to the Windows directory instead):

    set QT_QPA_PLATFORM=offscreen
    "C:/Program Files/QGIS 4.0.2/bin/python-qgis.bat" verify_qgis_end_to_end.py

Stage a copy of `mapdex_qgis/` and `mapdex-test-parcels.gpkg` beside the script.
The fixture is copied before it is touched: the field calculator writes real
columns into a real GeoPackage, so a second run against the same file trips its
own refuse-to-overwrite guard and reports a working refusal as a failure.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PRISTINE = os.path.join(HERE, "mapdex-test-parcels.gpkg")
# A live QGIS verification session may still have the previous fixture open.
# Reusing one fixed path then makes the next UI render fail with
# ``database is locked`` before it draws anything. Each run owns its copy;
# the source fixture stays immutable and parallel/manual checks cannot collide.
FIXTURE = os.path.join(tempfile.mkdtemp(prefix="mapdex-qgis-e2e-"), "parcels.gpkg")
shutil.copyfile(PRISTINE, FIXTURE)


# --------------------------------------------------------------------------
# Reporting. A harness reports; it never raises, and it never reports a check
# it could not run as a pass.
# --------------------------------------------------------------------------

RESULTS = []
TRACE = bool(os.environ.get("MAPDEX_VERIFY_TRACE"))


class NotRun(Exception):
    """A precondition for this check was absent. Reported, never counted."""


def check(name, fn):
    try:
        RESULTS.append(("PASS", name, fn() or ""))
    except NotRun as reason:
        RESULTS.append(("NOTRUN", name, str(reason)))
    except Exception as error:  # noqa: BLE001
        RESULTS.append(("FAIL", name, "{}: {}".format(type(error).__name__, error)))
        if TRACE:
            traceback.print_exc()


def note(name, detail):
    """An observation, not a verdict. Printed, never counted as a pass."""
    RESULTS.append(("NOTE", name, detail))


def approx(value, want, tolerance):
    return value is not None and abs(float(value) - float(want)) <= tolerance


# --------------------------------------------------------------------------
# Part 0: QGIS itself
# --------------------------------------------------------------------------

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeModel,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

QgsApplication.setPrefixPath(
    os.environ.get("QGIS_PREFIX_PATH", "C:/Program Files/QGIS 4.0.2/apps/qgis"), True
)
# GUI=True: this builds widgets, a dock and a map canvas.
QGS = QgsApplication([], True)
QGS.initQgis()

from qgis.PyQt.QtGui import QPixmap  # noqa: E402
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402
from qgis.PyQt.QtCore import (  # noqa: E402
    PYQT_VERSION_STR,
    QT_VERSION_STR,
    QCoreApplication,
    QSettings,
    QSize,
    Qt,
    QTimer,
)
from qgis.PyQt.QtWidgets import (  # noqa: E402
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
)
from qgis.gui import QgsLayerTreeView, QgsMapCanvas  # noqa: E402

VERSIONS = "QGIS {} / Qt {} / PyQt {}".format(Qgis.QGIS_VERSION, QT_VERSION_STR, PYQT_VERSION_STR)
print(VERSIONS)
print("platform:", os.environ.get("QT_QPA_PLATFORM"))
print()

# Keep the plugin's QSettings writes out of the user's real QGIS profile. The
# panel persists the conversation it is continuing, and a verification run must
# not leave that behind in someone's installation.
SETTINGS_DIR = tempfile.mkdtemp(prefix="mapdex-verify-settings-")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, SETTINGS_DIR)
QCoreApplication.setOrganizationName("MapdexVerify")
QCoreApplication.setApplicationName("MapdexVerify")

from mapdex_qgis._vendor.nivo import capabilities as capabilities_module  # noqa: E402
from mapdex_qgis import nivo as nivo_module  # noqa: E402
from mapdex_qgis import plugin as plugin_module  # noqa: E402
from mapdex_qgis.api_client import MapdexAPIError  # noqa: E402
from mapdex_qgis.generated_contracts import BatchKind  # noqa: E402
from mapdex_qgis import task_price as task_price_module  # noqa: E402
from mapdex_qgis.panel import build_companion_panel  # noqa: E402
from mapdex_qgis.qgis_runtime import QGISRuntime, bound_capability_ids  # noqa: E402
from mapdex_qgis.qt_compat import enum_member  # noqa: E402

WORKFLOWS = (
    ("Georeference maps", BatchKind.GEOREFERENCE),
    ("Digitize parcels", BatchKind.DIGITIZE_PARCELS),
)


def widgets_of(layout):
    """Direct child widgets of a layout, in order."""
    if layout is None:
        return []
    found = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            found.append(widget)
    return found


def descendants(widget, kind):
    return list(widget.findChildren(kind)) if widget is not None else []


# ==========================================================================
# Part 1: the panel, as a real widget tree
# ==========================================================================

PANEL = {}


def panel_builds():
    root, refs = build_companion_panel(WORKFLOWS, endpoint_settings=True)
    PANEL["root"] = root
    PANEL["refs"] = refs
    required = (
        "nivo_context", "nivo_new_button", "nivo_history_button", "nivo_reply",
        "nivo_status", "nivo_input", "nivo_send_button", "nivo_stop_button",
    )
    missing = [key for key in required if refs.get(key) is None]
    if missing:
        raise AssertionError("the panel did not hand back: {}".format(missing))
    return "{} widget handles; root {}".format(len(refs), type(root).__name__)


check("the companion panel builds under PyQt6", panel_builds)


def nivo_header_structure():
    """The message header identifies the assistant; the tab row holds the
    conversation controls.

    They used to share one row - avatar, name, layer context, runtime line,
    New chat and History - and no real dock width holds six things. New chat
    and History act on the transcript as a whole rather than on the message
    they sat beside, so they belong on the toolbar row with the tabs.
    """
    refs = PANEL.get("refs")
    if not refs:
        raise NotRun("the panel did not build")
    new_button = refs["nivo_new_button"]
    history_button = refs["nivo_history_button"]
    header = refs["nivo_context"].parentWidget().parentWidget()
    row_widgets = widgets_of(new_button.parentWidget().layout())
    if history_button not in row_widgets:
        raise AssertionError("History is not in the same row as New chat")
    if refs["tabs"] is None:
        raise AssertionError("the conversation controls are not on the tab row")
    if new_button.text() != "New chat":
        raise AssertionError("the new-conversation control reads {!r}".format(new_button.text()))
    if history_button.text() != "History":
        raise AssertionError("the history control reads {!r}".format(history_button.text()))
    titles = [
        label.text() for label in descendants(header, QLabel)
        if label.objectName() == "mapdexNivoTitle"
    ]
    if "Nivo" not in titles:
        raise AssertionError("the header carries no Nivo title; labels were {}".format(titles))
    if refs["nivo_context"] not in descendants(header, QLabel):
        raise AssertionError("the context line is not in the header row")
    if refs["nivo_runtime"] not in descendants(header, QLabel):
        raise AssertionError("the header does not say which engine answers")
    for button in (new_button, history_button):
        if not button.toolTip():
            raise AssertionError("{} carries no tooltip".format(button.text()))
    return "title 'Nivo', context {!r}, then {} / {}".format(
        refs["nivo_context"].text(), new_button.text(), history_button.text())


check("the Nivo header carries title, context, History and New chat", nivo_header_structure)


def composer_untouched():
    """The two conversation controls act on the transcript, not on the next message."""
    refs = PANEL.get("refs")
    if not refs:
        raise NotRun("the panel did not build")
    field, send, stop = refs["nivo_input"], refs["nivo_send_button"], refs["nivo_stop_button"]
    if not isinstance(field, QLineEdit) or not field.placeholderText():
        raise AssertionError("the composer input is not a prompted QLineEdit")
    # Send sits inside the field now, as a glyph. A full-width button under a
    # full-width field is most of the panel's bottom spent saying one thing
    # twice - and a control with no label needs an icon that is really there,
    # because there is nothing to fall back to.
    if send.icon().isNull():
        raise AssertionError("the send control has no glyph, so it draws nothing")
    if send.parentWidget() is not field.parentWidget():
        raise AssertionError("send is not inside the composer beside the field")
    if stop.isVisible() or stop.isEnabled():
        raise AssertionError("Stop is offered before anything is running")
    action_row = widgets_of(send.parentWidget().layout()) if send.parentWidget() else []
    intruders = [
        w for w in (refs["nivo_new_button"], refs["nivo_history_button"]) if w in action_row
    ]
    if intruders:
        raise AssertionError("a conversation control sits in the composer row")
    return "input {!r}, send is a glyph inside it, stop hidden".format(
        field.placeholderText()[:38])


check("the composer is unchanged and owns no conversation control", composer_untouched)


def transcript_is_widgets():
    """Not an HTML view. Assistant text is data, so it must not be markup."""
    refs = PANEL.get("refs")
    if not refs:
        raise NotRun("the panel did not build")
    reply = refs["nivo_reply"]
    if not isinstance(reply, QScrollArea):
        raise AssertionError("the transcript is a {}".format(type(reply).__name__))
    if hasattr(reply, "setHtml") or hasattr(reply, "setMarkdown"):
        raise AssertionError("the transcript surface accepts markup")
    content = reply.widget()
    if content is None or content.layout() is None:
        raise AssertionError("the transcript has no widget tree to put messages in")
    return "{} over {} with a {}".format(
        type(reply).__name__, type(content).__name__, type(content.layout()).__name__)


check("the transcript is a widget tree, not an HTML view", transcript_is_widgets)


# ==========================================================================
# Part 2: the fixture, whose answers are known (testdata/README.md)
# ==========================================================================

LAYER = QgsVectorLayer(FIXTURE + "|layername=parcels", "parcels", "ogr")
PROJECT = QgsProject.instance()
if LAYER.isValid():
    PROJECT.addMapLayer(LAYER)

CANVAS = QgsMapCanvas()
CANVAS.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
CANVAS.setExtent(QgsRectangle(28.90, 40.95, 29.10, 41.10))


class Iface:
    """Enough of QgisInterface to run the panel, the dock and the runtime.

    Backed by a real QMainWindow and a real QgsMapCanvas, so a zoom is a real
    extent change and the dock is really docked. Everything recorded here is
    an effect a human tester would look for on screen.
    """

    def __init__(self):
        self.window = QMainWindow()
        self.canvas = CANVAS
        self._active = None
        self.attribute_tables = []
        self.processing_dialogs = 0
        self.messages = []
        self.toolbars = []
        # A real one: `apply_visualization` calls refreshLayerSymbology on it
        # after every restyle, unguarded. A stub without it turns a working
        # restyle into a reported AttributeError.
        self.tree_model = QgsLayerTreeModel(PROJECT.layerTreeRoot())
        self.tree_view = QgsLayerTreeView()
        self.tree_view.setModel(self.tree_model)

    # -- the parts the plugin and the runtime call --------------------------
    def mainWindow(self):
        return self.window

    def addToolBar(self, title):
        """A real QToolBar, because the plugin configures the one it gets.

        This stub did not have the method at all, and `initGui` is @guarded, so
        the AttributeError was swallowed: `initGui` returned having built no
        dock, and Parts 3 and 4 - pressing the plugin's controls and dispatching
        every advertised capability, which is most of what this script is for -
        skipped themselves on "the plugin did not build its dock" while
        reporting nothing about the toolbar. A harness that quietly stops
        measuring is worse than one that fails.
        """
        bar = self.window.addToolBar(title)
        self.toolbars.append(bar)
        return bar

    def mapCanvas(self):
        return self.canvas

    def layerTreeView(self):
        return self.tree_view

    def activeLayer(self):
        return self._active

    def setActiveLayer(self, layer):
        self._active = layer
        return True

    def showAttributeTable(self, layer):
        self.attribute_tables.append(getattr(layer, "id", lambda: "")())
        return None

    def showProcessingAlgorithmDialog(self, *_args, **_kwargs):
        self.processing_dialogs += 1
        return None

    def addDockWidget(self, area, dock):
        self.window.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self.window.removeDockWidget(dock)

    def addPluginToWebMenu(self, *_args):
        return None

    def removePluginWebMenu(self, *_args):
        return None

    def addToolBarIcon(self, *_args):
        return None

    def removeToolBarIcon(self, *_args):
        return None

    def messageBar(self):
        return self

    def pushMessage(self, title, text, level=0, duration=0):
        self.messages.append((title, text, level))


IFACE = Iface()
IFACE.setActiveLayer(LAYER)
RUNTIME = QGISRuntime(IFACE, PROJECT) if LAYER.isValid() else None


def fixture_loads():
    if not LAYER.isValid():
        raise AssertionError("the fixture did not load from " + FIXTURE)
    crs = LAYER.crs().authid()
    if LAYER.featureCount() != 24 or crs != "EPSG:32635":
        raise AssertionError("{} features in {}, want 24 in EPSG:32635".format(
            LAYER.featureCount(), crs))
    return "24 features, EPSG:32635"


check("the fixture loads as a real QgsVectorLayer", fixture_loads)


def need_runtime():
    if RUNTIME is None:
        raise NotRun("the fixture did not load, so the runtime has nothing to run against")
    return RUNTIME


def geometry_area_matches_the_attribute():
    """The area a geometry actually has, measured independently of `alan_m2`.

    The trap this guards is answering an area question by summing a
    plausibly-named column. Here the two agree, which is what makes every later
    area answer in this file checkable.
    """
    if not LAYER.isValid():
        raise NotRun("the fixture did not load")
    measured = sum(feature.geometry().area() for feature in LAYER.getFeatures())
    stated = sum(float(feature["alan_m2"]) for feature in LAYER.getFeatures())
    if not approx(measured, 250000.0, 0.5):
        raise AssertionError("geometry area is {}, want 250000".format(measured))
    if not approx(stated, 250000.0, 0.5):
        raise AssertionError("alan_m2 sums to {}, want 250000".format(stated))
    return "geometry {:.1f} m2 == alan_m2 {:.1f} m2".format(measured, stated)


check("total area is 250,000 m2, measured and stated", geometry_area_matches_the_attribute)


def profile_counts_features():
    got = need_runtime().profile_layer(LAYER.id())
    if got.get("feature_count") != 24:
        raise AssertionError("feature_count={}".format(got.get("feature_count")))
    return "feature_count=24, crs {}".format(got.get("crs"))


check("inspect reports 24 features", profile_counts_features)


def area_statistics():
    got = need_runtime().numeric(LAYER.id(), "alan_m2")
    if not approx(got.get("sum"), 250000.0, 0.01):
        raise AssertionError("sum={}, want 250000".format(got.get("sum")))
    if not approx(got.get("mean"), 10416.667, 0.01):
        raise AssertionError("mean={}, want 10416.667".format(got.get("mean")))
    if not approx(got.get("max"), 20000.0, 0.01):
        raise AssertionError("max={}, want 20000".format(got.get("max")))
    return "sum {} mean {:.3f} max {}".format(got.get("sum"), got.get("mean"), got.get("max"))


check("area statistics: 250,000 total, 10,416.667 mean, 20,000 largest", area_statistics)


def largest_parcel_is_p013():
    got = need_runtime().top_n(LAYER.id(), "alan_m2", 1)
    rows = got.get("rows") or []
    if len(rows) != 1:
        raise AssertionError("top_n returned {} rows".format(len(rows)))
    feature = LAYER.getFeature(rows[0]["id"])
    name = feature["parsel_no"]
    if name != "P-013":
        raise AssertionError("largest is {} at {}, want P-013".format(name, rows[0]["value"]))
    if not approx(rows[0]["value"], 20000.0, 0.01):
        raise AssertionError("P-013 measured {}, want 20000".format(rows[0]["value"]))
    return "P-013 at {} m2".format(rows[0]["value"])


check("the largest parcel is P-013 at 20,000 m2", largest_parcel_is_p013)


def population_excludes_the_null():
    """The trap. 9,125 / 23 = 396.739; 9,125 / 24 = 380.2 counts a null as zero."""
    got = need_runtime().numeric(LAYER.id(), "nufus")
    mean, total = got.get("mean"), got.get("sum")
    if not approx(total, 9125.0, 0.01):
        raise AssertionError("sum={}, want 9125".format(total))
    if got.get("usable") != 23 or got.get("nulls") != 1:
        raise AssertionError("usable={} nulls={}, want 23 and 1".format(
            got.get("usable"), got.get("nulls")))
    if approx(mean, 380.2, 0.5):
        raise AssertionError("mean={} - the null was counted as zero".format(mean))
    if not approx(mean, 396.739, 0.01):
        raise AssertionError("mean={}, want 396.739".format(mean))
    return "sum 9125 over 23 values, mean {:.3f}, 1 null excluded".format(mean)


check("mean population is 396.739, not 380.2", population_excludes_the_null)


def category_breakdown():
    """The second trap. README: five buckets - konut 7, ticari 7, tarim 8, one
    null, one empty string - because a null and an empty string are distinct
    from each other and from a category.
    """
    got = need_runtime().categories(LAYER.id(), "kullanim")
    buckets = {row["value"]: row["count"] for row in (got.get("categories") or [])}
    if got.get("count") != 24:
        raise AssertionError("count={}, want 24 - records were dropped".format(got.get("count")))
    for name, want in (("konut", 7), ("ticari", 7), ("tarım", 8)):
        if buckets.get(name) != want:
            raise AssertionError("{} = {}, want {} (buckets {})".format(
                name, buckets.get(name), want, buckets))
    # The distinguishing question: can a reader tell the null from the empty
    # string? Either as two separate buckets, or as two separately reported
    # counts. One combined `nulls` figure cannot answer it.
    empty_bucket = "" in buckets
    separate_counts = got.get("nulls") == 1 and got.get("empty") == 1
    if not (empty_bucket or separate_counts):
        raise AssertionError(
            "null and empty string are merged into nulls={}; the README expects them "
            "distinguished (five buckets, not three). Reported: {} categories {}, "
            "count={}, usable={}, nulls={}".format(
                got.get("nulls"), len(buckets), sorted(buckets), got.get("count"),
                got.get("usable"), got.get("nulls")))
    return "konut 7, ticari 7, tarım 8, null and empty distinguished"


check("the kullanim breakdown separates the null from the empty string", category_breakdown)


def turkish_values_survive():
    got = need_runtime().categories(LAYER.id(), "mahalle")
    values = [row["value"] for row in (got.get("categories") or [])]
    expected = {"Şişli", "Üsküdar", "Beşiktaş"}
    missing = expected - set(values)
    if missing:
        raise AssertionError("Turkish values did not survive the read: missing {}, got {}".format(
            sorted(missing), values))
    return "Şişli / Üsküdar / Beşiktaş read back intact"


check("Turkish attribute values survive the read", turkish_values_survive)


def field_calculator_writes_and_refuses():
    runtime = need_runtime()
    wrote = runtime.calculate_field(LAYER.id(), "hektar", "alan_m2 / 10000")
    if wrote.get("rows") != 24:
        raise AssertionError("wrote {} rows".format(wrote.get("rows")))
    refusals = []
    for name, expression, why in (
        ("alan_m2", "1", "overwrite"),
        ("evil", "__import__('os')", "python source"),
    ):
        try:
            runtime.calculate_field(LAYER.id(), name, expression)
        except Exception as error:  # noqa: BLE001
            refusals.append("{}: {}".format(why, str(error)[:52]))
        else:
            raise AssertionError("it accepted {}".format(why))
    turkish = runtime.calculate_field(LAYER.id(), "nüfus_katı", "nufus * 2")
    if "nüfus_katı" not in [f.name() for f in LAYER.fields()]:
        raise AssertionError("the Turkish column name did not survive")
    return "24 rows; refused {}; wrote {} rows into 'nüfus_katı'".format(
        " and ".join(refusals), turkish.get("rows"))


check("the field calculator writes, refuses, and keeps a Turkish name",
      field_calculator_writes_and_refuses)


# ==========================================================================
# Part 3: the plugin, its dock, and its controls
# ==========================================================================
#
# Two stubs, both at boundaries a script cannot answer: the HTTP client and the
# modal question box. Everything they sit behind is the real code.

class StubAPI:
    """The HTTP boundary. Records what was asked; answers from a script."""

    def __init__(self):
        self.base_url = "https://api.example.invalid"
        self.token = ""
        self.threads = []
        self.thread_error = None
        self.messages_payload = {"messages": []}
        self.deleted = []
        self.calls = []

    def list_threads(self, project_id):
        self.calls.append(("list_threads", project_id))
        if self.thread_error is not None:
            raise self.thread_error
        return {"threads": list(self.threads)}

    def thread_messages(self, thread_id, project_id):
        self.calls.append(("thread_messages", thread_id, project_id))
        return self.messages_payload

    def delete_thread(self, thread_id, project_id):
        self.calls.append(("delete_thread", thread_id, project_id))
        self.deleted.append(thread_id)
        self.threads = [row for row in self.threads if row.get("id") != thread_id]
        return {}


class MessageBoxStub:
    """Answers the modal prompts, and records that they were asked at all.

    The enums come from the real QMessageBox, so `enum_member` resolves through
    this exactly as it does through the class it replaces.
    """

    StandardButton = QMessageBox.StandardButton
    Icon = QMessageBox.Icon

    asked = []
    answers = []
    default_answer = QMessageBox.StandardButton.Yes

    @classmethod
    def question(cls, _parent, title, text, *_args, **_kwargs):
        cls.asked.append((title, text))
        return cls.answers.pop(0) if cls.answers else cls.default_answer

    @classmethod
    def warning(cls, _parent, title, text, *_args, **_kwargs):
        cls.asked.append((title, text))
        return cls.StandardButton.Ok

    critical = warning
    information = warning


plugin_module.QMessageBox = MessageBoxStub

from mapdex_qgis.layout_rules import READING_WIDTH as READING_CAP  # noqa: E402

PLUGIN = None
try:
    PLUGIN = plugin_module.MapdexPlugin(IFACE)
    PLUGIN.api = StubAPI()
    PLUGIN.initGui()
except Exception as error:  # noqa: BLE001
    RESULTS.append(("FAIL", "the plugin constructs and builds its dock",
                    "{}: {}".format(type(error).__name__, error)))
    if TRACE:
        traceback.print_exc()
else:
    RESULTS.append(("PASS", "the plugin constructs and builds its dock",
                    "dock {!r}, {} in the main window".format(
                        PLUGIN.dock.windowTitle(),
                        "docked" if PLUGIN.dock.parentWidget() is not None else "floating")))


def render_panel(state, width=1540, height=1200):
    """Write a PNG of the assembled dock and a geometry table for it.

    Component tests passed while the composed screen was broken six ways at
    once, because a test can assert that a widget exists and cannot see that
    the decision it carries sits below two empty gaps, or that a 1,540 px dock
    leaves 800 px of nothing down the right.

    It renders THE PRODUCT'S OWN dock, in whatever state the harness has put it
    in, rather than a builder call with the state guessed alongside it - a
    harness that keeps its own copy of the state measures the copy.
    """
    plugin = need_plugin()
    dock = plugin.dock
    # Resize the CONTENT, not only the frame. A QDockWidget inside a main
    # window is laid out by that window, so resizing the dock alone left the
    # panel at its minimum and the harness reported a width nobody would see.
    root = plugin._panel_root
    dock.setFloating(True)
    dock.resize(width, height)
    root.resize(width, height)
    # A resize posts a layout request; `processEvents` alone delivered it one
    # render too late, so the first measurement reported the PREVIOUS width
    # (scroll 640 inside a 1,540 px root) and the panel looked broken when the
    # instrument was simply a frame behind. Activate the layouts explicitly,
    # top down, then let the queue drain.
    for _pass in range(3):
        for widget in [root] + root.findChildren(QWidget):
            layout = widget.layout()
            if layout is not None:
                layout.activate()
        QGS.processEvents()
    print("  dock {}x{}, root {}x{}".format(
        dock.width(), dock.height(), root.width(), root.height()))
    # The chain the width has to travel: root -> scroll -> viewport -> body ->
    # column. Whichever link stops growing is the one holding the cap back.
    from qgis.PyQt.QtWidgets import QScrollArea as _Scroll  # noqa: PLC0415
    for area in root.findChildren(_Scroll):
        if area.objectName() == "mapdexChatTranscript":
            continue
        inner = area.widget()
        print("    scroll {} viewport {} body {} (resizable {})".format(
            area.width(), area.viewport().width(),
            inner.width() if inner is not None else -1,
            area.widgetResizable()))
    pixmap = QPixmap(QSize(root.width(), root.height()))
    root.render(pixmap)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "panel-{}-{}.png".format(state, width))
    pixmap.save(out)

    # Which widget is DICTATING the width. A column that will not shrink is a
    # column some descendant refuses to shrink, and only the minimum size hint
    # says which one.
    def _widest(widget):
        out = []
        stack = [widget]
        while stack:
            current = stack.pop()
            out.append((current.minimumSizeHint().width(),
                        current.objectName() or type(current).__name__))
            stack.extend(
                child for child in current.children() if isinstance(child, QWidget))
        return out

    floor = sorted(_widest(root), reverse=True)[:6]
    print("  widest minimum size hints:")
    for hint, name in floor:
        print("    {:>5}  {}".format(hint, name))

    watched = (
        "connection_label", "status", "first_open_title",
        "first_open_prompt", "connect_button", "own_model", "tail",
        "save_settings_button", "provider_box", "api_key_input",
        "segment_bar", "tabs", "nivo_reply", "nivo_context", "nivo_runtime",
        "composer", "column",
    )
    print("\n=== {} ({}x{}) -> {}".format(state, width, height, os.path.basename(out)))
    print("  {:<20} {:>6} {:>6} {:>6} {:>6}  {}".format("widget", "x", "y", "w", "h", "shown"))
    for name in watched:
        widget = getattr(plugin, name, None)
        if widget is None:
            print("  {:<20} {:>6}".format(name, "absent"))
            continue
        point = widget.mapTo(root, widget.rect().topLeft())
        print("  {:<20} {:>6} {:>6} {:>6} {:>6}  {}".format(
            name, point.x(), point.y(), widget.width(), widget.height(),
            "yes" if widget.isVisible() else "NO"))
    return out


def panel_fits_and_centres():
    """The dock is usable at both ends of the width it can be dragged to.

    Measured, because this is exactly the class a component test cannot see:
    every widget existed and was correct while a 420 px dock rendered a 512 px
    column, so 92 px of every row - the Settings button included - was simply
    off-screen, and a 1,540 px dock left the column at x=0 with 820 px of
    nothing beside it.
    """
    plugin = need_plugin()
    root = plugin._panel_root
    column = plugin.column
    was_floating = plugin.dock.isFloating()
    plugin.dock.setFloating(True)
    # A hidden widget lays out lazily, so without this the scroll viewport kept
    # the width it was built at and the harness reported a 614 px column inside
    # a 420 px root - the instrument's number, not the panel's.
    plugin.dock.show()
    root.show()
    findings = []
    try:
        # 420 is the width the dock opens at and 1,540 is a dock dragged the
        # width of a monitor; both reproduce here and both match what the
        # founder photographed. 300 is NOT measured: the scroll chain does not
        # settle within the layout passes this harness can force, so it reports
        # a 300 px root around a 614 px column - a number from the instrument,
        # not from the panel. Narrower than 420 is unverified, and saying so is
        # better than a check that passes because it stopped asking.
        for width in (420, 1540):
            plugin.dock.resize(width, 900)
            root.resize(width, 900)
            for _pass in range(3):
                for widget in [root] + root.findChildren(QWidget):
                    layout = widget.layout()
                    if layout is not None:
                        layout.activate()
                QGS.processEvents()
            # Against the width the panel ACTUALLY took, never the width asked
            # for: a layout minimum can refuse a resize, and comparing against
            # the request then reports a refusal as an overflow.
            actual = root.width()
            left = column.mapTo(root, column.rect().topLeft()).x()
            right = actual - (left + column.width())
            if actual > width:
                findings.append("{}px refused, floor {}".format(width, actual))
                continue
            if left < 0 or right < 0:
                # Name the widget holding the floor open, or the failure says
                # only that something did and leaves the reader to grep.
                floor = []
                stack = [column]
                while stack:
                    current = stack.pop()
                    if current.isVisible() or current is column:
                        floor.append((current.minimumSizeHint().width(),
                                      current.objectName() or type(current).__name__))
                    stack.extend(c for c in current.children() if isinstance(c, QWidget))
                widest = "; ".join(
                    "{} needs {}".format(name, hint)
                    for hint, name in sorted(floor, reverse=True)[:4])
                raise AssertionError(
                    "at {} px the column runs off the dock: x={} w={} (overflow {}). {}"
                    .format(actual, left, column.width(), min(left, right), widest))
            if actual > READING_CAP + 40 and abs(left - right) > 4:
                raise AssertionError(
                    "at {} px the capped column is not centred: {} left, {} right"
                    .format(actual, left, right))
            findings.append("{}px -> col {} at x{}".format(actual, column.width(), left))
    finally:
        plugin.dock.setFloating(was_floating)
    return "; ".join(findings)


def need_plugin():
    if PLUGIN is None or PLUGIN.dock is None:
        raise NotRun("the plugin did not build its dock")
    return PLUGIN

if os.environ.get("MAPDEX_RENDER"):
    # The screen a stranger opens: no account, no provider, nothing chosen.
    # It has to be taken HERE, before a single check has touched anything.
    for _width in (1540, 420):
        render_panel("first-open", _width)
    # The state the founder objected to hardest: pressing "use my own AI model"
    # opened a nine-field form, and the two cards it belongs to were pushed
    # below it. Rendered separately because the fields are hidden until asked
    # for, so the first-open shot cannot show where they land.
    # `choose_own_model` was withdrawn with the signed-out own-model route, and
    # this line still calls it - so MAPDEX_RENDER died here before producing a
    # single PNG. A render mode that cannot render one state should skip that
    # state and say so, not take every other shot down with it.
    try:
        PLUGIN.choose_own_model()
    except AttributeError as _gone:
        print("skipped settings-open render:", _gone)
    else:
        for _width in (1540, 420):
            render_panel("settings-open", _width)
    PLUGIN.switch_page(0)
    # The first screen after connecting is a separate visual state: normal
    # conversation chrome plus the local reading, but no user turns yet. It is
    # where duplicate Nivo labels and a detached capability action previously
    # made the empty chat look like several unfinished components.
    PLUGIN.api.token = "render-session"
    PLUGIN._refresh_ui()
    for _width in (1540, 420):
        render_panel("connected-empty", _width)
    PLUGIN._say_capabilities()
    for _width in (1540, 420):
        render_panel("capability-discovery", _width)
    if os.environ.get("MAPDEX_RENDER") == "only":
        sys.exit(0)


def synchronous_tasks(plugin):
    """Run the plugin's background work inline, recording the state it starts in.

    `_task` puts work on the QGIS task manager. Inline is what lets a script
    observe the loading state the caller set immediately before handing over,
    which is otherwise a frame nobody can catch.
    """
    seen = []

    def run(_description, work, done, busy=True):
        refs = plugin._history_refs
        if refs is not None:
            # isVisibleTo, not isVisible: this snapshot is taken before the
            # dialog is shown, and what is being checked is the explicit
            # setVisible the loading state applied, not whether a window is up.
            dialog = refs["dialog"]
            seen.append({
                "state": refs["state_label"].text(),
                "state_visible": refs["state_label"].isVisibleTo(dialog),
                "list_visible": refs["list"].isVisibleTo(dialog),
                "open_enabled": refs["open_button"].isEnabled(),
            })
        try:
            result = work()
        except Exception as error:  # noqa: BLE001
            done(error, None)
        else:
            done(None, result)

    plugin._task = run
    return seen


TASK_STATES = synchronous_tasks(PLUGIN) if PLUGIN is not None else []


def transcript_cards(plugin):
    content = plugin.nivo_reply.widget()
    return widgets_of(content.layout())


def transcript_labels(plugin):
    content = plugin.nivo_reply.widget()
    return descendants(content, QLabel)


def replayed_markup_stays_text():
    """A `<b>` in replayed history must display as those characters.

    QLabel defaults to Qt::AutoText and renders anything that looks like HTML
    as HTML, so this is a live regression the moment a bubble is built without
    setTextFormat. Driven through the plugin's own renderer, not a copy of it.
    """
    plugin = need_plugin()
    raw = "<b>not bold</b> & <img src=x>"
    plugin._nivo_turns = [plugin_module.transcript_turn("user", "merhaba"),
                          plugin_module.transcript_turn("assistant", raw)]
    plugin._render_nivo_turns()
    plain = enum_member(Qt, "TextFormat", "PlainText")
    matches = [
        label for label in transcript_labels(plugin)
        if label.text() == raw and label.textFormat() == plain
    ]
    if not matches:
        shown = [label.text() for label in transcript_labels(plugin)]
        raise AssertionError("no PlainText label carries the raw markup; labels were {}".format(shown))
    return "rendered {!r} as characters, textFormat={}".format(raw[:26], plain)


check("replayed markup renders as text, not as HTML", replayed_markup_stays_text)


def new_chat_clears_the_transcript():
    """Pressed, not called: this goes through the signal `_ensure_dock` wired."""
    plugin = need_plugin()
    plugin._nivo_turns = [plugin_module.transcript_turn("user", "kaç parsel var"),
                          plugin_module.transcript_turn("assistant", "24")]
    plugin._render_nivo_turns()
    before = len(transcript_cards(plugin))
    if before < 2:
        raise AssertionError("the transcript did not render two turns; it had {}".format(before))
    plugin._adopt_conversation("th_seed")
    plugin.nivo_new_button.click()
    after = len(transcript_cards(plugin))
    if plugin._nivo_turns:
        raise AssertionError("{} turns survived New chat".format(len(plugin._nivo_turns)))
    # Not empty: clearing the conversation brings the opening reading back,
    # which is the point of keeping it apart from `_nivo_turns`. What must be
    # gone is the CONVERSATION, and what must return is a reading with no user
    # turn in it.
    if any(turn["sender"] == "user" for turn in plugin._nivo_opening):
        raise AssertionError("the reading came back carrying a user turn")
    if plugin._nivo_thread_id:
        raise AssertionError("New chat kept thread {}".format(plugin._nivo_thread_id))
    status = plugin.nivo_status.text()
    if "History" not in status:
        raise AssertionError("New chat did not say where the old conversation went: {!r}".format(status))
    return "{} widgets -> {}, thread dropped, status {!r}".format(before, after, status)


check("New chat clears the transcript and drops the thread", new_chat_clears_the_transcript)


def new_chat_refuses_mid_request():
    plugin = need_plugin()
    plugin._nivo_turns = [plugin_module.transcript_turn("user", "bekle"),
                          plugin_module.transcript_turn("assistant", "Thinking…")]
    plugin._render_nivo_turns()
    plugin._nivo_compose_task = object()
    try:
        plugin.nivo_new_button.click()
        if not plugin._nivo_turns:
            raise AssertionError("it cleared a transcript while a request was in flight")
        status = plugin.status.text()
        if "Stop" not in status:
            raise AssertionError("it did not explain the refusal: {!r}".format(status))
    finally:
        plugin._nivo_compose_task = None
    return status


check("New chat refuses while a request is in flight", new_chat_refuses_mid_request)


def history_needs_a_session():
    plugin = need_plugin()
    plugin.api.token = ""
    plugin.project_id = ""
    plugin._refresh_ui()
    enabled = plugin.nivo_history_button.isEnabled()
    plugin.nivo_history_button.click()
    if plugin._history_refs is not None:
        raise AssertionError("the dialog opened with no session")
    plugin.api.token = "tok_verify"  # nosec B105 - a stub, never a credential
    plugin.project_id = "proj_verify"
    plugin._refresh_ui()
    if not plugin.nivo_history_button.isEnabled():
        raise AssertionError("History stayed disabled after connecting")
    return "disabled without a session (enabled={}), enabled with one".format(enabled)


check("History is offered only with a session and a project", history_needs_a_session)


# The History dialog runs modally. These drive it by scheduling the interaction
# on the event loop the modal itself starts, then closing it - which is what a
# person does, in the order they would do it.

HISTORY = {}


def drive_history(interaction, watchdog_ms=5000):
    """Open History for real, run `interaction(refs)` inside it, then close."""
    plugin = need_plugin()
    outcome = {}

    def inside():
        refs = plugin._history_refs
        if refs is None:
            outcome["error"] = "the dialog did not register its widgets"
        else:
            try:
                outcome["detail"] = interaction(refs)
            except Exception as error:  # noqa: BLE001
                outcome["error"] = "{}: {}".format(type(error).__name__, error)
                if TRACE:
                    traceback.print_exc()
            refs["dialog"].reject()

    def watchdog():
        refs = plugin._history_refs
        if refs is not None:
            outcome.setdefault("error", "the dialog did not close on its own")
            refs["dialog"].reject()

    QTimer.singleShot(0, inside)
    QTimer.singleShot(watchdog_ms, watchdog)
    plugin.open_nivo_history()
    if "error" in outcome:
        raise AssertionError(outcome["error"])
    return outcome.get("detail", "")


def history_loading_state():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.threads = []
    del TASK_STATES[:]

    def inspect(refs):
        return refs["state_label"].text()

    drive_history(inspect)
    if not TASK_STATES:
        raise AssertionError("no background load was started, so there was no loading state")
    first = TASK_STATES[0]
    if "Loading" not in first["state"] or not first["state_visible"]:
        raise AssertionError("the loading state read {!r}".format(first["state"]))
    if first["list_visible"] or first["open_enabled"]:
        raise AssertionError("the loading state still offered a list to open")
    return "{!r}, list hidden, Open disabled".format(first["state"])


check("History shows a loading state before the rows arrive", history_loading_state)


def history_empty_state():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.threads = []

    def inspect(refs):
        text = refs["state_label"].text()
        if not refs["state_label"].isVisible() or refs["list"].isVisible():
            raise AssertionError("empty did not replace the list with an explanation")
        if refs["open_button"].isEnabled() or refs["delete_button"].isEnabled():
            raise AssertionError("empty still offered Open or Delete")
        if "No earlier conversations" not in text:
            raise AssertionError("empty state read {!r}".format(text))
        return text

    return drive_history(inspect)[:74]


check("History says it is empty rather than showing a blank box", history_empty_state)


def history_error_state():
    plugin = need_plugin()
    plugin.api.thread_error = MapdexAPIError("service unavailable", status=503)

    def inspect(refs):
        text = refs["state_label"].text()
        if not refs["retry_button"].isVisible():
            raise AssertionError("a failed load offered no retry")
        if refs["list"].isVisible():
            raise AssertionError("a failed load still showed a list")
        if "Could not load" not in text:
            raise AssertionError("the error state read {!r}".format(text))
        return "{!r} with {!r}".format(text[:56], refs["retry_button"].text())

    try:
        return drive_history(inspect)
    finally:
        plugin.api.thread_error = None


check("a failed History load says so and offers a retry", history_error_state)


def history_loaded_state():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.threads = [
        {"id": "th_2", "title": "En büyük parseli göster",
         "updated_at": "2026-08-17T11:32:04Z", "created_at": "2026-08-17T11:30:00Z"},
        {"id": "th_1", "title": "<b>not bold</b>", "message_count": 4,
         "updated_at": "2026-08-01T09:00:00+03:00", "created_at": "2026-08-01T08:00:00+03:00"},
    ]

    def inspect(refs):
        listing = refs["list"]
        if not listing.isVisible() or refs["state_label"].isVisible():
            raise AssertionError("the loaded state did not replace the message with the list")
        if listing.count() != 2:
            raise AssertionError("{} rows listed, want 2".format(listing.count()))
        if not refs["open_button"].isEnabled() or not refs["delete_button"].isEnabled():
            raise AssertionError("a selected row did not enable Open and Delete")
        markup_row = [listing.item(i).text() for i in range(2) if "<b>" in listing.item(i).text()]
        if not markup_row:
            raise AssertionError("a title's markup was interpreted: {}".format(
                [listing.item(i).text() for i in range(2)]))
        if "büyük" not in listing.item(0).text():
            raise AssertionError("Turkish did not survive the row: {!r}".format(listing.item(0).text()))
        HISTORY["rows"] = [listing.item(i).text() for i in range(2)]
        return "row0 {!r}; markup shown as text in row {!r}".format(
            listing.item(0).text()[:34], markup_row[0][:22])

    return drive_history(inspect)


check("History lists rows, selects one, and shows a title's markup as text",
      history_loaded_state)


def delete_asks_first_and_can_be_declined():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.deleted = []
    plugin.api.threads = [
        {"id": "th_9", "title": "Parselleri say", "updated_at": "2026-08-17T11:32:04Z"},
    ]
    MessageBoxStub.asked = []
    MessageBoxStub.answers = [MessageBoxStub.StandardButton.No]

    def inspect(refs):
        refs["list"].setCurrentRow(0)
        refs["delete_button"].click()
        if not MessageBoxStub.asked:
            raise AssertionError("Delete removed a conversation without asking")
        title, text = MessageBoxStub.asked[-1]
        if "Parselleri say" not in text or "cannot be undone" not in text:
            raise AssertionError("the confirmation did not name what it deletes: {!r}".format(text))
        if plugin.api.deleted:
            raise AssertionError("declining still deleted {}".format(plugin.api.deleted))
        return "{!r} / {!r}".format(title, text.replace("\n", " ")[:60])

    return drive_history(inspect)


check("Delete asks before it deletes, and No means no", delete_asks_first_and_can_be_declined)


def delete_confirmed_removes_and_reloads():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.deleted = []
    plugin.api.threads = [
        {"id": "th_9", "title": "Parselleri say", "updated_at": "2026-08-17T11:32:04Z"},
        {"id": "th_8", "title": "Alanı ölç", "updated_at": "2026-08-16T09:00:00Z"},
    ]
    MessageBoxStub.asked = []
    MessageBoxStub.answers = [MessageBoxStub.StandardButton.Yes]

    def inspect(refs):
        refs["list"].setCurrentRow(0)
        refs["delete_button"].click()
        if plugin.api.deleted != ["th_9"]:
            raise AssertionError("delete_thread was called with {}".format(plugin.api.deleted))
        if refs["list"].count() != 1:
            raise AssertionError("the list was not reloaded; it holds {} rows".format(
                refs["list"].count()))
        return "deleted th_9, list reloaded to {} row".format(refs["list"].count())

    return drive_history(inspect)


check("a confirmed delete removes the conversation and reloads the list",
      delete_confirmed_removes_and_reloads)


def opening_a_conversation_replays_it():
    plugin = need_plugin()
    plugin.api.thread_error = None
    plugin.api.threads = [
        {"id": "th_7", "title": "Önceki sohbet", "updated_at": "2026-08-17T11:32:04Z"},
    ]
    plugin.api.messages_payload = {"messages": [
        {"role": "user", "content": "kaç parsel var"},
        {"role": "assistant", "content": "<i>24</i>"},
    ]}
    plugin._nivo_turns = []
    plugin._render_nivo_turns()

    def inspect(refs):
        refs["list"].setCurrentRow(0)
        refs["open_button"].click()
        return "opened"

    drive_history(inspect)
    if len(plugin._nivo_turns) != 2:
        raise AssertionError("{} turns were replayed, want 2".format(len(plugin._nivo_turns)))
    if plugin._nivo_thread_id != "th_7":
        raise AssertionError("the panel is continuing {!r}".format(plugin._nivo_thread_id))
    literal = [label for label in transcript_labels(plugin) if label.text() == "<i>24</i>"]
    if not literal:
        raise AssertionError("the replayed message did not survive as text")
    return "2 turns replayed into the transcript, continuing th_7, markup kept literal"


check("opening a conversation replays it into the transcript",
      opening_a_conversation_replays_it)


# ==========================================================================
# Part 4: every advertised action, dispatched from a simulated compose response
# ==========================================================================
#
# The plugin tells the server what it can do (`supported_action_kinds` in the
# companion context). Anything on that list must either run or come back with a
# reason. An action accepted in words and discarded in fact is the failure the
# founder reported, and it is invisible from the server.

ADVERTISED = sorted(nivo_module.IMPLEMENTED_ACTIONS)

# Legacy `qgis:*` payloads, in the shape an older server sends them.
LEGACY_PAYLOADS = {
    "qgis:zoom_to_layer@1": ("target", {}),
    "qgis:zoom_to_selection@1": ("", {}),
    "qgis:zoom_to_extent@1": ("", {"bbox": [28.90, 40.95, 29.10, 41.10], "crs": "EPSG:4326"}),
    "qgis:set_layer_visibility@1": ("target", {"visible": False}),
    "qgis:set_layer_opacity@1": ("target", {"opacity": 55}),
    "qgis:open_attribute_table@1": ("target", {}),
    "qgis:open_processing@1": ("", {}),
    "qgis:inspect_layer@1": ("target", {}),
    "qgis:refresh_canvas@1": ("", {}),
    "qgis:previous_extent@1": ("", {}),
    "qgis:next_extent@1": ("", {}),
    "qgis:select_all@1": ("target", {}),
    "qgis:clear_selection@1": ("target", {}),
    "qgis:invert_selection@1": ("target", {}),
    "qgis:add_xyz_basemap@1": ("", {"provider": "osm"}),
    "qgis:create_layer@1": ("", {"geometry": "polygon", "crs": "EPSG:4326", "name": "Nivo scratch"}),
    "qgis:add_features@1": ("", {"geometry": "point", "count": 2, "area": "viewport"}),
    # Reaches the dispatcher only through the confirmation path; covered
    # separately below so the prompt itself is the thing being checked.
    "qgis:processing_operation@1": ("target", {"operation": "buffer", "distance": 10}),
}

# One plausible value per declared parameter name. Canonical payloads are
# derived from the registry rather than listed, so a capability added tomorrow
# is dispatched here without anyone remembering to extend a table.
PARAM_VALUES = {
    "layer_id": "LAYER",
    "other_layer_id": "LAYER",
    "polygon_layer_id": "LAYER",
    "point_layer_id": "LAYER",
    "field": "alan_m2",
    "group_field": "kullanim",
    "value_field": "alan_m2",
    "id_field": "parsel_no",
    "expression": "alan_m2 / 10000",
    "field_type": "number",
    "feature_ids": [1, 2, 3],
    "operator": "=",
    "value": "konut",
    "visible": True,
    "opacity": 60,
    "position": "bottom",
    "format": "geojson",
    "bbox": [28.90, 40.95, 29.10, 41.10],
    "crs": "EPSG:4326",
    "from_lon": 28.9784, "from_lat": 41.0082,
    "to_lon": 28.9784, "to_lat": 41.0172,
    "distance": 100,
    "unit": "m",
    "provider": "osm",
    "connection_id": "conn_verify",
    "schema": "public",
    "table": "parcels",
    "other_table": "roads",
    "operation": "numeric",
    "metric": "area",
    "objective": "buffer the selection",
    "workflow": "georeference_maps",
    "source_id": "file_verify",
    "source_ids": ["file_verify"],
    "run_id": "run_verify",
    "title": "Verification report",
    "predicate": "intersects",
    "classes": 4,
    "method": "quantile",
    "ramp": "Viridis",
    "bins": 6,
    "limit": 5,
    "size": 10,
    "enabled": True,
    "band": 1,
    "statistic": "sum",
    "scope": "all",
    "ascending": False,
    "state": "all",
    "x": 28.9784, "y": 41.0082, "srid": 4326,
    "stroke_width": 1, "color": "#5B5BD6", "segments": 8,
    # A shape drawn on the canvas. The vertices are a real square inside the
    # canvas extent set below, in the canvas CRS, so the sweep's dispatch of
    # draw.geometry@1 creates a layer that can be looked at afterwards.
    "geometry": "polygon",
    "vertices": [[28.92, 40.97], [29.08, 40.97], [29.08, 41.08], [28.92, 41.08]],
}

# Where the generic value would be wrong for that capability specifically.
PARAM_OVERRIDES = {
    "field.calculate@1": {"field": "nivo_hektar"},
    "layer.visibility@1": {"visible": True},
    "analytics.categories@1": {"field": "kullanim"},
    "style.categorized@1": {"field": "kullanim"},
    "style.labels@1": {"field": "parsel_no"},
    "filter.preview@1": {"field": "kullanim", "operator": "=", "value": "konut"},
}


def canonical_params(capability_id):
    capability = capabilities_module.get(capability_id)
    if capability is None:
        return {}
    overrides = PARAM_OVERRIDES.get(capability_id, {})
    params = {}
    for name, spec in capability.params.items():
        if name in overrides:
            params[name] = overrides[name]
            continue
        if not spec.get("required"):
            continue
        if name not in PARAM_VALUES:
            # A capability landed with a parameter this script has never seen.
            # That is worth saying out loud, and it is not a product failure:
            # reported as not run, with the one line needed to fix it.
            raise NotRun(
                "no verification value for the required parameter {!r}; add one to "
                "PARAM_VALUES so this capability is dispatched".format(name))
        value = PARAM_VALUES[name]
        params[name] = LAYER.id() if value == "LAYER" else value
    return params


DISPATCH = []
ACTION_SEQUENCE = [0]


def project_feature_total():
    total = 0
    for layer in PROJECT.mapLayers().values():
        if hasattr(layer, "featureCount"):
            try:
                total += int(layer.featureCount())
            except (TypeError, ValueError):
                pass
    return total


# The four legacy ids with no registered capability run a plugin method
# directly. Those methods change the host, not the transcript, so the sweep
# needs somewhere else to look before it can call one silent.
EFFECT_PROBES = {
    "qgis:open_processing@1": lambda: IFACE.processing_dialogs,
    "qgis:next_extent@1": lambda: CANVAS.extent().toString(6),
    "qgis:create_layer@1": lambda: len(PROJECT.mapLayers()),
    "qgis:add_features@1": lambda: (len(PROJECT.mapLayers()), project_feature_total()),
}


def select_a_few():
    LAYER.setSubsetString("")
    LAYER.selectByIds([1, 2, 3])
    # "Zoom to selection" reads the ACTIVE layer, and creating a scratch layer
    # earlier in the sweep made that the empty new one.
    IFACE.setActiveLayer(LAYER)


# Preconditions a real session would have and a sweep in alphabetical order
# would not. Without these the selection-scoped capabilities refuse for the
# want of a selection an earlier action happened to clear, which measures the
# order of this script rather than the product.
PRECONDITIONS = {
    "analytics.compare@1": select_a_few,
    "map.zoom_selection@1": select_a_few,
    "qgis:zoom_to_selection@1": select_a_few,
}

# A QgsMapCanvas records its extent history when a render job completes. There
# is no completed render job offscreen - verified: two setExtent calls followed
# by zoomToPreviousExtent leave the canvas exactly where it was, with the canvas
# shown and the event loop pumped. So "go forward" has nothing to go forward to
# here, and its outcome is reported as not run rather than as a defect.
UNOBSERVABLE = {
    "qgis:next_extent@1":
        "the canvas records no extent history offscreen, so there is no forward "
        "view to move to and no way to tell a working call from a no-op",
}


def dispatch(action_kind, target, params, summary="verification", probe=None):
    """Send one compose response through the real turn handler and classify it.

    Returns (verdict, evidence). `silent` is the verdict that matters: the
    plugin advertised the action, the server sent it, and afterwards there is
    no transcript line, no status message and no observable change.
    """
    plugin = need_plugin()
    ACTION_SEQUENCE[0] += 1
    action_id = "act_verify_{}".format(ACTION_SEQUENCE[0])
    response = {
        "text": "ok",
        "companion_actions": [{
            "kind": action_kind,
            "action_id": action_id,
            "target": target,
            "params": dict(params),
            "summary": summary,
        }],
    }
    admitted = bool(nivo_module.allowed_actions(response))
    plugin.status.setText("")
    plugin._nivo_turns = []
    plugin._nivo_state = "idle"
    plugin._nivo_request_id += 1
    before = probe() if probe is not None else None
    plugin._nivo_composed(plugin._nivo_request_id, None, {"thread_id": "", "response": response})
    after = probe() if probe is not None else None
    status = plugin.status.text()
    # The first turn is the reply text; anything beyond it is the outcome, and
    # the outcome says which kind it is.
    #
    # This used to read "a second line means it ran", which was true only while
    # success was the only thing that spoke. A refusal now states itself in the
    # transcript, because the reply above it has already claimed the result and
    # a reason that goes only to the status line is overwritten by the next
    # layer click. Counting lines classified every one of those refusals as an
    # execution - a degenerate polygon "executed", and a sweep in which nothing
    # was refused - so the harness read a product that had become MORE honest
    # as one that had stopped refusing.
    #
    # Severity is the right instrument: it is what the person sees, and it is
    # what DESIGN.md section 8 requires the panel to carry anyway.
    if len(plugin._nivo_turns) > 1:
        outcome = plugin._nivo_turns[-1]
        if str(outcome.get("severity") or "") in {"warning", "blocking"}:
            return "refused", outcome["text"]
        return "executed", outcome["text"]
    if probe is not None and before != after:
        return "executed", "no transcript line; observed effect {!r} -> {!r}".format(before, after)
    if not admitted:
        return "silent", "the protocol allowlist rejected it before dispatch"
    if status:
        return "refused", status
    if action_kind in UNOBSERVABLE:
        return "notrun", UNOBSERVABLE[action_kind]
    if probe is not None:
        return "silent", "admitted; no message and no change ({!r})".format(after)
    return "silent", "admitted, then neither executed nor explained"


def run_dispatch_sweep():
    if PLUGIN is None:
        return
    IFACE.setActiveLayer(LAYER)
    # Give the comparison capabilities something to compare, and give
    # "next extent" a forward history to move into, so neither is judged
    # silent for the want of a precondition a real session would have.
    LAYER.selectByIds([1, 2, 3])
    CANVAS.setExtent(QgsRectangle(28.90, 40.95, 29.10, 41.10))
    for action_kind in ADVERTISED:
        if action_kind == "qgis:processing_operation@1":
            continue  # confirmation path, checked on its own below
        try:
            setup = PRECONDITIONS.get(action_kind)
            if setup is not None:
                setup()
            if action_kind.startswith("qgis:"):
                target_kind, params = LEGACY_PAYLOADS[action_kind]
                target = LAYER.id() if target_kind == "target" else ""
            else:
                params = canonical_params(action_kind)
                target = LAYER.id() if "layer_id" in params else ""
            verdict, evidence = dispatch(
                action_kind, target, params, probe=EFFECT_PROBES.get(action_kind))
        except NotRun as reason:
            verdict, evidence = "notrun", str(reason)
        except Exception as error:  # noqa: BLE001
            verdict, evidence = "error", "{}: {}".format(type(error).__name__, error)
            if TRACE:
                traceback.print_exc()
        DISPATCH.append((action_kind, verdict, evidence))
    # The sweep leaves the fixture filtered, selected and restyled. Put it back
    # before anything measures it, or a later check reports the sweep's
    # leftovers as a product defect.
    LAYER.setSubsetString("")
    LAYER.removeSelection()
    IFACE.setActiveLayer(LAYER)


MessageBoxStub.answers = []
MessageBoxStub.default_answer = MessageBoxStub.StandardButton.Yes
MessageBoxStub.asked = []
run_dispatch_sweep()
CONFIRMATIONS_ASKED = list(MessageBoxStub.asked)


def advertised_set_is_what_it_claims():
    legacy = len(nivo_module.LEGACY_ACTIONS)
    bound = len(bound_capability_ids())
    declared = len(capabilities_module.all_capabilities())
    for_qgis = len(capabilities_module.for_client("qgis"))
    note("registry sizes",
         "{} declared, {} available to the QGIS client, {} bound to an executor".format(
             declared, for_qgis, bound))
    return "{} advertised = {} legacy qgis:* + {} bound capabilities".format(
        len(ADVERTISED), legacy, bound)


check("the advertised action set is legacy plus bound capabilities",
      advertised_set_is_what_it_claims)


def nothing_is_silently_dropped():
    if not DISPATCH:
        raise NotRun("the dispatch sweep did not run")
    silent = [(name, why) for name, verdict, why in DISPATCH if verdict in ("silent", "error")]
    counts = {}
    for _name, verdict, _why in DISPATCH:
        counts[verdict] = counts.get(verdict, 0) + 1
    summary = ", ".join("{} {}".format(count, verdict) for verdict, count in sorted(counts.items()))
    for name, verdict, why in DISPATCH:
        if verdict == "notrun":
            note("dispatch not verified: {}".format(name), why)
    if silent:
        raise AssertionError("{} advertised action(s) produced no effect and no message: {}".format(
            len(silent), "; ".join("{} ({})".format(n, w[:60]) for n, w in silent[:6])))
    return "{} of {} advertised actions: {}".format(len(DISPATCH), len(ADVERTISED), summary)


check("every advertised action either runs or refuses by name", nothing_is_silently_dropped)


def refusals_name_the_capability():
    """A refusal has to identify what was refused, or it is noise."""
    if not DISPATCH:
        raise NotRun("the dispatch sweep did not run")
    if not any(verdict == "refused" for _name, verdict, _why in DISPATCH):
        raise NotRun("nothing was refused in this sweep, so there is no refusal wording to judge")
    vague = [
        (name, why) for name, verdict, why in DISPATCH
        if verdict == "refused" and name.split("@")[0] not in why and "not available" not in why
        and "no layer" not in why and "nothing is selected" not in why
    ]
    if vague:
        raise AssertionError("{} refusal(s) did not say what was refused: {}".format(
            len(vague), vague[:4]))
    refused = [name for name, verdict, _why in DISPATCH if verdict == "refused"]
    return "{} refusals, each naming its capability or its missing precondition".format(len(refused))


check("the panel fits a narrow dock and centres in a wide one", panel_fits_and_centres)


check("each refusal names the capability or the missing precondition",
      refusals_name_the_capability)


# The high-value cases: not "it reported success", but "the project changed".

def selection_reached_the_layer():
    executed = dict((name, verdict) for name, verdict, _ in DISPATCH)
    if executed.get("qgis:select_all@1") != "executed":
        raise NotRun("select-all did not execute, so there is no selection to check")
    LAYER.removeSelection()
    dispatch("qgis:select_all@1", LAYER.id(), {})
    count = LAYER.selectedFeatureCount()
    if count != 24:
        raise AssertionError("{} features selected, want 24".format(count))
    dispatch("selection.by_ids@1", LAYER.id(), {"layer_id": LAYER.id(), "feature_ids": [1, 2, 3]})
    narrowed = LAYER.selectedFeatureCount()
    if narrowed != 3:
        raise AssertionError("selecting three ids left {} selected".format(narrowed))
    return "select-all -> 24 selected; select-by-ids -> 3 selected"


check("a selection command really selects features", selection_reached_the_layer)


def area_is_measured_from_the_geometry():
    """Ask the product for the area rather than reading the fixture.

    The failure this guards is answering an area question by summing a
    plausibly-named column. On this fixture the honest answer and the lazy one
    agree, so what is checked here is that the measurement runs off the geometry
    and lands on the right number.
    """
    if capabilities_module.get("analytics.geometry@1") is None:
        raise NotRun("this build has no geometry-measurement capability")
    got = need_runtime().measure_geometry(LAYER.id(), "area", "sum")
    value = got.get("value")
    if value is None:
        raise AssertionError("no value returned; keys were {}".format(sorted(got)[:12]))
    if not approx(value, 250000.0, 1.0):
        raise AssertionError("area sum is {}, want 250000".format(value))
    return "{} {} ({}, {})".format(value, got.get("unit"), got.get("method"), got.get("measured_in"))


check("total area is measured from the geometry and comes to 250,000",
      area_is_measured_from_the_geometry)


def the_area_answer_reaches_the_user():
    """A measurement the transcript does not state is a measurement nobody got."""
    if capabilities_module.get("analytics.geometry@1") is None:
        raise NotRun("this build has no geometry-measurement capability")
    verdict, evidence = dispatch(
        "analytics.geometry@1", LAYER.id(),
        {"layer_id": LAYER.id(), "metric": "area", "statistic": "sum"})
    if verdict != "executed":
        raise AssertionError("{}: {}".format(verdict, evidence))
    if "250,000" not in evidence and "250000" not in evidence:
        raise AssertionError(
            "the capability measured the area correctly, but the line the user reads states no "
            "number: {!r}. describe_capability_result has no describer for this result kind, so "
            "it falls back to the kind name.".format(evidence))
    return evidence[:88]


check("the area answer states its number in the transcript",
      the_area_answer_reaches_the_user)


def styling_really_restyled_the_layer():
    dispatch("style.categorized@1", LAYER.id(),
             {"layer_id": LAYER.id(), "field": "kullanim"})
    renderer = LAYER.renderer()
    name = type(renderer).__name__
    if "Categorized" not in name:
        raise AssertionError("the renderer is still {}".format(name))
    categories = len(renderer.categories())
    if categories < 3:
        raise AssertionError("{} categories drawn, want at least 3".format(categories))
    return "{} with {} categories".format(name, categories)


check("styling by a field really changes the renderer", styling_really_restyled_the_layer)


def labels_really_turned_on():
    dispatch("style.labels@1", LAYER.id(),
             {"layer_id": LAYER.id(), "field": "parsel_no", "size": 9, "enabled": True})
    if not LAYER.labelsEnabled():
        raise AssertionError("labelsEnabled() is still False")
    settings = LAYER.labeling().settings() if LAYER.labeling() is not None else None
    field = getattr(settings, "fieldName", "") if settings is not None else ""
    if field != "parsel_no":
        raise AssertionError("labels are drawn from {!r}".format(field))
    return "labels on, drawn from 'parsel_no'"


check("labelling a layer really turns labels on", labels_really_turned_on)


def opacity_really_changed():
    before = LAYER.opacity()
    dispatch("layer.opacity@1", LAYER.id(), {"layer_id": LAYER.id(), "opacity": 40})
    after = LAYER.opacity()
    if approx(after, before, 0.001):
        raise AssertionError("opacity stayed at {}".format(after))
    if not approx(after, 0.40, 0.001):
        raise AssertionError("opacity is {}, want 0.40".format(after))
    return "{:.2f} -> {:.2f}".format(before, after)


check("setting opacity really changes the layer", opacity_really_changed)


def zoom_really_moved_the_canvas():
    CANVAS.setExtent(QgsRectangle(0.0, 0.0, 1.0, 1.0))
    before = CANVAS.extent()
    dispatch("map.zoom_layer@1", LAYER.id(), {"layer_id": LAYER.id()})
    after = CANVAS.extent()
    if approx(after.xMinimum(), before.xMinimum(), 1e-9) and approx(
            after.yMinimum(), before.yMinimum(), 1e-9):
        raise AssertionError("the canvas did not move: still {}".format(before.toString(4)))
    # The layer is EPSG:32635 and the canvas EPSG:4326, so the destination
    # extent must be the reprojected one, not raw metres pasted onto degrees.
    if abs(after.xMinimum()) > 180 or abs(after.yMinimum()) > 90:
        raise AssertionError("the canvas was sent to projected metres: {}".format(after.toString(4)))
    return "canvas moved to {}".format(after.toString(3))


check("zoom to layer really moves the canvas, in the canvas CRS",
      zoom_really_moved_the_canvas)


def a_filter_really_filters_and_clears():
    dispatch("filter.preview@1", LAYER.id(),
             {"layer_id": LAYER.id(), "field": "kullanim", "operator": "=", "value": "konut"})
    subset = LAYER.subsetString()
    filtered = LAYER.featureCount()
    dispatch("filter.clear@1", LAYER.id(), {"layer_id": LAYER.id()})
    restored = LAYER.featureCount()
    if not subset:
        raise AssertionError("no subset string was applied")
    if filtered != 7:
        raise AssertionError("the filter left {} features, want 7 konut".format(filtered))
    if restored != 24:
        raise AssertionError("clearing the filter left {} features, want 24".format(restored))
    return "{!r} -> 7 features, cleared -> 24".format(subset)


check("a preview filter really filters, and clearing it restores the layer",
      a_filter_really_filters_and_clears)


def the_field_calculator_asked_first():
    """Consequential capabilities are gated by the registry, not by the model."""
    if not DISPATCH:
        raise NotRun("the dispatch sweep did not run")
    written = "nivo_hektar" in [field.name() for field in LAYER.fields()]
    asked = [text for _title, text in CONFIRMATIONS_ASKED if "cannot be undone" in text]
    if not asked:
        raise AssertionError("field.calculate@1 wrote without a confirmation prompt")
    if not written:
        raise AssertionError("the confirmation was shown but no column was written")
    return "{} confirmation(s) during the sweep; 'nivo_hektar' written after one".format(len(asked))


check("the field calculator asks before it writes into the user's data",
      the_field_calculator_asked_first)


def a_declined_confirmation_writes_nothing():
    before = [field.name() for field in LAYER.fields()]
    MessageBoxStub.answers = [MessageBoxStub.StandardButton.No]
    verdict, evidence = dispatch(
        "field.calculate@1", LAYER.id(),
        {"layer_id": LAYER.id(), "field": "nivo_declined", "expression": "alan_m2 * 2"})
    MessageBoxStub.answers = []
    after = [field.name() for field in LAYER.fields()]
    if "nivo_declined" in after:
        raise AssertionError("declining the confirmation still wrote the column")
    if before != after:
        raise AssertionError("the field list changed after a declined write")
    return "{}: {}".format(verdict, evidence[:70])


check("declining the confirmation writes nothing", a_declined_confirmation_writes_nothing)


def forbidden_capabilities_never_reach_dispatch():
    """`system.execute_code@1` and `system.execute_sql@1` are registry refusals."""
    outcomes = []
    for capability_id in ("system.execute_code@1", "system.execute_sql@1", "postgis.write@1"):
        capability = capabilities_module.get(capability_id)
        if capability is None:
            raise AssertionError("{} is not in the registry at all".format(capability_id))
        if capability.risk != capabilities_module.RISK_FORBIDDEN:
            raise AssertionError("{} is not marked forbidden".format(capability_id))
        if capability_id in nivo_module.IMPLEMENTED_ACTIONS:
            raise AssertionError("{} is advertised to the server".format(capability_id))
        try:
            capabilities_module.validate_request(capability_id, {})
        except capabilities_module.CapabilityError as error:
            outcomes.append("{} -> {}".format(capability_id.split("@")[0], str(error)[:40]))
        else:
            raise AssertionError("{} validated instead of refusing".format(capability_id))
    return "; ".join(outcomes)


check("forbidden capabilities are refused and never advertised",
      forbidden_capabilities_never_reach_dispatch)


def a_processing_operation_asks_before_running():
    """The confirmation-required path, taken as far as a script honestly can.

    Answered No: the prompt is the thing being checked. Whether the algorithm
    then produces the right buffer is a Processing question, not a dispatch one.
    """
    plugin = need_plugin()
    MessageBoxStub.asked = []
    MessageBoxStub.answers = [MessageBoxStub.StandardButton.No]
    ACTION_SEQUENCE[0] += 1
    response = {"text": "ok", "companion_actions": [{
        "kind": "qgis:processing_operation@1",
        "action_id": "act_confirm_{}".format(ACTION_SEQUENCE[0]),
        "confirmation_id": "conf_1",
        "idempotency_key": "idem_1",
        "requires_confirmation": True,
        "target": LAYER.id(),
        "params": {"operation": "buffer", "distance": 25, "segments": 8},
        "summary": "Buffer the parcels by 25 m",
    }]}
    offered = nivo_module.confirmation_actions(response)
    if not offered:
        raise AssertionError("the confirmation payload was rejected before it could be offered")
    plugin.status.setText("")
    plugin._nivo_turns = []
    plugin._nivo_state = "idle"
    plugin._nivo_request_id += 1
    plugin._nivo_composed(plugin._nivo_request_id, None, {"thread_id": "", "response": response})
    MessageBoxStub.answers = []
    if not MessageBoxStub.asked:
        raise AssertionError("a Processing run was dispatched with no confirmation")
    _title, text = MessageBoxStub.asked[-1]
    return "asked: {!r}".format(text.replace("\n", " ")[:76])


check("a Processing operation asks before it runs", a_processing_operation_asks_before_running)


def export_wrote_a_real_file():
    verdict, evidence = dispatch(
        "export.layer@1", LAYER.id(), {"layer_id": LAYER.id(), "format": "geojson"})
    if verdict != "executed":
        raise AssertionError("export {}: {}".format(verdict, evidence))
    written = need_runtime().export_layer(LAYER.id(), "geojson")
    path = written.get("path")
    if not path or not os.path.exists(path):
        raise AssertionError("reported {} but no file exists".format(path))
    return "{} ({} bytes, {} features)".format(
        os.path.basename(path), written.get("bytes"), written.get("features"))


check("export writes a file that exists on disk", export_wrote_a_real_file)


# --------------------------------------------------------------------------
# Drawing
#
# The one interaction where the user supplies geometry. Three questions, and
# only the first two can be answered here: does a validated request become a
# real layer, does an impossible shape get refused rather than stored, and does
# the canvas tool actually bind and convert clicks. The third is answered as far
# as offscreen allows - the tool is armed on the real canvas and real screen
# positions go through the real `toMapCoordinates` - but the release events are
# synthesized, so this does NOT prove that a human's click reaches the tool
# through Qt's own event delivery.
# --------------------------------------------------------------------------

class InputDialogStub:
    """Answers the geometry picker, and records that it was asked."""

    asked = []
    answer = ("Polygon", True)

    @classmethod
    def getItem(cls, _parent, title, label, items, *_args, **_kwargs):  # noqa: N802 - Qt naming
        cls.asked.append((title, label, list(items)))
        return cls.answer


plugin_module.QInputDialog = InputDialogStub


class ReleaseStub:
    """The two members `DrawMapTool.canvasReleaseEvent` reads off a Qt event."""

    def __init__(self, x, y, button):
        from qgis.PyQt.QtCore import QPoint

        self._pos = QPoint(int(x), int(y))
        self._button = button

    def pos(self):
        return self._pos

    def button(self):
        return self._button


def drawn_vertices_become_a_layer():
    before = set(PROJECT.mapLayers())
    verdict, evidence = dispatch("draw.geometry@1", "", {
        "geometry": "polygon",
        "vertices": [[28.92, 40.97], [29.08, 40.97], [29.08, 41.08], [28.92, 41.08]],
        "crs": "EPSG:4326",
        "name": "Verification AOI",
    })
    if verdict != "executed":
        raise AssertionError("draw {}: {}".format(verdict, evidence))
    created = [key for key in PROJECT.mapLayers() if key not in before]
    if len(created) != 1:
        raise AssertionError("expected one new layer, got {}".format(len(created)))
    layer = PROJECT.mapLayer(created[0])
    if layer.featureCount() != 1:
        raise AssertionError("the layer holds {} features".format(layer.featureCount()))
    geometry = next(layer.getFeatures()).geometry()
    if geometry.isEmpty() or not geometry.isGeosValid():
        raise AssertionError("the stored shape is not valid geometry")
    if layer.crs().authid() != "EPSG:4326":
        raise AssertionError(
            "the layer is in {}, not the CRS the vertices were drawn in".format(layer.crs().authid()))
    IFACE.setActiveLayer(LAYER)
    return "'{}' in {}, 1 valid polygon, area {:.4f} sq deg, and the line read: {}".format(
        layer.name(), layer.crs().authid(), geometry.area(), evidence)


check("a drawn shape becomes a real layer in the CRS it was drawn in",
      drawn_vertices_become_a_layer)


def a_shape_that_cannot_exist_is_refused():
    # Three clicks on one spot is a polygon QGIS will happily store and every
    # later measurement will report as zero area: a wrong answer with no error
    # in front of it. The executor has to refuse it even though the registry
    # sees three well-formed pairs of finite numbers.
    before = set(PROJECT.mapLayers())
    verdict, evidence = dispatch("draw.geometry@1", "", {
        "geometry": "polygon",
        "vertices": [[28.95, 41.00], [28.95, 41.00], [28.95, 41.00]],
        "crs": "EPSG:4326",
    })
    if verdict != "refused":
        raise AssertionError("a degenerate polygon was {}: {}".format(verdict, evidence))
    if set(PROJECT.mapLayers()) != before:
        raise AssertionError("the refused draw still left a layer behind")
    return evidence


check("a polygon with no distinct corners is refused, and leaves no layer",
      a_shape_that_cannot_exist_is_refused)


def the_draw_tool_arms_the_canvas_and_converts_clicks():
    from qgis.PyQt.QtCore import Qt

    from mapdex_qgis.maptools import DrawMapTool

    plugin = need_plugin()
    InputDialogStub.asked = []
    InputDialogStub.answer = ("Polygon", True)
    CANVAS.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    CANVAS.resize(400, 400)
    CANVAS.setExtent(QgsRectangle(28.90, 40.95, 29.10, 41.10))
    CANVAS.refresh()
    before = set(PROJECT.mapLayers())
    plugin._nivo_turns = []
    plugin._nivo_state = "idle"
    plugin.status.setText("")

    plugin._toggle_draw_tool(True)
    if not InputDialogStub.asked:
        raise AssertionError("the geometry picker was never shown")
    tool = CANVAS.mapTool()
    if not isinstance(tool, DrawMapTool):
        raise AssertionError("the canvas tool is {}, not the draw tool".format(type(tool).__name__))

    left = Qt.MouseButton.LeftButton
    right = Qt.MouseButton.RightButton
    for x, y in ((100, 300), (300, 300), (300, 100)):
        tool.canvasReleaseEvent(ReleaseStub(x, y, left))
    if len(tool.state.vertices) != 3:
        raise AssertionError("three clicks produced {} vertices".format(len(tool.state.vertices)))
    tool.canvasReleaseEvent(ReleaseStub(0, 0, right))

    created = [key for key in PROJECT.mapLayers() if key not in before]
    if len(created) != 1:
        raise AssertionError("finishing produced {} new layers".format(len(created)))
    layer = PROJECT.mapLayer(created[0])
    box = next(layer.getFeatures()).geometry().boundingBox()
    if not QgsRectangle(28.90, 40.95, 29.10, 41.10).contains(box):
        raise AssertionError(
            "the stored shape at {} is outside the canvas extent, so the screen "
            "positions were not converted through the canvas".format(box.toString(4)))
    # The tool must put itself away, or the user's next click on the map starts
    # a shape they did not ask for.
    if plugin._draw_tool is not None or isinstance(CANVAS.mapTool(), DrawMapTool):
        raise AssertionError("the draw tool is still armed after the shape finished")
    IFACE.setActiveLayer(LAYER)
    return ("armed on the real canvas; 3 synthesized clicks -> '{}' at {} "
            "(events synthesized: Qt event delivery is NOT proved)").format(
        layer.name(), box.toString(4))


check("the draw tool arms the canvas and turns clicked positions into a shape",
      the_draw_tool_arms_the_canvas_and_converts_clicks)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

print()

# --------------------------------------------------------------------------
# Toolbar lifecycle
# --------------------------------------------------------------------------


def toolbar_leaves_on_unload():
    """A toolbar the plugin never removes survives every reload."""
    import ast
    import pathlib

    source = (pathlib.Path(HERE) / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "unload":
            body = ast.get_source_segment(source, node) or ""
            if "toolbar" not in body:
                raise AssertionError("unload() does not remove the Mapdex toolbar")
            return "unload() removes the toolbar"
    raise NotRun("plugin.py has no unload()")


check("the toolbar is removed on unload", toolbar_leaves_on_unload)



# ==========================================================================
# Part N: the Jobs page — the batch, its sheets, and its buttons
# ==========================================================================

def _jobs_buttons():
    refs = PANEL.get("refs") or {}
    if not refs:
        raise NotRun("the panel did not build")
    names = (
        "retry_button", "import_button", "review_button",
        "cancel_button", "resume_button", "open_batch_button",
    )
    missing = [name for name in names if refs.get(name) is None]
    if missing:
        raise AssertionError("the Jobs page did not hand back: {}".format(missing))
    return {name: refs[name] for name in names}


def every_jobs_button_wears_mapdex_chrome():
    """No control on this page may fall back to QGIS's own button style.

    Three did. `review_button` carried no object name at all, and Cancel was a
    bare QToolButton, so the two controls that take a person out of the task
    were the two that did not look like the rest of the panel.
    """
    unstyled = [
        name for name, widget in _jobs_buttons().items()
        if not str(widget.objectName() or "").startswith("mapdex")
    ]
    if unstyled:
        raise AssertionError("no Mapdex object name on: {}".format(unstyled))
    return "6 Jobs controls, all styled by object name"


def exactly_one_filled_button_on_the_jobs_page():
    """The tier system means one thing on screen is filled. Prove it stays one."""
    filled = [
        name for name, widget in _jobs_buttons().items()
        if widget.objectName() == "mapdexPrimaryButton"
    ]
    if filled != ["import_button"]:
        raise AssertionError("primary tier claimed by: {}".format(filled))
    return "only 'Add result to QGIS' is filled"


def _drive_batch(items, counts=None, status="running"):
    plugin = need_plugin()
    if plugin.item_list is None:
        raise NotRun("this build has no batch item list")
    resolved = counts or {
        "total": len(items),
        "succeeded": sum(1 for i in items if i["state"] == "succeeded"),
        "needs_review": sum(1 for i in items if i["state"] == "needs_review"),
        "failed": sum(1 for i in items if i["state"] == "failed"),
    }
    plugin.api.token = "verify-session"
    plugin.batch_id = "batch_verify"
    plugin._last_batch = {"id": "batch_verify", "status": status,
                          "counts": resolved, "items": items}
    plugin._refresh_ui()
    return plugin


def _item_rows(plugin):
    from qgis.PyQt.QtWidgets import QFrame as _QFrame

    return [
        widget for widget in descendants(plugin.item_list, _QFrame)
        if widget.objectName() == "mapdexJobItemRow"
    ]


def the_batch_lists_its_sheets_attention_first():
    """Four sheets, four rows, and the one that failed is at the top.

    The ordering is decided in `job_items` and tested there without Qt. What
    this proves is the half that module cannot: that the rows were actually
    mounted, in that order, on a real panel.
    """
    plugin = _drive_batch([
        {"index": 0, "file_id": "f0", "state": "succeeded", "run_id": "run_0"},
        {"index": 1, "file_id": "f1", "state": "running", "run_id": "run_1"},
        {"index": 2, "file_id": "f2", "state": "failed", "run_id": "run_2",
         "error": {"code": "GEOREFERENCE_REQUIRED", "message": "This scan has no placement."}},
        {"index": 3, "file_id": "f3", "state": "needs_review", "run_id": "run_3"},
    ])
    rows = _item_rows(plugin)
    if len(rows) != 4:
        raise AssertionError("expected 4 rows, mounted {}".format(len(rows)))
    order = [str(row.property("itemState")) for row in rows]
    if order != ["failed", "needs_review", "running", "succeeded"]:
        raise AssertionError("rows mounted in the wrong order: {}".format(order))
    if not plugin.item_list.isVisibleTo(plugin.dock):
        raise AssertionError("the list was built but left hidden")
    return "4 rows: " + ", ".join(order)


def a_failed_sheet_says_why_on_its_own_row():
    plugin = _drive_batch([
        {"index": 0, "file_id": "f0", "state": "failed", "run_id": "run_0",
         "error": {"code": "GEOREFERENCE_REQUIRED", "message": "This scan has no placement."}},
        {"index": 1, "file_id": "f1", "state": "succeeded", "run_id": "run_1"},
    ])
    texts = [label.text() for label in descendants(plugin.item_list, QLabel)]
    if not any("no placement" in text for text in texts):
        raise AssertionError("the failure reason never reached the row: {}".format(texts))
    return "the reason is on the row, not behind a trip to the browser"


def the_sheet_name_the_user_chose_is_the_row_title():
    plugin = need_plugin()
    plugin._batch_file_names = {"f0": "CA_Cannell Peak_100592.tiff"}
    _drive_batch([{"index": 0, "file_id": "f0", "state": "failed", "run_id": "run_0"},
                  {"index": 1, "file_id": "f1", "state": "failed", "run_id": "run_1"}])
    # The label elides for the dock width, so the whole name lives on the
    # tooltip; checking the painted text would measure the font, not the wiring.
    tips = [label.toolTip() for label in descendants(plugin.item_list, QLabel)]
    plugin._batch_file_names = {}
    if "CA_Cannell Peak_100592.tiff" not in tips:
        raise AssertionError("the chosen file name never reached the row: {}".format(tips))
    return "rows are titled by the file the user picked"


def one_running_sheet_gets_no_list():
    """A single row reading "Running" under a bar that says so is noise.

    The list earns its space when there is more than one sheet, or when one
    sheet needs a person.
    """
    plugin = _drive_batch([{"index": 0, "file_id": "f0", "state": "running", "run_id": "run_0"}])
    rows = _item_rows(plugin)
    if rows:
        raise AssertionError("mounted {} row(s) for a single running sheet".format(len(rows)))
    return "no list for one sheet in flight"


def one_failed_sheet_does_get_a_list():
    plugin = _drive_batch(
        [{"index": 0, "file_id": "f0", "state": "failed", "run_id": "run_0",
          "error": {"message": "This scan has no placement."}}],
        status="completed",
    )
    if len(_item_rows(plugin)) != 1:
        raise AssertionError("a single failed sheet was not listed")
    return "one sheet, listed, because it needs a person"


def a_finished_sheet_offers_exactly_one_action():
    from qgis.PyQt.QtWidgets import QToolButton as _QToolButton

    plugin = _drive_batch([
        {"index": 0, "file_id": "f0", "state": "succeeded", "run_id": "run_0"},
        {"index": 1, "file_id": "f1", "state": "running", "run_id": "run_1"},
    ])
    actions = [
        button.text() for button in descendants(plugin.item_list, _QToolButton)
        if button.objectName() == "mapdexJobItemAction"
    ]
    # One button, on the finished sheet. The one still running offers nothing,
    # because a control that cannot act yet is one a person learns to distrust.
    if actions != ["Add"]:
        raise AssertionError("row actions were {}".format(actions))
    return "one action, on the sheet that has a result"


def opening_the_task_needs_a_real_batch():
    """The panel shows itself a placeholder batch while the upload is in flight.

    Offering "Open task in Mapdex" against it sends the user to a task the
    server has never heard of.
    """
    plugin = need_plugin()
    if plugin.open_batch_button is None:
        raise NotRun("this build has no open-task control")
    plugin.api.token = "verify-session"
    plugin.batch_id = "uploading"
    plugin._last_batch = {"status": "created", "counts": {"total": 1}}
    plugin._refresh_ui()
    during_upload = plugin.open_batch_button.isVisibleTo(plugin.dock)
    _drive_batch([{"index": 0, "file_id": "f0", "state": "failed", "run_id": "run_0"}],
                 status="completed")
    after = plugin.open_batch_button.isVisibleTo(plugin.dock)
    if during_upload:
        raise AssertionError("offered against the placeholder batch")
    if not after:
        raise AssertionError("not offered on a failed task, which is when it is wanted")
    return "hidden while uploading, offered once the task is real"


def settings_is_a_reachable_tab():
    """Four tabs, and the fourth one shows the settings form.

    It used to be a disclosure in the middle of the column, opened from a
    header control that named itself but not where it would appear.
    """
    refs = PANEL.get("refs") or {}
    if not refs:
        raise NotRun("the panel did not build")
    switch = refs.get("switch_page")
    page = refs.get("settings_page")
    panel_root = refs.get("column") or PANEL.get("root")
    if switch is None or page is None:
        raise AssertionError("the panel hands back no settings page")

    switch(0)
    if page.isVisibleTo(panel_root):
        raise AssertionError("the settings page is showing while Nivo is selected")
    switch(3)
    if not page.isVisibleTo(panel_root):
        raise AssertionError("the Settings tab does not show the settings form")
    # And the form itself, not an empty page.
    if refs["api_url_input"] not in descendants(page, type(refs["api_url_input"])):
        raise AssertionError("the settings page does not contain the connection fields")
    switch(0)
    return "Settings is page 3 and carries the connection fields"


def settings_has_exactly_one_way_in():
    """The tab, and nothing else.

    It used to also be a header control that expanded a panel in the column.
    Two controls for one destination is one more than this panel needs, and the
    header one was the worse of the two: it named itself but not where it would
    appear.
    """
    refs = PANEL.get("refs") or {}
    if not refs:
        raise NotRun("the panel did not build")
    if "settings_button" in refs:
        raise AssertionError("the header Settings control is back")
    from qgis.PyQt.QtWidgets import QToolButton as _QToolButton

    labels = [
        button.text() for button in descendants(PANEL["root"], _QToolButton)
        if button.text() == "Settings"
    ]
    # One: the tab. Two would mean the header control came back under another
    # name, which is the thing being removed rather than renamed.
    if len(labels) != 1:
        raise AssertionError("{} controls are labelled Settings".format(len(labels)))
    return "one way into Settings, and it is the tab"


def choosing_your_own_model_lands_on_the_settings_page():
    plugin = need_plugin()
    if plugin.switch_page is None:
        raise NotRun("this build has no page switcher")
    plugin.switch_page(0)
    plugin.choose_own_model()
    if plugin.tabs.currentIndex() != 3:
        raise AssertionError(
            "it landed on page {} instead of Settings".format(plugin.tabs.currentIndex())
        )
    if not plugin.provider_box.hasFocus():
        # Reported, not failed: focus needs an active window, which an
        # offscreen run may not have. The page it landed on is the claim.
        note("own model focuses the provider field", "no focus offscreen; the page is right")
    plugin.switch_page(0)
    return "it opens the Settings tab on the provider fields"


def the_demoted_connect_row_keeps_a_container():
    """Reported as "the background is broken": Connect Mapdex rendered as
    indigo text on the panel with no container, above a bordered card, while
    still saying "Recommended"."""
    refs = PANEL.get("refs") or {}
    connect = refs.get("connect_button")
    if connect is None or not hasattr(connect, "set_tone"):
        raise NotRun("this build has no rankable connect row")
    connect.set_tone("quiet")
    name = connect.objectName()
    connect.set_tone("primary")
    if name != "mapdexRowQuiet":
        raise AssertionError("the demoted row is {}".format(name))
    quiet = PANEL["root"].styleSheet()
    block = quiet[quiet.index("\n        QFrame#mapdexRowQuiet {"):]
    block = block[: block.index("}")]
    if "border: 1px solid" not in block:
        raise AssertionError("the demoted rank has no container")
    return "losing a rank loses the fill, not the object"


check("Settings is a reachable tab", settings_is_a_reachable_tab)
check("Settings has exactly one way in", settings_has_exactly_one_way_in)
check("choosing your own model lands on Settings", choosing_your_own_model_lands_on_the_settings_page)
check("the demoted Connect row keeps a container", the_demoted_connect_row_keeps_a_container)
check("every Jobs button wears Mapdex chrome", every_jobs_button_wears_mapdex_chrome)
check("exactly one filled button on the Jobs page", exactly_one_filled_button_on_the_jobs_page)
check("the batch lists its sheets, attention first", the_batch_lists_its_sheets_attention_first)
check("a failed sheet says why on its own row", a_failed_sheet_says_why_on_its_own_row)
check("the sheet name the user chose titles the row", the_sheet_name_the_user_chose_is_the_row_title)
check("one running sheet gets no list", one_running_sheet_gets_no_list)
check("one failed sheet does get a list", one_failed_sheet_does_get_a_list)
check("a finished sheet offers exactly one action", a_finished_sheet_offers_exactly_one_action)
check("opening the task needs a real batch", opening_the_task_needs_a_real_batch)


# --------------------------------------------------------------------------
# Part 5: the Task page - several sheets, from disk and from QGIS
# --------------------------------------------------------------------------
#
# The headless suite proves the list model (`test_task_sources.py`) and cannot
# prove that anything renders it: Qt is absent there, so a drop zone wired to
# nothing, a card grid that never emits, and a Start control that stays
# disabled all pass it. These press the real controls on a real dock.


from qgis.PyQt.QtWidgets import QToolButton as _TaskButton  # noqa: E402


def _task_page(plugin):
    plugin.switch_page(1)
    return plugin


def _clear_sources(plugin):
    plugin.selected_sources = []
    plugin._render_sources()


def _sheet(tmp_name, payload=b"II*\x00 raster bytes"):
    """A real file on disk. The list measures bytes, so it needs some."""
    path = os.path.join(SANDBOX, tmp_name)
    with open(path, "wb") as handle:
        handle.write(payload * 64)
    return path


SANDBOX = os.path.join(HERE, "task-page-sandbox")
os.makedirs(SANDBOX, exist_ok=True)


def the_four_workflows_are_cards():
    plugin = _task_page(need_plugin())
    chooser = plugin.workflow_box
    cards = descendants(chooser, _TaskButton)
    if len(cards) != 4:
        raise AssertionError("expected 4 workflow cards, found {}".format(len(cards)))
    checked = [card for card in cards if card.isChecked()]
    if len(checked) != 1:
        raise AssertionError(
            "exactly one card must be chosen; {} are".format(len(checked))
        )
    kinds = [chooser.itemData(index) for index in range(chooser.count())]
    return "{} cards: {}".format(len(cards), ", ".join(str(kind) for kind in kinds))


check("the four workflows are cards, one chosen", the_four_workflows_are_cards)


def pressing_a_card_changes_the_workflow():
    plugin = _task_page(need_plugin())
    chooser = plugin.workflow_box
    cards = descendants(chooser, _TaskButton)
    start = chooser.currentIndex()
    target = 1 if start != 1 else 2
    cards[target].click()
    if chooser.currentIndex() != target:
        raise AssertionError(
            "pressing a card did not move the selection: still {}".format(
                chooser.currentIndex())
        )
    moved = chooser.currentData()
    cards[start].click()
    return "pressed card {} -> {}, restored".format(target, moved)


check("pressing a workflow card really changes the workflow",
      pressing_a_card_changes_the_workflow)


def files_become_rows_the_user_can_read():
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    plugin._add_file_paths([_sheet("north.tif"), _sheet("south.tif")])
    named = [
        label.text()
        for label in descendants(plugin.source_list, QLabel)
        if label.objectName() == "mapdexSourceName"
    ]
    if "north.tif" not in named or "south.tif" not in named:
        raise AssertionError("the rows do not name the files: {}".format(named))
    if not plugin.source_list.isVisibleTo(plugin.workspace):
        raise AssertionError("the list was built but left hidden")
    return "{} sheets, named: {}".format(len(plugin.selected_sources), ", ".join(named))


check("chosen files become named rows, not a count",
      files_become_rows_the_user_can_read)


def two_sheets_say_batch_on_the_button():
    plugin = _task_page(need_plugin())
    # Left as the two files the previous check added.
    if len(plugin.selected_sources) != 2:
        raise AssertionError("expected 2 sheets, have {}".format(len(plugin.selected_sources)))
    label = plugin.run_button.text()
    if "2" not in label or "atch" not in label:
        raise AssertionError("the Start control does not say it starts a batch: {!r}".format(label))
    return label


check("two sheets make Start say it starts a batch", two_sheets_say_batch_on_the_button)


def the_same_file_twice_is_one_sheet():
    plugin = _task_page(need_plugin())
    before_count = len(plugin.selected_sources)
    plugin._add_file_paths([_sheet("north.tif")])
    if len(plugin.selected_sources) != before_count:
        raise AssertionError("a repeat was added again")
    said = plugin.status.text()
    if "lready" not in said:
        raise AssertionError("the repeat was swallowed silently: {!r}".format(said))
    return said


check("the same file twice is one sheet, and it is said",
      the_same_file_twice_is_one_sheet)


def removing_a_row_removes_the_sheet():
    plugin = _task_page(need_plugin())
    before_count = len(plugin.selected_sources)
    removers = [
        button
        for button in descendants(plugin.source_list, _TaskButton)
        if button.objectName() == "mapdexSourceRemove"
    ]
    if not removers:
        raise AssertionError("no row carries a remove control")
    removers[0].click()
    if len(plugin.selected_sources) != before_count - 1:
        raise AssertionError("pressing remove changed nothing")
    return "{} -> {} sheets".format(before_count, len(plugin.selected_sources))


check("pressing a row's remove really drops that sheet", removing_a_row_removes_the_sheet)


def start_agrees_with_the_summary():
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.api.token = "verification-session"
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    # A vector file under a raster workflow: the summary refuses it, so the
    # button must too.
    vector = os.path.join(SANDBOX, "parcels.geojson")
    with open(vector, "w", encoding="utf-8") as handle:
        handle.write("{}")
    plugin._add_file_paths([vector])
    plugin._refresh_ui()
    if plugin.run_button.isEnabled():
        raise AssertionError(
            "Start is offered over a source the summary refuses: {!r}".format(
                plugin.source_summary.text())
        )
    refused = plugin.source_summary.text()
    _clear_sources(plugin)
    plugin._add_file_paths([_sheet("ok.tif")])
    plugin._refresh_ui()
    if not plugin.run_button.isEnabled():
        raise AssertionError("Start stays refused over a compatible sheet")
    return "refused: {} | then enabled".format(refused[:60])


check("Start agrees with the sentence beside it", start_agrees_with_the_summary)


def an_open_layer_can_be_added_as_a_sheet():
    plugin = _task_page(need_plugin())
    layer = LAYER
    if not layer.isValid():
        raise NotRun("the fixture layer did not load")
    if True:
        plugin.workflow_box.setCurrentIndex(
            plugin.workflow_box.findData(BatchKind.VALIDATE_DELIVER))
        _clear_sources(plugin)
        entries = plugin._layer_picker_entries()
        mine = [entry for entry in entries if entry["id"] == layer.id()]
        if not mine:
            raise AssertionError("the open layer is not offered at all")
        if not mine[0]["eligible"]:
            raise AssertionError(
                "a vector layer is refused for validate_deliver: {}".format(mine[0]["reason"]))
        source = plugin._layer_source(layer.id())
        plugin._add_sources([source])
        if len(plugin.selected_sources) != 1:
            raise AssertionError("the layer did not reach the list")
        held = plugin.selected_sources[0]
        if held.kind != "layer" or held.key != layer.id():
            raise AssertionError("the sheet is not the layer: {!r}".format(held))
        return "{} added as a layer sheet ({})".format(held.label, held.detail)


check("an open QGIS layer can be added as a sheet", an_open_layer_can_be_added_as_a_sheet)


def an_incompatible_layer_is_shown_with_its_reason():
    plugin = _task_page(need_plugin())
    layer = LAYER
    if not layer.isValid():
        raise NotRun("the fixture layer did not load")
    if True:
        # A vector layer under a raster workflow: listed, not hidden, with the
        # reason on the row.
        plugin.workflow_box.setCurrentIndex(
            plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
        entries = plugin._layer_picker_entries()
        mine = [entry for entry in entries if entry["id"] == layer.id()]
        if not mine:
            raise AssertionError("an ineligible layer was filtered out of the picker")
        entry = mine[0]
        if entry["eligible"]:
            raise AssertionError("a vector layer was offered to a raster workflow")
        if not entry["reason"]:
            raise AssertionError("it is refused and does not say why")
        return "{}: {}".format(entry["name"], entry["reason"])


check("an unsuitable layer is listed with its reason, not hidden",
      an_incompatible_layer_is_shown_with_its_reason)


def options_reach_the_request():
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    # A PDF, because "every PDF page" is offered only over a list that has one
    # and this check is about the field reaching the wire, not about the gate.
    plugin._add_file_paths([_sheet("pages.pdf", b"%PDF-1.4 ")])
    plugin.expand_pages_check.setChecked(True)
    plugin.skip_completed_check.setChecked(True)
    try:
        chip = plugin.options.chip.text()
        if chip == "Default":
            raise AssertionError("the chip still says Default after two options changed")
        payload = plugin.task_options().payload()
        if payload.get("expand_pdf_pages") is not True:
            raise AssertionError("expand_pdf_pages does not reach the payload")
        if payload.get("skip_completed") is not True:
            raise AssertionError("skip_completed does not reach the payload")
        return "{} -> {}".format(chip, payload)
    finally:
        plugin.expand_pages_check.setChecked(False)
        plugin.skip_completed_check.setChecked(False)
        _clear_sources(plugin)


check("options reach the request and are named on the chip", options_reach_the_request)


def a_dropped_file_becomes_a_sheet():
    """The drop path, driven as a real Qt drop.

    This is the one route into the page that no click can reach, so nothing
    else here would notice a drop zone wired to a callback that was never
    assigned - which is precisely the shape of defect this harness exists for.
    """
    from qgis.PyQt.QtCore import QMimeData, QPointF, QUrl
    from qgis.PyQt.QtGui import QDropEvent

    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    dropped = _sheet("dropped.tif")

    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(dropped)])
    zone = plugin.drop_zone
    event = QDropEvent(
        QPointF(zone.width() / 2.0, zone.height() / 2.0),
        Qt.DropAction.CopyAction,
        data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    zone.dropEvent(event)

    labels = [source.label for source in plugin.selected_sources]
    if "dropped.tif" not in labels:
        raise AssertionError("the drop reached nothing: {}".format(labels))
    return "dropped.tif -> {} sheet(s)".format(len(plugin.selected_sources))


check("a file dropped on the zone becomes a sheet", a_dropped_file_becomes_a_sheet)


def a_drop_that_carries_no_file_is_ignored():
    """An in-app drag must not read as an upload.

    A QGIS layer dragged across the panel carries a mime payload too, and
    treating one as a file is the defect docs/UX.md names on the web side.
    """
    from qgis.PyQt.QtCore import QMimeData, QPointF
    from qgis.PyQt.QtGui import QDropEvent

    plugin = _task_page(need_plugin())
    before_count = len(plugin.selected_sources)
    data = QMimeData()
    data.setText("application/x-vnd.qgis.qgis.layertreemodeldata")
    event = QDropEvent(
        QPointF(1.0, 1.0),
        Qt.DropAction.CopyAction,
        data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    plugin.drop_zone.dropEvent(event)
    if len(plugin.selected_sources) != before_count:
        raise AssertionError("a drag carrying no file was taken as an upload")
    return "ignored, {} sheet(s) unchanged".format(before_count)


check("a drag carrying no local file is ignored", a_drop_that_carries_no_file_is_ignored)


def every_workflow_card_is_drawn_whole():
    """Both rows of the 2x2 grid, not one and a half.

    A card measured on its own is always the right height, so this is invisible
    to any component test: the defect was that the chooser's Fixed vertical
    policy handed the grid `sizeHint()`, a QToolButton hinted ~50 px for an icon
    over a label, and the second row was clipped through its text while each
    card still reported its 74 px minimum.
    """
    plugin = _task_page(need_plugin())
    root = plugin._panel_root
    plugin.dock.setFloating(True)
    plugin.dock.resize(460, 1100)
    root.resize(460, 1100)
    for _pass in range(3):
        for widget in [root] + root.findChildren(QWidget):
            layout = widget.layout()
            if layout is not None:
                layout.activate()
        QGS.processEvents()
    chooser = plugin.workflow_box
    cards = descendants(chooser, _TaskButton)
    # The decisive one, and the reason the first version of this check passed
    # over a visibly clipped render: card geometry can be entirely
    # self-consistent - cards at 0 and 74, each 74 tall - inside a widget that
    # was handed 141 px. Nothing about the cards is wrong there; the widget is
    # shorter than the layout it contains, and only the painted pixels say so.
    needed = chooser.minimumSizeHint().height()
    if chooser.height() < needed:
        raise AssertionError(
            "the chooser is {} px for a layout needing {} px, so its last row "
            "paints past its own bottom edge".format(chooser.height(), needed)
        )
    for card in cards:
        bottom = card.y() + card.height()
        if bottom > chooser.height():
            raise AssertionError(
                "card {!r} ends at {} px inside a {} px chooser, so it is clipped".format(
                    card.text(), bottom, chooser.height())
            )
        if card.height() < card.CARD_HEIGHT:
            raise AssertionError(
                "card {!r} is {} px tall, under its own {} px minimum".format(
                    card.text(), card.height(), card.CARD_HEIGHT)
            )
    rows = sorted({card.y() for card in cards})
    if len(rows) != 2:
        raise AssertionError("expected a 2x2 grid, found {} row(s)".format(len(rows)))
    # And it must not paint into whatever comes next. A card whose bottom edge
    # lands on the section label below it is 3 px of overlap that every geometry
    # number agrees with, because the two widgets are siblings and neither
    # knows about the other.
    below = plugin.source_list.parentWidget()
    for sibling in below.findChildren(QLabel):
        if sibling.text() != "Source":
            continue
        gap = sibling.mapTo(below, sibling.rect().topLeft()).y() - (
            chooser.mapTo(below, chooser.rect().bottomLeft()).y())
        if gap < 0:
            raise AssertionError(
                "the workflow cards overlap the Source label by {} px".format(-gap)
            )
        break
    return "{} cards over {} rows, chooser {} px tall".format(
        len(cards), len(rows), chooser.height())


check("every workflow card is drawn whole", every_workflow_card_is_drawn_whole)


def the_drop_hint_gets_real_width_rather_than_one_pixel():
    """The hint must not collapse to one word per line.

    `allow_narrow` floors every wrapping label at one pixel so no label can pin
    a minimum on the dock. A label with no stretch factor then RECEIVES one
    pixel, which is what rendered "Add more to run them as one batch" as eight
    stacked words. Measured on a laid-out dock, because the offscreen render
    made it look like a font artefact and it was not.
    """
    plugin = _task_page(need_plugin())
    root = plugin._panel_root
    plugin.dock.setFloating(True)
    plugin.dock.resize(460, 1100)
    root.resize(460, 1100)
    for _pass in range(3):
        for widget in [root] + root.findChildren(QWidget):
            layout = widget.layout()
            if layout is not None:
                layout.activate()
        QGS.processEvents()
    zone = plugin.drop_zone
    hint = zone.hint
    if hint.width() < zone.width() // 2:
        raise AssertionError(
            "the hint is {} px inside a {} px zone, so it wraps per word".format(
                hint.width(), zone.width())
        )
    # And the box stays a box: a hint that grew taller than its own zone is the
    # same defect measured from the other end.
    if hint.height() > zone.height():
        raise AssertionError("the hint is taller than the drop zone")
    return "hint {} px wide, {} px tall inside a {} px zone".format(
        hint.width(), hint.height(), zone.width())


check("the drop hint gets real width rather than one pixel",
      the_drop_hint_gets_real_width_rather_than_one_pixel)


def the_options_panel_is_separated_from_its_header():
    plugin = _task_page(need_plugin())
    options = plugin.options
    options.header.setChecked(True)
    QGS.processEvents()
    spacing = options.layout().spacing()
    if spacing < 4:
        raise AssertionError(
            "the options body sits {} px under its header, which reads as one "
            "clipped box".format(spacing)
        )
    margins = options.body.layout().contentsMargins()
    if margins.top() < 8 or margins.left() < 8:
        raise AssertionError("the options body has no internal padding")
    options.header.setChecked(False)
    return "header gap {} px, body padding {},{}".format(
        spacing, margins.left(), margins.top())


check("the options panel is separated from its header",
      the_options_panel_is_separated_from_its_header)


def the_estimate_states_what_the_batch_will_cost():
    """The figure comes from the server's own per-item charge, multiplied.

    Payload shape copied from a live GET /v1/plans on api.mapdex.ai, so this
    fails if the plugin starts reading a field the server does not send.
    """
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin._sheet_prices = task_price_module.read_sheet_prices({
        "sheet_prices": {"placement_cents": 200, "trace_cents": 500,
                         "trace_list_cents": 1800, "trace_beta": True},
        "batch_kinds": [
            {"kind": "georeference", "charge": {"placements": 1, "traces": 0}, "cents": 200},
            {"kind": "digitize_parcels", "charge": {"placements": 0, "traces": 1}, "cents": 500},
            {"kind": "validate_deliver", "charge": {"placements": 0, "traces": 0}, "cents": 0},
        ],
    })
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    plugin._add_file_paths([_sheet("p{}.tif".format(index)) for index in range(8)])
    line = plugin.price_label.text()
    # The rate is in the line, the TOTAL is on the control that spends it.
    if "$2 per sheet" not in line:
        raise AssertionError("the rate should be stated, got {!r}".format(line))
    if not plugin.price_label.isVisibleTo(plugin.workspace):
        raise AssertionError("the estimate was computed and left hidden")
    label = plugin.run_button.text()
    if "$16" not in label or "8 sheets" not in label:
        raise AssertionError(
            "Start should say what it starts and what it spends, got {!r}".format(label))

    # A workflow that bills nothing says so, rather than showing $0 or nothing.
    plugin.workflow_box.setCurrentIndex(
        plugin.workflow_box.findData(BatchKind.VALIDATE_DELIVER))
    free = plugin.price_label.text()
    if "No charge" not in free:
        raise AssertionError("a free workflow should say so, got {!r}".format(free))
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    return "{} | free: {}".format(line[:52], free)


check("the estimate states what the batch will cost",
      the_estimate_states_what_the_batch_will_cost)


def an_unpriced_server_shows_no_figure_at_all():
    """Silence, never $0.

    The plugin ships independently of the server it talks to, so a build
    against an older or newer /v1/plans must say nothing. Rendering an unknown
    price as zero is the one failure here that costs a customer money while
    looking entirely correct.
    """
    plugin = _task_page(need_plugin())
    kept = plugin._sheet_prices
    try:
        plugin._sheet_prices = {}
        plugin._render_price()
        line = plugin.price_label.text()
        if line:
            raise AssertionError("a price was invented with no server figure: {!r}".format(line))
        if plugin.price_label.isVisibleTo(plugin.workspace):
            raise AssertionError("an empty estimate is still on screen")
        # ...and the button says nothing about money either, rather than "· $0".
        if "$" in plugin.run_button.text():
            raise AssertionError(
                "a price reached the button with none published: {!r}".format(
                    plugin.run_button.text()))
        return "no server price -> no line, no $0"
    finally:
        plugin._sheet_prices = kept
        plugin._render_price()


check("an unpriced server shows no figure at all",
      an_unpriced_server_shows_no_figure_at_all)


def an_option_that_cannot_act_is_not_offered():
    """No PDF in the list, so no "every PDF page".

    Reported from a real session: the list held eight TIFFs, the chip read
    "Every PDF page", and the checkbox above it was ticked. Nothing failed -
    the server ignores a field it can do nothing with - which is exactly why it
    survived. Disabled and told why, rather than hidden, so somebody looking
    for the option they used last time can see where it went.
    """
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.expand_pages_check.setChecked(True)
    plugin._add_file_paths([_sheet("only-a-raster.tif")])
    if plugin.expand_pages_check.isEnabled():
        raise AssertionError("the PDF option is offered over a list with no PDF")
    if not plugin.expand_pages_check.toolTip():
        raise AssertionError("the option is refused with no reason given")
    if "PDF" in plugin.options.chip.text():
        raise AssertionError(
            "the chip names a file type nothing in the list has: {!r}".format(
                plugin.options.chip.text()))
    if plugin.task_options().payload().get("expand_pdf_pages"):
        raise AssertionError("a field the server can do nothing with reached the payload")

    # A PDF joins the list and the tick the user already made comes back.
    pdf = os.path.join(SANDBOX, "atlas.pdf")
    with open(pdf, "wb") as handle:
        handle.write(b"%PDF-1.4 not really a pdf")
    plugin._add_file_paths([pdf])
    if not plugin.expand_pages_check.isEnabled():
        raise AssertionError("the option stayed off with a PDF in the list")
    if not plugin.task_options().payload().get("expand_pdf_pages"):
        raise AssertionError("the stored tick did not come back")
    if "Every PDF page" not in plugin.options.chip.text():
        raise AssertionError("the chip does not name the option that is now on")
    plugin.expand_pages_check.setChecked(False)
    _clear_sources(plugin)
    return "offered only with a PDF present; the tick survives in between"


check("an option that cannot act is not offered",
      an_option_that_cannot_act_is_not_offered)


def a_balance_that_cannot_cover_the_batch_stops_it():
    """Blocked before the upload, with the numbers and the route out.

    Every sheet reserves its estimate as its own run starts, so a list the
    balance cannot cover does not fail cleanly: it runs until it stops, halfway
    through an archive the customer has already partly paid for. docs/UX.md 13
    asks for the block AND the top-up path, and a Start control that silently
    refuses to move is only the first half.
    """
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.api.token = "verification"  # noqa: S105 - a connected panel, not a session
    plugin._sheet_prices = task_price_module.read_sheet_prices({
        "sheet_prices": {"placement_cents": 200, "trace_beta": True},
        "batch_kinds": [
            {"kind": "georeference", "charge": {"placements": 1, "traces": 0}, "cents": 200},
        ],
    })
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    plugin._balance = task_price_module.read_balance({
        "credits_balance": 1000, "credits_reserved": 400,
        "credits_available": 600, "credits_enforced": True,
    })
    plugin._add_file_paths([_sheet("b{}.tif".format(index)) for index in range(8)])

    if plugin.run_button.isEnabled():
        raise AssertionError("Start is live over a batch the balance cannot pay for")
    notice = plugin.balance_notice.text()
    for figure in ("$6", "$16", "$10"):
        if figure not in notice:
            raise AssertionError(
                "the refusal does not state {}: {!r}".format(figure, notice))
    if not plugin.balance_row.isVisibleTo(plugin.workspace):
        raise AssertionError("the refusal was computed and left hidden")
    if not plugin.top_up_button.isVisibleTo(plugin.workspace):
        raise AssertionError("blocked with no route out")

    # Enough balance and the same list runs: the gate is the money, not the list.
    plugin._balance = task_price_module.read_balance({
        "credits_available": 5000, "credits_enforced": True})
    plugin._render_price()
    plugin._refresh_ui()
    if not plugin.run_button.isEnabled():
        raise AssertionError("a covered batch is still blocked")
    if plugin.balance_row.isVisibleTo(plugin.workspace):
        raise AssertionError("the refusal survived the balance that cleared it")

    # An unread balance blocks nothing. Refusing a customer who has the money
    # is the worse of the two errors, and the server still refuses at
    # reservation if we were wrong.
    plugin._balance = None
    plugin._render_price()
    plugin._refresh_ui()
    if not plugin.run_button.isEnabled():
        raise AssertionError("an unread balance was treated as an empty one")
    if plugin.balance_notice.text():
        raise AssertionError("a shortfall was claimed with no balance read")

    plugin.api.token = ""  # noqa: S105
    _clear_sources(plugin)
    plugin._refresh_ui()
    return "blocked at $6 against $16, cleared at $50, silent when unread"


check("a balance that cannot cover the batch stops it",
      a_balance_that_cannot_cover_the_batch_stops_it)


def a_batch_of_two_is_submitted_as_one_batch():
    plugin = _task_page(need_plugin())
    _clear_sources(plugin)
    plugin.api.token = "verification-session"
    plugin.workflow_box.setCurrentIndex(plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
    # One of the two is a PDF, so the page option is genuinely applicable and
    # this stays a check about what reaches `start_batch` rather than a second
    # check of the gate above it.
    plugin._add_file_paths([_sheet("one.tif"), _sheet("two.pdf", b"%PDF-1.4 ")])
    plugin.expand_pages_check.setChecked(True)

    sent = {}
    uploads = []

    def upload_file(path, project_id):
        uploads.append(path)
        return {"id": "file_{}".format(len(uploads))}

    def start_batch(project_id, file_ids, kind, options=None):
        sent["file_ids"] = list(file_ids)
        sent["kind"] = kind
        sent["options"] = options.payload() if options is not None else {}
        return {"id": "batch_verification", "status": "running",
                "counts": {"total": len(file_ids)}}

    plugin.api.upload_file = upload_file
    plugin.api.start_batch = start_batch
    plugin.project_box.clear()
    plugin.project_box.addItem("Verification project", "proj_verification")
    try:
        plugin.run_input()
    finally:
        plugin.expand_pages_check.setChecked(False)

    if len(uploads) != 2:
        raise AssertionError("expected 2 uploads, got {}".format(len(uploads)))
    if len(sent.get("file_ids") or []) != 2:
        raise AssertionError(
            "the batch did not carry both sheets: {!r}".format(sent.get("file_ids")))
    if sent.get("options", {}).get("expand_pdf_pages") is not True:
        raise AssertionError("the option did not reach start_batch: {!r}".format(sent.get("options")))
    return "2 uploads -> 1 batch, kind={}, options={}".format(
        sent.get("kind"), sent.get("options"))


check("two sheets are submitted as ONE batch, options included",
      a_batch_of_two_is_submitted_as_one_batch)


if os.environ.get("MAPDEX_RENDER") and PLUGIN is not None and PLUGIN.dock is not None:
    # The Task page, from the product's own dock, in the two states worth
    # looking at: several files, and a mixed list of a file plus an open QGIS
    # layer with the options panel open. Rendered AFTER the checks so the state
    # is one the checks have already proven, rather than one staged beside them.
    def _render_task_page():
        plugin = PLUGIN
        plugin.switch_page(1)
        plugin.api.token = "render-session"
        # The checks above left a submitted batch in flight, and Start is
        # correctly disabled while one is. A shot of that state would show a
        # dead button and read as the page being broken, so the session is put
        # back to idle first - which is the state this render is about.
        plugin.batch_id = ""
        plugin._last_batch = None
        plugin._busy = False
        plugin.project_box.clear()
        plugin.project_box.addItem("Default Project", "proj_render")

        plugin.selected_sources = []
        plugin._render_sources()
        plugin.workflow_box.setCurrentIndex(
            plugin.workflow_box.findData(BatchKind.GEOREFERENCE))
        sheets = []
        for name, size in (("cadastre_1943_sheet_04.tif", 900_000),
                           ("plan_1889_north.tif", 420_000)):
            path = os.path.join(SANDBOX, name)
            with open(path, "wb") as handle:
                handle.write(b"II*\x00" * (size // 4))
            sheets.append(path)
        plugin._add_file_paths(sheets)
        # A published price, so the shot shows the figure on the control that
        # spends it rather than a bare "Start batch".
        plugin._sheet_prices = task_price_module.read_sheet_prices({
            "sheet_prices": {"placement_cents": 200, "trace_beta": True},
            "batch_kinds": [
                {"kind": "georeference", "charge": {"placements": 1, "traces": 0},
                 "cents": 200},
            ],
        })
        plugin._balance = task_price_module.read_balance(
            {"credits_available": 9000, "credits_enforced": True})
        plugin._render_price()
        plugin._refresh_ui()
        render_panel("task-files", 460, 1180)
        render_panel("task-files", 900, 1180)

        # And the state this page can now refuse in: the balance will not cover
        # the list, Start is dead, and the reason and the route out are beside
        # it. Worth a render because a disabled control with nothing next to it
        # is exactly the failure the row exists to prevent.
        plugin._balance = task_price_module.read_balance(
            {"credits_available": 100, "credits_enforced": True})
        plugin._render_price()
        plugin._refresh_ui()
        render_panel("task-blocked", 460, 1180)
        plugin._balance = None
        plugin._render_price()

        # The shape the old page could not express at all: a file and an open
        # QGIS layer in one batch.
        plugin.selected_sources = []
        plugin._render_sources()
        plugin.workflow_box.setCurrentIndex(
            plugin.workflow_box.findData(BatchKind.VALIDATE_DELIVER))
        vector = os.path.join(SANDBOX, "parcels_batch_02.gpkg")
        with open(vector, "wb") as handle:
            handle.write(b"SQLite format 3\x00" * 4000)
        plugin._add_file_paths([vector])
        if LAYER.isValid():
            plugin._add_sources([plugin._layer_source(LAYER.id())])
        # `skip_completed`, not the PDF option: this list holds no PDF, and a
        # render staging an option the page correctly refuses to offer would be
        # a picture of the defect rather than of the product.
        plugin.skip_completed_check.setChecked(True)
        plugin.options.header.setChecked(True)
        plugin._refresh_ui()
        render_panel("task-mixed", 460, 1180)
    try:
        _render_task_page()
    except Exception as _error:  # noqa: BLE001
        print("task page render failed:", type(_error).__name__, _error)


print("=" * 96)
print("DISPATCH SWEEP — every action this build advertises to the server")
print("-" * 96)
for name, verdict, evidence in DISPATCH:
    print("  {:<9} {:<32} {}".format(verdict, name, str(evidence).replace("\n", " ")[:110]))
print("-" * 96)
print()
print("=" * 96)
counts = {"PASS": 0, "FAIL": 0, "NOTRUN": 0, "NOTE": 0}
for status, name, detail in RESULTS:
    counts[status] = counts.get(status, 0) + 1
    print("{:<6} {:<62} {}".format(status, name, str(detail).replace("\n", " ")[:150]))
print("=" * 96)
ran = counts["PASS"] + counts["FAIL"] + counts["NOTRUN"]
print("{} checks ran: {} passed, {} failed, {} not run. {}".format(
    ran, counts["PASS"], counts["FAIL"], counts["NOTRUN"], VERSIONS))

if PLUGIN is not None:
    try:
        PLUGIN.unload()
    except Exception as error:  # noqa: BLE001
        print("unload raised:", error)

QGS.exitQgis()
sys.exit(0 if counts["FAIL"] == 0 else 1)
