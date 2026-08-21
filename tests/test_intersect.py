"""Fixing a point from sights, checked against the server value by value.

The expectations were produced by running `packages/geodesy/intersect.go` on
the same inputs. The two agree to nine decimal places of latitude and to the
iteration count, which is the check that matters for a port of an iterative
solver: agreeing on the answer while taking a different path would mean the two
converge differently and will eventually disagree on a harder case.
"""

import pytest

from mapdex_qgis._vendor.nivo.intersect import intersect_bearings, project_onto_line
from mapdex_qgis._vendor.nivo.survey import SurveyError

FIRST = (39.9000, 32.8000)
SECOND = (39.9000, 32.8500)


@pytest.mark.parametrize("a,b,lat,lon,d1,d2,angle,iterations", [
    (45, 315, 39.919247088, 32.825000000, 3022.6822, 3022.6822, 90.0, 2),
    (30, 350, 39.951053539, 32.838303229, 6546.3899, 5756.1523, 40.0, 3),
    (60, 300, 39.911112495, 32.825000000, 2468.3017, 2468.3017, 60.0, 3),
])
def test_intersection_agrees_with_the_server(a, b, lat, lon, d1, d2, angle, iterations):
    result = intersect_bearings(FIRST[0], FIRST[1], a, SECOND[0], SECOND[1], b)
    assert result["lat"] == pytest.approx(lat, abs=1e-9)
    assert result["lon"] == pytest.approx(lon, abs=1e-9)
    assert result["distance_from_first_m"] == pytest.approx(d1, abs=0.0001)
    assert result["distance_from_second_m"] == pytest.approx(d2, abs=0.0001)
    assert result["convergence_angle_deg"] == pytest.approx(angle, abs=1e-6)
    assert result["iterations"] == iterations


def test_a_crossing_too_shallow_to_fix_a_point_names_the_angle():
    # A fix from two sights is uncertain by roughly (bearing error) x distance
    # / sin(crossing angle). Returning a confident coordinate at half a
    # thousandth of a degree would be a number with no meaning behind it.
    with pytest.raises(SurveyError) as raised:
        intersect_bearings(FIRST[0], FIRST[1], 45, SECOND[0], SECOND[1], 45.0005)
    assert "0.0005 degrees" in str(raised.value)


def test_a_crossing_behind_the_sights_names_which_one():
    # The correction is to re-enter ONE azimuth, so the surveyor has to be told
    # which. Both being behind usually means both were entered as back-azimuths
    # and the message says so.
    with pytest.raises(SurveyError) as raised:
        intersect_bearings(FIRST[0], FIRST[1], 225, SECOND[0], SECOND[1], 135)
    assert "behind both" in str(raised.value)


def test_two_stations_at_the_same_point_have_no_intersection():
    with pytest.raises(SurveyError):
        intersect_bearings(FIRST[0], FIRST[1], 45, FIRST[0], FIRST[1], 90)


LINE = (39.9000, 32.8000, 39.9500, 32.9000)


@pytest.mark.parametrize("lat,lon,station,offset,side,beyond_start", [
    (39.9200, 32.8300, 3360.9788, 463.9729, "right", False),
    (39.9100, 32.8600, 4907.2896, -1864.3151, "left", False),
    (39.8900, 32.7800, -2039.2640, 1.7813, "right", True),
])
def test_station_and_offset_agree_with_the_server(lat, lon, station, offset, side, beyond_start):
    result = project_onto_line(*LINE, lat, lon)
    assert result["station_m"] == pytest.approx(station, abs=0.0001)
    assert result["offset_m"] == pytest.approx(offset, abs=0.0001)
    assert result["side"] == side
    assert result["beyond_start"] is beyond_start


def test_the_iteration_stops_improving_rather_than_amplifying():
    """The case that used to report "the projection did not settle".

    A point 1.9 km off a 10 km line: the first guess is already correct to
    0.14 mm, the tolerance is 0.1 mm, and the correction then DOUBLED every
    pass until the loop ran out. Far off a long line the residual is a
    difference of two nearly equal large quantities, so where it bottoms out is
    decided by the geometry rather than by the tolerance.

    The answer is now returned with the residual attached, which is the honest
    form: sub-millimetre, and said rather than implied.
    """
    result = project_onto_line(*LINE, 39.9100, 32.8600)
    assert abs(result["residual_m"]) < 0.001
    assert result["iterations"] <= 3


def test_a_point_past_the_end_is_reported_rather_than_clamped():
    # Clamping to the endpoint would hand back a measurement nobody made.
    result = project_onto_line(*LINE, 39.8900, 32.7800)
    assert result["beyond_start"] is True
    assert result["station_m"] < 0


def test_a_line_with_no_length_is_refused():
    with pytest.raises(SurveyError):
        project_onto_line(39.9, 32.8, 39.9, 32.8, 39.91, 32.81)
