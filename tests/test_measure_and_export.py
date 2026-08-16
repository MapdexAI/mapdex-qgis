"""Measuring and exporting: the two ends of an ordinary GIS session.

Scenario 1 finishes with "export it" and Scenario 2 is measuring between two
clicked points. Neither had a capability, so the desktop could carry a whole
analysis and then not hand it over.

The measurement maths is pure and tested here. Writing the file needs QGIS and
is not claimed as verified.
"""
import math

import pytest

from mapdex_qgis.capabilities import CapabilityError, get, validate_request
from mapdex_qgis.spatial import measure_distance


def test_a_known_distance_comes_out_right():
    # Paris to Lyon, about 392 km great-circle.
    result = measure_distance((2.3522, 48.8566), (4.8357, 45.7640), True, "degrees")
    assert result["strategy"] == "measured"
    assert 390_000 < result["metres"] < 394_000
    assert result["method"] == "spherical"


# The method is part of the answer. Spherical and ellipsoidal differ by enough
# to matter on a cadastral boundary and not at all on a site plan, and the
# number alone cannot tell them apart.
def test_the_measurement_always_names_its_method():
    geographic = measure_distance((0, 0), (0, 1), True, "degrees")
    projected = measure_distance((0, 0), (3, 4), False, "m")
    assert geographic["method"] == "spherical"
    assert projected["method"] == "planar"


def test_a_projected_measurement_is_exact():
    result = measure_distance((0, 0), (3, 4), False, "m")
    assert result["metres"] == pytest.approx(5.0)
    assert result["crs_unit"] == "m"


def test_feet_are_converted_rather_than_reported_as_metres():
    result = measure_distance((0, 0), (0, 100), False, "ft")
    assert result["strategy"] == "measured"
    assert result["metres"] == pytest.approx(30.48, rel=1e-3)
    assert result["map_units"] == pytest.approx(100.0)


# Guessing a unit produces a plausible figure in the wrong scale, which is worse
# than refusing to answer.
def test_an_unknown_unit_is_refused_rather_than_guessed():
    result = measure_distance((0, 0), (1, 1), False, "widgets")
    assert result["strategy"] == "unsupported"
    assert result["reason"] == "unknown_crs_unit"


# Coordinates outside the geographic domain mean the CRS and the numbers
# disagree; measuring anyway returns a confident, meaningless figure.
def test_coordinates_that_contradict_their_crs_are_refused():
    result = measure_distance((200, 0), (1, 1), True, "degrees")
    assert result["strategy"] == "unsupported"
    assert result["reason"] == "coordinates_outside_geographic_range"


def test_malformed_points_are_refused():
    for bad in [((None, 0), (1, 1)), (("x", 0), (1, 1)), ((float("nan"), 0), (1, 1))]:
        assert measure_distance(bad[0], bad[1], False, "m")["strategy"] == "unsupported"


def test_zero_distance_is_a_measurement_not_a_failure():
    result = measure_distance((5, 5), (5, 5), True, "degrees")
    assert result["strategy"] == "measured"
    assert math.isclose(result["metres"], 0.0, abs_tol=1e-6)


def test_both_capabilities_are_registered_and_validate_their_parameters():
    assert get("measure.distance@1") is not None
    assert get("export.layer@1") is not None

    request = validate_request("measure.distance@1", {
        "from_lon": 2.3522, "from_lat": 48.8566, "to_lon": 4.8357, "to_lat": 45.7640})
    assert request["capability"] == "measure.distance@1"

    # A longitude outside the valid range is rejected before anything runs.
    with pytest.raises(CapabilityError):
        validate_request("measure.distance@1", {
            "from_lon": 999, "from_lat": 0, "to_lon": 0, "to_lat": 0})


def test_the_export_format_is_a_closed_set():
    request = validate_request("export.layer@1", {"layer_id": "layer_a", "format": "geojson"})
    assert request["params"]["format"] == "geojson"

    with pytest.raises(CapabilityError):
        validate_request("export.layer@1", {"layer_id": "layer_a", "format": "exe"})


# The output path is derived by the runtime, never accepted as a parameter: a
# model that can name the path can be steered into overwriting something.
def test_export_does_not_accept_an_output_path():
    with pytest.raises(CapabilityError):
        validate_request("export.layer@1", {
            "layer_id": "layer_a", "format": "gpkg", "path": "/etc/passwd"})
