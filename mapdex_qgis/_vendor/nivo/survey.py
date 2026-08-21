# SPDX-License-Identifier: MIT
"""Survey mathematics, on the desktop and offline.

Every one of these answers a question a surveyor asks about numbers they typed:
the bearing between two coordinates, the point at a bearing and distance, the
misclosure of a traverse, a position written as DMS or UTM. None of them needs
a layer, a project or a network, so none of them should need a server - and
until this module existed, all of them did.

The formulae are Vincenty's, on a NAMED ellipsoid. The name is not decoration:
the same two coordinates on WGS84 and on ED50 are different places on the
ground, and an answer that does not say which surface it was computed on is
not a survey answer. An unknown ellipsoid is refused rather than defaulted to
WGS84, because computing on the wrong surface and saying nothing is the one
failure this cannot be allowed to have.

This is a port of `packages/geodesy` in the server, deliberately kept
formula-for-formula so the two surfaces cannot drift into disagreeing about a
distance. `tests/test_survey.py` checks the port against values the Go tests
assert.
"""
from __future__ import annotations

import math
import re
from typing import Any

MAX_ITERATIONS = 200
TOLERANCE = 1e-12


class SurveyError(ValueError):
    """A question that cannot be answered as asked."""


class Ellipsoid:
    __slots__ = ("name", "a", "f")

    def __init__(self, name: str, a: float, f: float):
        self.name = name
        self.a = a
        self.f = f

    @property
    def b(self) -> float:
        return self.a * (1 - self.f)


WGS84 = Ellipsoid("WGS84", 6378137.0, 1 / 298.257223563)
GRS80 = Ellipsoid("GRS80", 6378137.0, 1 / 298.257222101)
INTERNATIONAL1924 = Ellipsoid("International 1924", 6378388.0, 1 / 297.0)
CLARKE1880IGN = Ellipsoid("Clarke 1880 (IGN)", 6378249.2, 1 / 293.4660212936269)
BESSEL1841 = Ellipsoid("Bessel 1841", 6377397.155, 1 / 299.1528128)
KRASSOWSKY1940 = Ellipsoid("Krassowsky 1940", 6378245.0, 1 / 298.3)

# Spellings, not a language table: these are the names of physical surfaces and
# the ids datum definitions use for them, which are the same in every language.
_ELLIPSOIDS = {
    "wgs84": WGS84, "wgs 84": WGS84, "wgs-84": WGS84, "epsg:4326": WGS84,
    "grs80": GRS80, "grs 80": GRS80, "grs-80": GRS80, "etrs89": GRS80,
    "nad83": GRS80, "turef": GRS80, "itrf": GRS80, "gda94": GRS80,
    "international 1924": INTERNATIONAL1924, "international1924": INTERNATIONAL1924,
    "hayford": INTERNATIONAL1924, "ed50": INTERNATIONAL1924,
    "clarke 1880": CLARKE1880IGN, "clarke1880": CLARKE1880IGN,
    "clarke 1880 (ign)": CLARKE1880IGN,
    "bessel": BESSEL1841, "bessel 1841": BESSEL1841, "bessel1841": BESSEL1841,
    "krassowsky": KRASSOWSKY1940, "krassowsky 1940": KRASSOWSKY1940,
    "krasovsky": KRASSOWSKY1940, "pulkovo 1942": KRASSOWSKY1940,
}


def ellipsoid_by_name(name: Any) -> Ellipsoid:
    """The named surface, or a refusal.

    Absent means WGS84, which is what the workspace stores geometry in. A name
    that is present and unrecognised is an ERROR: silently computing on a
    different surface than the one the caller named is the failure this module
    is built to avoid.
    """
    text = str(name or "").strip().lower()
    if not text:
        return WGS84
    found = _ELLIPSOIDS.get(re.sub(r"\s+", " ", text))
    if found is None:
        raise SurveyError("unknown ellipsoid: {}".format(name))
    return found


def normalize_azimuth(degrees: float) -> float:
    value = math.fmod(degrees, 360.0)
    return value + 360.0 if value < 0 else value


def normalize_longitude(degrees: float) -> float:
    value = math.fmod(degrees + 180.0, 360.0)
    if value < 0:
        value += 360.0
    return value - 180.0


