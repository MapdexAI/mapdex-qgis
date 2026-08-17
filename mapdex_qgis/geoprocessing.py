"""The everyday geoprocessing an analyst actually spends the day on.

Spatial join, select-by-location, clip, intersect, dissolve, buffer, reproject
and zonal statistics. These are the operations a GIS professional reaches for
before lunch, and Nivo could not name any of them - which is why the desktop
felt like a toy next to the tool it is embedded in.

Everything here is pure Python. No QGIS import, no GEOS, no shapely. That is a
deliberate boundary with two consequences worth stating plainly:

* **What is here is exact and testable.** Containment, segment intersection,
  polygon area, centroids, rectangle clipping and convex-polygon intersection
  are computed from the coordinates with fixed rules, so a fixture on round
  numbers has one arithmetically correct answer and a test can assert it.
* **What cannot be exact here is refused, not approximated.** Offsetting a line
  or a polygon outward (a real buffer), and intersecting two arbitrary concave
  polygons, need a full boolean/offset engine. This module returns a stated
  refusal with a reason instead of a shape that looks plausible and is wrong.
  A wrong parcel boundary that renders nicely is worse than no answer.

Three rules are enforced everywhere and are the reason this module is separate
from the QGIS adapter:

``Validation is separate from execution``
    :func:`validate_request` refuses a bad request - unknown operation, unknown
    predicate, a field the layer does not have, a metric distance on a
    geographic CRS, two layers in different CRSs - before a single feature is
    read. A refusal that arrives after a 200 000-feature scan is a refusal that
    already cost the user their afternoon.

``Units and CRS are stated, never assumed``
    A distance is resolved through :func:`mapdex_qgis.spatial.plan_distance`.
    "Buffer by 200 m" on an EPSG:4326 layer is not "buffer by 200 degrees"; it
    is a request that cannot be honoured until the layer is reprojected, and it
    is refused in exactly those words. Two input layers must declare the same
    CRS; joining across CRSs silently is how features end up in the wrong zone.

``A null is not a zero, and a feature is never dropped in passing``
    Every aggregate reports how many values it actually used and how many were
    null. A mean over 23 of 24 parcels is 396.739, not 380.2. Features that
    match nothing keep their row; features that fall in no zone are counted and
    reported rather than quietly vanishing from the total.

Geometry is plain data so the caller owns the QGIS boundary::

    {"id": 7, "geometry": {"kind": "polygon", "rings": [shell, *holes]},
     "attributes": {"kullanim": "konut", "nufus": 412}}

``kind`` is one of ``point``, ``line``, ``polygon`` (``multipolygon`` is
accepted as an alias of a multi-part polygon). Multi-part input is accepted
through ``points``/``paths``/``parts`` alongside the single-part
``point``/``path``/``rings`` spellings.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence

from . import analytics, spatial

# --------------------------------------------------------------------------
# Vocabulary. Every one of these is a technical identifier chosen by the
# caller's code, never a word matched out of a user's sentence: a predicate
# list that recognised "içinde" would be broken for every language not in it.
# --------------------------------------------------------------------------

OPERATIONS = (
    "spatial_join",
    "select_by_location",
    "clip",
    "intersect",
    "dissolve",
    "buffer",
    "reproject",
    "zonal_statistics",
)

# Predicates implemented exactly against simple (non-self-intersecting) rings.
PREDICATES = ("intersects", "contains", "within", "disjoint")

# What happens when one source feature matches several overlay features. There
# is no safe default here that suits every question, so the rule is part of the
# request and part of the answer.
JOIN_RULES = (
    "first",       # attributes of the first match in the overlay's own order
    "all",         # one output row per match; the source row multiplies
    "count_only",  # no overlay attributes, just how many matched
)

# What happens to attributes that are not the dissolve field. A field with no
# declared rule is DROPPED and listed, never carried over from whichever member
# happened to be first - that is a value invented by iteration order.
DISSOLVE_RULES = ("first", "sum", "mean", "min", "max", "count", "concat")

ZONAL_STATISTICS = ("count", "sum", "mean", "min", "max")

# How a source feature is assigned to a zone.
ASSIGNMENTS = (
    "centroid",    # one zone per feature; a concave zone may not contain its own centroid
    "intersects",  # every zone the feature touches; totals may exceed the feature count
)

GEOMETRY_KINDS = ("point", "line", "polygon")

# Stamped onto a validated request and required by :func:`execute`. A hand-built
# dict that merely names an operation is not a validated request, and letting
# one through would make the validation gate optional in practice.
KERNEL_VERSION = "geoprocessing.v1"

# Bounds. A pathological request must be refused, not left to stall the UI.
MAX_FEATURES = 200_000
MAX_PAIR_TESTS = 5_000_000
MAX_SEGMENT_PAIRS = 250_000
MAX_BUFFER_SEGMENTS = 96
MAX_PREFIX = 32
MAX_CONCAT_VALUES = 50
# Pairwise overlap detection inside a dissolve group is O(n^2); above this the
# check is reported as not performed rather than silently skipped.
MAX_OVERLAP_CHECK_MEMBERS = 200

# Coordinates are map units, which can be metres or degrees. The tolerance is
# relative-free on purpose: it is a coordinate-comparison epsilon, not a
# distance, and it is only ever used to decide whether two computed points are
# the same point.
_EPS = 1e-9

# Web Mercator is spherical by definition, so the forward formula is exact and
# needs no datum shift - which is why it is the one transform this module can
# perform without an engine.
_WEB_MERCATOR_RADIUS = 6378137.0
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066

_WGS84_AUTHIDS = {"EPSG:4326", "OGC:CRS84", "CRS:84"}
_WEB_MERCATOR_AUTHIDS = {"EPSG:3857", "EPSG:900913", "EPSG:3785"}


class GeoprocessingError(Exception):
    """A request was refused. The message is always safe to show to the user."""


# --------------------------------------------------------------------------
# CRS handling
# --------------------------------------------------------------------------

def normalize_crs(spec: Any, label: str = "layer") -> dict[str, Any]:
    """Turn a CRS description into ``{authid, is_geographic, map_units}``.

    An absent authority id is refused rather than defaulted. "Assume WGS84" is
    the assumption that turns a 200 m buffer into a 200 degree one, and a layer
    that cannot say what CRS it is in is a layer nothing should be measured on.
    """
    if not isinstance(spec, Mapping):
        raise GeoprocessingError("the {} CRS was not supplied".format(label))
    authid = str(spec.get("authid") or "").strip().upper()
    if not authid:
        raise GeoprocessingError("the {} CRS has no authority id (for example EPSG:32635)".format(label))
    units = spatial.normalize_unit(spec.get("map_units"))
    declared = spec.get("is_geographic")
    if isinstance(declared, bool):
        geographic = declared
    else:
        geographic = units in spatial.ANGULAR_UNITS
    if not units:
        units = "degree" if geographic else ""
    return {"authid": authid, "is_geographic": bool(geographic), "map_units": units}


def _require_same_crs(left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
    if left["authid"] != right["authid"]:
        raise GeoprocessingError(
            "these two layers are in different coordinate systems ({} and {}). Reproject one of them first; "
            "comparing them as they are would place features in the wrong location.".format(
                left["authid"], right["authid"])
        )


def _require_linear_crs(crs: Mapping[str, Any], what: str) -> float:
    """Square metres per squared map unit, refusing an unmeasurable CRS."""
    if crs["is_geographic"]:
        raise GeoprocessingError(
            "{} cannot be computed on {}, which measures in degrees. Reproject to a projected CRS "
            "(metres or feet) first.".format(what, crs["authid"])
        )
    factor = spatial.area_conversion(crs["map_units"])
    if factor is None:
        raise GeoprocessingError(
            "{} needs a CRS with a known linear unit; {} reports '{}'.".format(
                what, crs["authid"], crs["map_units"] or "no unit")
        )
    return factor


# --------------------------------------------------------------------------
# Geometry normalisation
# --------------------------------------------------------------------------

def _coordinate(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def _same_point(a: Sequence[float], b: Sequence[float]) -> bool:
    return abs(a[0] - b[0]) <= _EPS and abs(a[1] - b[1]) <= _EPS


def _clean_path(raw: Any) -> list[tuple[float, float]] | None:
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    points: list[tuple[float, float]] = []
    for item in raw:
        point = _coordinate(item)
        if point is None:
            return None
        if points and _same_point(points[-1], point):
            continue
        points.append(point)
    if len(points) > spatial.MAX_RING_VERTICES:
        return None
    return points or None


def _clean_ring(raw: Any) -> list[tuple[float, float]] | None:
    """A ring with no repeated closing vertex and no duplicate neighbours."""
    points = _clean_path(raw)
    if points is None:
        return None
    while len(points) > 1 and _same_point(points[0], points[-1]):
        points.pop()
    return points if len(points) >= 3 else None


def normalize_geometry(geometry: Any) -> dict[str, Any] | None:
    """Canonicalise a geometry, or return None when it is not usable.

    The canonical form is always multi-part, because a dissolve produces one and
    every consumer would otherwise need two code paths. Returning None rather
    than raising is deliberate: one malformed feature in a layer of 50 000 must
    be counted and reported, not turned into a failed run.
    """
    if not isinstance(geometry, Mapping):
        return None
    kind = str(geometry.get("kind") or geometry.get("type") or "").strip().lower()
    if kind == "multipolygon":
        kind = "polygon"
    if kind == "linestring":
        kind = "line"
    if kind not in GEOMETRY_KINDS:
        return None

    if kind == "point":
        raw = geometry.get("points")
        if raw is None and geometry.get("point") is not None:
            raw = [geometry.get("point")]
        points = _clean_path(raw)
        return {"kind": "point", "points": points} if points else None

    if kind == "line":
        raw = geometry.get("paths")
        if raw is None and geometry.get("path") is not None:
            raw = [geometry.get("path")]
        if not isinstance(raw, (list, tuple)):
            return None
        paths = []
        for candidate in raw:
            path = _clean_path(candidate)
            if path is None or len(path) < 2:
                return None
            paths.append(path)
        return {"kind": "line", "paths": paths} if paths else None

    raw = geometry.get("parts")
    if raw is None and geometry.get("rings") is not None:
        raw = [geometry.get("rings")]
    if not isinstance(raw, (list, tuple)):
        return None
    parts = []
    for candidate in raw:
        if not isinstance(candidate, (list, tuple)) or not candidate:
            return None
        rings = []
        for ring_raw in candidate:
            ring = _clean_ring(ring_raw)
            if ring is None:
                return None
            rings.append(ring)
        parts.append(rings)
    return {"kind": "polygon", "parts": parts} if parts else None


def geometry_coordinates(geometry: Mapping[str, Any]):
    """Every vertex of a canonical geometry, in order."""
    kind = geometry["kind"]
    if kind == "point":
        for point in geometry["points"]:
            yield point
    elif kind == "line":
        for path in geometry["paths"]:
            for point in path:
                yield point
    else:
        for rings in geometry["parts"]:
            for ring in rings:
                for point in ring:
                    yield point


def geometry_bbox(geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for x, y in geometry_coordinates(geometry):
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def geometry_area(geometry: Mapping[str, Any]) -> float:
    """Area in squared map units; zero for points and lines."""
    if geometry["kind"] != "polygon":
        return 0.0
    return math.fsum(spatial.polygon_area(rings) for rings in geometry["parts"])


def _ring_centroid(ring: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """Signed area and centroid of one ring (shoelace)."""
    count = len(ring)
    area = 0.0
    cx = 0.0
    cy = 0.0
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area *= 0.5
    if abs(area) <= _EPS:
        return (0.0, 0.0, 0.0)
    return (area, cx / (6.0 * area), cy / (6.0 * area))


def polygon_centroid(parts: Sequence[Sequence[Sequence[tuple[float, float]]]]) -> tuple[float, float] | None:
    """Area-weighted centroid of a multi-part polygon, holes subtracted.

    Note what this is NOT: a guarantee that the point lies inside the polygon.
    A crescent or a U-shaped district has its centroid in open air, which is why
    :data:`ASSIGNMENTS` offers ``intersects`` and why a centroid that falls in no
    zone is reported as unassigned rather than nudged to the nearest one.
    """
    weight_total = 0.0
    x_total = 0.0
    y_total = 0.0
    for rings in parts:
        for index, ring in enumerate(rings):
            area, cx, cy = _ring_centroid(ring)
            if area == 0.0:
                continue
            weight = abs(area) if index == 0 else -abs(area)
            weight_total += weight
            x_total += weight * cx
            y_total += weight * cy
    if abs(weight_total) <= _EPS:
        return None
    return (x_total / weight_total, y_total / weight_total)


def representative_point(geometry: Mapping[str, Any]) -> tuple[float, float] | None:
    """The single point that stands for a geometry under ``centroid`` assignment."""
    kind = geometry["kind"]
    if kind == "point":
        return geometry["points"][0]
    if kind == "polygon":
        return polygon_centroid(geometry["parts"])
    return None


# --------------------------------------------------------------------------
# Exact segment and ring primitives
# --------------------------------------------------------------------------

def _cross(origin, a, b) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def point_on_segment(point, start, end) -> bool:
    if abs(_cross(start, end, point)) > _EPS:
        return False
    return (min(start[0], end[0]) - _EPS <= point[0] <= max(start[0], end[0]) + _EPS
            and min(start[1], end[1]) - _EPS <= point[1] <= max(start[1], end[1]) + _EPS)


def segments_intersect(p1, p2, q1, q2) -> bool:
    """Do two closed segments share any point? Touching counts."""
    d1 = _cross(q1, q2, p1)
    d2 = _cross(q1, q2, p2)
    d3 = _cross(p1, p2, q1)
    d4 = _cross(p1, p2, q2)
    if (((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS))
            and ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS))):
        return True
    return (point_on_segment(p1, q1, q2) or point_on_segment(p2, q1, q2)
            or point_on_segment(q1, p1, p2) or point_on_segment(q2, p1, p2))


def segments_properly_cross(p1, p2, q1, q2) -> bool:
    """Do two segments cross at a point interior to both? Touching does not count.

    Used by containment: two parcels sharing an edge must not be read as one
    crossing the other, or "which parcels are inside this district" would never
    return the ones on its border.
    """
    d1 = _cross(q1, q2, p1)
    d2 = _cross(q1, q2, p2)
    d3 = _cross(p1, p2, q1)
    d4 = _cross(p1, p2, q2)
    return (((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS))
            and ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)))


def _ring_segments(ring: Sequence[tuple[float, float]]):
    count = len(ring)
    for index in range(count):
        yield ring[index], ring[(index + 1) % count]


def _path_segments(path: Sequence[tuple[float, float]]):
    for index in range(len(path) - 1):
        yield path[index], path[index + 1]


def geometry_segments(geometry: Mapping[str, Any]):
    kind = geometry["kind"]
    if kind == "line":
        for path in geometry["paths"]:
            for segment in _path_segments(path):
                yield segment
    elif kind == "polygon":
        for rings in geometry["parts"]:
            for ring in rings:
                for segment in _ring_segments(ring):
                    yield segment


def ring_is_convex(ring: Sequence[tuple[float, float]]) -> bool:
    """True when every turn along the ring has the same sign."""
    count = len(ring)
    if count < 3:
        return False
    sign = 0
    for index in range(count):
        a = ring[index]
        b = ring[(index + 1) % count]
        c = ring[(index + 2) % count]
        turn = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(turn) <= _EPS:
            continue
        current = 1 if turn > 0 else -1
        if sign == 0:
            sign = current
        elif current != sign:
            return False
    return sign != 0


def _signed_ring_area(ring: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _line_meet(p1, p2, q1, q2):
    """Intersection of the infinite lines p1p2 and q1q2, or None if parallel."""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s = (q2[0] - q1[0], q2[1] - q1[1])
    denominator = r[0] * s[1] - r[1] * s[0]
    if abs(denominator) <= _EPS:
        return None
    t = ((q1[0] - p1[0]) * s[1] - (q1[1] - p1[1]) * s[0]) / denominator
    return (p1[0] + t * r[0], p1[1] + t * r[1])


def clip_ring_to_convex(subject: Sequence[tuple[float, float]],
                        window: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a ring by a CONVEX window ring.

    Exact for a convex window, which is what makes a rectangle clip trustworthy.
    A concave *subject* still yields the correct area, but where the true result
    is two disjoint pieces the algorithm joins them with a zero-width bridge, so
    callers that care about part count must say the subject was concave. The
    window's own convexity is the caller's responsibility and is checked by
    every public entry point in this module.
    """
    output = list(subject)
    clip = list(window)
    if _signed_ring_area(clip) < 0:
        clip = list(reversed(clip))
    for edge_start, edge_end in _ring_segments(clip):
        if not output:
            return []
        current = output
        output = []
        previous = current[-1]
        previous_inside = _cross(edge_start, edge_end, previous) >= -_EPS
        for point in current:
            inside = _cross(edge_start, edge_end, point) >= -_EPS
            if inside:
                if not previous_inside:
                    meet = _line_meet(previous, point, edge_start, edge_end)
                    if meet is not None:
                        output.append(meet)
                output.append(point)
            elif previous_inside:
                meet = _line_meet(previous, point, edge_start, edge_end)
                if meet is not None:
                    output.append(meet)
            previous, previous_inside = point, inside
        # Collapse the duplicate vertices the clip introduces on an edge.
        cleaned: list[tuple[float, float]] = []
        for point in output:
            if cleaned and _same_point(cleaned[-1], point):
                continue
            cleaned.append(point)
        while len(cleaned) > 1 and _same_point(cleaned[0], cleaned[-1]):
            cleaned.pop()
        output = cleaned
    return output if len(output) >= 3 else []


