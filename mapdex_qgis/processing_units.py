"""A distance the user states in metres is applied in metres.

QGIS Processing measures a buffer distance, and a simplification tolerance, in
the layer's own units. Nothing converted the request: `distance: 25` from "buffer
this by 25 m" went straight into `native:buffer`. On a projected metric layer that
is 25 metres and nothing looks wrong. On EPSG:4326 - which is how GeoJSON, KML and
most downloaded data arrive - it is 25 DEGREES, and the result is a shape about
2,800 km across. Measured by replaying the server's own buffer response through
the plugin in QGIS 4.0.2: each side grew by 25.0000 degrees.

The vendored nivo kernel already knew this (`spatial.plan_distance` refuses to
turn metres into degrees with a scalar) and nothing on the Processing path called
it. This is that call, kept free of QGIS so it can be tested exactly:

- projected layer: metres converted into the layer's linear unit (feet included);
- geographic layer: the work runs on a copy in the local UTM zone and the result
  is brought back, because a degree is not a fixed length and no single factor is
  right anywhere but one parallel;
- anything else: refused with the reason, never guessed.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Sequence

from ._vendor.nivo.spatial import to_metres, utm_zone_for

# The operations whose parameter is a LENGTH on the ground, and which parameter
# carries it. A contour interval is a height and a z factor is a ratio; neither
# belongs here.
METRIC_LENGTH_PARAMS = {
    "buffer": "distance",
    "simplify": "tolerance",
}


# The field types QGIS reads for a nested value: a GeoJSON array or object
# arrives as JSON, a GeoPackage list as one of the list types.
STRUCTURED_FIELD_TYPE_NAMES = frozenset({
    "json", "jsonb", "stringlist", "integerlist", "integer64list", "reallist",
    "list", "map",
})


def structured_field_names(fields: Iterable[Sequence[Any]]) -> list[str]:
    """The fields no Processing algorithm can copy into the layer it writes.

    ``fields`` is ``(name, type_name, type)`` per field. A GeoJSON property
    holding an array - `image_corners` on every Mapdex extraction preview - is
    read by QGIS as a JSON field, and every algorithm writing a temporary layer
    then stops at the first feature: "Could not store attribute image_corners:
    Could not convert value "" to target type string". Measured in QGIS 4.0.2
    with native:buffer on vaudherland-cadastre-preview.geojson, outside Mapdex
    entirely, so this is QGIS's limit and not ours to wait for.
    """
    names = []
    for name, type_name, variant_type in fields:
        declared = str(type_name or "").strip().lower()
        stored = str(variant_type or "").lower().rsplit(".", 1)[-1]
        if declared in STRUCTURED_FIELD_TYPE_NAMES or stored.endswith(("list", "map")):
            names.append(str(name))
    return names


def structured_value_as_text(value: Any) -> Any:
    """A nested attribute value as the JSON text it was written as.

    None and QGIS's NULL pass through, so an absent value stays absent rather
    than becoming the four characters "null".
    """
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return value
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def describe_textual_fields(names: Sequence[str]) -> str:
    """The sentence that says a field changed type on the way through."""
    if not names:
        return ""
    quoted = ", ".join("'{}'".format(name) for name in names)
    return (" {} {} carried as text, because QGIS cannot write a list into a new layer."
            .format(quoted, "is" if len(names) == 1 else "are"))


def describe_processing_failure(label: str, source_name: str, log: str = "") -> str:
    """The sentence a failed Processing run leaves in the transcript.

    It used to leave nothing there: the status line said "failed or was
    cancelled" and was rewritten by the next layer click, while the transcript
    still showed the confirmation the user had accepted. QGIS's own last log line
    is usually the reason ("Input layer is not valid") and is kept, bounded.
    """
    lines = [line.strip() for line in str(log or "").splitlines() if line.strip()]
    detail = lines[-1][:200] if lines else ""
    head = "The {} on '{}' did not finish.".format(label or "operation", source_name or "the layer")
    return "{} QGIS reported: {}".format(head, detail) if detail else head


def input_parameter_aliases(parameter_names, working_layer) -> dict[str, Any]:
    """The input a Processing algorithm names something other than INPUT.

    `qgis:checkvalidity` calls its input INPUT_LAYER, so the geometry check never
    received a layer and failed with "Could not load source layer for
    INPUT_LAYER" - on every run, measured in QGIS 4.0.2.
    """
    names = set(parameter_names or ())
    if "INPUT_LAYER" in names:
        return {"INPUT_LAYER": working_layer}
    return {}


def describe_validation_outcome(source_name: str, invalid_count: Any) -> str:
    """What a geometry check found, which is not what a buffer produces.

    An empty INVALID_OUTPUT means every geometry passed. Described by the
    generic outcome, it read "produced an empty layer because 'parcels' has no
    features to work on" about a layer with 24 features.
    """
    source = source_name or "the layer"
    if invalid_count == 0:
        return "The geometry check found no invalid geometries in '{}'.".format(source)
    if isinstance(invalid_count, int) and invalid_count > 0:
        return "The geometry check found {} invalid geometr{} in '{}' and added them as a layer.".format(
            invalid_count, "y" if invalid_count == 1 else "ies", source)
    return "The geometry check on '{}' finished; its result was added as a layer.".format(source)


def plan_metric_operation(
    operation: str,
    params: dict[str, Any],
    crs_is_geographic: bool,
    metres_to_map_units: float | None,
    bbox_wgs84: Sequence[float] | None,
) -> dict[str, Any]:
    """Decide how a stated length reaches the algorithm.

    Returns a dict whose ``strategy`` is ``none``, ``map_units``, ``reproject``
    or ``refuse``, and whose ``params`` are what the algorithm should receive.
    ``reproject`` also names the projected ``crs`` to run in.
    """
    key = METRIC_LENGTH_PARAMS.get(str(operation or ""))
    params = dict(params or {})
    unit = str(params.pop("unit", "") or "m")
    if not key or params.get(key) in (None, ""):
        return {"strategy": "none", "params": params}
    try:
        value = float(params[key])
    except (TypeError, ValueError):
        return {"strategy": "refuse", "params": params,
                "reason": "the {} is not a number".format(key)}
    if not math.isfinite(value) or value < 0:
        return {"strategy": "refuse", "params": params,
                "reason": "the {} must be a positive length".format(key)}
    metres = to_metres(value, unit)
    if metres is None:
        return {"strategy": "refuse", "params": params,
                "reason": "'{}' is not a unit of length I know".format(unit)}
    if crs_is_geographic:
        zone = utm_zone_for(bbox_wgs84) if bbox_wgs84 is not None else None
        if zone is None:
            return {"strategy": "refuse", "params": params, "reason": (
                "this layer's coordinates are in degrees and it is too large for one UTM "
                "zone, so {:g} m cannot be applied to it as a single distance; reproject it "
                "to a projected coordinate system first".format(metres))}
        params[key] = metres
        return {"strategy": "reproject", "params": params, "crs": zone["crs"], "metres": metres}
    if metres_to_map_units is None or not math.isfinite(metres_to_map_units) \
            or metres_to_map_units <= 0:
        return {"strategy": "refuse", "params": params, "reason": (
            "this layer's coordinate system does not state a unit of length, so {:g} m "
            "cannot be converted into it".format(metres))}
    params[key] = metres * metres_to_map_units
    return {"strategy": "map_units", "params": params, "metres": metres}