def inverse(from_lat: float, from_lon: float, to_lat: float, to_lon: float,
            ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Distance and azimuths between two points, on the ellipsoid."""
    a, f = ellipsoid.a, ellipsoid.f
    b = ellipsoid.b

    phi1, phi2 = math.radians(from_lat), math.radians(to_lat)
    delta_lon = math.radians(to_lon - from_lon)

    tan_u1 = (1 - f) * math.tan(phi1)
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1

    tan_u2 = (1 - f) * math.tan(phi2)
    cos_u2 = 1 / math.sqrt(1 + tan_u2 * tan_u2)
    sin_u2 = tan_u2 * cos_u2

    lam = delta_lon
    sin_lambda = cos_lambda = 0.0
    sin_sigma = cos_sigma = sigma = 0.0
    cos_sq_alpha = cos2_sigma_m = 0.0
    iterations = 0

    while iterations < MAX_ITERATIONS:
        sin_lambda, cos_lambda = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt(
            (cos_u2 * sin_lambda) ** 2
            + (cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda) ** 2)
        if sin_sigma == 0:
            # Coincident points: the distance is zero and an azimuth is
            # undefined. Zero is the convention, and the distance says why.
            return {"distance_m": 0.0, "azimuth_deg": 0.0, "final_azimuth_deg": 0.0,
                    "back_azimuth_deg": 0.0, "ellipsoid": ellipsoid.name,
                    "iterations": iterations}
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lambda
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lambda / sin_sigma
        cos_sq_alpha = 1 - sin_alpha * sin_alpha
        # An equatorial line leaves cos(2 sigma_m) undefined; it is taken as 0.
        cos2_sigma_m = 0.0 if cos_sq_alpha == 0 else cos_sigma - 2 * sin_u1 * sin_u2 / cos_sq_alpha
        c = f / 16 * cos_sq_alpha * (4 + f * (4 - 3 * cos_sq_alpha))
        previous = lam
        lam = delta_lon + (1 - c) * f * sin_alpha * (
            sigma + c * sin_sigma * (cos2_sigma_m + c * cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)))
        iterations += 1
        if abs(lam - previous) < TOLERANCE:
            break
        if iterations >= MAX_ITERATIONS:
            raise SurveyError(
                "the inverse geodesic did not converge: the points are very nearly antipodal")

    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = big_b * sin_sigma * (
        cos2_sigma_m + big_b / 4 * (
            cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)
            - big_b / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos2_sigma_m ** 2)))

    reverse = math.degrees(math.atan2(
        cos_u1 * sin_lambda, -sin_u1 * cos_u2 + cos_u1 * sin_u2 * cos_lambda))
    return {
        "distance_m": b * big_a * (sigma - delta_sigma),
        "azimuth_deg": normalize_azimuth(math.degrees(math.atan2(
            cos_u2 * sin_lambda, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lambda))),
        "final_azimuth_deg": normalize_azimuth(reverse + 180),
        "back_azimuth_deg": normalize_azimuth(reverse),
        "ellipsoid": ellipsoid.name,
        "iterations": iterations,
    }


def forward(lat: float, lon: float, azimuth_deg: float, distance_m: float,
            ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """The point at an azimuth and distance: setting out."""
    if distance_m < 0:
        raise SurveyError("distance must not be negative")
    a, f = ellipsoid.a, ellipsoid.f
    b = ellipsoid.b

    alpha1 = math.radians(azimuth_deg)
    sin_alpha1, cos_alpha1 = math.sin(alpha1), math.cos(alpha1)

    tan_u1 = (1 - f) * math.tan(math.radians(lat))
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1

    sigma1 = math.atan2(tan_u1, cos_alpha1)
    sin_alpha = cos_u1 * sin_alpha1
    cos_sq_alpha = 1 - sin_alpha * sin_alpha
    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))

    sigma = distance_m / (b * big_a)
    sin_sigma = cos_sigma = cos2_sigma_m = 0.0
    for _ in range(MAX_ITERATIONS):
        cos2_sigma_m = math.cos(2 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        delta_sigma = big_b * sin_sigma * (
            cos2_sigma_m + big_b / 4 * (
                cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)
                - big_b / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma ** 2)
                * (-3 + 4 * cos2_sigma_m ** 2)))
        previous = sigma
        sigma = distance_m / (b * big_a) + delta_sigma
        if abs(sigma - previous) < TOLERANCE:
            break

    tmp = sin_u1 * sin_sigma - cos_u1 * cos_sigma * cos_alpha1
    lat2 = math.atan2(sin_u1 * cos_sigma + cos_u1 * sin_sigma * cos_alpha1,
                      (1 - f) * math.sqrt(sin_alpha * sin_alpha + tmp * tmp))
    lam = math.atan2(sin_sigma * sin_alpha1, cos_u1 * cos_sigma - sin_u1 * sin_sigma * cos_alpha1)
    c = f / 16 * cos_sq_alpha * (4 + f * (4 - 3 * cos_sq_alpha))
    l = lam - (1 - c) * f * sin_alpha * (
        sigma + c * sin_sigma * (cos2_sigma_m + c * cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)))

    return {
        "lat": math.degrees(lat2),
        "lon": normalize_longitude(lon + math.degrees(l)),
        "back_azimuth_deg": normalize_azimuth(math.degrees(math.atan2(sin_alpha, -tmp))),
        "ellipsoid": ellipsoid.name,
    }


def traverse(start_lat: float, start_lon: float, legs: list[dict[str, Any]],
             closed: bool = False, ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Walk a traverse and report where it closed.

    An OPEN traverse is not adjusted and reports no misclosure: there is
    nothing to close onto, so a misclosure would be a number about a comparison
    nobody made. A CLOSED one compares its last station with its first and
    distributes the error by the compass rule - each leg carries a share of the
    correction in proportion to its length, which is what Bowditch says and
    what the ratio at the end describes.
    """
    if not legs:
        raise SurveyError("a traverse needs at least one leg")
    stations = [{"lat": start_lat, "lon": start_lon}]
    total = 0.0
    for index, leg in enumerate(legs):
        try:
            azimuth = float(leg["azimuth_deg"])
            distance = float(leg["distance_m"])
        except (KeyError, TypeError, ValueError):
            raise SurveyError("leg {} needs azimuth_deg and distance_m".format(index + 1))
        if distance <= 0:
            raise SurveyError("leg {} has no length".format(index + 1))
        here = stations[-1]
        step = forward(here["lat"], here["lon"], azimuth, distance, ellipsoid)
        stations.append({"lat": step["lat"], "lon": step["lon"]})
        total += distance

    result: dict[str, Any] = {
        "stations": stations,
        "total_distance_m": total,
        "closed": bool(closed),
        "ellipsoid": ellipsoid.name,
    }
    if not closed:
        # Said out loud rather than reported as zero: an open traverse has no
        # misclosure, and zero would read as a perfect one.
        result["misclosure"] = None
        result["note"] = "open traverse: nothing to close onto, so no misclosure is reported"
        return result

    last = stations[-1]
    gap = inverse(last["lat"], last["lon"], start_lat, start_lon, ellipsoid)
    error = gap["distance_m"]
    result["misclosure"] = {
        "distance_m": error,
        "direction_deg": gap["azimuth_deg"],
        # The surveyor's ratio: one part in N. Reported as the denominator so
        # it reads the way it is written on a plan.
        "precision_ratio": (total / error) if error > 0 else None,
    }
    return result


def closure(legs: list[dict[str, Any]], ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """A deed description walked from its own first corner.

    The area is the shoelace on a LOCAL TANGENT PLANE, not the geodesic area,
    and the result says so. Over a parcel the difference is far below the
    measurement, and over a county it is not: naming the method is what lets a
    reader decide whether it applies to theirs.
    """
    walked = traverse(0.0, 0.0, legs, closed=True, ellipsoid=ellipsoid)
    # Plane coordinates from the same legs, which is what the shoelace needs.
    x = y = 0.0
    points = [(0.0, 0.0)]
    for leg in legs:
        azimuth = math.radians(float(leg["azimuth_deg"]))
        distance = float(leg["distance_m"])
        # Azimuth is clockwise from north, so north is +y and east is +x.
        y += distance * math.cos(azimuth)
        x += distance * math.sin(azimuth)
        points.append((x, y))

    twice = 0.0
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        twice += x1 * y2 - x2 * y1
    # Close the ring back to the first corner.
    x1, y1 = points[-1]
    x2, y2 = points[0]
    twice += x1 * y2 - x2 * y1

    return {
        "misclosure": walked["misclosure"],
        "total_distance_m": walked["total_distance_m"],
        "area_m2": abs(twice) / 2.0,
        "area_method": "shoelace on a local tangent plane, not the geodesic area",
        "ellipsoid": ellipsoid.name,
    }
