"""Bounded QGIS Processing operation registry for Nivo.

The server may ask for a named operation, but this plugin owns the native
algorithm mapping and validates it against the live QGIS registry before any
task can run.
"""
from __future__ import annotations

from typing import Any

MAX_PARAM_TEXT = 256

PROCESSING_OPERATION_CATALOG: dict[str, tuple[str, ...]] = {
    "buffer": ("native:buffer", "qgis:buffer"),
    "clip": ("native:clip", "qgis:clip"),
    "intersection": ("native:intersection", "qgis:intersection"),
    "select_by_location": ("native:selectbylocation", "qgis:selectbylocation"),
    "nearest_neighbor": ("native:joinbynearest",),
    "reproject": ("native:reprojectlayer", "qgis:reprojectlayer"),
    "heatmap": ("qgis:heatmapkerneldensityestimation",),
    "measure_geometry": ("native:exportaddgeometrycolumns", "qgis:exportaddgeometrycolumns"),
    # Operations a professional expects from any GIS and that Nivo could not
    # name. Each is an installed native algorithm; the gap was the allowlist,
    # not the capability. Alternative ids are listed because algorithm names
    # moved between QGIS versions and resolve_processing_algorithm tries them in
    # order against the live registry.
    "dissolve": ("native:dissolve", "qgis:dissolve"),
    "union": ("native:union", "qgis:union"),
    "difference": ("native:difference", "qgis:difference"),
    "merge": ("native:mergevectorlayers", "qgis:mergevectorlayers"),
    "centroid": ("native:centroids", "qgis:centroids"),
    "convex_hull": ("native:convexhull", "qgis:convexhull"),
    "split": ("native:splitwithlines", "qgis:splitwithlines"),
    "zonal_statistics": ("native:zonalstatisticsfb", "qgis:zonalstatistics"),
}

# Operations that combine two layers. Naming them makes the requirement
# checkable rather than implied by whichever parameter the algorithm happens to
# expose: running one of these against a single layer produces an empty or
# nonsensical result instead of an error.
TWO_LAYER_OPERATIONS = frozenset({
    "clip", "intersection", "union", "difference", "merge",
    "select_by_location", "nearest_neighbor", "split", "zonal_statistics",
})

PROCESSING_OUTPUT_KEYS = frozenset({"OUTPUT", "OUTPUT_LAYER", "OUTPUT_VECTOR", "OUTPUT_RASTER"})

# What the person who asked for a buffer calls it. `native:buffer` is how QGIS
# spells the algorithm internally: it is developer data, it is untranslatable,
# and reading it back to the user tells them nothing they can act on.
OPERATION_LABELS: dict[str, str] = {
    "buffer": "buffer",
    "clip": "clip",
    "intersection": "intersection",
    "select_by_location": "selection by location",
    "nearest_neighbor": "nearest-neighbour join",
    "reproject": "reprojection",
    "heatmap": "heatmap",
    "measure_geometry": "geometry measurement",
    "dissolve": "dissolve",
    "union": "union",
    "difference": "difference",
    "merge": "merge",
    "centroid": "centroids",
    "convex_hull": "convex hull",
    "split": "split",
    "zonal_statistics": "zonal statistics",
}


def operation_label(operation: Any) -> str:
    """A human name for an operation, never an algorithm id."""
    key = str(operation or "").strip()
    return OPERATION_LABELS.get(key) or key.replace("_", " ") or "that operation"


def describe_empty_input(operation: Any, source_name: str = "") -> str:
    """Why the run was not started at all.

    Running a buffer over an empty layer succeeds and produces an empty layer,
    which is how "buffer yaptım" ended with a new layer and nothing in it.
    """
    where = "'{}'".format(source_name) if source_name else "The selected layer"
    return (
        "{} has no features yet, so a {} would produce an empty layer. Add or "
        "import data first, then ask again."
    ).format(where, operation_label(operation))


