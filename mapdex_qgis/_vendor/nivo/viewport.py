# SPDX-License-Identifier: MIT
"""Turn a server viewport instruction into a canvas extent, in the right CRS.

The server geocodes in WGS84 because that is the only CRS a place lookup can
speak. A QGIS canvas is usually EPSG:3857 or a national projection, so applying
those degrees directly puts the view a few metres from null island - which is
exactly what "zoom to Istanbul" did: the right answer, drawn in the wrong place.

The maths is kept here, free of QGIS imports, so the degenerate cases that
actually bite (a point extent, a zoom level with no extent, a pole-adjacent
latitude) are covered by tests rather than discovered on a user's canvas.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# A point result needs a span, not an infinite zoom. This is the half-width in
# metres used when the server sends a centre without an extent, chosen so a
# city-level result frames a recognisable area.
DEFAULT_HALF_SPAN_M = 12_000.0
MIN_HALF_SPAN_M = 50.0
MAX_HALF_SPAN_M = 2_000_000.0
# Web-mercator tile maths: the equator is this many metres across.
EQUATOR_M = 40_075_016.686


def half_span_for_zoom(zoom: float, latitude: float = 0.0) -> float:
    """Metres from the centre to the edge for a slippy-map zoom level.

    A zoom level describes a scale, not an extent, so it only becomes an extent
    once a viewport width is assumed. 1024 px is taken as the reference width;
    the cos(latitude) term is what keeps a zoom-11 view of Oslo the same real
    size as a zoom-11 view of Nairobi.
    """
    try:
        level = float(zoom)
    except (TypeError, ValueError):
        return DEFAULT_HALF_SPAN_M
    if not math.isfinite(level) or level <= 0:
        return DEFAULT_HALF_SPAN_M
    level = min(level, 22.0)
    latitude = max(-85.0, min(85.0, float(latitude or 0.0)))
    metres_per_pixel = EQUATOR_M * math.cos(math.radians(latitude)) / (256.0 * (2.0 ** level))
    half = metres_per_pixel * 512.0
    return max(MIN_HALF_SPAN_M, min(half, MAX_HALF_SPAN_M))


def degrees_for_metres(metres: float, latitude: float) -> tuple[float, float]:
    """Approximate a metre span as (longitude, latitude) degrees at a latitude.

    Used only to build a WGS84 box around a point before it is transformed into
    the canvas CRS. The longitude term divides by cos(latitude) because a degree
    of longitude shrinks toward the poles - the assumption that it does not is
    the classic source of extents that are far too wide in northern Europe.
    """
    latitude = max(-85.0, min(85.0, float(latitude or 0.0)))
    lat_degrees = metres / 111_320.0
    cosine = math.cos(math.radians(latitude))
    # Near the poles the longitude span degenerates; clamp to the whole world
    # rather than dividing by something approaching zero.
    lon_degrees = 180.0 if cosine < 1e-6 else min(180.0, metres / (111_320.0 * cosine))
    return lon_degrees, lat_degrees


def _finite_pair(values: Any) -> tuple[float, float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        return None
    try:
        first, second = float(values[0]), float(values[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(first) or not math.isfinite(second):
        return None
    return first, second


def _finite_bbox(values: Any) -> list[float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        box = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) for value in box):
        return None
    if box[0] > box[2] or box[1] > box[3]:
        return None
    return box


def resolve_extent(params: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve a zoom_to_extent payload into a source CRS and a bounding box.

    Returns ``{"crs", "bbox"}`` where the box is expressed in ``crs``, or None
    when the payload carries nothing usable. A point extent is padded here, so
    the caller never hands a zero-area rectangle to a canvas.
    """
    crs = str(params.get("crs") or "EPSG:4326").strip() or "EPSG:4326"
    # `bounds` is the name the server's map:fit_bounds@1 uses and the one this
    # capability now declares. `bbox` is still read because this resolver's job
    # has always been to accept the shapes that actually arrive - an older
    # payload, a legacy centre-and-zoom - and a plugin newer than the server it
    # talks to would otherwise refuse a box it understands perfectly.
    box = _finite_bbox(params.get("bounds"))
    if box is None:
        box = _finite_bbox(params.get("bbox"))
    if box is not None:
        if box[0] == box[2] or box[1] == box[3]:
            centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
            return _box_around(centre, crs, None)
        return {"crs": crs, "bbox": box}
    centre = _finite_pair(params.get("center"))
    if centre is None:
        return None
    return _box_around(centre, crs, params.get("zoom"))


def _box_around(centre: Sequence[float], crs: str, zoom: Any) -> dict[str, Any]:
    longitude, latitude = float(centre[0]), float(centre[1])
    half = half_span_for_zoom(zoom, latitude) if zoom is not None else DEFAULT_HALF_SPAN_M
    if str(crs).upper() in {"EPSG:4326", "CRS:84", "OGC:CRS84", "WGS84"}:
        lon_degrees, lat_degrees = degrees_for_metres(half, latitude)
        return {
            "crs": crs,
            "bbox": [
                max(-180.0, longitude - lon_degrees),
                max(-90.0, latitude - lat_degrees),
                min(180.0, longitude + lon_degrees),
                min(90.0, latitude + lat_degrees),
            ],
        }
    # A projected CRS is already in linear units, so the half-span applies
    # directly without any degree conversion.
    return {
        "crs": crs,
        "bbox": [longitude - half, latitude - half, longitude + half, latitude + half],
    }
