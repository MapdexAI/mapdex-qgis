# SPDX-License-Identifier: MIT
"""Where you are, from what you can see.

Two fixes that work the other way round from an intersection: instead of
sighting an unknown point from known ones, the instrument stands on the unknown
point.

  - a distance-distance fix (trilateration), which has TWO answers;
  - a three-point resection from observed angles, which has one answer and one
    geometry where it has none at all.

The danger circle is the reason resection needs care. When the station sits on
the circle through the three known points, EVERY station on that circle
observes the same two angles, so the observations name no single place. The
margin from that case is reported with the answer, because it is the number
that decides whether the fix is worth anything - and it is free, being the
denominator the solution already divides by.

Ported formula-for-formula from `packages/geodesy/intersect.go`.
"""
from __future__ import annotations

import math
from typing import Any

from .intersect import (
    MAX_SOLVE_ITERATIONS,
    SOLVE_TOLERANCE_M,
    _direction_of,
    _Frame,
    _perpendicular,
    _solve_2x2,
    normalize_angle_signed,
)
from .survey import WGS84, Ellipsoid, SurveyError, inverse

DANGER_CIRCLE = ("the station lies on or near the circle through the three known points, "
                 "where every station on that circle observes the same angles")


def _side_of_line(start_lat, start_lon, end_lat, end_lon, lat, lon, ellipsoid) -> str:
    """Which side of a line a point falls on, read from the geometry.

    From the answer rather than from how the answer was constructed, so a
    degenerate case where both roots collapse still reports honestly.
    """
    line = inverse(start_lat, start_lon, end_lat, end_lon, ellipsoid)
    to_point = inverse(start_lat, start_lon, lat, lon, ellipsoid)
    difference = normalize_angle_signed(to_point["azimuth_deg"] - line["azimuth_deg"])
    if difference > 1e-9:
        return "right"
    if difference < -1e-9:
        return "left"
    return "on the line"


def _refine_distances(frame, guess, first, first_distance, second, second_distance, ellipsoid):
    for _ in range(MAX_SOLVE_ITERATIONS):
        candidate = frame.frm(guess)
        from_first = inverse(first[0], first[1], candidate[0], candidate[1], ellipsoid)
        from_second = inverse(second[0], second[1], candidate[0], candidate[1], ellipsoid)
        # Moving one metre along a sight changes that distance by one metre,
        # which is the whole Jacobian.
        dir_first = _direction_of(from_first["azimuth_deg"])
        dir_second = _direction_of(from_second["azimuth_deg"])
        step = _solve_2x2(
            dir_first[0], dir_first[1], dir_second[0], dir_second[1],
            first_distance - from_first["distance_m"],
            second_distance - from_second["distance_m"])
        if step is None:
            raise SurveyError("the two sights are collinear here")
        guess = (guess[0] + step[0], guess[1] + step[1])
        if math.hypot(step[0], step[1]) < SOLVE_TOLERANCE_M:
            return frame.frm(guess)
    raise SurveyError("the correction did not settle")


