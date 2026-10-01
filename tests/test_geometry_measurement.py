"""Measuring the shape, not the column that sounds like it.

The OGC conformance sample is the fixture this was written against, because it
is the cleanest demonstration of the failure. Its columns are named after their
storage types - `text`, `real`, `boolean`, `integer` - and carry no meaning at
all. The layer's true area is 7,627.77 m²; summing `real` gives 2,715.68, and
ranking by `real` names the smallest polygon as the largest. An assistant that
reaches for the plausibly-named column is right on every layer whose columns
happen to be named well and wrong on every layer that is not.

Two halves are tested here. The pure kernel in spatial.py decides how a measure
can honestly be expressed in metres and aggregates the per-feature values. The
runtime half is exercised against the same QGIS stubs the other runtime tests
use, so what is proven is the wiring, the skipping of geometries that cannot
carry the measure, and the refusals - not QGIS's own arithmetic.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo import spatial  # noqa: E402
from mapdex_qgis._vendor.nivo.capabilities import CapabilityError, get as get_capability, validate_request  # noqa: E402

# The runtime module needs the QGIS stubs the shared runtime test installs, and
# importing that module is how they get installed.
from test_qgis_runtime import (  # noqa: E402
    FakeCrs,
    FakeVectorLayer,
    _install_qgis_stubs,
)

_install_qgis_stubs()

from mapdex_qgis.qgis_runtime import QGISRuntime, build_executor  # noqa: E402


# --------------------------------------------------------------------------
# The pure kernel: what frame may a measurement be stated in
# --------------------------------------------------------------------------

def test_a_projected_metre_layer_is_measured_where_it_stands():
    plan = spatial.plan_geometry_measurement(False, "m", "area")
    assert plan["strategy"] == "map_units"
    assert plan["unit"] == "m²"
    assert plan["factor"] == 1.0


def test_a_foot_based_crs_squares_its_factor_for_an_area():
    # 3.28 or 10.76: the difference between a length conversion and an area one,
    # and the reason the factor is decided here rather than at each call site.
    area = spatial.plan_geometry_measurement(False, "ft", "area")
    length = spatial.plan_geometry_measurement(False, "ft", "length")
    assert area["factor"] == pytest.approx(0.3048 ** 2)
    assert length["factor"] == pytest.approx(0.3048)


def test_a_geographic_layer_is_measured_in_a_named_projection():
    # example.gpkg: Aurora, Colorado, EPSG:4326. Square degrees are not an area,
    # so the plan names the projection it will use instead.
    plan = spatial.plan_geometry_measurement(
        True, "degrees", "area", [-104.8075, 39.7140, -104.7994, 39.7203])
    assert plan["strategy"] == "project"
    assert plan["crs"] == "EPSG:32613"
    assert "geographic" in plan["reason"]


def test_a_geographic_layer_too_wide_for_one_zone_falls_back_to_the_ellipsoid():
    # Forcing a UTM zone on a continent-wide layer would give a confident figure
    # with percent-level error. The ellipsoid is exact and says so.
    plan = spatial.plan_geometry_measurement(True, "degrees", "area", [-20.0, 35.0, 40.0, 70.0])
    assert plan["strategy"] == "ellipsoidal"


def test_an_unknown_crs_unit_is_refused_rather_than_guessed():
    plan = spatial.plan_geometry_measurement(False, "furlongs", "area")
    assert plan["strategy"] == "unsupported"
    assert plan["reason"] == "unknown_crs_unit"


def test_an_unknown_measure_is_refused():
    assert spatial.plan_geometry_measurement(False, "m", "volume")["strategy"] == "unsupported"
    assert spatial.normalize_measure("VOLUME") == ""
    assert spatial.normalize_measure(" Area ") == "area"


def test_the_southern_hemisphere_picks_the_southern_utm_code():
    zone = spatial.utm_zone_for([-58.5, -34.7, -58.3, -34.5])
    assert zone["crs"] == "EPSG:32721"
    assert zone["hemisphere"] == "south"


def test_a_polar_extent_has_no_utm_zone():
    assert spatial.utm_zone_for([10.0, 85.0, 11.0, 86.0]) is None


def test_a_malformed_extent_has_no_utm_zone():
    for bbox in (None, [], [1.0, 2.0], [10.0, 10.0, 5.0, 20.0], [500.0, 0.0, 501.0, 1.0]):
        assert spatial.utm_zone_for(bbox) is None


# --------------------------------------------------------------------------
# The pure kernel: aggregating, and keeping the unmeasurable visible
# --------------------------------------------------------------------------

POLYGON1 = [
    {"id": 1, "name": "BIT Systems", "value": 4298.26},
    {"id": 2, "name": "BIT Systems Visitor Parking", "value": 1788.57},
    {"id": 3, "name": "CCA Administration Building", "value": 1540.94},
]


def test_a_total_is_the_sum_of_the_measured_shapes():
    result = spatial.summarize_geometry(POLYGON1, "sum")
    assert result["value"] == pytest.approx(7627.77)
    assert result["measured"] == 3 and result["skipped"] == 0


def test_the_largest_is_named_not_just_numbered():
    # By the `real` column the answer would be BIT Systems Visitor Parking,
    # which is the smallest polygon by actual area. The two answers are not
    # merely different, they are opposite, so this cannot be passed by luck.
    result = spatial.summarize_geometry(POLYGON1, "top", limit=1)
    assert [entry["name"] for entry in result["ranked"]] == ["BIT Systems"]
    assert result["value"] == pytest.approx(4298.26)


def test_the_smallest_is_the_same_question_from_the_other_side():
    result = spatial.summarize_geometry(POLYGON1, "bottom", limit=1)
    assert [entry["name"] for entry in result["ranked"]] == ["CCA Administration Building"]


def test_a_mixed_layer_reports_what_it_could_not_measure():
    # geometry1 holds 4 points, 3 lines and 3 polygons. A single total that
    # silently treats ten mixed features as polygons is wrong, and it is the
    # kind of wrong that survives review because it looks like a number.
    mixed = [{"id": index, "value": None} for index in range(7)] + POLYGON1
    result = spatial.summarize_geometry(mixed, "sum")
    assert result["features"] == 10
    assert result["measured"] == 3
    assert result["skipped"] == 7
    assert result["value"] == pytest.approx(7627.77)


def test_nothing_measurable_gives_no_number_rather_than_zero():
    result = spatial.summarize_geometry([{"id": 1, "value": None}], "sum")
    assert result["value"] is None and result["sum"] is None
    assert result["measured"] == 0


def test_a_count_of_measurable_shapes_is_a_count_not_a_total():
    result = spatial.summarize_geometry(POLYGON1 + [{"id": 9, "value": None}], "count")
    assert result["value"] == 3


def test_mean_min_and_max_read_off_the_same_measurements():
    result = spatial.summarize_geometry(POLYGON1, "mean")
    assert result["value"] == pytest.approx(7627.77 / 3)
    assert result["min"] == pytest.approx(1540.94)
    assert result["max"] == pytest.approx(4298.26)


# --------------------------------------------------------------------------
# The registry contract
# --------------------------------------------------------------------------

def test_the_capability_is_declared_and_takes_the_metric_it_needs():
    capability = get_capability("analytics.geometry@1")
    assert capability is not None
    assert capability.params["metric"]["required"] is True
    assert set(capability.params["metric"]["enum"]) == {"area", "length", "perimeter"}


def test_the_registry_refuses_a_metric_outside_the_closed_set():
    with pytest.raises(CapabilityError):
        validate_request("analytics.geometry@1", {"layer_id": "a", "metric": "volume"})


def test_the_registry_refuses_a_request_with_no_metric():
    with pytest.raises(CapabilityError):
        validate_request("analytics.geometry@1", {"layer_id": "a"})


def test_the_registry_refuses_a_smuggled_parameter():
    with pytest.raises(CapabilityError):
        validate_request("analytics.geometry@1",
                         {"layer_id": "a", "metric": "area", "expression": "$area"})


# --------------------------------------------------------------------------
# The runtime wiring
# --------------------------------------------------------------------------

class FakeAbstractGeometry:
    def __init__(self, dimension):
        self._dimension = dimension

    def dimension(self):
        return self._dimension


class FakeGeometry:
    """Just enough geometry: a dimension, an area and a length."""

    def __init__(self, dimension, area=0.0, length=0.0, empty=False):
        self._dimension = dimension
        self._area = area
        self._length = length
        self._empty = empty

    def isEmpty(self):
        return self._empty

    def get(self):
        return FakeAbstractGeometry(self._dimension)

    def area(self):
        return self._area

    def length(self):
        return self._length


class GeometryFeature:
    def __init__(self, identifier, values, geometry):
        self._id = identifier
        self._values = values
        self._geometry = geometry

    def id(self):
        return self._id

    def geometry(self):
        return self._geometry

    def __getitem__(self, key):
        return self._values[key]


class GeometryLayer(FakeVectorLayer):
    """A projected metre layer whose features carry stub geometry."""

    def __init__(self, rows, display="name", authid="EPSG:32613", geographic=False):
        super().__init__("lyr_geom", "shapes", rows, [display] if display else [])
        self._display = display
        self._crs = FakeCrs(authid=authid, geographic=geographic)

    def displayField(self):
        return self._display

    def getFeatures(self, request=None):
        for row in self._rows:
            yield GeometryFeature(row["id"], row, row["geometry"])


class FakeCanvas:
    def __init__(self):
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1

    def setExtent(self, extent):
        self.extent = extent


class FakeIface:
    def __init__(self):
        self._canvas = FakeCanvas()

    def mapCanvas(self):
        return self._canvas


class FakeProject:
    def __init__(self, layer):
        self._layer = layer

    def mapLayer(self, identifier):
        return self._layer if identifier == self._layer.id() else None

    def transformContext(self):
        return None

    def ellipsoid(self):
        return "WGS84"


def _runtime(rows, **kwargs):
    layer = GeometryLayer(rows, **kwargs)
    return QGISRuntime(FakeIface(), FakeProject(layer)), layer


POLYGON_ROWS = [
    {"id": 1, "name": "BIT Systems", "geometry": FakeGeometry(2, area=4298.26, length=270.0)},
    {"id": 2, "name": "BIT Systems Visitor Parking", "geometry": FakeGeometry(2, area=1788.57, length=180.0)},
    {"id": 3, "name": "CCA Administration Building", "geometry": FakeGeometry(2, area=1540.94, length=160.0)},
]


def test_the_runtime_totals_the_area_and_states_its_frame():
    runtime, _ = _runtime(POLYGON_ROWS)
    result = runtime.measure_geometry("lyr_geom", "area", "sum")
    assert result["value"] == pytest.approx(7627.77)
    assert result["unit"] == "m²"
    assert result["method"] == "planar"
    assert result["measured_in"] == "EPSG:32613"
    # The frame is part of the answer, not decoration: the same shapes measured
    # planar and on the ellipsoid differ by about a tenth of a percent.
    assert "EPSG:32613" in result["frame"]


def test_the_runtime_names_the_largest_shape_from_the_layers_display_field():
    runtime, _ = _runtime(POLYGON_ROWS)
    result = runtime.measure_geometry("lyr_geom", "area", "top", limit=1)
    assert [entry["name"] for entry in result["ranked"]] == ["BIT Systems"]


def test_a_point_has_no_area_and_is_counted_as_skipped():
    rows = POLYGON_ROWS + [
        {"id": 4, "name": "a point", "geometry": FakeGeometry(0)},
        {"id": 5, "name": "a line", "geometry": FakeGeometry(1, length=437.94)},
    ]
    runtime, _ = _runtime(rows)
    result = runtime.measure_geometry("lyr_geom", "area", "sum")
    assert result["measured"] == 3
    assert result["skipped"] == 2
    assert result["skipped_reason"] == "no_area"
    assert result["value"] == pytest.approx(7627.77)


def test_a_length_measures_the_lines_and_skips_the_polygons():
    rows = POLYGON_ROWS + [{"id": 9, "name": "E Centretech Cir",
                            "geometry": FakeGeometry(1, length=437.94)}]
    runtime, _ = _runtime(rows)
    result = runtime.measure_geometry("lyr_geom", "length", "sum")
    assert result["value"] == pytest.approx(437.94)
    assert result["unit"] == "m"
    assert result["skipped"] == 3


def test_a_perimeter_measures_the_polygons_own_boundary():
    runtime, _ = _runtime(POLYGON_ROWS)
    result = runtime.measure_geometry("lyr_geom", "perimeter", "sum")
    assert result["value"] == pytest.approx(610.0)
    assert result["unit"] == "m"


def test_a_layer_in_an_unknown_unit_is_refused_with_the_reason():
    runtime, _ = _runtime(POLYGON_ROWS, authid="EPSG:99999")
    runtime.layer("lyr_geom")  # the layer resolves; the CRS unit does not
    runtime.project._layer._crs = FakeCrs(authid="EPSG:99999")
    original = runtime._crs_map_units
    runtime._crs_map_units = lambda crs: "furlongs"
    try:
        with pytest.raises(CapabilityError) as failure:
            runtime.measure_geometry("lyr_geom", "area", "sum")
    finally:
        runtime._crs_map_units = original
    assert "furlongs" in str(failure.value)


def test_an_unknown_metric_is_refused_before_the_layer_is_touched():
    runtime, _ = _runtime(POLYGON_ROWS)
    with pytest.raises(CapabilityError):
        runtime.measure_geometry("lyr_geom", "volume", "sum")


def test_an_empty_geometry_is_skipped_not_measured_as_zero():
    rows = [{"id": 1, "name": "empty", "geometry": FakeGeometry(2, area=0.0, empty=True)}]
    runtime, _ = _runtime(rows)
    result = runtime.measure_geometry("lyr_geom", "area", "sum")
    assert result["measured"] == 0 and result["skipped"] == 1


def test_a_layer_with_no_display_field_still_ranks_by_id():
    rows = [{"id": 7, "geometry": FakeGeometry(2, area=10.0)}]
    layer = GeometryLayer([], display=None)
    layer._rows = rows
    runtime = QGISRuntime(FakeIface(), FakeProject(layer))
    result = runtime.measure_geometry("lyr_geom", "area", "top", limit=1)
    assert result["ranked"][0]["id"] == 7
    assert result["ranked"][0]["name"] == ""


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def test_the_executor_routes_the_capability_and_selects_a_ranking():
    runtime, layer = _runtime(POLYGON_ROWS)
    execute = build_executor(runtime)
    assert "analytics.geometry@1" in execute.capabilities
    outcome = execute({"capability": "analytics.geometry@1",
                       "params": {"layer_id": "lyr_geom", "metric": "area",
                                  "statistic": "top", "limit": 1}})
    assert outcome["analysis"]["ranked"][0]["name"] == "BIT Systems"
    # A ranking the user asked for lands on the map; an aggregate does not.
    assert layer.selectedFeatureIds() == [1]


def test_an_aggregate_changes_nothing_on_the_canvas():
    runtime, layer = _runtime(POLYGON_ROWS)
    execute = build_executor(runtime)
    outcome = execute({"capability": "analytics.geometry@1",
                       "params": {"layer_id": "lyr_geom", "metric": "area", "statistic": "sum"}})
    assert outcome["map"]["kind"] == "no_map_change"
    assert layer.selectedFeatureIds() == []
