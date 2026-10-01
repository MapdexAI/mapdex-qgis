"""The two-layer spatial capabilities, against stubbed QGIS geometry.

These five capabilities were registered, advertised by the server and bound to
nothing: `spatial.py` held the unit and counting kernels and no code path
reached it, so every spatial question a QGIS user asked came back as "not
available in this build yet".

What is tested here is the part that carries real risk, which is not whether
GEOS can intersect two rectangles:

* a metric distance on a layer in degrees is REFUSED, never approximated,
* a multipolygon's parts are counted separately and aggregated back to one
  label, so a point in a hole is not counted as inside,
* the frame every comparison happened in is reported,
* the pair-comparison bound truncates and says so.

Geometry is stubbed as axis-aligned rectangles. That is enough to prove the
wiring, the aggregation and the refusals; whether QGIS binds to a live canvas
remains a question only a running QGIS answers.
"""
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeRect:
    """An axis-aligned box with the QgsRectangle surface the runtime uses."""

    def __init__(self, xmin=0.0, ymin=0.0, xmax=0.0, ymax=0.0):
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self._minimal = False

    def setMinimal(self):
        self._minimal = True
        self.xmin = self.ymin = float("inf")
        self.xmax = self.ymax = float("-inf")

    def combineExtentWith(self, other):
        self.xmin, self.ymin = min(self.xmin, other.xmin), min(self.ymin, other.ymin)
        self.xmax, self.ymax = max(self.xmax, other.xmax), max(self.ymax, other.ymax)

    def grow(self, amount):
        self.xmin -= amount
        self.ymin -= amount
        self.xmax += amount
        self.ymax += amount

    def intersects(self, other):
        return not (self.xmax < other.xmin or other.xmax < self.xmin
                    or self.ymax < other.ymin or other.ymax < self.ymin)


