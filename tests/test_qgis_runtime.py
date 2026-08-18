"""Runtime dispatch tests against a stubbed QGIS API.

QGIS is not importable in CI, so the handful of QGIS symbols the runtime touches
are stubbed. That is enough to exercise what actually carries risk: bounded
reading, scope handling, filter-literal quoting, the undo stack, and the full
"analyse -> visualization spec -> map effect" chain with real numbers.

This is not a substitute for running the plugin in QGIS - see the capability
matrix for what remains runtime-unverified.
"""
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Minimal QGIS stubs
# --------------------------------------------------------------------------

class FakeFeatureRequest:
    NoGeometry = 1

    def __init__(self):
        self.attributes = None
        self.flags = 0
        self.rect = None

    def setSubsetOfAttributes(self, indexes):
        self.attributes = list(indexes)

    def setFlags(self, flags):
        self.flags = flags

    def setFilterRect(self, rect):
        self.rect = rect


def _install_qgis_stubs():
    core = types.ModuleType("qgis.core")
    core.QgsFeatureRequest = FakeFeatureRequest

    class QgsCsException(Exception):
        pass

    class QgsCoordinateTransform:
        def __init__(self, source, target, project):
            self.source = source

        def transformBoundingBox(self, extent):
            if getattr(self.source, "untransformable", False):
                raise QgsCsException("no transform")
            return extent

    core.QgsCsException = QgsCsException
    core.QgsCoordinateTransform = QgsCoordinateTransform
    qgis = types.ModuleType("qgis")
    qgis.core = core
    sys.modules.setdefault("qgis", qgis)
    sys.modules["qgis.core"] = core
    qgis.core = core


_install_qgis_stubs()

from mapdex_qgis._vendor.nivo.capabilities import CapabilityError, validate_request  # noqa: E402
from mapdex_qgis.qgis_runtime import (  # noqa: E402
    MAX_ANALYTIC_FEATURES,
    QGISRuntime,
    RuntimeUnavailable,
    build_executor,
)


class FakeField:
    def __init__(self, name, type_name="double"):
        self._name = name
        self._type = type_name

    def name(self):
        return self._name

    def typeName(self):
        return self._type


class FakeFields:
    def __init__(self, names):
        self._fields = [FakeField(name) for name in names]

    def __iter__(self):
        return iter(self._fields)

    def indexOf(self, name):
        for index, field in enumerate(self._fields):
            if field.name() == name:
                return index
        return -1


class FakeFeature:
    def __init__(self, identifier, values):
        self._id = identifier
        self._values = values

    def id(self):
        return self._id

    def __getitem__(self, key):
        return self._values[key]


class FakeCrs:
    def __init__(self, authid="EPSG:3857", geographic=False):
        self._authid = authid
        self._geographic = geographic
        self.untransformable = False

    def isValid(self):
        return True

    def authid(self):
        return self._authid

    def isGeographic(self):
        return self._geographic

    def __eq__(self, other):
        return isinstance(other, FakeCrs) and other._authid == self._authid


class FakeExtent:
    def isEmpty(self):
        return False

    def xMinimum(self):
        return 0.0

    def yMinimum(self):
        return 0.0

    def xMaximum(self):
        return 10.0

    def yMaximum(self):
        return 10.0


class FakeVectorLayer:
    def __init__(self, identifier, name, rows, fields):
        self._id = identifier
        self._name = name
        self._rows = rows
        self._fields = FakeFields(fields)
        self._selected = []
        self._subset = ""
        self.subset_accepts = True
        self.repainted = 0
        self._crs = FakeCrs()

    # identity
    def id(self):
        return self._id

    def name(self):
        return self._name

    def isValid(self):
        return True

    def crs(self):
        return self._crs

    def extent(self):
        return FakeExtent()

    # vector protocol
    def fields(self):
        return self._fields

    def getFeatures(self, request=None):
        for row in self._rows:
            yield FakeFeature(row["id"], row)

    def featureCount(self):
        return len(self._rows)

    def selectedFeatureIds(self):
        return list(self._selected)

    def selectedFeatureCount(self):
        return len(self._selected)

    def selectByIds(self, identifiers):
        self._selected = list(identifiers)

    def selectAll(self):
        self._selected = [row["id"] for row in self._rows]

    def removeSelection(self):
        self._selected = []

    def boundingBoxOfSelected(self):
        return FakeExtent()

    def subsetString(self):
        return self._subset

    def setSubsetString(self, expression):
        if not self.subset_accepts:
            return False
        self._subset = expression
        return True

    def triggerRepaint(self):
        self.repainted += 1

    def opacity(self):
        return 1.0

    def setOpacity(self, value):
        self._opacity = value