def rectangle_ring(bbox: Sequence[float]) -> list[tuple[float, float]]:
    minx, miny, maxx, maxy = (float(value) for value in bbox)
    return [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]


def clip_segment_to_bbox(start, end, bbox):
    """Liang-Barsky segment clip. Returns the clipped segment or None."""
    x0, y0 = start
    x1, y1 = end
    dx = x1 - x0
    dy = y1 - y0
    low, high = 0.0, 1.0
    for numerator, denominator in (
        (x0 - bbox[0], -dx),
        (bbox[2] - x0, dx),
        (y0 - bbox[1], -dy),
        (bbox[3] - y0, dy),
    ):
        if abs(denominator) <= _EPS:
            if numerator < -_EPS:
                return None
            continue
        ratio = numerator / denominator
        if denominator < 0:
            if ratio > high:
                return None
            low = max(low, ratio)
        else:
            if ratio < low:
                return None
            high = min(high, ratio)
    if high < low:
        return None
    return ((x0 + low * dx, y0 + low * dy), (x0 + high * dx, y0 + high * dy))


def point_in_bbox(point, bbox) -> bool:
    return (bbox[0] - _EPS <= point[0] <= bbox[2] + _EPS
            and bbox[1] - _EPS <= point[1] <= bbox[3] + _EPS)


