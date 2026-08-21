"""Fixes taken from the unknown point, checked against the server.

The expectations were produced by running `packages/geodesy/intersect.go` on
the same inputs. They agree to nine decimal places of latitude, on the
danger-circle margin, and on the iteration count.
"""

import pytest

from mapdex_qgis._vendor.nivo.resection import intersect_distances, resect_three_points
from mapdex_qgis._vendor.nivo.survey import SurveyError

FIRST = (39.9000, 32.8000)
SECOND = (39.9000, 32.8500)
THIRD = (39.9400, 32.8250)


def test_a_taped_fix_returns_both_answers_with_their_sides():
    # It has two, and returning one would be choosing for the surveyor. Only
    # they know which side of the line they were standing on.
    result = intersect_distances(FIRST[0], FIRST[1], 3000, SECOND[0], SECOND[1], 3000)
    assert len(result["solutions"]) == 2
    left, right = result["solutions"]
    assert left["lat"] == pytest.approx(39.918957005, abs=1e-9)
    assert right["lat"] == pytest.approx(39.881048322, abs=1e-9)
    assert {left["side"], right["side"]} == {"left", "right"}


def test_tapes_that_cannot_reach_say_by_how_much():
    # The fix is to re-measure one tape, so the shortfall is the actionable
    # part: "no intersection" alone sends somebody back to guess which.
    with pytest.raises(SurveyError) as raised:
        intersect_distances(FIRST[0], FIRST[1], 100, SECOND[0], SECOND[1], 100)
    assert "4075.915 m short" in str(raised.value)


def test_a_circle_inside_the_other_is_refused():
    with pytest.raises(SurveyError):
        intersect_distances(FIRST[0], FIRST[1], 10000, SECOND[0], SECOND[1], 100)


def test_negative_or_zero_tapes_are_refused():
    for a, b in ((0, 3000), (3000, -1)):
        with pytest.raises(SurveyError):
            intersect_distances(FIRST[0], FIRST[1], a, SECOND[0], SECOND[1], b)


@pytest.mark.parametrize("a,b,lat,lon,margin,iterations", [
    (40, 55, 39.922213903, 32.786105881, 0.473767065, 1),
    (35, 60, 39.906698256, 32.790783505, 0.651019738, 1),
])
def test_resection_agrees_with_the_server(a, b, lat, lon, margin, iterations):
    result = resect_three_points(FIRST[0], FIRST[1], SECOND[0], SECOND[1],
                                 THIRD[0], THIRD[1], a, b)
    assert result["lat"] == pytest.approx(lat, abs=1e-9)
    assert result["lon"] == pytest.approx(lon, abs=1e-9)
    assert result["danger_circle_margin"] == pytest.approx(margin, abs=1e-9)
    assert result["iterations"] == iterations
    # The angles it reports are the ones it actually observes from the answer,
    # not the ones it was given: if they differed, the fix would be wrong and
    # the report would hide it.
    assert result["angle_first_second_deg"] == pytest.approx(a, abs=1e-6)
    assert result["angle_second_third_deg"] == pytest.approx(b, abs=1e-6)


def test_the_danger_circle_margin_travels_with_the_answer():
    # It decides whether the fix is worth anything, and it costs nothing to
    # report: it is the denominator the solution already divides by.
    result = resect_three_points(FIRST[0], FIRST[1], SECOND[0], SECOND[1],
                                 THIRD[0], THIRD[1], 40, 55)
    assert 0 < result["danger_circle_margin"] <= 1


def test_angles_outside_a_half_turn_are_refused():
    for a, b in ((0, 55), (40, 180), (-5, 55), (40, 200)):
        with pytest.raises(SurveyError):
            resect_three_points(FIRST[0], FIRST[1], SECOND[0], SECOND[1],
                                THIRD[0], THIRD[1], a, b)


def test_three_collinear_points_cannot_fix_anything():
    # Their triangle has no area, so the barycentric solution has no basis.
    with pytest.raises(SurveyError):
        resect_three_points(39.90, 32.80, 39.90, 32.85, 39.90, 32.90, 40, 55)


def test_angles_that_leave_no_room_for_the_third_are_refused():
    # The three observed angles around the station must sum to a full turn.
    with pytest.raises(SurveyError):
        resect_three_points(FIRST[0], FIRST[1], SECOND[0], SECOND[1],
                            THIRD[0], THIRD[1], 179, 179)