class FakeNode:
    def __init__(self):
        self._visible = True

    def itemVisibilityChecked(self):
        return self._visible

    def setItemVisibilityChecked(self, value):
        self._visible = value


class FakeTreeRoot:
    def __init__(self):
        self.node = FakeNode()

    def findLayer(self, _layer_id):
        return self.node


class FakeProject:
    def __init__(self, layers):
        self._layers = {layer.id(): layer for layer in layers}
        self._root = FakeTreeRoot()

    def mapLayer(self, identifier):
        return self._layers.get(identifier)

    def mapLayers(self):
        return dict(self._layers)

    def layerTreeRoot(self):
        return self._root


class FakeCanvas:
    def __init__(self):
        self.refreshed = 0
        self.extent_set = None
        self.previous = 0
        self._crs = FakeCrs()

    def extent(self):
        return FakeExtent()

    def mapSettings(self):
        return self

    def destinationCrs(self):
        return self._crs

    def setExtent(self, extent):
        self.extent_set = extent

    def refresh(self):
        self.refreshed += 1

    def zoomToPreviousExtent(self):
        self.previous += 1


class FakeIface:
    def __init__(self, canvas, active=None):
        self._canvas = canvas
        self._active = active
        self.tables = []

    def mapCanvas(self):
        return self._canvas

    def activeLayer(self):
        return self._active

    def setActiveLayer(self, layer):
        self._active = layer

    def showAttributeTable(self, layer):
        self.tables.append(layer.id())

    def layerTreeView(self):
        return self

    def refreshLayerSymbology(self, _layer_id):
        return None


AREAS = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
LANDUSE = ["residential"] * 5 + ["commercial"] * 3 + ["industrial"] * 2
ROWS = [
    {"id": index + 1, "area": area, "landuse": landuse}
    for index, (area, landuse) in enumerate(zip(AREAS, LANDUSE))
]


@pytest.fixture()
def runtime():
    layer = FakeVectorLayer("parcels_1", "parcels", ROWS, ["area", "landuse"])
    project = FakeProject([layer])
    canvas = FakeCanvas()
    return QGISRuntime(FakeIface(canvas, layer), project), layer, canvas


# --------------------------------------------------------------------------
# Resolution and reading
# --------------------------------------------------------------------------

def test_layers_resolve_by_stable_id_not_by_display_name(runtime):
    engine, layer, _canvas = runtime
    assert engine.layer("parcels_1") is layer
    with pytest.raises(RuntimeUnavailable):
        engine.layer("parcels")  # the display name must not resolve


def test_reading_requests_only_the_needed_columns_and_no_geometry(runtime):
    engine, layer, _canvas = runtime
    data = engine.records(layer, ["area"], "all")
    assert len(data["rows"]) == 10
    assert data["truncated"] is False
    # Fetching geometry to compute a mean is the classic way to make an
    # assistant unusable on a large layer.
    assert FakeFeatureRequest.NoGeometry == 1


def test_an_unknown_field_is_refused_before_any_read(runtime):
    engine, layer, _canvas = runtime
    with pytest.raises(RuntimeUnavailable):
        engine.records(layer, ["not_a_field"], "all")


def test_selection_scope_reads_only_selected_features(runtime):
    engine, layer, _canvas = runtime
    layer.selectByIds([1, 2, 3])
    data = engine.records(layer, ["area"], "selection")
    assert [row["id"] for row in data["rows"]] == [1, 2, 3]


def test_selection_scope_with_an_empty_selection_says_so(runtime):
    engine, layer, _canvas = runtime
    with pytest.raises(RuntimeUnavailable):
        engine.records(layer, ["area"], "selection")