# --------------------------------------------------------------------------
# Predicates
# --------------------------------------------------------------------------

def _point_in_polygon_geometry(point, geometry: Mapping[str, Any]) -> bool:
    return any(spatial.point_in_polygon(point, rings) for rings in geometry["parts"])


def _any_vertex_inside(source: Mapping[str, Any], polygon: Mapping[str, Any]) -> bool:
    return any(_point_in_polygon_geometry(point, polygon) for point in geometry_coordinates(source))


def _segments_touch(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_segments = list(geometry_segments(left))
    right_segments = list(geometry_segments(right))
    if len(left_segments) * len(right_segments) > MAX_SEGMENT_PAIRS:
        raise GeoprocessingError(
            "these geometries have too many vertices to compare exactly in the desktop "
            "({} x {} segment pairs). Simplify them or run the operation on a smaller extent.".format(
                len(left_segments), len(right_segments))
        )
    for p1, p2 in left_segments:
        for q1, q2 in right_segments:
            if segments_intersect(p1, p2, q1, q2):
                return True
    return False


def geometries_intersect(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Do two geometries share at least one point? Touching counts."""
    if not spatial.bbox_intersects(list(geometry_bbox(left)), list(geometry_bbox(right))):
        return False
    if left["kind"] == "point" and right["kind"] == "point":
        return any(_same_point(a, b) for a in left["points"] for b in right["points"])
    if left["kind"] == "point":
        if right["kind"] == "polygon":
            return any(_point_in_polygon_geometry(point, right) for point in left["points"])
        return any(point_on_segment(point, start, end)
                   for point in left["points"] for start, end in geometry_segments(right))
    if right["kind"] == "point":
        return geometries_intersect(right, left)
    if left["kind"] == "polygon" and _any_vertex_inside(right, left):
        return True
    if right["kind"] == "polygon" and _any_vertex_inside(left, right):
        return True
    return _segments_touch(left, right)


def geometry_contains(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    """Is ``inner`` wholly inside ``outer``? Only a polygon can contain an area.

    Correct for simple rings: every vertex of the inner geometry is inside or on
    the outer boundary, and no edge of one properly crosses an edge of the
    other. The crossing test is what catches an inner edge that leaves through a
    concave notch and returns, which a vertex-only test would miss.
    """
    if outer["kind"] != "polygon":
        return False
    outer_box = geometry_bbox(outer)
    inner_box = geometry_bbox(inner)
    if not spatial.bbox_contains(list(outer_box), list(inner_box)):
        return False
    for point in geometry_coordinates(inner):
        if not _point_in_polygon_geometry(point, outer):
            return False
    if inner["kind"] == "point":
        return True
    outer_segments = list(geometry_segments(outer))
    inner_segments = list(geometry_segments(inner))
    if len(outer_segments) * len(inner_segments) > MAX_SEGMENT_PAIRS:
        raise GeoprocessingError(
            "these geometries have too many vertices to compare exactly in the desktop "
            "({} x {} segment pairs).".format(len(outer_segments), len(inner_segments))
        )
    for p1, p2 in outer_segments:
        for q1, q2 in inner_segments:
            if segments_properly_cross(p1, p2, q1, q2):
                return False
    return True


def relate(source: Mapping[str, Any], target: Mapping[str, Any], predicate: str) -> bool:
    """Evaluate one named predicate of ``source`` against ``target``."""
    if predicate == "intersects":
        return geometries_intersect(source, target)
    if predicate == "disjoint":
        return not geometries_intersect(source, target)
    if predicate == "contains":
        return geometry_contains(source, target)
    if predicate == "within":
        return geometry_contains(target, source)
    raise GeoprocessingError("unknown predicate: {}".format(str(predicate)[:40]))


def _point_to_segment_distance(point, start, end) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dx) <= _EPS and abs(dy) <= _EPS:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    ratio = max(0.0, min(1.0, ratio))
    return math.hypot(point[0] - (start[0] + ratio * dx), point[1] - (start[1] + ratio * dy))


def geometry_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Shortest distance between two geometries, in map units. Zero if they meet.

    Planar, because the coordinates are planar; the CRS gate above is what makes
    that legitimate. On a geographic CRS this is never reached - the request is
    refused before any feature is read.
    """
    if geometries_intersect(left, right):
        return 0.0
    best = float("inf")
    left_segments = list(geometry_segments(left))
    right_segments = list(geometry_segments(right))
    for point in left["points"] if left["kind"] == "point" else ():
        for start, end in right_segments:
            best = min(best, _point_to_segment_distance(point, start, end))
        for other in right["points"] if right["kind"] == "point" else ():
            best = min(best, math.hypot(point[0] - other[0], point[1] - other[1]))
    for point in right["points"] if right["kind"] == "point" else ():
        for start, end in left_segments:
            best = min(best, _point_to_segment_distance(point, start, end))
    if left_segments and right_segments:
        if len(left_segments) * len(right_segments) > MAX_SEGMENT_PAIRS:
            raise GeoprocessingError("these geometries have too many vertices to measure exactly in the desktop.")
        for p1, p2 in left_segments:
            for q1, q2 in right_segments:
                best = min(best,
                           _point_to_segment_distance(p1, q1, q2),
                           _point_to_segment_distance(p2, q1, q2),
                           _point_to_segment_distance(q1, p1, p2),
                           _point_to_segment_distance(q2, p1, p2))
    return best


# --------------------------------------------------------------------------
# Validation - refuse before a single feature is read
# --------------------------------------------------------------------------

_TWO_LAYER = frozenset({"spatial_join", "select_by_location", "intersect", "zonal_statistics"})


def _string(request: Mapping[str, Any], key: str, allowed: Sequence[str], default: str, label: str) -> str:
    value = str(request.get(key) or default).strip().lower()
    if value not in allowed:
        raise GeoprocessingError("{} must be one of: {}".format(label, ", ".join(allowed)))
    return value


def _field(request: Mapping[str, Any], key: str, declared: Any, required: bool = True) -> str:
    name = str(request.get(key) or "").strip()
    if not name:
        if required:
            raise GeoprocessingError("{} is required".format(key))
        return ""
    if isinstance(declared, (list, tuple)) and declared and name not in declared:
        raise GeoprocessingError("the field '{}' is not in that layer".format(name[:60]))
    return name


def validate_request(operation: Any, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Refuse a bad request before any layer is touched.

    Everything checkable without reading features is checked here: the operation
    name, the predicate, the join and dissolve rules, the statistic, whether the
    named fields exist (when the caller declares the layer's field list), that a
    metric distance is meaningful on this CRS, and that two input layers agree
    on their CRS. What remains for execution is only what depends on the actual
    geometry.
    """
    name = str(operation or "").strip().lower()
    if name not in OPERATIONS:
        raise GeoprocessingError("unknown operation: {}".format(str(operation)[:60]))
    request = dict(request or {})
    source_fields = request.get("source_fields")
    overlay_fields = request.get("overlay_fields")

    if name == "reproject":
        source_crs = normalize_crs(request.get("source_crs"), "source")
        target_crs = normalize_crs(request.get("target_crs"), "target")
        if source_crs["authid"] == target_crs["authid"]:
            raise GeoprocessingError(
                "the source and target CRS are both {}, so there is nothing to reproject.".format(
                    source_crs["authid"])
            )
        return {
            "validated": KERNEL_VERSION,
            "operation": name,
            "source_crs": source_crs,
            "target_crs": target_crs,
            "unit_change": source_crs["map_units"] != target_crs["map_units"],
        }

    source_crs = normalize_crs(request.get("source_crs"), "source")
    validated: dict[str, Any] = {"validated": KERNEL_VERSION, "operation": name, "source_crs": source_crs}

    needs_overlay = name in _TWO_LAYER or (name == "clip" and request.get("bbox") is None)
    if needs_overlay:
        overlay_crs = normalize_crs(request.get("overlay_crs"), "second")
        _require_same_crs(source_crs, overlay_crs)
        validated["overlay_crs"] = overlay_crs

    if name in {"spatial_join", "select_by_location", "intersect"}:
        validated["predicate"] = _string(request, "predicate", PREDICATES, "intersects", "predicate")

    if name == "spatial_join":
        raw_fields = request.get("join_fields")
        if not isinstance(raw_fields, (list, tuple)) or not raw_fields:
            raise GeoprocessingError("join_fields must list at least one field to bring across")
        fields = []
        for candidate in raw_fields:
            field = str(candidate or "").strip()
            if not field:
                raise GeoprocessingError("join_fields contains an empty field name")
            if isinstance(overlay_fields, (list, tuple)) and overlay_fields and field not in overlay_fields:
                raise GeoprocessingError("the field '{}' is not in the second layer".format(field[:60]))
            fields.append(field)
        validated["join_fields"] = fields
        validated["rule"] = _string(request, "rule", JOIN_RULES, "first", "rule")
        validated["prefix"] = str(request.get("prefix") or "joined_").strip()[:MAX_PREFIX] or "joined_"

    if name == "select_by_location":
        if request.get("distance") is None:
            validated["distance_map_units"] = None
            validated["distance_plan"] = None
        else:
            if validated["predicate"] not in {"intersects", "disjoint"}:
                # A distance turns the test into "within d of" (or, for
                # disjoint, "further than d from"). Combining it with contains
                # or within would quietly answer a different question than the
                # one whose name is in the request.
                raise GeoprocessingError(
                    "a distance can only be combined with the 'intersects' predicate (within that distance) or "
                    "'disjoint' (beyond it); '{}' asks a different question.".format(validated["predicate"]))
            unit = spatial.normalize_unit(request.get("unit") or "m")
            plan = spatial.plan_distance(
                source_crs["is_geographic"], source_crs["map_units"], request.get("distance"), unit)
            if plan["strategy"] == "reproject":
                raise GeoprocessingError(
                    "a distance of {:g} {} cannot be applied on {}, which measures in degrees. Reproject the "
                    "layer to a projected CRS first - a degree is not a fixed length.".format(
                        plan["requested"], plan["unit"], source_crs["authid"])
                )
            if plan["strategy"] != "map_units":
                raise GeoprocessingError(
                    "that distance cannot be resolved on this layer ({}).".format(plan.get("reason", "unsupported")))
            validated["distance_map_units"] = plan["map_units"]
            validated["distance_plan"] = plan

    if name == "clip":
        bbox = request.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise GeoprocessingError("bbox must be [minx, miny, maxx, maxy]")
            try:
                box = [float(value) for value in bbox]
            except (TypeError, ValueError):
                raise GeoprocessingError("bbox must contain four numbers")
            if box[0] > box[2] or box[1] > box[3]:
                raise GeoprocessingError("bbox is inverted")
            validated["bbox"] = box
        else:
            validated["bbox"] = None

    if name == "intersect":
        validated["prefix"] = str(request.get("prefix") or "joined_").strip()[:MAX_PREFIX] or "joined_"
        if validated["predicate"] != "intersects":
            raise GeoprocessingError("intersect always uses the 'intersects' predicate")

    if name == "dissolve":
        validated["field"] = _field(request, "field", source_fields, required=False)
        rules = request.get("attribute_rules") or {}
        if not isinstance(rules, Mapping):
            raise GeoprocessingError("attribute_rules must map a field name to a rule")
        resolved: dict[str, str] = {}
        for field, rule in rules.items():
            field_name = str(field or "").strip()
            if not field_name:
                raise GeoprocessingError("attribute_rules contains an empty field name")
            if isinstance(source_fields, (list, tuple)) and source_fields and field_name not in source_fields:
                raise GeoprocessingError("the field '{}' is not in that layer".format(field_name[:60]))
            rule_name = str(rule or "").strip().lower()
            if rule_name not in DISSOLVE_RULES:
                raise GeoprocessingError(
                    "the rule for '{}' must be one of: {}".format(field_name[:60], ", ".join(DISSOLVE_RULES)))
            resolved[field_name] = rule_name
        validated["attribute_rules"] = resolved

    if name == "buffer":
        unit = spatial.normalize_unit(request.get("unit") or "m")
        plan = spatial.plan_distance(
            source_crs["is_geographic"], source_crs["map_units"], request.get("distance"), unit)
        if plan["strategy"] == "reproject":
            raise GeoprocessingError(
                "a buffer of {:g} {} cannot be drawn on {}, which measures in degrees: a degree is about 111 km "
                "of latitude but shrinks towards the poles in longitude, so the shape would be wrong everywhere "
                "except one parallel. Reproject to a projected CRS first.".format(
                    plan["requested"], plan["unit"], source_crs["authid"])
            )
        if plan["strategy"] != "map_units":
            raise GeoprocessingError(
                "that buffer distance cannot be resolved on this layer ({}).".format(
                    plan.get("reason", "unsupported")))
        segments = request.get("segments", 8)
        try:
            segments = int(segments)
        except (TypeError, ValueError):
            raise GeoprocessingError("segments must be a whole number")
        if segments < 1 or segments > MAX_BUFFER_SEGMENTS:
            raise GeoprocessingError("segments must be between 1 and {}".format(MAX_BUFFER_SEGMENTS))
        validated["distance_plan"] = plan
        validated["radius_map_units"] = plan["map_units"]
        validated["segments"] = segments

    if name == "zonal_statistics":
        validated["statistic"] = _string(request, "statistic", ZONAL_STATISTICS, "count", "statistic")
        validated["assignment"] = _string(request, "assignment", ASSIGNMENTS, "centroid", "assignment")
        if validated["statistic"] == "count":
            validated["value_field"] = _field(request, "value_field", source_fields, required=False)
        else:
            validated["value_field"] = _field(request, "value_field", source_fields, required=True)
        validated["zone_field"] = _field(request, "zone_field", overlay_fields, required=False)

    return validated


# --------------------------------------------------------------------------
# Feature plumbing
# --------------------------------------------------------------------------

def _prepare(features: Sequence[Mapping[str, Any]], label: str) -> tuple[list[dict[str, Any]], int]:
    """Normalise a feature set, counting - never hiding - unusable geometry."""
    if len(features) > MAX_FEATURES:
        raise GeoprocessingError(
            "the {} layer has {} features, above the {} this desktop operation reads. Filter it or run the "
            "operation on a smaller extent.".format(label, len(features), MAX_FEATURES))
    prepared: list[dict[str, Any]] = []
    invalid = 0
    for index, feature in enumerate(features):
        geometry = normalize_geometry((feature or {}).get("geometry"))
        record = {
            "id": (feature or {}).get("id", index),
            "attributes": dict((feature or {}).get("attributes") or {}),
            "geometry": geometry,
            "bbox": geometry_bbox(geometry) if geometry else None,
        }
        if geometry is None:
            invalid += 1
        prepared.append(record)
    return prepared, invalid


def _guard_pair_budget(source_count: int, overlay_count: int) -> None:
    if source_count * overlay_count > MAX_PAIR_TESTS:
        raise GeoprocessingError(
            "comparing {} features against {} would be {} pair tests, above the {} this desktop operation "
            "runs. Filter one of the layers, or run the operation on the server.".format(
                source_count, overlay_count, source_count * overlay_count, MAX_PAIR_TESTS))


def _matches(record: Mapping[str, Any], overlay: Sequence[Mapping[str, Any]], predicate: str,
             distance: float | None = None) -> list[Mapping[str, Any]]:
    """Overlay features satisfying the predicate, in the overlay's own order."""
    if record["geometry"] is None:
        return []
    found = []
    for candidate in overlay:
        if candidate["geometry"] is None:
            continue
        if distance is None:
            if predicate != "disjoint" and not spatial.bbox_intersects(
                    list(record["bbox"]), list(candidate["bbox"])):
                continue
            if relate(record["geometry"], candidate["geometry"], predicate):
                found.append(candidate)
            continue
        box = list(candidate["bbox"])
        grown = [box[0] - distance, box[1] - distance, box[2] + distance, box[3] + distance]
        if not spatial.bbox_intersects(list(record["bbox"]), grown):
            continue
        if geometry_distance(record["geometry"], candidate["geometry"]) <= distance + _EPS:
            found.append(candidate)
    return found


def _merge_attributes(base: Mapping[str, Any], extra: Mapping[str, Any], prefix: str,
                      renamed: dict[str, str]) -> dict[str, Any]:
    """Copy ``extra`` onto ``base``, renaming rather than overwriting a clash.

    Silently overwriting is how a join replaces a parcel's own ``id`` with the
    district's and makes every downstream reference wrong.
    """
    merged = dict(base)
    for key, value in extra.items():
        name = key
        if name in merged:
            name = "{}{}".format(prefix, key)
            renamed[key] = name
        merged[name] = value
    return merged


# --------------------------------------------------------------------------
# The operations
# --------------------------------------------------------------------------

def spatial_join(validated: Mapping[str, Any],
                 source: Sequence[Mapping[str, Any]],
                 overlay: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bring attributes of the second layer onto features of the first.

    A source feature that matches nothing keeps its row with null join values.
    Dropping it would turn "which parcels have no district?" - usually the most
    interesting question in the answer - into silence.
    """
    predicate = validated["predicate"]
    rule = validated["rule"]
    fields = validated["join_fields"]
    prefix = validated["prefix"]
    source_records, source_invalid = _prepare(source, "first")
    overlay_records, overlay_invalid = _prepare(overlay, "second")
    _guard_pair_budget(len(source_records), len(overlay_records))

    renamed: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    matched = 0
    multiple = 0
    for record in source_records:
        found = _matches(record, overlay_records, predicate)
        if found:
            matched += 1
            if len(found) > 1:
                multiple += 1
        if rule == "count_only":
            row = dict(record["attributes"])
            row["join_count"] = len(found)
            rows.append({"id": record["id"], "geometry": record["geometry"],
                         "attributes": row, "matched": bool(found), "match_count": len(found)})
            continue
        if not found:
            empty = {field: None for field in fields}
            rows.append({"id": record["id"], "geometry": record["geometry"],
                         "attributes": _merge_attributes(record["attributes"], empty, prefix, renamed),
                         "matched": False, "match_count": 0})
            continue
        chosen = found if rule == "all" else found[:1]
        for partner in chosen:
            values = {field: partner["attributes"].get(field) for field in fields}
            rows.append({"id": record["id"], "geometry": record["geometry"],
                         "attributes": _merge_attributes(record["attributes"], values, prefix, renamed),
                         "matched": True, "match_count": len(found),
                         "joined_id": partner["id"]})
    return {
        "kind": "spatial_join",
        "predicate": predicate,
        "rule": rule,
        "join_fields": list(fields),
        "source_features": len(source_records),
        "overlay_features": len(overlay_records),
        "rows": rows,
        "rows_out": len(rows),
        "matched": matched,
        "unmatched": len(source_records) - matched,
        "multiple_matches": multiple,
        "renamed_fields": renamed,
        "invalid_geometry": {"source": source_invalid, "overlay": overlay_invalid},
    }


def select_by_location(validated: Mapping[str, Any],
                       source: Sequence[Mapping[str, Any]],
                       overlay: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Identifiers of source features satisfying a predicate against the overlay.

    Identifiers rather than geometry, because the answer's destination is the
    map selection: this is the operation that turns "the parcels within 50 m of
    the river" into something the user can see highlighted.
    """
    predicate = validated["predicate"]
    distance = validated.get("distance_map_units")
    source_records, source_invalid = _prepare(source, "first")
    overlay_records, overlay_invalid = _prepare(overlay, "second")
    _guard_pair_budget(len(source_records), len(overlay_records))

    selected = []
    for record in source_records:
        if record["geometry"] is None:
            continue
        if distance is not None and predicate == "disjoint":
            # "Further than d from everything" is the complement of the distance
            # test, and computing it as a predicate match would invert it.
            if not _matches(record, overlay_records, "intersects", distance):
                selected.append(record["id"])
            continue
        if _matches(record, overlay_records, predicate, distance):
            selected.append(record["id"])
    return {
        "kind": "select_by_location",
        "predicate": predicate,
        "distance_map_units": distance,
        "crs": validated["source_crs"]["authid"],
        "map_units": validated["source_crs"]["map_units"],
        "considered": len(source_records),
        "matched": len(selected),
        "feature_ids": selected,
        "invalid_geometry": {"source": source_invalid, "overlay": overlay_invalid},
    }


def clip(validated: Mapping[str, Any],
         source: Sequence[Mapping[str, Any]],
         overlay: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Cut the source layer to a rectangular window - a bbox, or the overlay's extent.

    A rectangle is convex, so the clip is exact for any input. Cutting to the
    overlay's *shape* rather than its extent is :func:`intersect`, which is a
    different and harder operation and says so.
    """
    source_records, source_invalid = _prepare(source, "first")
    window = validated.get("bbox")
    if window is None:
        overlay_records, _overlay_invalid = _prepare(overlay, "second")
        boxes = [record["bbox"] for record in overlay_records if record["bbox"] is not None]
        if not boxes:
            raise GeoprocessingError("the second layer has no usable geometry, so it defines no clip extent")
        window = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes)]

    ring = rectangle_ring(window)
    features: list[dict[str, Any]] = []
    dropped = 0
    trimmed = 0
    bridged = 0
    for record in source_records:
        geometry = record["geometry"]
        if geometry is None:
            dropped += 1
            continue
        if not spatial.bbox_intersects(list(record["bbox"]), list(window)):
            dropped += 1
            continue
        result = None
        if geometry["kind"] == "point":
            kept = [point for point in geometry["points"] if point_in_bbox(point, window)]
            if kept:
                result = {"kind": "point", "points": kept}
        elif geometry["kind"] == "line":
            paths = []
            for path in geometry["paths"]:
                current: list[tuple[float, float]] = []
                for start, end in _path_segments(path):
                    piece = clip_segment_to_bbox(start, end, window)
                    if piece is None:
                        if len(current) >= 2:
                            paths.append(current)
                        current = []
                        continue
                    if current and _same_point(current[-1], piece[0]):
                        current.append(piece[1])
                    else:
                        if len(current) >= 2:
                            paths.append(current)
                        current = [piece[0], piece[1]]
                if len(current) >= 2:
                    paths.append(current)
            if paths:
                result = {"kind": "line", "paths": paths}
        else:
            parts = []
            for rings in geometry["parts"]:
                if not ring_is_convex(rings[0]):
                    bridged += 1
                shell = clip_ring_to_convex(rings[0], ring)
                if not shell:
                    continue
                clipped_rings = [shell]
                for hole in rings[1:]:
                    piece = clip_ring_to_convex(hole, ring)
                    if piece:
                        clipped_rings.append(piece)
                parts.append(clipped_rings)
            if parts:
                result = {"kind": "polygon", "parts": parts}
        if result is None:
            dropped += 1
            continue
        if geometry_bbox(result) != record["bbox"]:
            trimmed += 1
        features.append({"id": record["id"], "attributes": dict(record["attributes"]), "geometry": result})
    return {
        "kind": "clip",
        "window": list(window),
        "window_source": "bbox" if validated.get("bbox") is not None else "overlay_extent",
        "input": len(source_records),
        "output": len(features),
        "dropped_outside_window": dropped,
        "trimmed": trimmed,
        "concave_parts_may_be_bridged": bridged,
        "features": features,
        "invalid_geometry": {"source": source_invalid},
        "area_map_units": math.fsum(geometry_area(item["geometry"]) for item in features),
    }


def intersect(validated: Mapping[str, Any],
              source: Sequence[Mapping[str, Any]],
              overlay: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Geometric intersection of two layers, carrying both attribute sets.

    Restricted, on purpose, to what can be computed exactly without a boolean
    engine: a **convex** overlay part against a **convex** source part, and a
    point against any polygon. Sutherland-Hodgman is exact there and only there.
    An arbitrary pair of concave polygons is refused per pair, with the reason
    recorded, because a boolean result that is subtly wrong along one shared
    boundary is indistinguishable from a correct one until a surveyor checks it.
    """
    prefix = validated["prefix"]
    source_records, source_invalid = _prepare(source, "first")
    overlay_records, overlay_invalid = _prepare(overlay, "second")
    _guard_pair_budget(len(source_records), len(overlay_records))

    renamed: dict[str, str] = {}
    features: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for record in source_records:
        geometry = record["geometry"]
        if geometry is None:
            continue
        for candidate in overlay_records:
            other = candidate["geometry"]
            if other is None or other["kind"] != "polygon":
                if other is not None:
                    refused.append({"source_id": record["id"], "overlay_id": candidate["id"],
                                    "reason": "overlay_is_not_a_polygon"})
                continue
            if not spatial.bbox_intersects(list(record["bbox"]), list(candidate["bbox"])):
                continue
            if geometry["kind"] == "point":
                kept = [point for point in geometry["points"] if _point_in_polygon_geometry(point, other)]
                if not kept:
                    continue
                result: dict[str, Any] = {"kind": "point", "points": kept}
            elif geometry["kind"] == "polygon":
                non_convex = [rings for rings in other["parts"] if not ring_is_convex(rings[0]) or len(rings) > 1]
                if non_convex:
                    refused.append({"source_id": record["id"], "overlay_id": candidate["id"],
                                    "reason": "overlay_ring_is_not_convex"})
                    continue
                if any(not ring_is_convex(rings[0]) or len(rings) > 1 for rings in geometry["parts"]):
                    refused.append({"source_id": record["id"], "overlay_id": candidate["id"],
                                    "reason": "source_ring_is_not_convex"})
                    continue
                parts = []
                for source_rings in geometry["parts"]:
                    for overlay_rings in other["parts"]:
                        piece = clip_ring_to_convex(source_rings[0], overlay_rings[0])
                        if piece:
                            parts.append([piece])
                if not parts:
                    continue
                result = {"kind": "polygon", "parts": parts}
            else:
                refused.append({"source_id": record["id"], "overlay_id": candidate["id"],
                                "reason": "line_intersection_needs_the_geometry_engine"})
                continue
            features.append({
                "id": record["id"],
                "overlay_id": candidate["id"],
                "geometry": result,
                "attributes": _merge_attributes(record["attributes"], candidate["attributes"], prefix, renamed),
            })
    return {
        "kind": "intersect",
        "source_features": len(source_records),
        "overlay_features": len(overlay_records),
        "features": features,
        "output": len(features),
        "refused_pairs": refused,
        "renamed_fields": renamed,
        "area_map_units": math.fsum(geometry_area(item["geometry"]) for item in features),
        "invalid_geometry": {"source": source_invalid, "overlay": overlay_invalid},
        "exactness": ("convex polygon pairs and point-in-polygon only; other pairs are listed in refused_pairs"),
    }


def _group_key(value: Any) -> tuple[str, str]:
    """A group identity that keeps null and empty string apart.

    They are different facts - "nobody recorded a land use" versus "somebody
    recorded that it has none" - and collapsing them into one bucket decides
    something on the data owner's behalf.
    """
    if value is None:
        return ("null", "")
    text = str(value)
    if not text.strip():
        return ("empty", "")
    return ("value", text.strip())


def _resolve_attribute(rule: str, values: Sequence[Any]) -> dict[str, Any]:
    if rule == "count":
        return {"value": float(len(values)), "values_used": len(values), "nulls": 0}
    if rule == "first":
        for value in values:
            if value is not None:
                return {"value": value, "values_used": 1, "nulls": sum(1 for item in values if item is None)}
        return {"value": None, "values_used": 0, "nulls": len(values)}
    if rule == "concat":
        seen = sorted({str(value).strip() for value in values if value is not None and str(value).strip()})
        nulls = len(values) - sum(1 for value in values if value is not None and str(value).strip())
        return {"value": "; ".join(seen[:MAX_CONCAT_VALUES]), "values_used": len(seen), "nulls": nulls,
                "truncated": len(seen) > MAX_CONCAT_VALUES}
    numbers, nulls = analytics.numeric_values(values)
    if not numbers:
        return {"value": None, "values_used": 0, "nulls": nulls}
    if rule == "sum":
        value = math.fsum(numbers)
    elif rule == "mean":
        value = math.fsum(numbers) / len(numbers)
    elif rule == "min":
        value = min(numbers)
    else:
        value = max(numbers)
    return {"value": value, "values_used": len(numbers), "nulls": nulls}


def _overlap_pairs(parts: Sequence[Sequence[Sequence[tuple[float, float]]]]) -> dict[str, Any]:
    """How many member parts genuinely overlap in area, when that is decidable."""
    if len(parts) > MAX_OVERLAP_CHECK_MEMBERS:
        return {"checked": False, "reason": "too_many_parts"}
    for rings in parts:
        if len(rings) > 1 or not ring_is_convex(rings[0]):
            return {"checked": False, "reason": "a_part_is_not_a_convex_ring"}
    overlapping = 0
    for index, left in enumerate(parts):
        for right in parts[index + 1:]:
            piece = clip_ring_to_convex(left[0], right[0])
            if piece and spatial.ring_area(piece) > _EPS:
                overlapping += 1
    return {"checked": True, "overlapping_pairs": overlapping}


def dissolve(validated: Mapping[str, Any], source: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge features into one multi-part geometry per value of a field.

    Two honest limitations travel with the result rather than being hidden by
    the field names:

    * ``boundaries_removed`` is False. The members are collected into a
      multi-part geometry, which IS the exact set union when they do not
      overlap - the adjacent-parcel case dissolve is used for - but the shared
      interior edges are still there. Erasing them needs a boolean engine.
    * The area is ``area_sum_of_parts``, not ``area``, because that is what it
      is. Where an overlap check was possible it is reported, so the reader can
      tell whether the sum is also the union area.

    Attributes that have no declared rule are dropped and listed. Carrying over
    whichever member came first is how a dissolved district ends up labelled
    with one arbitrary parcel's owner.
    """
    field = validated.get("field") or ""
    rules: Mapping[str, str] = validated.get("attribute_rules") or {}
    records, invalid = _prepare(source, "first")

    order: list[tuple[str, str]] = []
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = _group_key(record["attributes"].get(field)) if field else ("all", "")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(record)

    present_fields: set[str] = set()
    for record in records:
        present_fields.update(record["attributes"].keys())
    dropped_fields = sorted(present_fields - set(rules) - ({field} if field else set()))

    groups = []
    for key in order:
        members = buckets[key]
        parts: list[Sequence[Sequence[tuple[float, float]]]] = []
        non_polygon = 0
        for record in members:
            geometry = record["geometry"]
            if geometry is None:
                continue
            if geometry["kind"] != "polygon":
                non_polygon += 1
                continue
            parts.extend(geometry["parts"])
        attributes: dict[str, Any] = {}
        if field:
            attributes[field] = key[1] if key[0] == "value" else None
        details = {}
        for name, rule in rules.items():
            resolved = _resolve_attribute(rule, [record["attributes"].get(name) for record in members])
            attributes[name] = resolved["value"]
            details[name] = {"rule": rule, "values_used": resolved["values_used"], "nulls": resolved["nulls"]}
        groups.append({
            "group": key[1],
            "group_kind": key[0],
            "members": len(members),
            "feature_ids": [record["id"] for record in members],
            "geometry": {"kind": "polygon", "parts": parts} if parts else None,
            "parts": len(parts),
            "non_polygon_members": non_polygon,
            "area_sum_of_parts": math.fsum(spatial.polygon_area(rings) for rings in parts),
            "attributes": attributes,
            "attribute_detail": details,
            "overlap": _overlap_pairs(parts) if parts else {"checked": True, "overlapping_pairs": 0},
        })
    return {
        "kind": "dissolve",
        "field": field,
        "groups": groups,
        "group_count": len(groups),
        "input": len(records),
        "dropped_fields": dropped_fields,
        "boundaries_removed": False,
        "note": ("Members are collected as a multi-part geometry; shared interior boundaries are kept and "
                 "area_sum_of_parts equals the union area only where the parts do not overlap."),
        "invalid_geometry": {"source": invalid},
    }


def buffer(validated: Mapping[str, Any], source: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Buffer point features exactly; refuse to fake an offset for lines and areas.

    A vector buffer is never a circle: it is a regular polygon, and QGIS says so
    through its ``segments`` setting (segments per quarter circle). This produces
    the same shape with the same convention and reports the resulting area
    alongside the true circle area, so a reader can see the approximation rather
    than infer it.

    Offsetting a line or a polygon outward is a different problem - Minkowski
    sum with a disc, with mitre and join rules - and is delegated to the
    geometry engine with the radius already resolved into map units, which is
    the part that is usually got wrong.
    """
    radius = validated["radius_map_units"]
    segments = validated["segments"]
    vertices = 4 * segments
    records, invalid = _prepare(source, "first")

    features: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for record in records:
        geometry = record["geometry"]
        if geometry is None:
            continue
        if geometry["kind"] != "point":
            refused.append({"id": record["id"], "geometry": geometry["kind"],
                            "reason": "offset_geometry_requires_the_geometry_engine"})
            continue
        parts = []
        for centre in geometry["points"]:
            parts.append([[
                (centre[0] + radius * math.cos(2.0 * math.pi * index / vertices),
                 centre[1] + radius * math.sin(2.0 * math.pi * index / vertices))
                for index in range(vertices)
            ]])
        features.append({"id": record["id"], "attributes": dict(record["attributes"]),
                         "geometry": {"kind": "polygon", "parts": parts}})
    polygon_area_each = 0.5 * vertices * radius * radius * math.sin(2.0 * math.pi / vertices)
    circle_area = math.pi * radius * radius
    return {
        "kind": "buffer",
        "radius_map_units": radius,
        "requested": validated["distance_plan"]["requested"],
        "unit": validated["distance_plan"]["unit"],
        "metres": validated["distance_plan"]["metres"],
        "crs": validated["source_crs"]["authid"],
        "map_units": validated["source_crs"]["map_units"],
        "segments": segments,
        "vertices": vertices,
        "features": features,
        "output": len(features),
        "refused": refused,
        "area_per_buffer": polygon_area_each,
        "circle_area": circle_area,
        "area_ratio": polygon_area_each / circle_area if circle_area else None,
        "approximation": ("a regular polygon of 4 x segments vertices; segments is per quarter circle, "
                          "matching the QGIS convention"),
        "invalid_geometry": {"source": invalid},
    }


def wgs84_to_web_mercator(lon: float, lat: float) -> tuple[float, float] | None:
    """Spherical Web Mercator forward. None beyond the projection's own domain.

    Latitude is not clamped to 85.0511 when it exceeds it. A clamped point is a
    point somewhere else, and returning one silently is how a feature moves
    hundreds of kilometres without anybody being told.
    """
    if not (-180.0 <= lon <= 180.0) or abs(lat) > WEB_MERCATOR_MAX_LATITUDE:
        return None
    x = _WEB_MERCATOR_RADIUS * math.radians(lon)
    # asinh(tan(phi)) rather than the textbook ln(tan(pi/4 + phi/2)): they are
    # the same function, but the textbook form computes tan(pi/4) as
    # 0.9999999999999999 and puts the equator 0.7 nanometres south of itself.
    y = _WEB_MERCATOR_RADIUS * math.asinh(math.tan(math.radians(lat)))
    return (x, y)


def web_mercator_to_wgs84(x: float, y: float) -> tuple[float, float] | None:
    lon = math.degrees(x / _WEB_MERCATOR_RADIUS)
    lat = math.degrees(math.atan(math.sinh(y / _WEB_MERCATOR_RADIUS)))
    if not (-180.0 <= lon <= 180.0) or abs(lat) > 90.0:
        return None
    return (lon, lat)


def built_in_transform(source_authid: str, target_authid: str) -> Callable[[float, float], Any] | None:
    """The transforms this module can perform with no projection library.

    Only the spherical Web Mercator pair, because it is defined on a sphere and
    involves no datum shift, so the closed form is the whole answer. Everything
    else - a UTM zone, a national grid, any datum change - needs real grids and
    is the caller's to supply.
    """
    source = str(source_authid or "").upper()
    target = str(target_authid or "").upper()
    if source in _WGS84_AUTHIDS and target in _WEB_MERCATOR_AUTHIDS:
        return wgs84_to_web_mercator
    if source in _WEB_MERCATOR_AUTHIDS and target in _WGS84_AUTHIDS:
        return web_mercator_to_wgs84
    return None


def reproject(validated: Mapping[str, Any],
              source: Sequence[Mapping[str, Any]],
              transform: Callable[[float, float], Any] | None = None) -> dict[str, Any]:
    """Move features between two declared CRSs using a supplied point transform.

    The transform is injected for the same reason :func:`spatial.transform_bbox`
    injects one: real reprojection needs PROJ's grids, and pretending otherwise
    would produce coordinates that are close enough to look right. A feature
    whose transform fails is listed in ``failed`` and named, never dropped into
    a silent gap between the input count and the output count.
    """
    source_crs = validated["source_crs"]
    target_crs = validated["target_crs"]
    if transform is None:
        transform = built_in_transform(source_crs["authid"], target_crs["authid"])
    if transform is None:
        raise GeoprocessingError(
            "reprojecting {} to {} needs a projection library; this desktop kernel performs only the spherical "
            "Web Mercator pair on its own. Supply a transform, or run it through QGIS.".format(
                source_crs["authid"], target_crs["authid"])
        )

    def move(point):
        try:
            moved = transform(point[0], point[1])
        except Exception:
            return None
        return _coordinate(moved)

    records, invalid = _prepare(source, "first")
    features: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for record in records:
        geometry = record["geometry"]
        if geometry is None:
            failed.append({"id": record["id"], "reason": "invalid_geometry"})
            continue
        moved_points: list[Any] = []
        broken = False
        for point in geometry_coordinates(geometry):
            moved = move(point)
            if moved is None:
                broken = True
                break
            moved_points.append(moved)
        if broken:
            failed.append({"id": record["id"], "reason": "a_vertex_falls_outside_the_target_projection"})
            continue
        features.append({
            "id": record["id"],
            "attributes": dict(record["attributes"]),
            "geometry": _rebuild(geometry, iter(moved_points)),
        })
    return {
        "kind": "reproject",
        "source_crs": source_crs["authid"],
        "target_crs": target_crs["authid"],
        "source_map_units": source_crs["map_units"],
        "target_map_units": target_crs["map_units"],
        "unit_change": validated["unit_change"],
        "input": len(records),
        "output": len(features),
        "failed": failed,
        "features": features,
        "invalid_geometry": {"source": invalid},
    }


def _rebuild(geometry: Mapping[str, Any], points) -> dict[str, Any]:
    """Rebuild a canonical geometry from a stream of transformed vertices."""
    kind = geometry["kind"]
    if kind == "point":
        return {"kind": "point", "points": [next(points) for _ in geometry["points"]]}
    if kind == "line":
        return {"kind": "line",
                "paths": [[next(points) for _ in path] for path in geometry["paths"]]}
    return {"kind": "polygon",
            "parts": [[[next(points) for _ in ring] for ring in rings] for rings in geometry["parts"]]}


def zonal_statistics(validated: Mapping[str, Any],
                     source: Sequence[Mapping[str, Any]],
                     zones: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a field of the source layer by the polygons of the zone layer.

    Every zone is reported, including those that caught nothing - an empty zone
    is a finding, and omitting it turns a district with no schools into a
    district nobody asked about. Every statistic reports ``values_used`` and
    ``nulls`` beside it: a mean over 23 of 24 parcels is 396.739, and the same
    sum divided by 24 is 380.2, a different and false claim about the data.
    Source features that fall in no zone are counted in ``unassigned`` and their
    values are in no total.
    """
    statistic = validated["statistic"]
    value_field = validated.get("value_field") or ""
    zone_field = validated.get("zone_field") or ""
    assignment = validated["assignment"]

    source_records, source_invalid = _prepare(source, "first")
    zone_records, zone_invalid = _prepare(zones, "second")
    _guard_pair_budget(len(source_records), len(zone_records))

    polygon_zones = []
    for record in zone_records:
        if record["geometry"] is None or record["geometry"]["kind"] != "polygon":
            continue
        key = record["attributes"].get(zone_field) if zone_field else record["id"]
        polygon_zones.append({"record": record, "key": key})

    collected: dict[int, list[Any]] = {index: [] for index in range(len(polygon_zones))}
    assignments = 0
    unassigned = 0
    unassignable = 0
    for record in source_records:
        geometry = record["geometry"]
        if geometry is None:
            unassignable += 1
            continue
        hits: list[int] = []
        if assignment == "centroid":
            point = representative_point(geometry)
            if point is None:
                # A line has no meaningful single representative point; saying so
                # is better than inventing one at the path midpoint.
                unassignable += 1
                continue
            for index, zone in enumerate(polygon_zones):
                if point_in_bbox(point, zone["record"]["bbox"]) and _point_in_polygon_geometry(
                        point, zone["record"]["geometry"]):
                    hits.append(index)
                    break
        else:
            for index, zone in enumerate(polygon_zones):
                if not spatial.bbox_intersects(list(record["bbox"]), list(zone["record"]["bbox"])):
                    continue
                if geometries_intersect(geometry, zone["record"]["geometry"]):
                    hits.append(index)
        if not hits:
            unassigned += 1
            continue
        assignments += len(hits)
        for index in hits:
            collected[index].append(record["attributes"].get(value_field) if value_field else None)

    rows = []
    for index, zone in enumerate(polygon_zones):
        values = collected[index]
        if statistic == "count":
            rows.append({"zone": zone["key"], "features": len(values), "values_used": len(values),
                         "nulls": 0, "value": float(len(values))})
            continue
        numbers, nulls = analytics.numeric_values(values)
        if not numbers:
            value = None
        elif statistic == "sum":
            value = math.fsum(numbers)
        elif statistic == "mean":
            value = math.fsum(numbers) / len(numbers)
        elif statistic == "min":
            value = min(numbers)
        else:
            value = max(numbers)
        rows.append({"zone": zone["key"], "features": len(values), "values_used": len(numbers),
                     "nulls": nulls, "value": value})
    return {
        "kind": "zonal_statistics",
        "statistic": statistic,
        "value_field": value_field,
        "zone_field": zone_field,
        "assignment": assignment,
        "zones": rows,
        "zone_count": len(rows),
        "source_features": len(source_records),
        "assigned": assignments,
        "unassigned": unassigned,
        "unassignable": unassignable,
        # An "intersects" assignment counts a feature in every zone it touches,
        # so the zone totals can exceed the feature count. That is correct for
        # the question asked and wrong to read as a partition, so it is stated.
        "double_counted": assignment == "intersects" and assignments > (len(source_records) - unassigned
                                                                        - unassignable),
        "invalid_geometry": {"source": source_invalid, "zones": zone_invalid},
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def execute(validated: Mapping[str, Any],
            source: Sequence[Mapping[str, Any]] = (),
            overlay: Sequence[Mapping[str, Any]] = (),
            transform: Callable[[float, float], Any] | None = None) -> dict[str, Any]:
    """Run an already-validated request. Never accepts a raw request dict."""
    request = validated or {}
    operation = str(request.get("operation") or "")
    if request.get("validated") != KERNEL_VERSION or operation not in OPERATIONS:
        raise GeoprocessingError("this request has not been through validate_request()")
    if operation == "spatial_join":
        return spatial_join(validated, source, overlay)
    if operation == "select_by_location":
        return select_by_location(validated, source, overlay)
    if operation == "clip":
        return clip(validated, source, overlay)
    if operation == "intersect":
        return intersect(validated, source, overlay)
    if operation == "dissolve":
        return dissolve(validated, source)
    if operation == "buffer":
        return buffer(validated, source)
    if operation == "reproject":
        return reproject(validated, source, transform)
    return zonal_statistics(validated, source, overlay)
