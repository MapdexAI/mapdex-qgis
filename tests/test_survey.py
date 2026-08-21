"""The desktop survey kernel, checked against the values the server asserts.

This is a port, and a port that disagrees with its original by a metre is worse
than no port: the same question would get two answers depending on which
surface the user happened to ask. The first three tests use Vincenty's own
published Flinders Peak / Buninyong pair, which `packages/geodesy` asserts to
the millimetre.
"""

import math

import pytest

from mapdex_qgis._vendor.nivo.survey import (
    GRS80,
    WGS84,
    SurveyError,
    closure,
    ellipsoid_by_name,
    forward,
    inverse,
    traverse,
)


def dms(degrees, minutes, seconds):
    return degrees + minutes / 60.0 + seconds / 3600.0


FLINDERS = (-dms(37, 57, 3.72030), dms(144, 25, 29.52440))
BUNINYONG = (-dms(37, 39, 10.15610), dms(143, 55, 35.38390))


def test_the_published_vincenty_pair():
    result = inverse(FLINDERS[0], FLINDERS[1], BUNINYONG[0], BUNINYONG[1])
    assert result["distance_m"] == pytest.approx(54972.271, abs=0.001)
    assert result["azimuth_deg"] == pytest.approx(dms(306, 52, 5.37), abs=0.00002)
    assert result["final_azimuth_deg"] == pytest.approx(dms(127, 10, 25.07), abs=0.00002)
    assert result["ellipsoid"] == "WGS84"


def test_forward_returns_to_where_inverse_started():
    # The two solve the same geodesic from opposite ends, so walking the answer
    # has to land on the point the question named.
    result = inverse(FLINDERS[0], FLINDERS[1], BUNINYONG[0], BUNINYONG[1])
    walked = forward(FLINDERS[0], FLINDERS[1], result["azimuth_deg"], result["distance_m"])
    assert walked["lat"] == pytest.approx(BUNINYONG[0], abs=1e-9)
    assert walked["lon"] == pytest.approx(BUNINYONG[1], abs=1e-9)


@pytest.mark.parametrize("lat,lon", [
    (39.925533, 32.866287),   # Ankara
    (-33.8688, 151.2093),     # Sydney
    (64.9631, -19.0208),      # high latitude
    (0.3476, 32.5825),        # near the equator
])
@pytest.mark.parametrize("azimuth", [0.0, 45.0, 137.5, 270.0, 359.9])
@pytest.mark.parametrize("distance", [10.0, 1000.0, 250000.0])
def test_a_walk_and_its_measurement_agree_everywhere(lat, lon, azimuth, distance):
    there = forward(lat, lon, azimuth, distance)
    back = inverse(lat, lon, there["lat"], there["lon"])
    assert back["distance_m"] == pytest.approx(distance, abs=0.001)
    assert math.cos(math.radians(back["azimuth_deg"] - azimuth)) == pytest.approx(1.0, abs=1e-9)


def test_the_back_azimuth_is_not_the_forward_one_turned_around():
    # On a curved surface it is not, and a client that assumes it is puts the
    # instrument on the wrong bearing. 60N over four degrees of longitude is
    # far enough for the difference to be visible.
    result = inverse(60.0, 10.0, 60.5, 14.0)
    naive = math.fmod(result["azimuth_deg"] + 180, 360)
    assert abs(result["back_azimuth_deg"] - naive) > 0.5


def test_nearly_antipodal_points_refuse_rather_than_returning_a_number():
    with pytest.raises(SurveyError):
        inverse(0.0, 0.0, 0.5, 179.7)


def test_coincident_points_are_zero_rather_than_undefined():
    result = inverse(41.0, 29.0, 41.0, 29.0)
    assert result["distance_m"] == 0.0


def test_the_surface_is_named_and_an_unknown_one_is_refused():
    # Computing on a different surface than the caller named, silently, is the
    # failure this module exists to avoid.
    assert ellipsoid_by_name("").name == "WGS84"
    assert ellipsoid_by_name("ED50").name == "International 1924"
    assert ellipsoid_by_name("  GRS 80 ").name == "GRS80"
    with pytest.raises(SurveyError):
        ellipsoid_by_name("the local one")


def test_the_surface_changes_the_answer():
    # If it did not, naming it would be decoration.
    on_wgs = inverse(FLINDERS[0], FLINDERS[1], BUNINYONG[0], BUNINYONG[1], WGS84)
    on_ed50 = inverse(FLINDERS[0], FLINDERS[1], BUNINYONG[0], BUNINYONG[1],
                      ellipsoid_by_name("ED50"))
    assert on_wgs["distance_m"] != on_ed50["distance_m"]
    # GRS80 differs from WGS84 in the last millimetre of the semi-minor axis,
    # so at this distance the two agree to well under a millimetre - which is
    # the point of carrying both names rather than treating them as one.
    on_grs = inverse(FLINDERS[0], FLINDERS[1], BUNINYONG[0], BUNINYONG[1], GRS80)
    assert on_wgs["distance_m"] == pytest.approx(on_grs["distance_m"], abs=0.001)


def test_an_open_traverse_reports_no_misclosure_rather_than_zero():
    # Zero would read as a perfect closure onto something it never closed onto.
    walked = traverse(39.9, 32.8, [
        {"azimuth_deg": 90, "distance_m": 100},
        {"azimuth_deg": 180, "distance_m": 100},
    ])
    assert walked["misclosure"] is None
    assert len(walked["stations"]) == 3
    assert walked["total_distance_m"] == pytest.approx(200.0)


def test_a_closed_square_closes_on_itself():
    side = 100.0
    walked = traverse(39.9, 32.8, [
        {"azimuth_deg": 0, "distance_m": side},
        {"azimuth_deg": 90, "distance_m": side},
        {"azimuth_deg": 180, "distance_m": side},
        {"azimuth_deg": 270, "distance_m": side},
    ], closed=True)
    # Not exactly zero: a square walked on an ellipsoid does not close, and the
    # residual is the convergence of the meridians rather than an error. It is
    # small, and reporting it is more honest than rounding it away.
    assert walked["misclosure"]["distance_m"] < 1.0
    assert walked["misclosure"]["precision_ratio"] > 400


def test_a_traverse_needs_legs_with_length():
    with pytest.raises(SurveyError):
        traverse(39.9, 32.8, [])
    with pytest.raises(SurveyError):
        traverse(39.9, 32.8, [{"azimuth_deg": 90, "distance_m": 0}])
    with pytest.raises(SurveyError):
        traverse(39.9, 32.8, [{"azimuth_deg": 90}])


def test_a_deed_closure_reports_its_area_and_says_how_it_was_computed():
    # A 100 m square: 10 000 m2, and the method named because a shoelace on a
    # tangent plane is not the geodesic area.
    result = closure([
        {"azimuth_deg": 0, "distance_m": 100},
        {"azimuth_deg": 90, "distance_m": 100},
        {"azimuth_deg": 180, "distance_m": 100},
        {"azimuth_deg": 270, "distance_m": 100},
    ])
    assert result["area_m2"] == pytest.approx(10000.0, abs=0.001)
    assert "tangent plane" in result["area_method"]