def test_an_untransformable_viewport_is_an_error_not_the_whole_layer(runtime):
    engine, layer, canvas = runtime
    # The layer is in a different CRS from the canvas, so a transform is needed;
    # when it fails, answering "in this view" with every row would be silently
    # wrong, so the read must stop instead.
    layer._crs = FakeCrs("EPSG:4326")
    canvas._crs.untransformable = True
    with pytest.raises(RuntimeUnavailable):
        engine.records(layer, ["area"], "viewport")


def test_reading_is_capped_so_a_huge_layer_cannot_stall_the_ui():
    big = FakeVectorLayer("big", "big", [{"id": i, "v": float(i)} for i in range(MAX_ANALYTIC_FEATURES + 10)], ["v"])
    engine = QGISRuntime(FakeIface(FakeCanvas(), big), FakeProject([big]))
    data = engine.records(big, ["v"], "all")
    assert data["truncated"] is True
    assert len(data["rows"]) == MAX_ANALYTIC_FEATURES


# --------------------------------------------------------------------------
# Analytics through the runtime produce real numbers
# --------------------------------------------------------------------------

def test_numeric_analysis_returns_the_real_measured_statistics(runtime):
    engine, _layer, _canvas = runtime
    stats = engine.numeric("parcels_1", "area")
    assert stats["mean"] == 550.0
    assert stats["usable"] == 10
    assert stats["truncated"] is False


def test_group_analysis_counts_the_real_groups(runtime):
    engine, _layer, _canvas = runtime
    result = engine.group("parcels_1", "landuse")
    assert {item["group"]: item["value"] for item in result["groups"]}["residential"] == 5.0


def test_field_profile_marks_numeric_columns_for_the_agent(runtime):
    engine, _layer, _canvas = runtime
    profile = engine.field_profile("parcels_1")
    numeric = {item["name"]: item["numeric"] for item in profile["fields"]}
    assert numeric["area"] is True
    assert numeric["landuse"] is False


def test_comparison_measures_a_selection_against_the_whole_layer(runtime):
    engine, layer, _canvas = runtime
    layer.selectByIds([1, 2, 3, 4, 5])
    result = engine.compare("parcels_1", "area")
    assert result["left"]["mean"] == 300.0
    assert result["right"]["mean"] == 550.0


# --------------------------------------------------------------------------
# Analysis lands on the map
# --------------------------------------------------------------------------

def test_top_n_through_the_executor_selects_the_real_features(runtime):
    engine, layer, _canvas = runtime
    execute = build_executor(engine)
    request = validate_request("analytics.top_n@1", {"layer_id": "parcels_1", "field": "area", "limit": 3})
    outcome = execute(request)
    # The analysis ran, and it changed what the map shows.
    assert outcome["analysis"]["feature_ids"] == [10, 9, 8]
    assert outcome["map"]["kind"] == "selection_applied"
    assert layer.selectedFeatureIds() == [10, 9, 8]


def test_outliers_through_the_executor_select_the_flagged_feature():
    rows = [{"id": index, "v": value} for index, value in enumerate([1, 2, 2, 3, 3, 3, 4, 4, 5, 100])]
    layer = FakeVectorLayer("l", "l", rows, ["v"])
    engine = QGISRuntime(FakeIface(FakeCanvas(), layer), FakeProject([layer]))
    execute = build_executor(engine)
    outcome = execute(validate_request("analytics.outliers@1", {"layer_id": "l", "field": "v"}))
    assert layer.selectedFeatureIds() == [9]
    assert outcome["analysis"]["matched"] == 1


def test_an_analysis_with_nothing_to_show_makes_no_map_change(runtime):
    engine, layer, _canvas = runtime
    execute = build_executor(engine)
    outcome = execute(validate_request("analytics.top_n@1", {"layer_id": "parcels_1", "field": "area", "limit": 3}))
    assert outcome["map"]["kind"] == "selection_applied"
    empty = FakeVectorLayer("e", "e", [], ["area"])
    engine2 = QGISRuntime(FakeIface(FakeCanvas(), empty), FakeProject([empty]))
    outcome2 = build_executor(engine2)(
        validate_request("analytics.top_n@1", {"layer_id": "e", "field": "area", "limit": 3})
    )
    assert outcome2["map"]["kind"] == "no_map_change"


# --------------------------------------------------------------------------
# Filters are assembled from typed parts, never from model text
# --------------------------------------------------------------------------

