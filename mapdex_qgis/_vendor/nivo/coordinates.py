# SPDX-License-Identifier: MIT
"""Writing and reading a position, and what a grid distance is worth.

Three things a surveyor does with a coordinate before anything else happens to
it: read it off a plan written in degrees, minutes and seconds; write it back
as UTM or MGRS for someone else's system; and find out how far the grid is from
the ground at that point.

None of them needs a layer or a network, so none of them should need a server.
Like `survey.py`, this is a port of `packages/geodesy` kept formula-for-formula
so the two surfaces cannot disagree about where a point is.

One refusal is load-bearing. A written coordinate with no sign and no
hemisphere - "39 55 31.92" - is a position AND its mirror image, and choosing
one is a coin toss the reader cannot see. It is refused rather than assumed.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .survey import WGS84, Ellipsoid, SurveyError, normalize_longitude

UTM_SCALE_FACTOR = 0.9996
UTM_FALSE_EASTING = 500000.0
UTM_FALSE_NORTHING = 10000000.0

# The MGRS latitude bands from 80S upward, in 8-degree steps with X covering 12.
# I and O are absent because they read as 1 and 0.
UTM_BANDS = "CDEFGHJKLMNPQRSTUVWX"
MGRS_COLUMNS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
MGRS_ROWS = ("ABCDEFGHJKLMNPQRSTUV", "FGHJKLMNPQRSTUVABCDE")

# A separation this size is what "unknown geoid" costs, reported rather than
# hidden. 30 m over the earth's radius is 4.7 ppm: under 5 mm per kilometre.
TYPICAL_GEOID_SEPARATION_M = 30.0


def _split_dms(value: float) -> tuple[int, int, float]:
    """Decompose a positive decimal degree, carrying explicitly.

    59.999 seconds rounds to 60.00 at two decimals, and printing 39d55'60.00"
    is the kind of output that makes a reader distrust everything else on the
    page.
    """
    degrees = int(value)
    remainder = (value - degrees) * 60
    minutes = int(remainder)
    seconds = (remainder - minutes) * 60
    if round(seconds * 100) / 100 >= 60:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1
    return degrees, minutes, seconds


def format_dms(value: float, is_latitude: bool) -> str:
    """A coordinate as degrees, minutes, seconds and a hemisphere."""
    if is_latitude:
        hemisphere = "S" if value < 0 else "N"
    else:
        hemisphere = "W" if value < 0 else "E"
    d, m, s = _split_dms(abs(value))
    return "{}°{:02d}'{:05.2f}\"{}".format(d, m, s, hemisphere)


def format_angle(degrees: float) -> str:
    """A bearing as degrees, minutes and seconds.

    Separate from `format_dms` because an azimuth has no hemisphere: 47d30'00"
    is a direction, and appending an E to it would read as a longitude.
    """
    d, m, s = _split_dms(math.fmod(math.fmod(degrees, 360) + 360, 360))
    return "{}°{:02d}'{:05.2f}\"".format(d, m, s)


_HEMISPHERE = re.compile(r"[NSEWnsew]")
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_dms(text: Any) -> float:
    """Read a coordinate written the way a person writes it.

    Accepts the forms field notes actually contain: 39d55'31.92"N,
    39 55 31.92 N, -39 55 31.92, 39:55:31.92N. Refuses a bare unsigned sequence
    with no hemisphere and no sign.
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        raise SurveyError("no coordinate to read")

    letters = _HEMISPHERE.findall(cleaned)
    if len(letters) > 1:
        raise SurveyError("this coordinate names more than one hemisphere")
    hemisphere = letters[0].upper() if letters else ""

    numbers = [float(part.replace(",", ".")) for part in _NUMBER.findall(cleaned)]
    if not numbers:
        raise SurveyError("no numbers in {!r}".format(text))
    if len(numbers) > 3:
        raise SurveyError("a coordinate has at most degrees, minutes and seconds")

    negative = numbers[0] < 0
    if negative and hemisphere in ("S", "W"):
        # Signed AND lettered in the same direction is a contradiction the
        # writer did not mean to make; silently agreeing with one of them hides
        # a transcription error.
        raise SurveyError("this coordinate is both signed and lettered: which is meant?")
    if not negative and not hemisphere:
        raise SurveyError(
            "this coordinate could be read more than one way: it has no sign and no hemisphere")

    value = abs(numbers[0])
    if len(numbers) > 1:
        if not 0 <= numbers[1] < 60:
            raise SurveyError("minutes must be between 0 and 60")
        value += numbers[1] / 60.0
    if len(numbers) > 2:
        if not 0 <= numbers[2] < 60:
            raise SurveyError("seconds must be between 0 and 60")
        value += numbers[2] / 3600.0

    if negative or hemisphere in ("S", "W"):
        value = -value
    return value


