"""Terrain on the desktop, and the refusal that is the whole point of it.

QGIS will compute a slope on a layer whose coordinates are in degrees and say
nothing. The Workspace's own terrain tests measure what that produces: a 10%
grade reads as **89.99 degrees**, because the horizontal distance is in degrees
and the elevation in metres, so the ratio a slope is has no meaning.

The two surfaces answer this differently on purpose. The Workspace reprojects to
a metre grid and says which one it computed on, because it holds the file and
can. The desktop refuses and names the reprojection this same assistant can
perform, because warping somebody's loaded raster underneath them is a larger
liberty than declining to answer.
"""

import pytest

from mapdex_qgis._vendor.nivo import processing


def test_the_five_core_measurements_are_named():
    assert processing.TERRAIN_OPERATIONS == {
        "slope", "aspect", "hillshade", "ruggedness", "roughness"}


def test_flow_routing_and_viewshed_are_deliberately_absent():
    # They live in the GRASS and SAGA providers, which a given install may not
    # have. A capability that usually cannot resolve is worse than one that is
    # honestly missing: the user is told the action exists and then it does not.
    for absent in ("watershed", "flow_accumulation", "viewshed", "fill_sinks"):
        assert absent not in processing.PROCESSING_OPERATION_CATALOG


def test_every_terrain_operation_resolves_to_a_core_provider():
    # native or gdal only. A saga: or grass: id here would be the exact defect
    # the test above guards against, arriving through the back door.
    for operation in processing.TERRAIN_OPERATIONS:
        candidates = processing.PROCESSING_OPERATION_CATALOG[operation]
        assert candidates, operation
        for algorithm in candidates:
            assert algorithm.split(":")[0] in {"native", "gdal", "qgis"}, algorithm


def test_only_the_three_ratio_measurements_are_unit_sensitive():
    # Roughness and ruggedness are elevation differences alone, so they carry no
    # ratio and a grid measured in degrees cannot corrupt them. Refusing them
    # would cost a real answer for no correctness gain.
    assert processing.UNIT_SENSITIVE_TERRAIN == {"slope", "aspect", "hillshade"}
    assert processing.UNIT_SENSITIVE_TERRAIN < processing.TERRAIN_OPERATIONS


def test_the_refusal_names_the_cause_and_the_fix():
    message = processing.describe_geographic_terrain_refusal("slope", "EPSG:4326")
    lowered = message.lower()
    assert "degrees" in lowered
    assert "reproject" in lowered
    assert "EPSG:4326" in message
    # The operation in the words a person used, never the algorithm id.
    assert "slope" in lowered
    assert "native:" not in lowered and "gdal:" not in lowered


def test_the_refusal_works_without_a_crs_name():
    # A layer whose reference system has no authority code still gets an
    # actionable sentence rather than a dangling parenthesis.
    message = processing.describe_geographic_terrain_refusal("hillshade")
    assert "()" not in message
    assert "hillshade" in message.lower()


def test_each_terrain_operation_has_a_human_label():
    for operation in processing.TERRAIN_OPERATIONS:
        label = processing.operation_label(operation)
        assert label and ":" not in label


class _Definition:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Algorithm:
    def __init__(self, *names):
        self._names = names

    def parameterDefinitions(self):
        return [_Definition(name) for name in self._names]


def test_the_z_factor_reaches_the_algorithm():
    # How many horizontal units one vertical unit is. Reading a surface in feet
    # as metres makes every slope three times too steep, and no metadata says
    # which it is.
    payload = processing.build_algorithm_parameters(
        _Algorithm("INPUT", "Z_FACTOR", "OUTPUT"), "slope", "layer", {"z_factor": 0.3048})
    assert payload["Z_FACTOR"] == pytest.approx(0.3048)


def test_an_absent_z_factor_means_the_elevation_is_already_in_the_horizontal_unit():
    payload = processing.build_algorithm_parameters(
        _Algorithm("INPUT", "Z_FACTOR"), "slope", "layer", {})
    assert payload["Z_FACTOR"] == 1.0


def test_edges_are_computed_where_the_algorithm_offers_it():
    # Without it the border of every terrain output is nodata, which reads as a
    # hole in the data rather than as an edge effect.
    payload = processing.build_algorithm_parameters(
        _Algorithm("INPUT", "COMPUTE_EDGES"), "roughness", "layer", {})
    assert payload["COMPUTE_EDGES"] is True


@pytest.mark.parametrize("z_factor", [0, -1, 1e9, "steep"])
def test_an_impossible_z_factor_refuses_the_whole_request(z_factor):
    # Returning the request without it would run the algorithm at the default
    # and report success for a measurement nobody asked for.
    assert processing.safe_processing_params(
        {"operation": "slope", "z_factor": z_factor}) == {}


def test_a_valid_z_factor_survives():
    safe = processing.safe_processing_params({"operation": "slope", "z_factor": 0.3048})
    assert safe == {"operation": "slope", "z_factor": 0.3048}


@pytest.mark.parametrize("band", [0, -3, 99999, "first"])
def test_an_impossible_band_refuses_the_whole_request(band):
    assert processing.safe_processing_params({"operation": "aspect", "band": band}) == {}


def test_terrain_is_not_a_two_layer_operation():
    # A slope reads one surface. Requiring a second layer would refuse every
    # legitimate request.
    for operation in processing.TERRAIN_OPERATIONS:
        assert operation not in processing.TWO_LAYER_OPERATIONS


def test_the_capabilities_are_declared_for_both_clients():
    from mapdex_qgis._vendor.nivo import capabilities

    for operation in processing.TERRAIN_OPERATIONS:
        capability = capabilities.get("terrain.{}@1".format(operation))
        assert capability is not None, operation
        # Raster only: offering a slope for a vector layer produces an algorithm
        # failure where a refusal belongs.
        assert capability.targets == ("raster",)


def test_every_terrain_capability_is_bound():
    from mapdex_qgis.qgis_runtime import PLUGIN_BOUND_CAPABILITIES, bound_capability_ids

    bound = bound_capability_ids() | PLUGIN_BOUND_CAPABILITIES
    for operation in processing.TERRAIN_OPERATIONS:
        assert "terrain.{}@1".format(operation) in bound, operation
