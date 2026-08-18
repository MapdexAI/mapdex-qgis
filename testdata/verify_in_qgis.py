"""Run the Mapdex QGIS runtime against real QGIS 4 / PyQt6, headless.

Screen capture cannot see the QGIS window on this machine, so the GUI cannot be
driven. That is a smaller loss than it sounds: the risk that unit tests could
not touch was never the pixels, it was that the runtime is exercised against
*stubs*. PyQt6 removed the flat enum forms PyQt5 accepted, and a wrong one
raises AttributeError at the moment a code path first runs, which is exactly
what a double will never reproduce.

This loads the real libraries, the real plugin source, and a fixture whose
answers are known, and reports each check as PASS or FAIL with the numbers.
"""
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# A pristine copy per run. The field calculator writes real columns into a real
# GeoPackage, so a second run against the same file hits its own
# refuse-to-overwrite guard and reports a pass as a failure.
import shutil as _shutil  # noqa: E402

_PRISTINE = os.path.join(HERE, "mapdex-test-parcels.gpkg")
FIXTURE = os.path.join(HERE, "_run-parcels.gpkg")
if os.path.exists(FIXTURE):
    os.remove(FIXTURE)
_shutil.copyfile(_PRISTINE, FIXTURE)

results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(("PASS", name, detail or ""))
    except Exception as error:  # noqa: BLE001 - a harness reports, it does not raise
        results.append(("FAIL", name, "{}: {}".format(type(error).__name__, error)))
        if os.environ.get("MAPDEX_VERIFY_TRACE"):
            traceback.print_exc()


# --------------------------------------------------------------------------
# 0. QGIS itself
# --------------------------------------------------------------------------
from qgis.core import QgsApplication, QgsVectorLayer, Qgis  # noqa: E402

QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "C:/Program Files/QGIS 4.0.2/apps/qgis"), True)
QGS = QgsApplication([], False)
QGS.initQgis()

print("QGIS version:", Qgis.QGIS_VERSION)
try:
    from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
    print("Qt:", QT_VERSION_STR, "PyQt:", PYQT_VERSION_STR)
except Exception as error:  # noqa: BLE001
    print("Qt version unavailable:", error)
print()


# --------------------------------------------------------------------------
# 1. Import every plugin module. This alone is the Qt6 smoke test.
# --------------------------------------------------------------------------
def import_all():
    import importlib
    import pkgutil
    import mapdex_qgis

    failed = []
    imported = 0
    for module in pkgutil.iter_modules(mapdex_qgis.__path__):
        name = "mapdex_qgis." + module.name
        try:
            importlib.import_module(name)
            imported += 1
        except Exception as error:  # noqa: BLE001
            failed.append("{}: {}".format(module.name, error))
    if failed:
        raise AssertionError("{} module(s) failed to import: {}".format(len(failed), "; ".join(failed[:5])))
    return "{} modules imported".format(imported)


check("every plugin module imports under PyQt6", import_all)


# --------------------------------------------------------------------------
# 2. qt_compat.field_type — written blind against Qt6 and never executed there
# --------------------------------------------------------------------------
def field_types():
    from mapdex_qgis.qt_compat import field_type
    resolved = {kind: field_type(kind) for kind in ("number", "integer", "text")}
    if any(value is None for value in resolved.values()):
        raise AssertionError("unresolved: {}".format(resolved))
    return ", ".join("{}={}".format(k, v) for k, v in resolved.items())


check("qt_compat.field_type resolves on this binding", field_types)


def writer_enum():
    from qgis.core import QgsVectorFileWriter
    from mapdex_qgis.qt_compat import enum_member
    return "NoError={}".format(enum_member(QgsVectorFileWriter, "WriterError", "NoError"))


check("QgsVectorFileWriter.NoError resolves", writer_enum)


# --------------------------------------------------------------------------
# 3. The registry
# --------------------------------------------------------------------------
def registry():
    from mapdex_qgis import capabilities
    from mapdex_qgis.qgis_runtime import bound_capability_ids
    declared = len(capabilities.all_capabilities())
    bound = bound_capability_ids()
    missing = [c for c in ("field.calculate@1", "layer.reorder@1",
                           "measure.distance@1", "export.layer@1") if c not in bound]
    if missing:
        raise AssertionError("declared but not bound: {}".format(missing))
    return "{} declared, {} bound to an executor".format(declared, len(bound))


check("capability registry loads and this session's work is bound", registry)


# --------------------------------------------------------------------------
# 4. The fixture, whose answers are known
# --------------------------------------------------------------------------
LAYER = QgsVectorLayer(FIXTURE + "|layername=parcels", "parcels", "ogr")


def fixture_loads():
    if not LAYER.isValid():
        raise AssertionError("layer did not load from " + FIXTURE)
    return "{} features, CRS {}".format(LAYER.featureCount(), LAYER.crs().authid())


check("fixture loads in real QGIS", fixture_loads)


class Iface:
    """The runtime touches iface only for the canvas; headless needs a stand-in."""

    class _Canvas:
        def refresh(self):
            return None

        def mapSettings(self):
            return None

    def __init__(self):
        self._canvas = self._Canvas()

    def mapCanvas(self):
        return self._canvas


def runtime():
    from qgis.core import QgsProject
    from mapdex_qgis.qgis_runtime import QGISRuntime
    project = QgsProject.instance()
    project.addMapLayer(LAYER)
    return QGISRuntime(Iface(), project)


RUNTIME = None
try:
    RUNTIME = runtime()
except Exception as error:  # noqa: BLE001
    results.append(("FAIL", "construct the runtime", str(error)))


def layer_id():
    return LAYER.id()