def describe_processing_outcome(
    operation: Any,
    output_name: str = "",
    feature_count: Any = None,
    source_name: str = "",
) -> str:
    """One honest line about what the run actually left on the map."""
    label = operation_label(operation)
    if not output_name:
        return "The {} ran but produced no layer, so nothing was added to the map.".format(label)
    if feature_count == 0:
        if source_name:
            return "The {} produced an empty layer because '{}' has no features to work on.".format(
                label, source_name)
        return "The {} produced an empty layer, so there is nothing to see on the map.".format(label)
    if isinstance(feature_count, int) and feature_count > 0:
        return "Added '{}' with {} feature{} from the {}.".format(
            output_name, feature_count, "s" if feature_count != 1 else "", label)
    return "Added '{}' from the {}.".format(output_name, label)


def safe_processing_params(params: dict[str, Any]) -> dict[str, Any]:
    """Accept only operation-level parameters owned by the companion contract."""
    operation = str(params.get("operation") or "").strip()
    if operation not in PROCESSING_OPERATION_CATALOG:
        return {}
    safe: dict[str, Any] = {"operation": operation}
    if operation in TWO_LAYER_OPERATIONS and not str(params.get("target_layer") or "").strip():
        # Refused rather than defaulted. A two-layer operation with one layer is
        # not a smaller version of the same request; it is a different question
        # nobody asked.
        return {}
    if "distance" in params:
        try:
            distance = float(params.get("distance"))
        except (TypeError, ValueError):
            return {}
        if distance <= 0 or distance > 1000000:
            return {}
        safe["distance"] = distance
    if "segments" in params:
        try:
            segments = int(params.get("segments"))
        except (TypeError, ValueError):
            return {}
        if segments < 1 or segments > 96:
            return {}
        safe["segments"] = segments
    for key in ("predicate", "target_layer", "field"):
        if key in params:
            value = str(params.get(key) or "").strip()[:MAX_PARAM_TEXT]
            if value:
                safe[key] = value
    return safe


def resolve_processing_algorithm(registry: Any, operation: str) -> tuple[str, Any] | tuple[str, None]:
    """Resolve a trusted operation name to an installed QGIS algorithm."""
    for algorithm_id in PROCESSING_OPERATION_CATALOG.get(operation, ()):
        algorithm = registry.algorithmById(algorithm_id) if registry is not None else None
        if algorithm is not None:
            return algorithm_id, algorithm
    return "", None


def build_algorithm_parameters(algorithm: Any, operation: str, layer: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Build a conservative native parameter map from validated operation params."""
    names = {definition.name() for definition in algorithm.parameterDefinitions()}
    payload: dict[str, Any] = {}
    if "INPUT" in names:
        payload["INPUT"] = layer
    if "INTERSECT" in names and params.get("target_layer"):
        payload["INTERSECT"] = params["target_layer"]
    if "OVERLAY" in names and params.get("target_layer"):
        payload["OVERLAY"] = params["target_layer"]
    if "DISTANCE" in names:
        payload["DISTANCE"] = float(params.get("distance") or 100)
    if "RADIUS" in names:
        payload["RADIUS"] = float(params.get("distance") or 100)
    if "SEGMENTS" in names:
        payload["SEGMENTS"] = int(params.get("segments") or 16)
    if "TARGET_CRS" in names:
        payload["TARGET_CRS"] = layer.crs()
    if "LAYERS" in names:
        # native:mergevectorlayers takes a list rather than INPUT/OVERLAY.
        payload["LAYERS"] = [layer] + ([params["target_layer"]] if params.get("target_layer") else [])
    if "LINES" in names and params.get("target_layer"):
        payload["LINES"] = params["target_layer"]
    if "FIELD" in names and params.get("field"):
        payload["FIELD"] = params["field"]
    if "PREDICATE" in names:
        payload["PREDICATE"] = [0]
    for key in PROCESSING_OUTPUT_KEYS:
        if key in names:
            payload[key] = "TEMPORARY_OUTPUT"
    return payload