def utm_zone_for(lat: float, lon: float) -> tuple[int, float]:
    """The zone and its central meridian, including the two exceptions.

    South-west Norway widens zone 32 and Svalbard re-cuts four zones. They are
    not decoration: a point in Bergen belongs to 32 and computing it in 31
    puts it in the wrong grid.
    """
    normalized = normalize_longitude(lon)
    zone = int(math.floor((normalized + 180) / 6)) + 1
    if 56 <= lat < 64 and 3 <= normalized < 12:
        zone = 32
    if 72 <= lat < 84:
        if 0 <= normalized < 9:
            zone = 31
        elif 9 <= normalized < 21:
            zone = 33
        elif 21 <= normalized < 33:
            zone = 35
        elif 33 <= normalized < 42:
            zone = 37
    return zone, float((zone - 1) * 6 - 180 + 3)


def utm_band_for(lat: float) -> str:
    if lat < -80 or lat > 84:
        return ""
    if lat >= 72:
        return "X"
    index = int(math.floor((lat + 80) / 8))
    return UTM_BANDS[index] if 0 <= index < len(UTM_BANDS) else ""


def to_utm(lat: float, lon: float, ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Project a geographic position onto its UTM zone.

    The ellipsoid changes the PROJECTION, not the point. Passing WGS84
    coordinates with `ellipsoid="ED50"` moves the grid values by a few
    decimetres - the difference between the two figures of the earth - and does
    NOT produce an ED50 grid reference. The hundreds of metres between those
    systems come from the datum shift, where the same physical point has
    different latitude and longitude, and nothing here performs one.
    """
    if lat > 84 or lat < -80:
        raise SurveyError("UTM does not cover this latitude; it runs from 80S to 84N")
    zone, central_meridian = utm_zone_for(lat, lon)

    a = ellipsoid.a
    e2 = ellipsoid.f * (2 - ellipsoid.f)
    ep2 = e2 / (1 - e2)

    phi = math.radians(lat)
    sin_phi, cos_phi, tan_phi = math.sin(phi), math.cos(phi), math.tan(phi)

    n = a / math.sqrt(1 - e2 * sin_phi * sin_phi)
    t = tan_phi * tan_phi
    c = ep2 * cos_phi * cos_phi
    delta_lon = math.radians(normalize_longitude(lon - central_meridian))
    big_a = cos_phi * delta_lon

    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))

    easting = UTM_FALSE_EASTING + UTM_SCALE_FACTOR * n * (
        big_a
        + (1 - t + c) * big_a ** 3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * big_a ** 5 / 120)
    northing = UTM_SCALE_FACTOR * (m + n * tan_phi * (
        big_a ** 2 / 2
        + (5 - t + 9 * c + 4 * c * c) * big_a ** 4 / 24
        + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * big_a ** 6 / 720))

    northern = lat >= 0
    if not northern:
        northing += UTM_FALSE_NORTHING

    return {
        "zone": zone,
        "band": utm_band_for(lat),
        "northern": northern,
        "easting_m": easting,
        "northing_m": northing,
        "central_meridian": central_meridian,
        "ellipsoid": ellipsoid.name,
    }


def to_mgrs(lat: float, lon: float, digits: int = 5,
            ellipsoid: Ellipsoid = WGS84) -> str:
    """A position as an MGRS grid reference."""
    if not isinstance(digits, int) or digits < 0 or digits > 5:
        raise SurveyError("MGRS precision must be between 0 and 5 digits")
    utm = to_utm(lat, lon, ellipsoid)
    if not utm["band"]:
        raise SurveyError("MGRS does not cover this latitude")

    column = MGRS_COLUMNS[(utm["zone"] - 1) % 3]
    column_index = int(math.floor(utm["easting_m"] / 100000)) - 1
    if column_index < 0 or column_index >= len(column):
        raise SurveyError("easting is outside the zone's 100 km columns")

    row = MGRS_ROWS[(utm["zone"] - 1) % 2]
    row_index = int(math.floor(utm["northing_m"] / 100000) % 20)

    square = column[column_index] + row[row_index]
    if digits == 0:
        return "{}{}{}".format(utm["zone"], utm["band"], square)

    divisor = 10 ** (5 - digits)
    easting = int(math.floor((utm["easting_m"] % 100000) / divisor))
    northing = int(math.floor((utm["northing_m"] % 100000) / divisor))
    return "{}{}{}{:0{d}d}{:0{d}d}".format(
        utm["zone"], utm["band"], square, easting, northing, d=digits)


def _mean_radius_of_curvature(lat: float, ellipsoid: Ellipsoid) -> float:
    e2 = ellipsoid.f * (2 - ellipsoid.f)
    sin_phi = math.sin(math.radians(lat))
    w = math.sqrt(1 - e2 * sin_phi * sin_phi)
    meridional = ellipsoid.a * (1 - e2) / (w ** 3)
    prime_vertical = ellipsoid.a / w
    return math.sqrt(meridional * prime_vertical)


def point_scale_factor(lat: float, lon: float, ellipsoid: Ellipsoid = WGS84) -> float:
    """How much the UTM grid stretches distance at this point."""
    if lat > 84 or lat < -80:
        raise SurveyError("UTM does not cover this latitude; it runs from 80S to 84N")
    _, central_meridian = utm_zone_for(lat, lon)

    e2 = ellipsoid.f * (2 - ellipsoid.f)
    ep2 = e2 / (1 - e2)
    phi = math.radians(lat)
    cos_phi, tan_phi = math.cos(phi), math.tan(phi)
    t = tan_phi * tan_phi
    c = ep2 * cos_phi * cos_phi
    big_a = cos_phi * math.radians(normalize_longitude(lon - central_meridian))
    a2 = big_a * big_a
    return UTM_SCALE_FACTOR * (
        1
        + (1 + c) * a2 / 2
        + (5 - 4 * t + 42 * c + 13 * c * c - 28 * ep2) * a2 * a2 / 24
        + (61 - 148 * t + 16 * t * t) * a2 * a2 * a2 / 720)


def combined_scale(lat: float, lon: float, height_m: float = 0.0,
                   height_reference: str = "orthometric",
                   geoid_separation_m: float = 0.0,
                   ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Grid scale and elevation factor, reported SEPARATELY and then combined.

    Separately because they are different problems with different answers: a
    discrepancy at a zone edge is fixed by a projection and one on a mountain
    is fixed by a height, and a single combined number hides which one a
    surveyor is looking at.

    The height reference is stated rather than assumed. A height measured from
    the geoid - which is what a levelled height is - needs the separation added
    before it means anything to the ellipsoid, and when that separation is
    unknown the SIZE of the omission is reported instead of being hidden: about
    4.7 ppm, under 5 mm per kilometre.
    """
    if height_reference not in ("orthometric", "ellipsoidal"):
        raise SurveyError("height_reference must be orthometric or ellipsoidal")
    scale = point_scale_factor(lat, lon, ellipsoid)

    ellipsoidal = float(height_m)
    if height_reference == "orthometric":
        ellipsoidal = float(height_m) + float(geoid_separation_m)
    radius = _mean_radius_of_curvature(lat, ellipsoid)
    if radius + ellipsoidal <= 0:
        raise SurveyError("that height is below the centre of the ellipsoid")
    elevation = radius / (radius + ellipsoidal)
    combined = scale * elevation

    unapplied = 0.0
    if height_reference == "orthometric" and geoid_separation_m == 0:
        unapplied = TYPICAL_GEOID_SEPARATION_M / radius * 1e6

    zone, meridian = utm_zone_for(lat, lon)
    return {
        "point_scale": scale,
        "elevation_factor": elevation,
        "combined_factor": combined,
        "parts_per_million": (combined - 1) * 1e6,
        "ellipsoidal_height_m": ellipsoidal,
        "height_reference": height_reference,
        "geoid_separation_m": geoid_separation_m,
        "unapplied_separation_ppm": unapplied,
        "zone": zone,
        "central_meridian": meridian,
        "ellipsoid": ellipsoid.name,
    }