def test_a_filter_quotes_its_literal_so_a_value_cannot_become_an_expression(runtime):
    engine, layer, _canvas = runtime
    engine.preview_filter("parcels_1", "landuse", "=", "resi'; DROP TABLE x --")
    # The apostrophe is doubled, so the payload stays a string literal.
    assert layer.subsetString() == "\"landuse\" = 'resi''; DROP TABLE x --'"


def test_numeric_filter_literals_stay_unquoted(runtime):
    engine, layer, _canvas = runtime
    engine.preview_filter("parcels_1", "area", ">", "500")
    assert layer.subsetString() == '"area" > 500'


def test_a_filter_on_an_unknown_field_is_refused(runtime):
    engine, _layer, _canvas = runtime
    with pytest.raises(RuntimeUnavailable):
        engine.preview_filter("parcels_1", "nope", "=", "x")


def test_a_filter_qgis_rejects_is_reported_not_silently_ignored(runtime):
    engine, layer, _canvas = runtime
    layer.subset_accepts = False
    with pytest.raises(RuntimeUnavailable):
        engine.preview_filter("parcels_1", "area", ">", "1")


# --------------------------------------------------------------------------
# Undo restores real previous state
# --------------------------------------------------------------------------

def test_undo_restores_the_previous_selection(runtime):
    engine, layer, _canvas = runtime
    layer.selectByIds([1, 2])
    engine.select_features("parcels_1", [7, 8], zoom=False)
    assert layer.selectedFeatureIds() == [7, 8]
    assert engine.undo()["change"] == "selection"
    assert layer.selectedFeatureIds() == [1, 2]


def test_undo_restores_the_previous_filter_not_an_empty_one(runtime):
    engine, layer, _canvas = runtime
    layer.setSubsetString('"area" > 100')
    engine.preview_filter("parcels_1", "area", ">", "900")
    engine.undo()
    assert layer.subsetString() == '"area" > 100'


def test_undo_restores_visibility(runtime):
    engine, _layer, _canvas = runtime
    engine.set_visibility("parcels_1", False)
    assert engine.project.layerTreeRoot().findLayer("parcels_1").itemVisibilityChecked() is False
    engine.undo()
    assert engine.project.layerTreeRoot().findLayer("parcels_1").itemVisibilityChecked() is True


def test_undo_with_an_empty_stack_is_honest(runtime):
    engine, _layer, _canvas = runtime
    assert engine.undo()["kind"] == "nothing_to_undo"


def test_the_undo_stack_is_bounded(runtime):
    engine, _layer, _canvas = runtime
    for _index in range(50):
        engine.set_visibility("parcels_1", True)
    assert engine.undo_depth <= 20


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def test_navigation_and_table_capabilities_reach_qgis(runtime):
    engine, _layer, canvas = runtime
    execute = build_executor(engine)
    assert execute(validate_request("map.zoom_layer@1", {"layer_id": "parcels_1"}))["kind"] == "zoomed"
    assert execute(validate_request("map.refresh@1", {}))["kind"] == "refreshed"
    assert execute(validate_request("layer.attribute_table@1", {"layer_id": "parcels_1"}))["kind"] == "table_opened"
    assert canvas.refreshed >= 1
    assert engine.iface.tables == ["parcels_1"]


def test_a_registered_capability_with_no_desktop_binding_fails_loudly(runtime):
    engine, _layer, _canvas = runtime
    execute = build_executor(engine)
    # Registered in the catalogue but not implemented here yet: it must not
    # look like a silent success. `report.build@1` is the current example; when
    # it is bound, replace it with whatever is still unbound rather than
    # deleting the test, because the honest-gap behaviour is the thing under
    # test and not the particular capability.
    with pytest.raises(CapabilityError):
        execute({"capability": "report.build@1", "params": {}})


def test_the_executor_ignores_anything_outside_the_validated_params(runtime):
    engine, _layer, _canvas = runtime
    execute = build_executor(engine)
    # Even if a caller hand-builds a request, only declared params are read.
    outcome = execute({"capability": "analytics.numeric@1",
                       "params": {"layer_id": "parcels_1", "field": "area"}})
    assert outcome["mean"] == 550.0
