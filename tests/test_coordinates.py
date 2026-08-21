"""Writing and reading a position, checked against the server value by value.

The expectations below were produced by RUNNING `packages/geodesy` on the same
six points and pasting what it printed. That is the strongest check available
for a port: not "both look plausible" but "they agree to the fourth decimal of
a metre", including the two zone exceptions that are easy to get wrong.

Bergen is in the widened zone 32 and Svalbard in the re-cut 33. A port that
skipped those exceptions would pass every other test here and put a Norwegian
survey in the wrong grid.
"""

import pytest

from mapdex_qgis._vendor.nivo.coordinates import (
    combined_scale,
    format_angle,
    format_dms,
    parse_dms,
    point_scale_factor,
    to_mgrs,
    to_utm,
    utm_zone_for,
)
from mapdex_qgis._vendor.nivo.survey import SurveyError, ellipsoid_by_name

# lat, lon, zone, band, easting, northing, mgrs, scale
FROM_THE_SERVER = [
    (39.925533, 32.866287, 36, "S", 488573.9079, 4419500.7203, "36SVK8857319500", 0.9996016072),
    (-33.8688, 151.2093, 56, "H", 334368.6336, 6250948.3453, "56HLH3436850948", 0.9999382005),
    (60.39, 5.32, 32, "V", 297230.2202, 6700510.1760, "32VKN9723000510", 1.0001038550),
    (78.22, 15.65, 33, "X", 514813.5273, 8683004.1541, "33XWG1481383004", 0.9996026816),
    (38.9592, -77.1456, 18, "S", 314092.8866, 4314438.2265, "18SUJ1409214438", 1.0000255913),
    (0.3476, 32.5825, 36, "N", 453543.1383, 38421.2756, "36NVF5354338421", 0.9996267162),
]


@pytest.mark.parametrize("lat,lon,zone,band,easting,northing,mgrs,scale", FROM_THE_SERVER)
def test_the_port_agrees_with_the_server(lat, lon, zone, band, easting, northing, mgrs, scale):
    utm = to_utm(lat, lon)
    assert utm["zone"] == zone
    assert utm["band"] == band
    assert utm["easting_m"] == pytest.approx(easting, abs=0.0001)
    assert utm["northing_m"] == pytest.approx(northing, abs=0.0001)
    assert to_mgrs(lat, lon, 5) == mgrs
    assert point_scale_factor(lat, lon) == pytest.approx(scale, abs=1e-10)


def test_the_two_zone_exceptions_are_not_skipped():
    # South-west Norway widens 32 westward; the rule that produces every other
    # zone would put Bergen in 31.
    assert utm_zone_for(60.39, 5.32)[0] == 32
    assert utm_zone_for(55.0, 5.32)[0] == 31  # same longitude, outside the band
    # Svalbard re-cuts four zones.
    assert utm_zone_for(78.0, 15.0)[0] == 33
    assert utm_zone_for(78.0, 25.0)[0] == 35


def test_utm_refuses_the_latitudes_it_does_not_cover():
    for lat in (85.0, -81.0):
        with pytest.raises(SurveyError):
            to_utm(lat, 0.0)


def test_mgrs_precision_is_bounded_and_shortens_predictably():
    full = to_mgrs(39.925533, 32.866287, 5)
    assert to_mgrs(39.925533, 32.866287, 0) == full[:5]
    assert len(to_mgrs(39.925533, 32.866287, 3)) == 11
    for digits in (-1, 6, 2.5):
        with pytest.raises(SurveyError):
            to_mgrs(39.925533, 32.866287, digits)


def test_a_written_coordinate_with_no_sign_and_no_hemisphere_is_refused():
    # It is a position AND its mirror image, and choosing one is a coin toss
    # the reader cannot see.
    with pytest.raises(SurveyError):
        parse_dms("39 55 31.92")