class FakePoint:
    def __init__(self, x, y):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeGeom:
    """A rectangle or a point, with the QgsGeometry surface the runtime uses."""

    def __init__(self, bounds=None, point=None, transformed=0):
        self.bounds = bounds
        self.point = point
        self.transformed = transformed

    # -- shape -----------------------------------------------------------
    def isEmpty(self):
        return self.bounds is None and self.point is None

    def isMultipart(self):
        return isinstance(self.bounds, list)

    def type(self):
        return 0 if self.point is not None else 2

    def boundingBox(self):
        if self.point is not None:
            return FakeRect(self.point[0], self.point[1], self.point[0], self.point[1])
        boxes = self.bounds if self.isMultipart() else [self.bounds]
        xs = [value for box in boxes for value in (box[0], box[2])]
        ys = [value for box in boxes for value in (box[1], box[3])]
        return FakeRect(min(xs), min(ys), max(xs), max(ys))

    def _rings(self, box):
        minx, miny, maxx, maxy = box
        return [[(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]]

    def asPolygon(self):
        return [[FakePoint(x, y) for x, y in ring] for ring in self._rings(self.bounds)]

    def asMultiPolygon(self):
        return [
            [[FakePoint(x, y) for x, y in ring] for ring in self._rings(box)]
            for box in self.bounds
        ]

    def asPoint(self):
        return FakePoint(*self.point)

    def pointOnSurface(self):
        box = self.boundingBox()
        return FakeGeom(point=((box.xmin + box.xmax) / 2.0, (box.ymin + box.ymax) / 2.0))

    # -- predicates ------------------------------------------------------
    def intersects(self, other):
        return self.boundingBox().intersects(other.boundingBox())

    def within(self, other):
        mine, theirs = self.boundingBox(), other.boundingBox()
        return (theirs.xmin <= mine.xmin and mine.xmax <= theirs.xmax
                and theirs.ymin <= mine.ymin and mine.ymax <= theirs.ymax)

    def distance(self, other):
        mine, theirs = self.boundingBox(), other.boundingBox()
        dx = max(theirs.xmin - mine.xmax, mine.xmin - theirs.xmax, 0.0)
        dy = max(theirs.ymin - mine.ymax, mine.ymin - theirs.ymax, 0.0)
        return (dx * dx + dy * dy) ** 0.5

    def transform(self, _transform):
        self.transformed += 1
        return 0


def _install_qgis_stubs():
    """Add the geometry symbols to `qgis.core` without replacing it.

    Additive on purpose. Another test module installs its own `qgis.core`, and
    whichever imported last used to win: replacing the module here removed that
    file's `transformBoundingBox` and failed one of its tests for reasons that
    had nothing to do with either test. A stub module is shared state, so it is
    extended rather than overwritten.
    """

    class FakeFeatureRequest:
        NoGeometry = 1

        def __init__(self):
            self.rect = None

        def setSubsetOfAttributes(self, indexes):
            self.attributes = list(indexes)

        def setNoAttributes(self):
            self.attributes = []

        def setFlags(self, flags):
            self.flags = flags

        def setFilterRect(self, rect):
            self.rect = rect

    class QgsCsException(Exception):
        pass

    class QgsCoordinateTransform:
        def __init__(self, source, target, project):
            self.source = source

        def transformBoundingBox(self, extent):
            if getattr(self.source, "untransformable", False):
                raise QgsCsException("no transform")
            return extent

    class QgsGeometryFactory(FakeGeom):
        def __init__(self, other=None, **kwargs):
            if isinstance(other, FakeGeom):
                super().__init__(other.bounds, other.point, other.transformed)
            else:
                super().__init__(**kwargs)

        @staticmethod
        def fromPointXY(point):
            return FakeGeom(point=(point.x(), point.y()))

        @staticmethod
        def collectGeometry(geometries):
            boxes = [
                (geometry.boundingBox().xmin, geometry.boundingBox().ymin,
                 geometry.boundingBox().xmax, geometry.boundingBox().ymax)
                for geometry in geometries
            ]
            return FakeGeom(bounds=boxes)

    qgis = sys.modules.get("qgis") or types.ModuleType("qgis")
    core = sys.modules.get("qgis.core") or types.ModuleType("qgis.core")
    for name, value in (
        ("QgsFeatureRequest", FakeFeatureRequest),
        ("QgsCsException", QgsCsException),
        ("QgsCoordinateTransform", QgsCoordinateTransform),
        ("QgsRectangle", FakeRect),
        ("QgsPointXY", FakePoint),
        ("QgsGeometry", QgsGeometryFactory),
    ):
        if not hasattr(core, name):
            setattr(core, name, value)
    qgis.core = core
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = core


_install_qgis_stubs()

from mapdex_qgis.qgis_runtime import QGISRuntime, RuntimeUnavailable, build_executor  # noqa: E402


# --------------------------------------------------------------------------
# Project doubles
# --------------------------------------------------------------------------

class FakeCrs:
    def __init__(self, authid="EPSG:3857", geographic=False):
        self._authid, self._geographic = authid, geographic

    def isValid(self):
        return True

    def isGeographic(self):
        return self._geographic

    def authid(self):
        return self._authid

    def __eq__(self, other):
        return isinstance(other, FakeCrs) and other._authid == self._authid


class FakeFeature:
    def __init__(self, identifier, geometry, values=None):
        self._id, self._geometry, self._values = identifier, geometry, values or {}

    def id(self):
        return self._id

    def geometry(self):
        return self._geometry

    def __getitem__(self, key):
        return self._values[key]


class FakeFields:
    def __init__(self, names):
        self._names = list(names)

    def __iter__(self):
        return iter([types.SimpleNamespace(name=lambda n=n: n, typeName=lambda: "string") for n in self._names])

    def indexOf(self, name):
        return self._names.index(name) if name in self._names else -1


class FakeLayer:
    def __init__(self, identifier, features, crs=None, fields=(), display=""):
        self._id, self._features = identifier, features
        self._crs = crs or FakeCrs()
        self._fields = FakeFields(fields)
        self._display = display
        self.selected = []

    def id(self):
        return self._id

    def name(self):
        return self._id

    def isValid(self):
        return True

    def crs(self):
        return self._crs

    def fields(self):
        return self._fields

    def displayField(self):
        return self._display

    def type(self):
        return 0

    def getFeatures(self, _request=None):
        return list(self._features)

    def featureCount(self):
        return len(self._features)

    def selectedFeatureIds(self):
        return list(self.selected)

    def selectedFeatureCount(self):
        return len(self.selected)

    def selectByIds(self, identifiers):
        self.selected = list(identifiers)

    def boundingBoxOfSelected(self):
        return FakeRect(0, 0, 10, 10)

    def extent(self):
        return FakeRect(0, 0, 10, 10)


class FakeProject:
    def __init__(self, layers):
        self._layers = {layer.id(): layer for layer in layers}

    def mapLayer(self, identifier):
        return self._layers.get(identifier)

    def mapLayers(self):
        return dict(self._layers)

    def transformContext(self):
        return None

    def ellipsoid(self):
        return "WGS84"


class FakeCanvas:
    def __init__(self, crs):
        self._crs = crs
        self.refreshed = 0

    def extent(self):
        return FakeRect(0, 0, 10, 10)

    def center(self):
        return FakePoint(0.0, 0.0)

    def mapSettings(self):
        return types.SimpleNamespace(destinationCrs=lambda: self._crs)

    def setExtent(self, _extent):
        pass

    def zoomToSelected(self, _layer=None):
        pass

    def refresh(self):
        self.refreshed += 1


class FakeIface:
    def __init__(self, canvas):
        self._canvas = canvas

    def mapCanvas(self):
        return self._canvas

    def layerTreeView(self):
        return types.SimpleNamespace(refreshLayerSymbology=lambda _id: None)


def _runtime(layers, crs=None):
    project = FakeProject(layers)
    canvas = FakeCanvas(crs or FakeCrs())
    return QGISRuntime(FakeIface(canvas), project)


def _polygon(identifier, box, name):
    return FakeFeature(identifier, FakeGeom(bounds=box), {"district": name})


def _point(identifier, x, y):
    return FakeFeature(identifier, FakeGeom(point=(x, y)))


# --------------------------------------------------------------------------
# Counting one layer into another
# --------------------------------------------------------------------------

def test_points_are_counted_into_the_polygon_that_contains_them():
    districts = FakeLayer("districts", [
        _polygon(1, (0, 0, 10, 10), "North"),
        _polygon(2, (10, 0, 20, 10), "South"),
    ], fields=["district"], display="district")
    buildings = FakeLayer("buildings", [
        _point(1, 5, 5), _point(2, 6, 6), _point(3, 15, 5), _point(4, 99, 99),
    ])
    result = _runtime([districts, buildings]).count_in_polygons("districts", "buildings")
    groups = {entry["group"]: entry["value"] for entry in result["groups"]}
    assert groups == {"North": 2, "South": 1}
    # The building that fell in no district is reported, never dropped: "12 of
    # 500 fell outside every district" is the fact an analyst needs.
    assert result["unmatched"] == 1
    assert result["matched"] == 3
    assert result["compared_in"] == "EPSG:3857"


def test_multipolygon_parts_aggregate_back_to_one_label():
    # Two disjoint parts of one feature. Flattening their rings into a single
    # list would make the second part read as a hole in the first.
    islands = FakeLayer("islands", [
        FakeFeature(1, FakeGeom(bounds=[(0, 0, 5, 5), (20, 20, 25, 25)]), {"district": "Archipelago"}),
    ], fields=["district"], display="district")
    points = FakeLayer("points", [_point(1, 2, 2), _point(2, 22, 22), _point(3, 12, 12)])
    result = _runtime([islands, points]).count_in_polygons("islands", "points")
    assert result["groups"] == [{"group": "Archipelago", "value": 2}]
    assert result["unmatched"] == 1


def test_a_non_point_layer_is_reduced_and_says_so():
    districts = FakeLayer("districts", [_polygon(1, (0, 0, 10, 10), "North")],
                          fields=["district"], display="district")
    parcels = FakeLayer("parcels", [FakeFeature(1, FakeGeom(bounds=(2, 2, 4, 4)))])
    result = _runtime([districts, parcels]).count_in_polygons("districts", "parcels")
    assert result["groups"] == [{"group": "North", "value": 1}]
    assert result["used_representative_point"] is True


def test_counting_a_layer_into_itself_is_refused():
    layer = FakeLayer("one", [_polygon(1, (0, 0, 1, 1), "A")], fields=["district"], display="district")
    with pytest.raises(RuntimeUnavailable):
        _runtime([layer]).count_in_polygons("one", "one")


# --------------------------------------------------------------------------
# Density: the unit is the answer
# --------------------------------------------------------------------------

def test_density_states_its_unit_and_normalises_by_area():
    districts = FakeLayer("districts", [
        _polygon(1, (0, 0, 1000, 1000), "Big"),
        _polygon(2, (2000, 0, 2100, 100), "Small"),
    ], fields=["district"], display="district")
    points = FakeLayer("points", [_point(1, 500, 500), _point(2, 2050, 50)])
    result = _runtime([districts, points]).density("districts", "points")
    assert result["unit"] == "per km²"
    # 1 point in 1 km² against 1 point in 0.01 km²: the small district is the
    # dense one, which is the whole reason density is not a count.
    assert result["values"]["Big"] == pytest.approx(1.0)
    assert result["values"]["Small"] == pytest.approx(100.0)


def test_density_on_a_geographic_layer_is_refused_not_approximated():
    degrees = FakeCrs("EPSG:4326", geographic=True)
    districts = FakeLayer("districts", [_polygon(1, (0, 0, 1, 1), "North")],
                          crs=degrees, fields=["district"], display="district")
    points = FakeLayer("points", [_point(1, 0.5, 0.5)], crs=degrees)
    with pytest.raises(RuntimeUnavailable) as error:
        _runtime([districts, points], crs=degrees).density("districts", "points")
    assert "reproject" in str(error.value).lower()


# --------------------------------------------------------------------------
# Distance: the refusal is the feature
# --------------------------------------------------------------------------

def test_a_metric_search_on_a_layer_in_degrees_is_refused():
    # There is no single degrees-per-metre factor. An approximation here would
    # be wrong everywhere except one parallel and would look exactly like a
    # right answer, which is worse than no answer.
    degrees = FakeCrs("EPSG:4326", geographic=True)
    parcels = FakeLayer("parcels", [FakeFeature(1, FakeGeom(bounds=(0, 0, 1, 1)))], crs=degrees)
    roads = FakeLayer("roads", [FakeFeature(1, FakeGeom(bounds=(2, 2, 3, 3)))], crs=degrees)
    with pytest.raises(RuntimeUnavailable) as error:
        _runtime([parcels, roads], crs=degrees).near("parcels", "roads", 2, "km")
    message = str(error.value)
    assert "degrees" in message and "2" in message


def test_a_metric_search_on_a_projected_layer_converts_and_reports_the_unit():
    parcels = FakeLayer("parcels", [
        FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10))),      # 90 m away
        FakeFeature(2, FakeGeom(bounds=(0, 0, 1000, 1000))),  # touching
        FakeFeature(3, FakeGeom(bounds=(5000, 5000, 5010, 5010))),  # far
    ])
    roads = FakeLayer("roads", [FakeFeature(1, FakeGeom(bounds=(100, 0, 110, 10)))])
    result = _runtime([parcels, roads]).near("parcels", "roads", 100, "m")
    assert result["map_units"] == pytest.approx(100.0)
    assert result["crs_unit"] == "m"
    assert result["metres"] == pytest.approx(100.0)
    assert result["matched"] == 2
    assert parcels.selected == [1, 2]


