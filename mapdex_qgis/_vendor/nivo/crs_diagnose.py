# SPDX-License-Identifier: MIT
"""Is this layer's declared reference system consistent with its coordinates?

Only CONTRADICTIONS: things provable from the file itself, without asking where
the data is supposed to be. A file whose declared system cannot hold the
numbers in it is wrong whatever anyone intended, and saying so needs no
external knowledge.

Guessing what the system SHOULD be is a different question and is not answered
here. It needs a hint - a place, an expected extent - and without one a ranked
list of candidates is a list of coincidences.

The rules take plain data so they can be tested without QGIS: the runtime reads
the extent, the kind and the area of use from the project and passes them in.
"""
from __future__ import annotations

from typing import Any

AXIS_ORDER_LOOKS_SWAPPED = "axis_order_looks_swapped"
GEOGRAPHIC_CRS_WITH_PROJECTED_COORDINATES = "geographic_crs_with_projected_coordinates"
PROJECTED_CRS_WITH_GEOGRAPHIC_COORDINATES = "projected_crs_with_geographic_coordinates"
COORDINATES_OUTSIDE_DECLARED_AREA = "coordinates_outside_declared_area"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

VERDICT_CONSISTENT = "consistent"
VERDICT_CONTRADICTED = "contradicted"
VERDICT_SUSPICIOUS = "suspicious"
VERDICT_UNDECLARED = "undeclared"


def _within_degrees(extent: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = extent
    return (-180 <= minx <= 180 and -180 <= maxx <= 180
            and -90 <= miny <= 90 and -90 <= maxy <= 90)


def _intersects(a: tuple[float, float, float, float],
                b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _gap_deg(a: tuple[float, float, float, float],
             b: tuple[float, float, float, float]) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return max(dx, dy)


def contradictions(extent: tuple[float, float, float, float],
                   kind: str | None,
                   area_of_use: tuple[float, float, float, float] | None = None,
                   extent_wgs84: tuple[float, float, float, float] | None = None
                   ) -> list[dict[str, Any]]:
    """Everything provable from the file alone."""
    minx, miny, maxx, maxy = extent
    findings: list[dict[str, Any]] = []
    if not kind:
        return findings

    # A swap is tested BEFORE the general range check, because it explains the
    # same observation more precisely. Both fire on "this latitude is 151", and
    # only one of them sends the reader to the right fix: rewriting the file's
    # axis order rather than hunting for a projection it was never in.
    #
    # The rule is provable rather than suggestive: the pair is invalid as
    # written and valid the other way round. When both numbers are under 90 - a
    # great deal of the inhabited world - a swap is UNDETECTABLE from the
    # coordinates, and this stays quiet instead of guessing.
    swapped = (kind == "geographic"
               and abs(minx) <= 90 and abs(maxx) <= 90
               and (abs(miny) > 90 or abs(maxy) > 90)
               and abs(miny) <= 180 and abs(maxy) <= 180)

    if swapped:
        findings.append({
            "code": AXIS_ORDER_LOOKS_SWAPPED, "severity": SEVERITY_ERROR,
            "bounds": list(extent),
            "reason": "the pair is out of range as written and in range the other way round",
        })
    elif kind == "geographic" and not _within_degrees(extent):
        # Nothing in a geographic system can exceed those ranges, so this is a
        # contradiction rather than an opinion.
        findings.append({
            "code": GEOGRAPHIC_CRS_WITH_PROJECTED_COORDINATES, "severity": SEVERITY_ERROR,
            "bounds": list(extent),
            "reason": ("a geographic CRS cannot hold a coordinate outside 180 degrees "
                       "of longitude or 90 of latitude"),
        })
    elif kind == "projected" and _within_degrees(extent):
        # A projected extent this small is possible - a local engineering grid
        # near its own origin - so this is a warning with its reasoning
        # attached, not a verdict.
        findings.append({
            "code": PROJECTED_CRS_WITH_GEOGRAPHIC_COORDINATES, "severity": SEVERITY_WARNING,
            "bounds": list(extent),
            "reason": ("every coordinate would fit inside degrees of longitude and "
                       "latitude, which is what a geographic file looks like; a local "
                       "grid near its own origin looks the same"),
        })

    if area_of_use is not None and extent_wgs84 is not None:
        if not _intersects(extent_wgs84, area_of_use):
            findings.append({
                "code": COORDINATES_OUTSIDE_DECLARED_AREA, "severity": SEVERITY_ERROR,
                "data_bounds_wgs84": list(extent_wgs84),
                "area_of_use": list(area_of_use),
                "distance_deg": round(_gap_deg(extent_wgs84, area_of_use), 4),
            })
    return findings


def verdict(declared: str | None, findings: list[dict[str, Any]]) -> str:
    """One word for what the findings add up to.

    `undeclared` is its own answer rather than a kind of failure: a file with no
    system stated is not contradicting anything, and telling somebody their CRS
    is wrong when they never gave one sends them to fix the wrong thing.
    """
    if not declared:
        return VERDICT_UNDECLARED
    for finding in findings:
        if finding.get("severity") == SEVERITY_ERROR:
            return VERDICT_CONTRADICTED
    if findings:
        return VERDICT_SUSPICIOUS
    return VERDICT_CONSISTENT
