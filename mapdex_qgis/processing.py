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