def test_a_search_distance_in_kilometres_is_converted_before_it_is_used():
    parcels = FakeLayer("parcels", [FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10)))])
    roads = FakeLayer("roads", [FakeFeature(1, FakeGeom(bounds=(1500, 0, 1510, 10)))])
    runtime = _runtime([parcels, roads])
    assert runtime.near("parcels", "roads", 1, "km")["matched"] == 0
    assert runtime.near("parcels", "roads", 2, "km")["matched"] == 1


# --------------------------------------------------------------------------
# Relate
# --------------------------------------------------------------------------

def test_relate_selects_the_features_that_stand_in_the_relationship():
    parcels = FakeLayer("parcels", [
        FakeFeature(1, FakeGeom(bounds=(0, 0, 5, 5))),
        FakeFeature(2, FakeGeom(bounds=(100, 100, 105, 105))),
    ])
    flood = FakeLayer("flood", [FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10)))])
    result = _runtime([parcels, flood]).relate("parcels", "flood", "intersects")
    assert result["matched"] == 1
    assert parcels.selected == [1]
    assert result["compared_in"] == "EPSG:3857"
    assert result["reference_features"] == 1


def test_disjoint_is_the_negation_and_must_test_every_reference():
    parcels = FakeLayer("parcels", [
        FakeFeature(1, FakeGeom(bounds=(0, 0, 5, 5))),
        FakeFeature(2, FakeGeom(bounds=(100, 100, 105, 105))),
    ])
    flood = FakeLayer("flood", [
        FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10))),
        FakeFeature(2, FakeGeom(bounds=(50, 50, 60, 60))),
    ])
    _runtime([parcels, flood]).relate("parcels", "flood", "disjoint")
    assert parcels.selected == [2]


