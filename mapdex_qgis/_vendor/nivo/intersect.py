# SPDX-License-Identifier: MIT
"""Fixing a point from sights, and where a point sits along a line.

Intersection from two bearings, and station-and-offset against a line. Both are
solved ON THE ELLIPSOID rather than in a plane: an exact azimuthal-equidistant
frame carries the geometry, a plane solution is the first guess, and a Newton
correction removes the plane assumption using residuals from the geodesic
primitives themselves.

Ported formula-for-formula from `packages/geodesy/intersect.go`, and verified
against it numerically rather than by inspection.

Two refusals matter more than the arithmetic:

  - a crossing angle too shallow to fix a point is refused with the ANGLE
    named. A fix from two sights is uncertain by roughly
    (bearing error) x (distance) / sin(crossing angle), so at two kilometres
    with a one-arc-second instrument a crossing of 0.057 degrees is already
    ten metres out. A confident coordinate there is the failure this module
    exists to avoid.
  - a crossing that falls BEHIND a sight is refused with the sight named,
    because the correction is to re-enter one azimuth and the surveyor needs
    to know which.
"""
from __future__ import annotations

import math
from typing import Any

from .survey import (
    WGS84,
    Ellipsoid,
    SurveyError,
    forward,
    inverse,
    normalize_azimuth,
)

MAX_SOLVE_ITERATIONS = 12
SOLVE_TOLERANCE_M = 1e-4
# The cross product IS the sine of the crossing angle, so this is an angle test
# rather than a coordinate one. 1e-3 is 0.057 degrees.
MIN_CROSSING_SINE = 1e-3


def normalize_angle_signed(degrees: float) -> float:
    while degrees <= -180:
        degrees += 360
    while degrees > 180:
        degrees -= 360
    return degrees


def _direction_of(azimuth_deg: float) -> tuple[float, float]:
    """The unit vector along an azimuth: (east, north).

    North is second because an azimuth is measured from north, clockwise.
    """
    radian = math.radians(azimuth_deg)
    return math.sin(radian), math.cos(radian)


def _perpendicular(direction: tuple[float, float]) -> tuple[float, float]:
    """Rotate 90 degrees clockwise: the side an increasing azimuth turns to."""
    east, north = direction
    return north, -east


def _solve_2x2(a11, a12, a21, a22, b1, b2):
    determinant = a11 * a22 - a12 * a21
    if abs(determinant) < 1e-15:
        return None
    return ((b1 * a22 - a12 * b2) / determinant,
            (a11 * b2 - b1 * a21) / determinant)


class _Frame:
    """An azimuthal-equidistant frame centred on one point.

    Converting in is `inverse` and out is `forward`, so the round trip is exact
    to the precision of the geodesic primitives. That is what lets the
    iteration blame every remaining residual on the plane assumption rather
    than on the frame.
    """

    def __init__(self, lat: float, lon: float, ellipsoid: Ellipsoid):
        self.lat = lat
        self.lon = lon
        self.ellipsoid = ellipsoid

    def to(self, lat: float, lon: float) -> tuple[float, float]:
        result = inverse(self.lat, self.lon, lat, lon, self.ellipsoid)
        east, north = _direction_of(result["azimuth_deg"])
        return east * result["distance_m"], north * result["distance_m"]

    def frm(self, offset: tuple[float, float]) -> tuple[float, float]:
        east, north = offset
        distance = math.hypot(east, north)
        if distance < 1e-9:
            return self.lat, self.lon
        azimuth = normalize_azimuth(math.degrees(math.atan2(east, north)))
        step = forward(self.lat, self.lon, azimuth, distance, self.ellipsoid)
        return step["lat"], step["lon"]


def _behind_which(first: float, second: float) -> str:
    if first < 0 and second < 0:
        return ("the crossing is behind both instruments, which usually means both "
                "azimuths were entered as back-azimuths")
    if first < 0:
        return "the crossing is behind the first station"
    return "the crossing is behind the second station"