def intersect_distances(first_lat: float, first_lon: float, first_distance_m: float,
                        second_lat: float, second_lon: float, second_distance_m: float,
                        ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """A taped fix from two stations, which has TWO answers.

    Both are returned with the side of the baseline each falls on. Returning
    one would be choosing for the surveyor, and the choice is theirs: only they
    know which side of the line they were standing on.
    """
    if first_distance_m <= 0 or second_distance_m <= 0:
        raise SurveyError("both distances must be positive")
    baseline = inverse(first_lat, first_lon, second_lat, second_lon, ellipsoid)
    separation = baseline["distance_m"]
    if separation < SOLVE_TOLERANCE_M:
        raise SurveyError("the two stations are the same point")
    if first_distance_m + second_distance_m < separation:
        # Named with the shortfall, because the fix is to re-measure one tape
        # and the surveyor needs to know by how much.
        raise SurveyError(
            "the distances are {:.3f} m short of reaching each other".format(
                separation - (first_distance_m + second_distance_m)))
    if abs(first_distance_m - second_distance_m) > separation:
        raise SurveyError("one circle lies wholly inside the other")

    frame = _Frame(first_lat, first_lon, ellipsoid)
    second_local = frame.to(second_lat, second_lon)

    along = (separation ** 2 + first_distance_m ** 2 - second_distance_m ** 2) / (2 * separation)
    offset = math.sqrt(max(0.0, first_distance_m ** 2 - along ** 2))

    unit = (second_local[0] / separation, second_local[1] / separation)
    normal = _perpendicular(unit)
    roots = [
        (unit[0] * along - normal[0] * offset, unit[1] * along - normal[1] * offset),
        (unit[0] * along + normal[0] * offset, unit[1] * along + normal[1] * offset),
    ]

    solutions = []
    for root in roots:
        lat, lon = _refine_distances(
            frame, root, (first_lat, first_lon), first_distance_m,
            (second_lat, second_lon), second_distance_m, ellipsoid)
        solutions.append({
            "lat": lat, "lon": lon,
            "side": _side_of_line(first_lat, first_lon, second_lat, second_lon,
                                  lat, lon, ellipsoid),
        })
    return {"solutions": solutions, "ellipsoid": ellipsoid.name}


def _angle_between(at, toward, other) -> float:
    first = math.atan2(toward[0] - at[0], toward[1] - at[1])
    second = math.atan2(other[0] - at[0], other[1] - at[1])
    return abs(normalize_angle_signed(math.degrees(first - second)))


def _cot(angle_deg: float) -> float:
    return 1 / math.tan(math.radians(angle_deg))


def _tienstra_weight(interior_deg: float, observed_deg: float):
    denominator = _cot(interior_deg) - _cot(observed_deg)
    if abs(denominator) < 1e-12:
        return None
    return 1 / denominator


def _tienstra(a, b, c, angle_ab: float, angle_bc: float):
    """The classical barycentric solution, in the plane frame."""
    angle_ca = 360 - angle_ab - angle_bc
    if angle_ca <= 0 or angle_ca >= 360:
        raise SurveyError("the two observed angles leave no room for the third")
    interior_a = _angle_between(a, b, c)
    interior_b = _angle_between(b, c, a)
    interior_c = _angle_between(c, a, b)
    if min(interior_a, interior_b, interior_c) < 1e-6:
        raise SurveyError("the three known points are collinear")

    weights = [_tienstra_weight(interior_a, angle_bc),
               _tienstra_weight(interior_b, angle_ca),
               _tienstra_weight(interior_c, angle_ab)]
    if any(weight is None for weight in weights):
        raise SurveyError(DANGER_CIRCLE)
    total = sum(weights)
    scale = sum(abs(weight) for weight in weights)
    margin = abs(total) / scale if scale > 0 else 0.0
    # The weights cancelling IS the danger circle.
    if margin < 1e-3:
        raise SurveyError(DANGER_CIRCLE)
    points = [a, b, c]
    east = sum(weights[i] * points[i][0] for i in range(3)) / total
    north = sum(weights[i] * points[i][1] for i in range(3)) / total
    return margin, (east, north)


def _observed_angles(station, first, second, third, ellipsoid):
    to_first = inverse(station[0], station[1], first[0], first[1], ellipsoid)
    to_second = inverse(station[0], station[1], second[0], second[1], ellipsoid)
    to_third = inverse(station[0], station[1], third[0], third[1], ellipsoid)
    return (abs(normalize_angle_signed(to_second["azimuth_deg"] - to_first["azimuth_deg"])),
            abs(normalize_angle_signed(to_third["azimuth_deg"] - to_second["azimuth_deg"])))


def resect_three_points(first_lat: float, first_lon: float,
                        second_lat: float, second_lon: float,
                        third_lat: float, third_lon: float,
                        angle_first_second_deg: float, angle_second_third_deg: float,
                        ellipsoid: Ellipsoid = WGS84) -> dict[str, Any]:
    """Where the instrument stands, from the angles it observes."""
    for angle in (angle_first_second_deg, angle_second_third_deg):
        if angle <= 0 or angle >= 180:
            raise SurveyError("each observed angle must be between 0 and 180 degrees")

    frame = _Frame(second_lat, second_lon, ellipsoid)
    a = frame.to(first_lat, first_lon)
    b = (0.0, 0.0)
    c = frame.to(third_lat, third_lon)
    margin, guess = _tienstra(a, b, c, angle_first_second_deg, angle_second_third_deg)

    first = (first_lat, first_lon)
    second = (second_lat, second_lon)
    third = (third_lat, third_lon)

    def residuals(at):
        candidate = frame.frm(at)
        observed1, observed2 = _observed_angles(candidate, first, second, third, ellipsoid)
        return (normalize_angle_signed(observed1 - angle_first_second_deg),
                normalize_angle_signed(observed2 - angle_second_third_deg))

    # The Jacobian is numerical because the residual is an angle between two
    # geodesic azimuths. A centimetre is small enough to be linear and large
    # enough not to be lost in rounding.
    probe = 0.01
    iterations = 0
    settled = False
    while iterations < MAX_SOLVE_ITERATIONS:
        r1, r2 = residuals(guess)
        if abs(r1) < 1e-9 and abs(r2) < 1e-9:
            settled = True
            break
        east_r1, east_r2 = residuals((guess[0] + probe, guess[1]))
        north_r1, north_r2 = residuals((guess[0], guess[1] + probe))
        step = _solve_2x2(
            (east_r1 - r1) / probe, (north_r1 - r1) / probe,
            (east_r2 - r2) / probe, (north_r2 - r2) / probe,
            -r1, -r2)
        if step is None:
            raise SurveyError(DANGER_CIRCLE)
        guess = (guess[0] + step[0], guess[1] + step[1])
        iterations += 1
        if math.hypot(step[0], step[1]) < SOLVE_TOLERANCE_M:
            settled = True
            break
    if not settled:
        raise SurveyError("the correction did not settle")

    lat, lon = frame.frm(guess)
    observed1, observed2 = _observed_angles((lat, lon), first, second, third, ellipsoid)
    return {
        "lat": lat,
        "lon": lon,
        "angle_first_second_deg": observed1,
        "angle_second_third_deg": observed2,
        # How far the geometry is from the case where the observations name no
        # single place. Reported because it decides whether the fix is worth
        # anything, and it costs nothing: it is the denominator already used.
        "danger_circle_margin": margin,
        "ellipsoid": ellipsoid.name,
        "iterations": iterations,
    }