def test_an_unregistered_predicate_is_refused():
    parcels = FakeLayer("parcels", [FakeFeature(1, FakeGeom(bounds=(0, 0, 5, 5)))])
    flood = FakeLayer("flood", [FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10)))])
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    with pytest.raises(CapabilityError):
        _runtime([parcels, flood]).relate("parcels", "flood", "somewhere_near")


def test_the_pair_bound_truncates_and_reports_it(monkeypatch):
    import mapdex_qgis.qgis_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "MAX_PAIR_TESTS", 2)
    parcels = FakeLayer("parcels", [FakeFeature(index, FakeGeom(bounds=(0, 0, 5, 5))) for index in range(1, 6)])
    flood = FakeLayer("flood", [
        FakeFeature(1, FakeGeom(bounds=(0, 0, 10, 10))),
        FakeFeature(2, FakeGeom(bounds=(0, 0, 10, 10))),
    ])
    result = _runtime([parcels, flood]).relate("parcels", "flood", "intersects")
    assert result["truncated"] is True
    assert result["tested"] < 5


# --------------------------------------------------------------------------
# Nearest
# --------------------------------------------------------------------------

def test_nearest_names_what_it_measured_from():
    parcels = FakeLayer("parcels", [
        FakeFeature(1, FakeGeom(bounds=(1, 1, 2, 2))),
        FakeFeature(2, FakeGeom(bounds=(50, 50, 51, 51))),
        FakeFeature(3, FakeGeom(bounds=(5, 5, 6, 6))),
    ])
    result = _runtime([parcels]).nearest("parcels", 2)
    assert result["returned"] == 2
    assert parcels.selected == [1, 3]
    assert "map view" in result["reference"]


def test_nearest_prefers_a_selection_on_exactly_one_other_layer():
    parcels = FakeLayer("parcels", [
        FakeFeature(1, FakeGeom(bounds=(1, 1, 2, 2))),
        FakeFeature(2, FakeGeom(bounds=(98, 98, 99, 99))),
    ])
    schools = FakeLayer("schools", [FakeFeature(7, FakeGeom(bounds=(100, 100, 101, 101)))])
    schools.selected = [7]
    result = _runtime([parcels, schools]).nearest("parcels", 1)
    assert parcels.selected == [2]
    assert "schools" in result["reference"]


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------

def test_every_spatial_capability_is_bound_to_the_executor():
    # The point of this whole file. These five were registered and advertised
    # while `spatial.py` was imported by nothing but its own test.
    bound = build_executor(None).capabilities
    for capability in ("spatial.relate@1", "spatial.near@1", "spatial.nearest@1",
                       "spatial.count_in_polygons@1", "spatial.density@1"):
        assert capability in bound, "{} is registered but nothing executes it".format(capability)
