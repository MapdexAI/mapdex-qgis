"""Contradictions a file proves about itself.

Every case here is a real transcription failure. The point of the module is
that none of them needs to know where the data is supposed to be: a declared
system that cannot hold the numbers in the file is wrong whatever anyone
intended.
"""

from mapdex_qgis._vendor.nivo.crs_diagnose import (
    AXIS_ORDER_LOOKS_SWAPPED,
    COORDINATES_OUTSIDE_DECLARED_AREA,
    GEOGRAPHIC_CRS_WITH_PROJECTED_COORDINATES,
    PROJECTED_CRS_WITH_GEOGRAPHIC_COORDINATES,
    VERDICT_CONSISTENT,
    VERDICT_CONTRADICTED,
    VERDICT_SUSPICIOUS,
    VERDICT_UNDECLARED,
    contradictions,
    verdict,
)


def codes(findings):
    return [finding["code"] for finding in findings]


def test_a_swap_is_reported_as_a_swap_rather_than_as_a_range_error():
    # Sydney written the wrong way round: latitude 151 is impossible, and both
    # rules fire on that observation. Only one sends the reader to the right
    # fix - rewrite the axis order - rather than hunting for a projection the
    # file was never in.
    found = contradictions((-33.87, 151.21, -33.85, 151.22), "geographic")
    assert codes(found) == [AXIS_ORDER_LOOKS_SWAPPED]


def test_a_swap_under_ninety_degrees_stays_quiet():
    # Both numbers valid either way round: a great deal of the inhabited world.
    # The rule is provable or silent, never suggestive.
    assert contradictions((32.8, 39.9, 32.9, 40.0), "geographic") == []


def test_projected_coordinates_under_a_geographic_label_are_a_contradiction():
    # Nothing in a geographic system can be at 488573 east.
    found = contradictions((488573.0, 4419500.0, 489000.0, 4420000.0), "geographic")
    assert codes(found) == [GEOGRAPHIC_CRS_WITH_PROJECTED_COORDINATES]
    assert found[0]["severity"] == "error"


def test_degrees_under_a_projected_label_are_a_warning_with_their_reasoning():
    # A local engineering grid near its own origin looks exactly like this, so
    # it is a warning rather than a verdict, and the reason is attached.
    found = contradictions((32.8, 39.9, 32.9, 40.0), "projected")
    assert codes(found) == [PROJECTED_CRS_WITH_GEOGRAPHIC_COORDINATES]
    assert found[0]["severity"] == "warning"
    assert "local grid" in found[0]["reason"]


def test_data_outside_the_declared_system_s_own_area_is_a_contradiction():
    # A Turkish grid holding data in the Atlantic.
    found = contradictions(
        (400000.0, 4400000.0, 410000.0, 4410000.0), "projected",
        area_of_use=(25.0, 35.0, 45.0, 43.0),
        extent_wgs84=(-30.0, 10.0, -29.0, 11.0))
    assert COORDINATES_OUTSIDE_DECLARED_AREA in codes(found)
    outside = [f for f in found if f["code"] == COORDINATES_OUTSIDE_DECLARED_AREA][0]
    assert outside["distance_deg"] > 0


def test_data_inside_the_declared_area_says_nothing_about_it():
    found = contradictions(
        (400000.0, 4400000.0, 410000.0, 4410000.0), "projected",
        area_of_use=(25.0, 35.0, 45.0, 43.0),
        extent_wgs84=(32.0, 39.0, 33.0, 40.0))
    assert COORDINATES_OUTSIDE_DECLARED_AREA not in codes(found)


def test_a_file_with_no_declared_system_is_not_contradicting_anything():
    # Telling somebody their CRS is wrong when they never gave one sends them
    # to fix the wrong thing.
    assert contradictions((1.0, 2.0, 3.0, 4.0), None) == []
    assert verdict("", []) == VERDICT_UNDECLARED


def test_the_verdict_separates_proof_from_suspicion():
    error = [{"code": "x", "severity": "error"}]
    warning = [{"code": "y", "severity": "warning"}]
    assert verdict("EPSG:4326", error) == VERDICT_CONTRADICTED
    assert verdict("EPSG:4326", warning) == VERDICT_SUSPICIOUS
    assert verdict("EPSG:4326", []) == VERDICT_CONSISTENT
