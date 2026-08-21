"""Turning a survey answer into something on the canvas.

A bearing is a number and belongs in the reply. A traverse is not: it is a walk
between stations, and a surveyor who is handed six coordinate pairs in a chat
bubble copies them into QGIS by hand, which is the work this plugin exists to
remove.

The server already sends the positions - `spatial:cogo@1` returns a
FeatureCollection beside its numbers whenever the answer IS positions. This
module decides what to make of it, and it is deliberately pure: no QGIS import,
no project, no side effect. Everything that can be wrong with the payload is
decided here where it can be tested, and the caller is left with nothing to do
but create the layers.

Three rules the tests hold.

A collection is split BY GEOMETRY TYPE, because a QGIS memory layer holds one.
Points and lines from the same answer become two layers, named for the operation
that produced them, so a traverse arrives as "Traverse stations" and "Traverse
lines" rather than as one thing that cannot exist.

Coordinates are range-checked. This plugin has no business drawing a latitude of
2589 on a map, and the server's own placement rule is the same one: a pair
outside the geographic range is not a position.

And an answer with nothing to draw produces nothing, quietly. `render: "none"`
is the honest majority case - a scale factor, a notation, a bearing - and a
plugin that created an empty layer for it would be adding noise to a correct
answer.
"""
from __future__ import annotations

MAX_FEATURES = 5000

# QGIS memory-layer type names, by the GeoJSON type they hold.
_LAYER_TYPES = {
    "Point": "Point",
    "MultiPoint": "MultiPoint",
    "LineString": "LineString",
    "MultiLineString": "MultiLineString",
    "Polygon": "Polygon",
    "MultiPolygon": "MultiPolygon",
}

# What each operation is called on the canvas. Named rather than derived from
# the operation id, because "intersect_bearings" is a wire value and "Sight
# intersection" is what a person calls it.
_OPERATION_NAMES = {
    "inverse": "Bearing and distance",
    "forward": "Set out",
    "traverse": "Traverse",
    "deed_closure": "Deed closure",
    "intersect_bearings": "Sight intersection",
    "intersect_distances": "Taped fix",
    "resection": "Resection",
    "station_offset": "Station and offset",
}


class SurveyLayerSpec:
    """One memory layer to create: its type, its name, and its features."""

    def __init__(self, geometry_type: str, name: str, features: list[dict]):
        self.geometry_type = geometry_type
        self.name = name
        self.features = features

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SurveyLayerSpec({!r}, {!r}, {} features)".format(
            self.geometry_type, self.name, len(self.features))


def layer_specs(result: object) -> list[SurveyLayerSpec]:
    """Read a spatial tool result and say which layers it asks for.

    Returns an empty list for anything that is not a drawable survey answer,
    which is most of them.
    """
    if not isinstance(result, dict):
        return []
    if result.get("render") != "drawing":
        return []
    drawing = result.get("drawing")
    if not isinstance(drawing, dict):
        return []
    features = drawing.get("features")
    if not isinstance(features, list) or not features:
        return []

    operation = str(result.get("operation") or "")
    label = _OPERATION_NAMES.get(operation) or "Survey result"

    grouped: dict[str, list[dict]] = {}
    for entry in features[:MAX_FEATURES]:
        prepared = _prepare(entry)
        if prepared is None:
            continue
        grouped.setdefault(prepared[0], []).append(prepared[1])

    specs = []
    # Sorted so the same answer always produces the same layers in the same
    # order; a canvas that reorders itself between identical questions is a
    # surface nobody trusts.
    for geometry_type in sorted(grouped):
        specs.append(SurveyLayerSpec(
            geometry_type,
            "{} {}".format(label, _suffix(geometry_type)),
            grouped[geometry_type]))
    return specs


def _prepare(entry: object) -> tuple[str, dict] | None:
    if not isinstance(entry, dict):
        return None
    geometry = entry.get("geometry")
    if not isinstance(geometry, dict):
        return None
    kind = _LAYER_TYPES.get(str(geometry.get("type") or ""))
    if kind is None:
        return None
    coordinates = geometry.get("coordinates")
    if not _geographic(coordinates):
        return None
    properties = entry.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    return kind, {"geometry": geometry, "properties": properties}


def _geographic(coordinates: object) -> bool:
    """Every coordinate must be a position on the earth.

    A latitude of 2589 is a pixel row, and this plugin has no business drawing
    one on a map. The check walks whatever nesting the geometry uses rather than
    assuming a depth, so a MultiPolygon is held to the same rule as a point.
    """
    if isinstance(coordinates, (list, tuple)):
        if coordinates and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               for value in coordinates):
            if len(coordinates) < 2:
                return False
            longitude, latitude = float(coordinates[0]), float(coordinates[1])
            return -180 <= longitude <= 180 and -90 <= latitude <= 90
        if not coordinates:
            return False
        return all(_geographic(item) for item in coordinates)
    return False


def _suffix(geometry_type: str) -> str:
    if geometry_type in ("Point", "MultiPoint"):
        return "stations"
    if geometry_type in ("LineString", "MultiLineString"):
        return "lines"
    return "areas"


def attribute_names(features: list[dict]) -> list[str]:
    """The union of property names, in a stable order.

    A memory layer needs its fields declared before the features go in, and a
    survey answer labels its features unevenly - a traverse station carries a
    label, the start does not. Taking the union rather than the first feature's
    keys is what stops a label being silently dropped.
    """
    names: list[str] = []
    for feature in features:
        for name in feature.get("properties", {}):
            text = str(name)
            if text not in names:
                names.append(text)
    return sorted(names)
