"""Spatial reasoning that is correct about units, plus a pure geometry fallback.

Two responsibilities live here, both deliberately free of QGIS imports:

1. **Unit correctness.** "Buffer roads by 200 metres" on an EPSG:4326 layer must
   never become "buffer by 200 degrees". :func:`plan_distance` turns a request
   in real-world units into an explicit, inspectable plan and refuses to guess.
   This is the single most damaging class of silent GIS error, so it is decided
   in one place and covered by tests rather than left to each call site.

2. **A small exact geometry kernel** (ray-casting point-in-polygon, bbox
   predicates, shoelace area) used for tests and for cheap in-memory work. At
   runtime the QGIS adapter supplies the real engine for anything heavier; the
   orchestration above it is shared, so both paths agree on what an answer means.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

# Ring geometry is bounded so a pathological feature cannot stall the UI thread.
MAX_RING_VERTICES = 200_000

# Linear units expressed in metres. Only units a CRS actually declares are here;
# an unknown unit is an explicit failure, never a silent 1:1 assumption.
METRES_PER_UNIT = {
    "m": 1.0,
    "metre": 1.0,
    "meter": 1.0,
    "metres": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kilometre": 1000.0,
    "kilometer": 1000.0,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "us-ft": 1200.0 / 3937.0,
    "us survey foot": 1200.0 / 3937.0,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
    "nmi": 1852.0,
    "yd": 0.9144,
    "yard": 0.9144,
}

# Units that mean "this CRS is angular". A distance in these is not a length.
ANGULAR_UNITS = {"degree", "degrees", "deg", "radian", "radians", "gon", "grad"}


def normalize_unit(unit: str | None) -> str:
    return str(unit or "").strip().lower().replace("_", "-")


def to_metres(distance: float, unit: str) -> float | None:
    """Convert a real-world distance to metres, or None for an unknown unit."""
    factor = METRES_PER_UNIT.get(normalize_unit(unit))
    if factor is None:
        return None
    return float(distance) * factor


def plan_distance(
    crs_is_geographic: bool,
    crs_map_units: str,
    distance: float,
    unit: str = "m",
) -> dict[str, Any]:
    """Decide how to apply a real-world distance on a layer's own CRS.

    Returns one of three strategies, always stating which was chosen:

    ``map_units``
        The layer is projected in a known linear unit; the distance is converted
        into map units and can be used directly.
    ``reproject``
        The layer is geographic (degrees). A metric operation is meaningless in
        degrees, so the caller must reproject to a projected CRS first. The plan
        carries the metre value; it never fabricates a degree equivalent.
    ``unsupported``
        The unit or the CRS unit is unknown. The caller must ask, not guess.
    """
    try:
        magnitude = float(distance)
    except (TypeError, ValueError):
        return {"strategy": "unsupported", "reason": "invalid_distance"}
    if not math.isfinite(magnitude) or magnitude <= 0:
        return {"strategy": "unsupported", "reason": "invalid_distance"}
    metres = to_metres(magnitude, unit)
    if metres is None:
        return {"strategy": "unsupported", "reason": "unknown_unit", "unit": normalize_unit(unit)}
    map_unit = normalize_unit(crs_map_units)
    if crs_is_geographic or map_unit in ANGULAR_UNITS:
        # Deliberately no degrees-per-metre shortcut. The conversion is
        # latitude-dependent and anisotropic, so a single scalar would be wrong
        # everywhere except one parallel.
        return {
            "strategy": "reproject",
            "metres": metres,
            "requested": magnitude,
            "unit": normalize_unit(unit),
            "reason": "geographic_crs_cannot_measure_length",
        }
    factor = METRES_PER_UNIT.get(map_unit)
    if factor is None:
        return {"strategy": "unsupported", "reason": "unknown_crs_unit", "crs_unit": map_unit}
    return {
        "strategy": "map_units",
        "metres": metres,
        "map_units": metres / factor,
        "crs_unit": map_unit,
        "requested": magnitude,
        "unit": normalize_unit(unit),
    }


def area_conversion(crs_map_units: str) -> float | None:
    """Square-metres per squared map unit, or None when the CRS is not linear."""
    factor = METRES_PER_UNIT.get(normalize_unit(crs_map_units))
    if factor is None:
        return None
    return factor * factor


# --------------------------------------------------------------------------
# Exact small geometry kernel
# --------------------------------------------------------------------------

Point = tuple[float, float]
Ring = Sequence[Point]


def ring_area(ring: Ring) -> float:
    """Unsigned shoelace area of a closed or open ring, in squared map units."""
    if len(ring) < 3:
        return 0.0
    total = 0.0
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_area(rings: Sequence[Ring]) -> float:
    """Area of a polygon whose first ring is the shell and the rest are holes."""
    if not rings:
        return 0.0
    return max(0.0, ring_area(rings[0]) - math.fsum(ring_area(ring) for ring in rings[1:]))


def ring_bbox(ring: Ring) -> tuple[float, float, float, float]:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_intersects(left: Sequence[float], right: Sequence[float]) -> bool:
    """Do two [minx, miny, maxx, maxy] boxes share any area or edge?"""
    if len(left) != 4 or len(right) != 4:
        return False
    return not (left[2] < right[0] or right[2] < left[0] or left[3] < right[1] or right[3] < left[1])


def bbox_contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    if len(outer) != 4 or len(inner) != 4:
        return False
    return (
        outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]
    )


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Ray-casting containment test, counting boundary points as inside.

    Boundary handling matters: a point sitting exactly on a shared parcel edge
    must be counted once and deterministically, otherwise "points per polygon"
    totals drift depending on floating point noise.
    """
    if len(ring) < 3 or len(ring) > MAX_RING_VERTICES:
        return False
    x, y = point
    inside = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if _on_segment(x, y, x1, y1, x2, y2):
            return True
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
    return inside