def intersect_bearings(first_lat: float, first_lon: float, first_azimuth_deg: float,
                       second_lat: float, second_lon: float, second_azimuth_deg: float,
                       ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Where two sights cross."""
    baseline = inverse(first_lat, first_lon, second_lat, second_lon, ellipsoid)
    if baseline["distance_m"] < SOLVE_TOLERANCE_M:
        raise SurveyError("the two stations are the same point")

    frame = _Frame(first_lat, first_lon, ellipsoid)
    second_local = frame.to(second_lat, second_lon)

    first_dir = _direction_of(first_azimuth_deg)
    second_dir = _direction_of(second_azimuth_deg)
    cross = first_dir[0] * second_dir[1] - first_dir[1] * second_dir[0]
    if abs(cross) < MIN_CROSSING_SINE:
        raise SurveyError(
            "the bearings cross at {:.4f} degrees, too shallow to fix a point".format(
                math.degrees(math.asin(min(1.0, abs(cross))))))

    # Plane intersection of the two rays. BOTH parameters are wanted: each says
    # how far along its own sight the crossing lies, and a negative one means
    # behind the instrument.
    t = (second_local[0] * second_dir[1] - second_local[1] * second_dir[0]) / cross
    u = (second_local[0] * first_dir[1] - second_local[1] * first_dir[0]) / cross
    if t < 0 or u < 0:
        # Caught here rather than after the correction, because there is
        # nothing to correct: two sights pointing away from each other satisfy
        # no point at all, and the iteration would wander and then report a
        # numerical excuse instead of the real cause.
        raise SurveyError(_behind_which(t, u))

    guess = (first_dir[0] * t, first_dir[1] * t)
    iterations = 0
    while iterations < MAX_SOLVE_ITERATIONS:
        candidate = frame.frm(guess)
        from_first = inverse(first_lat, first_lon, candidate[0], candidate[1], ellipsoid)
        from_second = inverse(second_lat, second_lon, candidate[0], candidate[1], ellipsoid)
        if (from_first["distance_m"] < SOLVE_TOLERANCE_M
                or from_second["distance_m"] < SOLVE_TOLERANCE_M):
            raise SurveyError("the crossing coincides with a station")

        # Moving the point sideways to a sight by one metre turns that bearing
        # by 1/distance radians, which is the whole Jacobian.
        residual_first = math.radians(normalize_angle_signed(
            from_first["azimuth_deg"] - first_azimuth_deg))
        residual_second = math.radians(normalize_angle_signed(
            from_second["azimuth_deg"] - second_azimuth_deg))
        normal_first = _perpendicular(_direction_of(from_first["azimuth_deg"]))
        normal_second = _perpendicular(_direction_of(from_second["azimuth_deg"]))
        step = _solve_2x2(
            normal_first[0] / from_first["distance_m"],
            normal_first[1] / from_first["distance_m"],
            normal_second[0] / from_second["distance_m"],
            normal_second[1] / from_second["distance_m"],
            -residual_first, -residual_second)
        if step is None:
            raise SurveyError("the sights are too nearly parallel to fix a point")
        guess = (guess[0] + step[0], guess[1] + step[1])
        iterations += 1
        if math.hypot(step[0], step[1]) < SOLVE_TOLERANCE_M:
            break
    else:
        raise SurveyError("the correction did not settle")

    lat, lon = frame.frm(guess)
    from_first = inverse(first_lat, first_lon, lat, lon, ellipsoid)
    from_second = inverse(second_lat, second_lon, lat, lon, ellipsoid)
    # A backstop on the SOLVED bearings: the plane test catches the honest
    # cases, this catches a geometry where the correction crossed to the far
    # side of a station on its way, which the first guess could not have known.
    if (abs(normalize_angle_signed(from_first["azimuth_deg"] - first_azimuth_deg)) > 90
            or abs(normalize_angle_signed(from_second["azimuth_deg"] - second_azimuth_deg)) > 90):
        raise SurveyError("the crossing fell behind a sight after correction")

    crossing = abs(normalize_angle_signed(first_azimuth_deg - second_azimuth_deg))
    return {
        "lat": lat,
        "lon": lon,
        "distance_from_first_m": from_first["distance_m"],
        "distance_from_second_m": from_second["distance_m"],
        "convergence_angle_deg": min(crossing, 180 - crossing),
        "ellipsoid": ellipsoid.name,
        "iterations": iterations,
    }


def project_onto_line(start_lat: float, start_lon: float,
                      end_lat: float, end_lon: float,
                      point_lat: float, point_lon: float,
                      ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Station and offset: how far along a line a point sits, and how far off."""
    line = inverse(start_lat, start_lon, end_lat, end_lon, ellipsoid)
    if line["distance_m"] < SOLVE_TOLERANCE_M:
        raise SurveyError("the line has no length")
    to_point = inverse(start_lat, start_lon, point_lat, point_lon, ellipsoid)

    station = to_point["distance_m"] * math.cos(math.radians(
        normalize_angle_signed(to_point["azimuth_deg"] - line["azimuth_deg"])))
    offset = 0.0
    residual = float("inf")
    best = (station, 0.0, float("inf"))
    iterations = 0
    while iterations < MAX_SOLVE_ITERATIONS:
        foot = forward(start_lat, start_lon, line["azimuth_deg"], station, ellipsoid)
        # The geodesic's own azimuth WHERE THE FOOT SITS. It is not the azimuth
        # at the start: a geodesic curves, and using the start's azimuth is the
        # error this iteration exists to remove.
        line_azimuth_here = normalize_azimuth(foot["back_azimuth_deg"] + 180)
        from_foot = inverse(foot["lat"], foot["lon"], point_lat, point_lon, ellipsoid)
        angle = math.radians(normalize_angle_signed(
            from_foot["azimuth_deg"] - line_azimuth_here))
        along = from_foot["distance_m"] * math.cos(angle)
        offset = from_foot["distance_m"] * math.sin(angle)
        iterations += 1
        if abs(along) < abs(best[2]):
            best = (station, offset, along)
        if abs(along) < SOLVE_TOLERANCE_M:
            residual = along
            break
        if abs(along) >= abs(best[2]) and iterations > 1:
            # The step stopped improving. Far off a long line the residual is a
            # difference of two nearly equal large quantities, so it bottoms out
            # near a tenth of a millimetre and then AMPLIFIES - measured
            # doubling every pass. Reporting the best station with its residual
            # is the honest answer; carrying on until the pass limit turned a
            # sub-millimetre fix into "the projection did not settle".
            station, offset, residual = best
            break
        station += along
    else:
        station, offset, residual = best

    foot = forward(start_lat, start_lon, line["azimuth_deg"], station, ellipsoid)
    side = "on the line"
    if offset > SOLVE_TOLERANCE_M:
        side = "right"
    elif offset < -SOLVE_TOLERANCE_M:
        side = "left"
    return {
        "station_m": station,
        "offset_m": offset,
        "side": side,
        "foot_lat": foot["lat"],
        "foot_lon": foot["lon"],
        "line_length_m": line["distance_m"],
        # What the iteration could not remove, in metres along the line. Stated
        # rather than implied by a success: the geometry decides how small this
        # can get, and a reader measuring to the millimetre needs to know.
        "residual_m": residual,
        # Stated rather than clamped: a point past the end of a line is a real
        # measurement and a surveyor needs to know it is past the end, not to
        # be handed the endpoint.
        "beyond_start": station < 0,
        "beyond_end": station > line["distance_m"],
        "ellipsoid": ellipsoid.name,
        "iterations": iterations,
    }
