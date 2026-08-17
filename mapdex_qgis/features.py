"""Build the coordinates for features Nivo places on a layer.

Kept free of QGIS imports so the part that is easy to get wrong - how many
points, where, and in which coordinate order - is testable without a QGIS
runtime. The plugin turns these WGS84 pairs into real geometry and transforms
them into the layer's CRS.

Placement is seeded and therefore reproducible: asking twice for "20 points in
Turkey" with the same request produces the same points, which is what makes an
answer checkable rather than a surprise each time.
"""
from __future__ import annotations

import math
import random
from typing import Any, Mapping, Sequence

MAX_FEATURES = 1000
# Placement invents positions, never shapes. A point IS a position; a polygon or
# a line is a shape whose vertices nobody supplied, so producing one would be
# fabricating geometry. The layer type used to be honoured while the features
# were always points, so a polygon request created "New polygon layer" and then
# QGIS refused every feature ("Could not add feature with geometry type Point to
# layer of type Polygon"), leaving an empty layer that looked like a done job.
PLACEABLE_GEOMETRIES = ("point", "multipoint")
_SHAPE_WORDS = {
    "polygon": "polygon",
    "multipolygon": "polygon",
    "linestring": "line",
    "multilinestring": "line",
}
# A bbox smaller than this is treated as a point: scattering inside a degenerate
# extent would stack every feature on one spot.
MIN_SPAN_DEGREES = 1e-9
# Used when only a centre is known, roughly a city-sized area.
DEFAULT_SPREAD_DEGREES = 0.05


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def valid_pair(pair: Any) -> tuple[float, float] | None:
    """A WGS84 [longitude, latitude] pair, or None."""
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    lon, lat = _finite(pair[0]), _finite(pair[1])
    if lon is None or lat is None:
        return None
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        return None
    return (lon, lat)


def _bbox(values: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    numbers = [_finite(value) for value in values]
    if any(number is None for number in numbers):
        return None
    minx, miny, maxx, maxy = numbers
    if minx > maxx or miny > maxy:
        return None
    return (minx, miny, maxx, maxy)


def plan_points(params: Mapping[str, Any], seed: str = "") -> dict[str, Any]:
    """Decide where the requested features go.

    Returns ``{"points": [(lon, lat), …], "crs": "EPSG:4326", "source": …}`` or
    a reason when nothing can be placed. ``source`` records how the location was
    decided so the reply can say it plainly.
    """
    count = params.get("count")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, MAX_FEATURES))

    explicit = [pair for pair in (valid_pair(item) for item in (params.get("coordinates") or [])) if pair]
    if explicit:
        points = list(explicit)
        # More features than coordinates: repeat the given points rather than
        # inventing locations near them.
        while len(points) < count:
            points.append(explicit[len(points) % len(explicit)])
        return {"points": points[:count], "crs": "EPSG:4326", "source": "coordinates"}

    # Deterministic sample geometry: the caller's seed must reproduce the same
    # points, so a cryptographic generator would be the wrong tool here. Nothing
    # about this placement is a security decision. (B311 below.)
    generator = random.Random(seed or "mapdex")  # nosec B311
    box = _bbox(params.get("bbox"))
    if box is None:
        centre = valid_pair(params.get("center"))
        if centre is not None:
            box = (
                centre[0] - DEFAULT_SPREAD_DEGREES, centre[1] - DEFAULT_SPREAD_DEGREES,
                centre[0] + DEFAULT_SPREAD_DEGREES, centre[1] + DEFAULT_SPREAD_DEGREES,
            )
    if box is None:
        return {"points": [], "crs": "EPSG:4326", "reason": "no_area"}

    minx, miny, maxx, maxy = box
    if (maxx - minx) < MIN_SPAN_DEGREES or (maxy - miny) < MIN_SPAN_DEGREES:
        midpoint = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
        return {"points": [midpoint] * count, "crs": "EPSG:4326", "source": "point_extent"}

    points = [
        (generator.uniform(minx, maxx), generator.uniform(miny, maxy))
        for _index in range(count)
    ]
    return {"points": points, "crs": "EPSG:4326", "source": "area"}


def scatter_in_rectangle(
    extent: Sequence[float],
    count: int,
    seed: str = "",
) -> list[tuple[float, float]]:
    """Scatter inside an extent already expressed in the target CRS.

    Used for the "what I'm looking at" case, where the desktop knows its own
    canvas extent and no geocoding or transform is involved.
    """
    box = _bbox(extent)
    count = max(1, min(int(count or 1), MAX_FEATURES))
    if box is None:
        return []
    minx, miny, maxx, maxy = box
    if (maxx - minx) < MIN_SPAN_DEGREES or (maxy - miny) < MIN_SPAN_DEGREES:
        return [((minx + maxx) / 2.0, (miny + maxy) / 2.0)] * count
    # Deterministic sample geometry: the caller's seed must reproduce the same
    # points, so a cryptographic generator would be the wrong tool here. Nothing
    # about this placement is a security decision. (B311 below.)
    generator = random.Random(seed or "mapdex")  # nosec B311
    return [(generator.uniform(minx, maxx), generator.uniform(miny, maxy)) for _index in range(count)]


def can_place(geometry: Any) -> bool:
    """True when features of this geometry can be placed from coordinates."""
    return str(geometry or "").strip().lower() in PLACEABLE_GEOMETRIES


def describe_unplaceable_geometry(geometry: Any) -> str:
    """Say what will not happen, and what the user can do instead."""
    kind = str(geometry or "").strip().lower()
    shape = _SHAPE_WORDS.get(kind, "those")
    return (
        "I can place points, but I cannot invent {} shapes - their corners would "
        "be made up. Draw them on a layer yourself, or extract them from a "
        "scanned map or an existing dataset."
    ).format(shape)


def describe_placement(added: int, requested: int, where: str) -> str:
    """State what actually landed, not what was asked for."""
    if added == 0:
        return "I could not place any features."
    location = " in {}".format(where) if where else ""
    if added == requested:
        return "Added {} feature{}{}.".format(added, "s" if added != 1 else "", location)
    return "Added {} of the {} requested feature(s){}.".format(added, requested, location)