def test_the_forms_field_notes_actually_contain():
    assert parse_dms("39°55'31.92\"N") == pytest.approx(39.925533, abs=1e-6)
    assert parse_dms("39 55 31.92 N") == pytest.approx(39.925533, abs=1e-6)
    assert parse_dms("-39 55 31.92") == pytest.approx(-39.925533, abs=1e-6)
    assert parse_dms("39:55:31.92S") == pytest.approx(-39.925533, abs=1e-6)
    assert parse_dms("32,8663 E") == pytest.approx(32.8663, abs=1e-6)


def test_a_coordinate_that_contradicts_itself_is_refused():
    # Signed and lettered the same way is a transcription error, and agreeing
    # with one of them hides it.
    with pytest.raises(SurveyError):
        parse_dms("-39 55 31.92 S")
    with pytest.raises(SurveyError):
        parse_dms("39N 55E")
    with pytest.raises(SurveyError):
        parse_dms("39 61 31.92 N")


def test_writing_carries_the_sixty_second_case():
    # 59.999 seconds rounds to 60.00 at two decimals, and 39d55'60.00" makes a
    # reader distrust everything else on the page.
    assert "60.00" not in format_dms(39.0 + 55 / 60.0 + 59.999 / 3600.0, True)
    assert format_dms(39.999999999, True).startswith("40°00'00.00")


def test_an_azimuth_has_no_hemisphere():
    # Appending an E to a bearing would read as a longitude.
    written = format_angle(47.5)
    assert written == "47°30'00.00\""
    assert not any(letter in written for letter in "NSEW")


def test_the_two_scale_factors_are_reported_apart():
    # A discrepancy at a zone edge and one on a mountain need different
    # answers, and a single combined number hides which one is being looked at.
    at_altitude = combined_scale(39.925533, 32.866287, 1800.0,
                                 height_reference="ellipsoidal")
    assert at_altitude["point_scale"] == pytest.approx(0.9996016072, abs=1e-10)
    assert at_altitude["elevation_factor"] < 1.0
    assert at_altitude["combined_factor"] == pytest.approx(
        at_altitude["point_scale"] * at_altitude["elevation_factor"], abs=1e-12)
    # No geoid was needed, so nothing is left unapplied.
    assert at_altitude["unapplied_separation_ppm"] == 0.0


def test_an_unknown_geoid_separation_is_reported_rather_than_hidden():
    # A levelled height is measured from the geoid, so using it as an
    # ellipsoidal height is wrong by the separation. The SIZE of the omission
    # is stated: about 4.7 ppm, under 5 mm per kilometre.
    levelled = combined_scale(39.925533, 32.866287, 850.0)
    assert levelled["height_reference"] == "orthometric"
    assert levelled["unapplied_separation_ppm"] == pytest.approx(4.7, abs=0.2)

    known = combined_scale(39.925533, 32.866287, 850.0, geoid_separation_m=36.0)
    assert known["unapplied_separation_ppm"] == 0.0
    assert known["ellipsoidal_height_m"] == pytest.approx(886.0)


def test_the_height_reference_must_be_one_of_the_two():
    with pytest.raises(SurveyError):
        combined_scale(39.9, 32.8, 100.0, height_reference="above sea level")


def test_the_surface_reaches_these_too_and_is_not_a_datum_shift():
    """The ellipsoid changes the projection; it does not move the point.

    My first version of this test asserted the two differ "by hundreds of
    metres" and it failed at half a metre, which is the correct answer and a
    useful one. Projecting the SAME latitude and longitude onto ED50's
    ellipsoid rather than WGS84's changes the grid coordinates only by the
    difference between the two figures of the earth - a few decimetres here.

    The hundreds of metres between ED50 and WGS84 come from the DATUM SHIFT:
    the same physical point has different latitude and longitude in the two
    systems. This function does not do that and must not be read as doing it.
    Handing it WGS84 coordinates with `ellipsoid="ED50"` produces a number that
    is not an ED50 grid reference.
    """
    on_ed50 = to_utm(39.925533, 32.866287, ellipsoid_by_name("ED50"))
    on_wgs = to_utm(39.925533, 32.866287)
    difference = abs(on_ed50["easting_m"] - on_wgs["easting_m"])
    assert 0.01 < difference < 5.0
    assert on_ed50["ellipsoid"] == "International 1924"