# --------------------------------------------------------------------------
# 5. Analytics against known answers
# --------------------------------------------------------------------------
def profile():
    got = RUNTIME.profile_layer(layer_id())
    count = got.get("feature_count") or got.get("count")
    if count != 24:
        raise AssertionError("feature_count={}, want 24 ({})".format(count, sorted(got)[:8]))
    return "feature_count=24"


check("profile reports 24 features", profile)


def numeric_area():
    got = RUNTIME.numeric(layer_id(), "alan_m2")
    stats = got.get("statistics") or got
    mean = stats.get("mean")
    if mean is None or abs(mean - 10416.667) > 0.01:
        raise AssertionError("mean={}, want 10416.667 (keys {})".format(mean, sorted(stats)[:10]))
    return "mean area = {}".format(mean)


check("mean area is 10,416.667", numeric_area)


def numeric_population():
    # The trap: one parcel has no value. 9125/23 = 396.739 is right,
    # 9125/24 = 380.2 means a null was counted as zero.
    got = RUNTIME.numeric(layer_id(), "nufus")
    stats = got.get("statistics") or got
    mean = stats.get("mean")
    if mean is None:
        raise AssertionError("no mean returned; keys {}".format(sorted(stats)[:10]))
    if abs(mean - 380.2) < 0.5:
        raise AssertionError("mean={} — a null was counted as zero".format(mean))
    if abs(mean - 396.739) > 0.01:
        raise AssertionError("mean={}, want 396.739".format(mean))
    return "mean population = {} (nulls excluded, correctly)".format(mean)


check("mean population excludes the null rather than zeroing it", numeric_population)


def categories():
    # The trap: two of the 24 parcels have no usable category (one null, one
    # empty string). An answer that reports only the three buckets has quietly
    # dropped two records. The total and the null count must both survive.
    got = RUNTIME.categories(layer_id(), "kullanim")
    payload = got.get("analysis") or got
    total = payload.get("count")
    nulls = payload.get("nulls")
    usable = payload.get("usable")
    buckets = payload.get("categories") or []
    if total != 24:
        raise AssertionError("count={}, want 24 — records were dropped".format(total))
    if nulls != 2:
        raise AssertionError("nulls={}, want 2".format(nulls))
    if usable != 22:
        raise AssertionError("usable={}, want 22".format(usable))
    if len(buckets) != 3:
        raise AssertionError("{} buckets, want 3".format(len(buckets)))
    return "24 total, 22 usable, 2 nulls reported, 3 categories"


check("category breakdown keeps the 2 unusable records visible", categories)


# --------------------------------------------------------------------------
# 6. The field calculator, including its refusals
# --------------------------------------------------------------------------
def calculate():
    got = RUNTIME.calculate_field(layer_id(), "hektar", "alan_m2 / 10000")
    if got.get("rows") != 24:
        raise AssertionError("rows={}, want 24".format(got.get("rows")))
    return "wrote {} rows, nulls={}".format(got.get("rows"), got.get("nulls"))


check("field calculator writes a computed column", calculate)


def refuse_overwrite():
    try:
        RUNTIME.calculate_field(layer_id(), "alan_m2", "1")
    except Exception as error:  # noqa: BLE001
        if "already exists" in str(error):
            return "refused: {}".format(error)
        raise AssertionError("refused for the wrong reason: {}".format(error))
    raise AssertionError("it overwrote an existing column")


check("field calculator refuses to overwrite an existing column", refuse_overwrite)


def refuse_code():
    try:
        RUNTIME.calculate_field(layer_id(), "evil", "__import__('os')")
    except Exception as error:  # noqa: BLE001
        return "refused: {}".format(str(error)[:90])
    raise AssertionError("it accepted Python source as an expression")


check("field calculator refuses Python source", refuse_code)


def turkish_field():
    got = RUNTIME.calculate_field(layer_id(), "nüfus_katı", "nufus * 2")
    names = [f.name() for f in LAYER.fields()]
    if "nüfus_katı" not in names:
        raise AssertionError("the Turkish column name did not survive: {}".format(names))
    return "wrote {} rows into 'nüfus_katı'".format(got.get("rows"))


check("a Turkish column name survives intact", turkish_field)


# --------------------------------------------------------------------------
# 7. Reorder, export, measure
# --------------------------------------------------------------------------
def reorder():
    got = RUNTIME.reorder_layer(layer_id(), "bottom")
    return str(got)


check("layer reorder runs", reorder)


def export_layer():
    got = RUNTIME.export_layer(layer_id(), "geojson")
    path = got.get("path")
    if not path or not os.path.exists(path):
        raise AssertionError("reported {} but no file exists".format(path))
    return "{} ({} bytes, {} features)".format(path, got.get("bytes"), got.get("features"))


check("export writes a file that actually exists", export_layer)


def measure():
    # P-001 bottom-left to bottom-right is exactly 100 m in EPSG:32635.
    # The runtime converts to WGS84 first, so this is where a conversion fault
    # produces a plausible wrong number.
    from mapdex_qgis import spatial
    got = spatial.measure_distance((28.9784, 41.0082), (28.9784, 41.0172), True, "degrees")
    return "spherical 0.009 deg lat -> {}".format(str(got)[:160])


check("distance measurement returns a value", measure)


# --------------------------------------------------------------------------
print()
print("=" * 72)
passed = sum(1 for r in results if r[0] == "PASS")
for status, name, detail in results:
    print("{:4}  {:<58} {}".format(status, name, detail))
print("=" * 72)
print("{} passed, {} failed, of {}".format(passed, len(results) - passed, len(results)))

QGS.exitQgis()
sys.exit(0 if passed == len(results) else 1)
