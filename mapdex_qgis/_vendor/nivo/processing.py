# SPDX-License-Identifier: MIT
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
    "spatial_join": ("native:joinattributesbylocation", "qgis:joinattributesbylocation"),
    "simplify": ("native:simplifygeometries", "qgis:simplifygeometries"),
    "repair": ("native:fixgeometries", "qgis:fixgeometries"),
    "validate": ("qgis:checkvalidity", "native:checkvalidity"),
}

# The operations that have a capability of their own rather than being reached
# through the generic bridge. Kept here, beside the catalog, so the handler
# table and the capability declarations cannot name different sets: an id
# advertised with no handler is the "Nivo prepared an action" and nothing
# happens failure, and a handler nobody advertises is dead code.
NAMED_GEOPROCESSING_OPERATIONS: tuple[str, ...] = (
    "buffer", "clip", "intersection", "union", "difference",
    "dissolve", "merge", "centroid", "convex_hull", "reproject",
    "spatial_join", "simplify", "repair", "validate", "split", "zonal_statistics",
)

# Operations that combine two layers. Naming them makes the requirement
# checkable rather than implied by whichever parameter the algorithm happens to
# expose: running one of these against a single layer produces an empty or
# nonsensical result instead of an error.
TWO_LAYER_OPERATIONS = frozenset({
    "clip", "intersection", "union", "difference", "merge",
    "select_by_location", "nearest_neighbor", "split", "zonal_statistics",
    "spatial_join",
})

# Operations that need a destination reference system. Reprojection is the
# whole request here: without one there is nothing to reproject TO, and the
# algorithm's TARGET_CRS used to be filled with the layer's OWN crs, so a
# reprojection ran, reported success and added a duplicate layer in the
# reference system it started in.
CRS_REQUIRED_OPERATIONS = frozenset({"reproject"})

# The parameter names an algorithm writes its result to. Ordered rather than a
# set, because the ORDER decides which layer is loaded when an algorithm writes
# several: `checkvalidity` writes valid, invalid and error layers, and the
# invalid one is the answer - loading the features that passed tells a reviewer
# nothing they can act on.
PROCESSING_OUTPUT_ORDER: tuple[str, ...] = (
    "OUTPUT", "OUTPUT_LAYER", "OUTPUT_VECTOR", "OUTPUT_RASTER", "INVALID_OUTPUT",
)

PROCESSING_OUTPUT_KEYS = frozenset(PROCESSING_OUTPUT_ORDER)

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
    "spatial_join": "spatial join",
    "simplify": "simplification",
    "repair": "geometry repair",
    "validate": "geometry check",
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
    if "tolerance" in params:
        try:
            tolerance = float(params.get("tolerance"))
        except (TypeError, ValueError):
            return {}
        if tolerance <= 0 or tolerance > 1000000:
            return {}
        safe["tolerance"] = tolerance
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
    if "target_crs" in params:
        crs = normalize_crs_reference(params.get("target_crs"))
        if not crs:
            return {}
        safe["target_crs"] = crs
    if operation in CRS_REQUIRED_OPERATIONS and "target_crs" not in safe:
        # Refused rather than defaulted to the layer's own system. That default
        # is what made a reprojection a no-op that reported success.
        return {}
    return safe


def normalize_crs_reference(value: Any) -> str:
    """An authority:code reference, or empty when it is not one.

    Only the FORM is checked here; whether the code exists is QGIS's question
    and it answers it when the string is resolved. Accepting free text would
    hand an unresolvable reference to the algorithm and turn a typo into a
    silently wrong projection.
    """
    text = str(value or "").strip().upper()
    if ":" not in text:
        return ""
    authority, _, code = text.partition(":")
    authority = authority.strip()
    code = code.strip()
    if not authority.isalpha() or len(authority) > 16:
        return ""
    if not code.isdigit() or len(code) > 12:
        return ""
    return "{}:{}".format(authority, code)



def predicate_index(algorithm: Any, requested: Any) -> int | None:
    """Where this algorithm keeps the named relationship in its own options.

    Returns None when the algorithm does not offer it. Absent means the
    algorithm's own first option, which is what a caller who named no
    relationship is asking for.
    """
    name = str(requested or "").strip().lower()
    options: list[str] = []
    try:
        definition = algorithm.parameterDefinition("PREDICATE")
        options = [str(option).strip().lower() for option in (definition.options() or [])]
    except Exception:  # noqa: BLE001 - a definition without options is not fatal
        options = []
    if not name:
        return 0
    if not options:
        # Nothing to match against. Refusing is right: guessing an index here
        # is how the constant [0] survived.
        return None
    for index, option in enumerate(options):
        # QGIS spells them "intersect", "are within", "contain"; the contract
        # spells them "intersects", "within", "contains". Dropping the leading
        # "are " and letting either string be a prefix of the other matches
        # every pair exactly, with no character count to get wrong: a first
        # version compared six-character stems and silently refused "within"
        # against "are within".
        candidate = option[4:] if option.startswith("are ") else option
        if candidate and (name.startswith(candidate) or candidate.startswith(name)):
            return index
    return None


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
        # The destination the caller asked for. Falling back to the layer's own
        # crs is kept only for algorithms where TARGET_CRS is incidental output
        # metadata rather than the request; `reproject` can no longer reach here
        # without one, because safe_processing_params refuses it.
        payload["TARGET_CRS"] = params.get("target_crs") or layer.crs()
    if "LAYERS" in names:
        # native:mergevectorlayers takes a list rather than INPUT/OVERLAY.
        payload["LAYERS"] = [layer] + ([params["target_layer"]] if params.get("target_layer") else [])
    if "LINES" in names and params.get("target_layer"):
        payload["LINES"] = params["target_layer"]
    if "FIELD" in names and params.get("field"):
        payload["FIELD"] = params["field"]
    if "TOLERANCE" in names:
        # Simplification is entirely this number: without it the algorithm's
        # own default decides how much detail a customer's boundary loses.
        payload["TOLERANCE"] = float(params.get("tolerance") or 1.0)
    if "JOIN" in names and params.get("target_layer"):
        # `native:joinattributesbylocation` names the second layer JOIN rather
        # than OVERLAY or INTERSECT.
        payload["JOIN"] = params["target_layer"]
    if "PREDICATE" in names:
        # The requested relationship, matched against THIS algorithm's own
        # option list.
        #
        # It used to be the constant [0], so every spatial relationship
        # question ran as `intersects` whatever was asked - the parameter was
        # accepted, validated against an enum, and then thrown away. An index
        # table would not fix it either: `native:selectbylocation` and
        # `native:joinattributesbylocation` do not order their options the
        # same way, and the versions move. Reading the live options is the only
        # way the index means what the name says.
        index = predicate_index(algorithm, params.get("predicate"))
        if index is None:
            # A relationship this algorithm does not offer. Refused by
            # returning nothing for it, so the caller sees the algorithm
            # decline rather than a confident answer about a different
            # relationship.
            return {}
        payload["PREDICATE"] = [index]
    for key in PROCESSING_OUTPUT_KEYS:
        if key in names:
            payload[key] = "TEMPORARY_OUTPUT"
    return payload