def _on_segment(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
    if abs(cross) > 1e-12:
        return False
    return min(x1, x2) - 1e-12 <= x <= max(x1, x2) + 1e-12 and min(y1, y2) - 1e-12 <= y <= max(y1, y2) + 1e-12


def point_in_polygon(point: Point, rings: Sequence[Ring]) -> bool:
    """Containment against a shell plus holes."""
    if not rings or not point_in_ring(point, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(point, hole) and not _on_ring_boundary(point, hole):
            return False
    return True


def _on_ring_boundary(point: Point, ring: Ring) -> bool:
    x, y = point
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if _on_segment(x, y, x1, y1, x2, y2):
            return True
    return False


def count_points_in_polygons(
    polygons: Sequence[Mapping[str, Any]],
    points: Iterable[Mapping[str, Any]],
    id_key: str = "id",
) -> dict[str, Any]:
    """Point-in-polygon counting - the kernel behind "buildings per district".

    Each polygon is ``{"id":…, "rings":[[(x,y),…], …]}`` and each point is
    ``{"id":…, "point": (x, y)}``. Both sets must already be in the same CRS;
    the caller owns that transform, and :func:`plan_distance` documents why the
    boundary is drawn there. Points that fall in no polygon are counted and
    reported rather than dropped, because "12 of 500 buildings fell outside
    every district" is exactly the kind of fact an analyst needs to see.
    """
    boxes = []
    for polygon in polygons:
        rings = polygon.get("rings") or []
        box = ring_bbox(rings[0]) if rings and rings[0] else (0.0, 0.0, -1.0, -1.0)
        boxes.append(box)
    counts = {str(polygon.get(id_key)): 0 for polygon in polygons}
    unmatched = 0
    total = 0
    for record in points:
        total += 1
        position = record.get("point")
        if not position or len(position) != 2:
            unmatched += 1
            continue
        x, y = float(position[0]), float(position[1])
        hit = False
        for index, polygon in enumerate(polygons):
            box = boxes[index]
            if box[2] < box[0] or not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            if point_in_polygon((x, y), polygon.get("rings") or []):
                counts[str(polygon.get(id_key))] += 1
                hit = True
                break
        if not hit:
            unmatched += 1
    return {
        "kind": "points_in_polygons",
        "polygons": len(polygons),
        "points": total,
        "matched": total - unmatched,
        "unmatched": unmatched,
        "counts": counts,
    }


def density_per_area(
    counts: Mapping[str, int],
    areas: Mapping[str, float],
    crs_map_units: str,
    per_square_km: bool = True,
) -> dict[str, Any]:
    """Normalise counts by polygon area, stating the unit of the result.

    Density is the difference between "this district has many buildings" and
    "this district is unusually built up" - a large rural district can top a raw
    count while being the sparsest on the map. A polygon with zero area yields
    None instead of infinity.
    """
    square_metres = area_conversion(crs_map_units)
    if square_metres is None:
        return {"kind": "density", "unit": "", "values": {}, "reason": "unknown_crs_unit"}
    scale = 1_000_000.0 if per_square_km else 1.0
    unit = "per km²" if per_square_km else "per m²"
    values: dict[str, float | None] = {}
    for key, count in counts.items():
        area_units = areas.get(key)
        if not area_units or area_units <= 0:
            values[key] = None
            continue
        area_m2 = float(area_units) * square_metres
        values[key] = float(count) * scale / area_m2
    return {"kind": "density", "unit": unit, "values": values}


def transform_bbox(bbox: Sequence[float], transform) -> list[float] | None:
    """Densified bounding-box transform.

    Transforming only the two corners is wrong for anything but a translation:
    a rotated or curved projection can push the true extent well outside the
    corner-only box, which is how a "zoom to layer" lands beside the data. The
    edges are sampled so the result actually contains the transformed shape.
    """
    if len(bbox) != 4 or transform is None:
        return None
    minx, miny, maxx, maxy = (float(value) for value in bbox)
    steps = 8
    samples = []
    for index in range(steps + 1):
        ratio = index / float(steps)
        samples.append((minx + (maxx - minx) * ratio, miny))
        samples.append((minx + (maxx - minx) * ratio, maxy))
        samples.append((minx, miny + (maxy - miny) * ratio))
        samples.append((maxx, miny + (maxy - miny) * ratio))
    transformed = []
    for x, y in samples:
        try:
            point = transform(x, y)
        except Exception:
            continue
        if point and len(point) == 2 and math.isfinite(point[0]) and math.isfinite(point[1]):
            transformed.append((float(point[0]), float(point[1])))
    if not transformed:
        return None
    xs = [point[0] for point in transformed]
    ys = [point[1] for point in transformed]
    return [min(xs), min(ys), max(xs), max(ys)]


# Mean Earth radius (IUGG). Used only by the spherical fallback below, which
# always says it is spherical.
_EARTH_RADIUS_M = 6371008.8


def measure_distance(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    crs_is_geographic: bool,
    crs_map_units: str,
) -> dict[str, Any]:
    """Distance between two points, stating how it was obtained.

    The inverse of :func:`plan_distance`: that converts a real-world distance
    into a layer's units, this reads a real-world distance out of two positions.

    ``method`` is part of the answer, not decoration. A spherical result and an
    ellipsoidal one differ by up to about 0.5%, which is nothing on a site plan
    and a great deal on a cadastral boundary. A measurement that does not say
    which it is invites the reader to assume the better one.

    Returns ``unsupported`` rather than a number when the CRS unit is unknown.
    Guessing a unit here would produce a plausible figure in the wrong scale,
    which is worse than refusing.
    """
    try:
        ax, ay = float(point_a[0]), float(point_a[1])
        bx, by = float(point_b[0]), float(point_b[1])
    except (TypeError, ValueError, IndexError):
        return {"strategy": "unsupported", "reason": "invalid_points"}
    for value in (ax, ay, bx, by):
        if not math.isfinite(value):
            return {"strategy": "unsupported", "reason": "invalid_points"}

    map_unit = normalize_unit(crs_map_units)
    if crs_is_geographic or map_unit in ANGULAR_UNITS:
        if not (-180.0 <= ax <= 180.0 and -180.0 <= bx <= 180.0
                and -90.0 <= ay <= 90.0 and -90.0 <= by <= 90.0):
            # Coordinates outside the geographic domain mean the CRS and the
            # numbers disagree. Measuring anyway would return a confident,
            # meaningless figure.
            return {"strategy": "unsupported", "reason": "coordinates_outside_geographic_range"}
        return {
            "strategy": "measured",
            "metres": _haversine_metres(ax, ay, bx, by),
            "method": "spherical",
            "note": "Great-circle distance on a sphere. QGIS reports an ellipsoidal figure when available.",
        }

    factor = METRES_PER_UNIT.get(map_unit)
    if factor is None:
        return {"strategy": "unsupported", "reason": "unknown_crs_unit", "crs_unit": map_unit}
    planar = math.hypot(bx - ax, by - ay)
    return {
        "strategy": "measured",
        "metres": planar * factor,
        "map_units": planar,
        "crs_unit": map_unit,
        "method": "planar",
    }


def _haversine_metres(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    """Great-circle distance. Stable for the short distances a user clicks.

    Haversine rather than the spherical law of cosines: the latter loses
    precision at small separations through floating-point cancellation, and
    small separations are exactly what someone measuring two points on screen
    produces.
    """
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    h = (math.sin(delta_phi / 2) ** 2
         + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))
