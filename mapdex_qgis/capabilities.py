"""The trusted capability registry: what Nivo is allowed to do, and where.

This is the contract between the model and the product. The model never emits
Python, SQL, QML, shell, a GDAL command line or a QGIS algorithm id. It emits a
capability id plus parameters; trusted code resolves and validates them here,
and only then executes.

Everything the agent needs to reason about a capability is declared, not
inferred from a name: which client can run it, where it executes, what it can
target, whether it is reversible, and whether it needs confirmation. That makes
the registry the single place a contributor edits to add a capability - one
:func:`register` call plus an executor - instead of extending an ``if/elif``
chain in a prompt router.

Risk is metadata, never a model claim. A capability the registry marks
``consequential`` requires confirmation even if a model insists otherwise, and a
capability that is not registered at all cannot be executed by any path.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Mapping, Sequence

PROTOCOL_VERSION = "companion.v2"

# Where a capability physically runs. Distinct from "which vendor" - that is the
# provider layer - and from risk.
EXEC_LOCAL = "local"                # in-process QGIS/Python, no network
EXEC_QGIS_PROCESSING = "qgis_processing"
EXEC_POSTGIS = "postgis"            # read-only, on the user's database
EXEC_MAPDEX = "mapdex"              # durable server-side Run
EXEC_CLIENT_UI = "client_ui"        # a pure UI/navigation effect

# Risk classes. ``forbidden`` exists so a refusal is a registry fact with a
# stated reason, not an ad-hoc string in a prompt.
RISK_SAFE = "safe"
RISK_CONSEQUENTIAL = "consequential"
RISK_FORBIDDEN = "forbidden"

CLIENT_QGIS = "qgis"
CLIENT_WORKSPACE = "workspace"

MAX_PARAM_STRING = 256
MAX_PARAM_LIST = 500


class CapabilityError(Exception):
    """A capability request was not valid. Always safe to show to the user."""


class Capability:
    """One declared, trusted operation."""

    def __init__(
        self,
        identifier: str,
        domain: str,
        summary: str,
        params: Mapping[str, Mapping[str, Any]] | None = None,
        targets: Sequence[str] = (),
        risk: str = RISK_SAFE,
        reversible: bool = False,
        execution: str = EXEC_LOCAL,
        clients: Sequence[str] = (CLIENT_QGIS,),
        previewable: bool = False,
        produces: Sequence[str] = (),
        requires_confirmation: bool | None = None,
        refusal: str = "",
    ):
        self.id = identifier
        self.domain = domain
        self.summary = summary
        self.params = {name: dict(spec) for name, spec in (params or {}).items()}
        self.targets = tuple(targets)
        self.risk = risk
        self.reversible = bool(reversible)
        self.execution = execution
        self.clients = tuple(clients)
        self.previewable = bool(previewable)
        self.produces = tuple(produces)
        # Confirmation is derived from risk unless explicitly overridden, so a
        # new consequential capability cannot be added without a gate by
        # forgetting a flag.
        self.requires_confirmation = (
            bool(requires_confirmation) if requires_confirmation is not None else risk == RISK_CONSEQUENTIAL
        )
        self.refusal = refusal

    def available_to(self, client: str) -> bool:
        return client in self.clients

    def describe(self) -> dict[str, Any]:
        """The machine-readable form given to the agent so it can plan."""
        return {
            "id": self.id,
            "domain": self.domain,
            "summary": self.summary,
            "params": self.params,
            "targets": list(self.targets),
            "risk": self.risk,
            "reversible": self.reversible,
            "requires_confirmation": self.requires_confirmation,
            "execution": self.execution,
            "produces": list(self.produces),
        }


_REGISTRY: dict[str, Capability] = {}


def register(capability: Capability) -> Capability:
    """Add a capability. The extension point for OSS contributors."""
    if not capability.id or "@" not in capability.id:
        raise ValueError("capability id must be name@version")
    _REGISTRY[capability.id] = capability
    return capability


def get(identifier: str) -> Capability | None:
    return _REGISTRY.get(str(identifier or ""))


def all_capabilities() -> list[Capability]:
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def for_client(client: str, include_forbidden: bool = False) -> list[Capability]:
    """The catalogue one client may use - the agent's allowed-tool list."""
    return [
        capability
        for capability in all_capabilities()
        if capability.available_to(client) and (include_forbidden or capability.risk != RISK_FORBIDDEN)
    ]


def catalog_for_prompt(client: str) -> list[dict[str, Any]]:
    return [capability.describe() for capability in for_client(client)]


# --------------------------------------------------------------------------
# Parameter validation
# --------------------------------------------------------------------------

def _coerce(name: str, spec: Mapping[str, Any], value: Any) -> Any:
    kind = str(spec.get("type") or "string")
    if kind == "string":
        text = str(value if value is not None else "").strip()[:MAX_PARAM_STRING]
        options = spec.get("enum")
        if options and text not in options:
            raise CapabilityError("{} must be one of: {}".format(name, ", ".join(map(str, options))))
        if not text and spec.get("required"):
            raise CapabilityError("{} is required".format(name))
        return text
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise CapabilityError("{} must be a number".format(name))
        if number != number or number in (float("inf"), float("-inf")):
            raise CapabilityError("{} must be a finite number".format(name))
        low, high = spec.get("min"), spec.get("max")
        if low is not None and number < low:
            raise CapabilityError("{} must be at least {}".format(name, low))
        if high is not None and number > high:
            raise CapabilityError("{} must be at most {}".format(name, high))
        return number
    if kind == "integer":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise CapabilityError("{} must be a whole number".format(name))
        low, high = spec.get("min"), spec.get("max")
        if low is not None and number < low:
            raise CapabilityError("{} must be at least {}".format(name, low))
        if high is not None and number > high:
            raise CapabilityError("{} must be at most {}".format(name, high))
        return number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        raise CapabilityError("{} must be true or false".format(name))
    if kind == "bbox":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise CapabilityError("{} must be [minx, miny, maxx, maxy]".format(name))
        try:
            box = [float(item) for item in value]
        except (TypeError, ValueError):
            raise CapabilityError("{} must contain four numbers".format(name))
        if box[0] > box[2] or box[1] > box[3]:
            raise CapabilityError("{} is inverted".format(name))
        return box
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            raise CapabilityError("{} must be a list".format(name))
        return [str(item)[:MAX_PARAM_STRING] for item in value[:MAX_PARAM_LIST]]
    if kind == "points":
        return _coerce_points(name, spec, value)
    raise CapabilityError("unsupported parameter type for {}".format(name))


def _coerce_points(name: str, spec: Mapping[str, Any], value: Any) -> list[list[float]]:
    """A bounded list of ``[x, y]`` pairs, each a real number and nothing else.

    ``list`` cannot carry this: it stringifies every item, which turns a
    coordinate into text. The discipline is the one ``postgis.bind_numeric_parameters``
    already applies to a database parameter, for the same reason - the value is
    about to become geometry, so anything that is not a finite number has to be
    refused rather than coerced into something plausible.

    ``bool`` is rejected explicitly. It subclasses ``int``, so ``True`` would
    otherwise sail through as the coordinate 1.0 and place a vertex on the
    equator. Numeric STRINGS are refused too: a caller sending ``"41.0"`` has
    sent text, and quietly reading it as a position is how a shape ends up
    somewhere nobody chose.
    """
    if not isinstance(value, (list, tuple)):
        raise CapabilityError("{} must be a list of [x, y] pairs".format(name))
    if len(value) > MAX_PARAM_LIST:
        raise CapabilityError("{} carries more than {} points".format(name, MAX_PARAM_LIST))
    points: list[list[float]] = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise CapabilityError("{} must contain [x, y] pairs".format(name))
        ordinates: list[float] = []
        for ordinate in pair:
            if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
                raise CapabilityError("{} must contain numbers".format(name))
            number = float(ordinate)
            if not math.isfinite(number):
                raise CapabilityError("{} must contain finite numbers".format(name))
            ordinates.append(number)
        points.append(ordinates)
    minimum = spec.get("min_points")
    if minimum is not None and len(points) < minimum:
        raise CapabilityError("{} needs at least {} point(s)".format(name, minimum))
    if not points and spec.get("required"):
        raise CapabilityError("{} is required".format(name))
    return points


def validate_request(identifier: str, params: Mapping[str, Any] | None, client: str = CLIENT_QGIS) -> dict[str, Any]:
    """Turn an untrusted capability request into a validated, typed call.

    Rejects unknown capabilities, capabilities the client cannot run, unknown
    parameters (so a smuggled ``sql`` or ``python`` key cannot ride along),
    out-of-range values, and anything the registry marks forbidden.
    """
    capability = get(identifier)
    if capability is None:
        raise CapabilityError("unknown capability: {}".format(str(identifier)[:80]))
    if capability.risk == RISK_FORBIDDEN:
        raise CapabilityError(capability.refusal or "that operation is not available")
    if not capability.available_to(client):
        raise CapabilityError("{} is not available in this client".format(capability.id))
    supplied = dict(params or {})
    unknown = set(supplied) - set(capability.params)
    if unknown:
        # An unexpected key is a protocol violation, not something to ignore:
        # silently dropping it is how a smuggled parameter gets normalised away
        # in one release and quietly honoured in the next.
        raise CapabilityError("unexpected parameter: {}".format(sorted(unknown)[0]))
    validated: dict[str, Any] = {}
    for name, spec in capability.params.items():
        if name not in supplied:
            if spec.get("required"):
                raise CapabilityError("{} is required".format(name))
            if "default" in spec:
                validated[name] = spec["default"]
            continue
        validated[name] = _coerce(name, spec, supplied[name])
    return {
        "capability": capability.id,
        "params": validated,
        "risk": capability.risk,
        "requires_confirmation": capability.requires_confirmation,
        "reversible": capability.reversible,
        "execution": capability.execution,
    }


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

_LAYER = {"layer_id": {"type": "string", "required": True}}
_FIELD = {"field": {"type": "string", "required": True}}


def _c(identifier, domain, summary, **kwargs) -> Capability:
    return register(Capability(identifier, domain, summary, **kwargs))


# -- inspection (safe, read-only, both clients) ----------------------------
_c("inspect.layer@1", "inspect", "Report a layer's type, CRS, extent, feature count and fields.",
   params=_LAYER, targets=("vector", "raster"), produces=("layer_profile",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("inspect.project@1", "inspect", "List the layers in the project with their type and CRS.",
   produces=("project_profile",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("inspect.fields@1", "inspect", "List a layer's fields with their types and null counts.",
   params=_LAYER, targets=("vector",), produces=("field_profile",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("inspect.raster@1", "inspect", "Report raster size, bands, resolution, nodata and georeferencing state.",
   params=_LAYER, targets=("raster",), produces=("raster_profile",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- analytics (safe; the heart of the product) ----------------------------
_c("analytics.profile@1", "analytics", "Profile a dataset: counts, field types and notable distributions.",
   params=_LAYER, targets=("vector",), produces=("analysis",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.numeric@1", "analytics", "Descriptive statistics for one numeric field.",
   params=dict(_LAYER, **_FIELD, **{"scope": {"type": "string", "enum": ["all", "selection", "viewport"],
                                              "default": "all"}}),
   targets=("vector",), produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.categories@1", "analytics", "Frequency breakdown of one categorical field.",
   params=dict(_LAYER, **_FIELD, **{"limit": {"type": "integer", "min": 1, "max": 50, "default": 25},
                                    "scope": {"type": "string", "enum": ["all", "selection", "viewport"],
                                              "default": "all"}}),
   targets=("vector",), produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.histogram@1", "analytics", "Distribution of a numeric field as a histogram.",
   params=dict(_LAYER, **_FIELD, **{"bins": {"type": "integer", "min": 2, "max": 60, "default": 12}}),
   targets=("vector",), produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.top_n@1", "analytics", "Rank features by a numeric field and select the top or bottom N.",
   params=dict(_LAYER, **_FIELD, **{"limit": {"type": "integer", "min": 1, "max": 500, "default": 10},
                                    "ascending": {"type": "boolean", "default": False}}),
   targets=("vector",), produces=("analysis", "selection"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.outliers@1", "analytics", "Find unusually high or low values with a stated rule.",
   params=dict(_LAYER, **_FIELD, **{"method": {"type": "string", "enum": ["iqr", "zscore"], "default": "iqr"}}),
   targets=("vector",), produces=("analysis", "selection"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.group@1", "analytics", "Aggregate a field by group: count, sum, mean, min, max or median.",
   params=dict(_LAYER, **{"group_field": {"type": "string", "required": True},
                          "value_field": {"type": "string"},
                          "statistic": {"type": "string",
                                        "enum": ["count", "sum", "mean", "min", "max", "median", "distinct"],
                                        "default": "count"}}),
   targets=("vector",), produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
#
# Measuring the shape rather than a column. Everything above this line answers a
# question about an attribute; nothing answered "what is the total area?", so a
# question about the geometry had to be declined or, worse, answered from the
# column whose name looked closest. On the OGC conformance sample that column is
# literally called `real`, it sums to 2,715.68 where the true area is 7,627.77,
# and by it the largest polygon is the smallest one.
#
# `metric` is the same word the server's own spatial:measure@1 uses, and
# scripts/nivo_registry_drift.py pairs the two so the shared parameters cannot
# drift apart. The unit is never assumed: the executor states the frame the
# figure was taken in, or refuses.
_c("analytics.geometry@1", "analytics",
   "Measure the geometry itself - area, length or perimeter - totalled, averaged or ranked.",
   params=dict(_LAYER, **{"metric": {"type": "string", "required": True,
                                     "enum": ["area", "length", "perimeter"]},
                          "statistic": {"type": "string",
                                        "enum": ["sum", "mean", "min", "max", "count", "top", "bottom"],
                                        "default": "sum"},
                          "limit": {"type": "integer", "min": 1, "max": 500, "default": 5},
                          "scope": {"type": "string", "enum": ["all", "selection", "viewport"],
                                    "default": "all"}}),
   targets=("vector",), produces=("analysis", "selection"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("analytics.compare@1", "analytics", "Compare a numeric field between two feature sets or two layers.",
   params={"layer_id": {"type": "string", "required": True},
           "field": {"type": "string", "required": True},
           "other_layer_id": {"type": "string"},
           "scope": {"type": "string", "enum": ["selection_vs_all", "viewport_vs_all", "layer_vs_layer"],
                     "default": "selection_vs_all"}},
   targets=("vector",), produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- spatial analytics (safe: computed, not written) -----------------------
_c("spatial.count_in_polygons@1", "spatial", "Count features of one layer inside each polygon of another.",
   params={"polygon_layer_id": {"type": "string", "required": True},
           "point_layer_id": {"type": "string", "required": True},
           "group_field": {"type": "string"}},
   targets=("vector",), produces=("analysis", "map_effect"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("spatial.density@1", "spatial", "Normalise counts by polygon area to show density per km².",
   params={"polygon_layer_id": {"type": "string", "required": True},
           "point_layer_id": {"type": "string", "required": True},
           "group_field": {"type": "string"}},
   targets=("vector",), produces=("analysis", "map_effect"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("spatial.relate@1", "spatial", "Select features of one layer by their spatial relationship to another.",
   params={"layer_id": {"type": "string", "required": True},
           "other_layer_id": {"type": "string", "required": True},
           "predicate": {"type": "string",
                         "enum": ["intersects", "within", "contains", "overlaps", "touches", "crosses", "disjoint"],
                         "default": "intersects"},
           "use_selection": {"type": "boolean", "default": False}},
   targets=("vector",), produces=("analysis", "selection"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("spatial.near@1", "spatial", "Select features within a real-world distance of another layer.",
   params={"layer_id": {"type": "string", "required": True},
           "other_layer_id": {"type": "string", "required": True},
           "distance": {"type": "number", "required": True, "min": 0, "max": 1_000_000},
           "unit": {"type": "string", "enum": ["m", "km", "ft", "mi"], "default": "m"}},
   targets=("vector",), produces=("analysis", "selection"), reversible=True, previewable=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("spatial.nearest@1", "spatial", "Find the N nearest features to a point or to the current selection.",
   params={"layer_id": {"type": "string", "required": True},
           "limit": {"type": "integer", "min": 1, "max": 500, "default": 10}},
   targets=("vector",), produces=("analysis", "selection"), reversible=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- map navigation and presentation (safe, reversible) --------------------
_c("map.zoom_layer@1", "map", "Zoom the map to a layer's extent.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("map.zoom_selection@1", "map", "Zoom the map to the current selection.",
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("map.zoom_extent@1", "map", "Zoom to an explicit bounding box.",
   params={"bbox": {"type": "bbox", "required": True}, "crs": {"type": "string"}},
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("map.previous_extent@1", "map", "Go back to the previous map view.",
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",))
_c("map.refresh@1", "map", "Redraw the map canvas.", execution=EXEC_CLIENT_UI, produces=("map_effect",))
_c("map.basemap@1", "map", "Add an OpenStreetMap XYZ basemap.",
   params={"provider": {"type": "string", "enum": ["osm"], "default": "osm"}},
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",))
_c("layer.visibility@1", "layer", "Show or hide a layer.",
   params=dict(_LAYER, **{"visible": {"type": "boolean", "required": True}}),
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("layer.opacity@1", "layer", "Set a layer's opacity.",
   params=dict(_LAYER, **{"opacity": {"type": "number", "required": True, "min": 0, "max": 100}}),
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("layer.activate@1", "layer", "Make a layer the active layer.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("layer.attribute_table@1", "layer", "Open the attribute table for a layer.", params=_LAYER,
   execution=EXEC_CLIENT_UI, produces=("ui_effect",))

# -- selection and filtering (safe, reversible) ----------------------------
_c("selection.all@1", "selection", "Select every feature in a layer.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("selection",))
_c("selection.clear@1", "selection", "Clear a layer's selection.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("selection",))
_c("selection.invert@1", "selection", "Invert a layer's selection.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("selection",))
_c("selection.by_ids@1", "selection", "Select specific features by identifier - how analysis reaches the map.",
   params=dict(_LAYER, **{"feature_ids": {"type": "list", "required": True}}),
   execution=EXEC_CLIENT_UI, reversible=True, produces=("selection",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("filter.preview@1", "filter", "Preview a validated attribute or spatial filter on a layer.",
   params=dict(_LAYER, **{"field": {"type": "string", "required": True},
                          "operator": {"type": "string", "required": True,
                                       "enum": ["=", "!=", ">", ">=", "<", "<=", "in", "is null", "is not null"]},
                          "value": {"type": "string"}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("filter.clear@1", "filter", "Remove a preview filter from a layer.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- cartography (safe, reversible previews) -------------------------------
_c("style.categorized@1", "style", "Colour a layer by the distinct values of a field.",
   params=dict(_LAYER, **_FIELD, **{"ramp": {"type": "string", "default": "Spectral"}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("style.graduated@1", "style", "Colour a layer by numeric classes - the map form of an analysis.",
   params=dict(_LAYER, **_FIELD, **{"classes": {"type": "integer", "min": 2, "max": 12, "default": 5},
                                    "method": {"type": "string",
                                               "enum": ["quantile", "equal_interval", "natural_breaks", "stddev"],
                                               "default": "quantile"},
                                    "ramp": {"type": "string", "default": "Viridis"}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("style.single@1", "style", "Apply a single symbol with a colour, stroke and size.",
   params=dict(_LAYER, **{"color": {"type": "string"}, "stroke_width": {"type": "number", "min": 0, "max": 20},
                          "size": {"type": "number", "min": 0, "max": 50},
                          "opacity": {"type": "number", "min": 0, "max": 100}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",))
_c("style.labels@1", "style", "Label a layer from one of its existing fields.",
   params=dict(_LAYER, **{"field": {"type": "string", "required": True},
                          "size": {"type": "number", "min": 4, "max": 48, "default": 9},
                          "enabled": {"type": "boolean", "default": True}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("style.raster@1", "style", "Set raster band rendering, stretch and opacity.",
   params=dict(_LAYER, **{"band": {"type": "integer", "min": 1, "max": 64},
                          "ramp": {"type": "string"},
                          "opacity": {"type": "number", "min": 0, "max": 100}}),
   targets=("raster",), execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",))
_c("style.undo@1", "style", "Undo the last reversible presentation change.",
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- PostGIS: read-only analytics ------------------------------------------
_c("postgis.profile@1", "postgis", "Profile a PostGIS table: feature count, extent, SRID and validity.",
   params={"connection_id": {"type": "string", "required": True},
           "schema": {"type": "string", "required": True},
           "table": {"type": "string", "required": True}},
   execution=EXEC_POSTGIS, produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
#
# These three are declared for both clients and the Workspace implements them as
# `geo:postgis_profile@1`, `geo:postgis_analyze@1` and `geo:postgis_spatial@1`.
# `scripts/nivo_registry_drift.py` pairs them and fails the build if the two
# registries stop agreeing about what a question takes, so a parameter added
# here without its counterpart is caught rather than discovered by a user who
# asked the same thing on the other surface.
_c("postgis.analyze@1", "postgis", "Read-only statistics, categories or group-by aggregation on a PostGIS table.",
   params={"connection_id": {"type": "string", "required": True},
           "schema": {"type": "string", "required": True},
           "table": {"type": "string", "required": True},
           "operation": {"type": "string", "required": True,
                         "enum": ["numeric", "categories", "group", "top_n", "bbox_count", "nearest"]},
           "field": {"type": "string"},
           "group_field": {"type": "string"},
           # top_n and nearest return identifiers so the answer can be selected
           # on the map, and nearest needs a probe point. build_top_n and
           # build_nearest have always taken these; leaving them undeclared made
           # two of the six operations unreachable through the registry.
           "id_field": {"type": "string"},
           "ascending": {"type": "boolean", "default": False},
           "x": {"type": "number"},
           "y": {"type": "number"},
           "srid": {"type": "integer", "default": 4326},
           "statistic": {"type": "string",
                         "enum": ["count", "sum", "mean", "min", "max", "median", "distinct"], "default": "count"},
           "limit": {"type": "integer", "min": 1, "max": 1000, "default": 25},
           "bbox": {"type": "bbox"}},
   execution=EXEC_POSTGIS, produces=("analysis", "selection"),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("postgis.spatial@1", "postgis", "Read-only spatial relationship counts and joins between PostGIS tables.",
   params={"connection_id": {"type": "string", "required": True},
           "schema": {"type": "string", "required": True},
           "table": {"type": "string", "required": True},
           # Optional: the overwhelmingly common case is two tables in the same
           # schema, and making the caller repeat it is friction with no safety
           # value. It defaults to `schema`.
           "other_schema": {"type": "string"},
           "other_table": {"type": "string", "required": True},
           "predicate": {"type": "string",
                         "enum": ["intersects", "within", "contains", "overlaps", "touches", "crosses", "disjoint"],
                         "default": "intersects"},
           "group_field": {"type": "string"},
           "limit": {"type": "integer", "min": 1, "max": 200, "default": 50}},
   execution=EXEC_POSTGIS, produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("postgis.write@1", "postgis", "Modify PostGIS data.", risk=RISK_FORBIDDEN, execution=EXEC_POSTGIS,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE),
   refusal=("Nivo's database access is read-only by design, so it cannot insert, update, delete or change "
            "schema. Make that change in QGIS or your database client and I'll analyse the result."))

# -- QGIS Processing: consequential, confirmation required -----------------
_c("processing.run@1", "processing", "Run an installed QGIS Processing algorithm and load its output.",
   params={"operation": {"type": "string", "required": True},
           "layer_id": {"type": "string", "required": True},
           "other_layer_id": {"type": "string"},
           "distance": {"type": "number", "min": 0, "max": 1_000_000},
           "unit": {"type": "string", "enum": ["m", "km", "ft", "mi"], "default": "m"},
           "segments": {"type": "integer", "min": 1, "max": 96},
           "predicate": {"type": "string",
                         "enum": ["intersects", "within", "contains", "overlaps", "touches", "crosses", "disjoint"]}},
   risk=RISK_CONSEQUENTIAL, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=False)
_c("processing.discover@1", "processing", "List the installed Processing algorithms that fit an objective.",
   params={"objective": {"type": "string", "required": True}},
   execution=EXEC_LOCAL, produces=("catalog",))

# -- Mapdex server-side work: consequential --------------------------------
_c("mapdex.workflow@1", "mapdex", "Run a Mapdex workflow (georeference, extract, validate, convert) on a source.",
   params={"workflow": {"type": "string", "required": True,
                        "enum": ["georeference_maps", "digitize_parcels", "validate_deliver"]},
           "source_id": {"type": "string", "required": True}},
   risk=RISK_CONSEQUENTIAL, execution=EXEC_MAPDEX, produces=("run",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("mapdex.batch@1", "mapdex", "Run one verified workflow across several prepared sources.",
   params={"workflow": {"type": "string", "required": True},
           "source_ids": {"type": "list", "required": True}},
   risk=RISK_CONSEQUENTIAL, execution=EXEC_MAPDEX, produces=("batch",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("mapdex.jobs@1", "mapdex", "List Mapdex runs with their state, and explain a failure.",
   params={"state": {"type": "string", "enum": ["all", "active", "failed", "review_required", "completed"],
                     "default": "all"}},
   execution=EXEC_MAPDEX, produces=("jobs",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("mapdex.open_review@1", "mapdex", "Open the Mapdex review surface for a run that needs review.",
   params={"run_id": {"type": "string"}},
   execution=EXEC_MAPDEX, produces=("ui_effect",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("mapdex.import_result@1", "mapdex", "Add a completed Mapdex result to the map.",
   params={"run_id": {"type": "string", "required": True}},
   risk=RISK_CONSEQUENTIAL, execution=EXEC_MAPDEX, produces=("layer",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- reporting -------------------------------------------------------------
_c("report.build@1", "report", "Build an evidence-based report from the analyses run in this session.",
   params={"title": {"type": "string"}},
   execution=EXEC_LOCAL, produces=("report",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- explicit refusals -----------------------------------------------------
# -- Measurement and delivery: the two ends of an ordinary GIS session --------
#
# Scenario 1 finishes with "export it" and Scenario 2 is measuring between two
# clicked points. Both were unreachable, so the desktop could carry a whole
# analysis and then not hand it over.
_c("measure.distance@1", "measure", "Measure the distance between two positions.",
   params={"from_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "from_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "to_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "to_lat": {"type": "number", "required": True, "min": -90, "max": 90}},
   execution=EXEC_LOCAL, produces=("measurement",))
_c("export.layer@1", "export", "Write a layer to a file in a standard GIS format.",
   params={"layer_id": {"type": "string", "required": True},
           "format": {"type": "string",
                      "enum": ["geojson", "gpkg", "shp", "csv"],
                      "default": "gpkg"}},
   targets=("vector",), execution=EXEC_LOCAL, produces=("file",))

# -- Drawing: the shape the user pointed at ----------------------------------
#
# `features.py` refuses to invent a polygon or a line, and it is right to:
# corners nobody supplied are fabricated geometry. That refusal left the third
# release-gate scenario - draw an AOI, analyse it, inspect the result - with no
# way to start, because the plugin's answer to "draw" was to create an empty
# layer and tell the user to toggle editing themselves. The vertices here come
# from a person clicking on the canvas, which is the one honest source of them,
# and the shape lands as a real layer so every capability that takes a
# `layer_id` can then be pointed at it.
#
# `crs` is required and carries no default. The vertices are in whatever the
# canvas is projected to - a national grid as often as not - and stamping
# EPSG:4326 on them because nothing better was supplied would produce a shape
# claiming a place it was not drawn in. How many vertices each geometry needs is
# `maptools.refusal_for`, not a registry rule: it depends on the value of
# another parameter, and a schema that cannot see that would have to guess.
_c("draw.geometry@1", "draw",
   "Turn a shape drawn on the map canvas into a layer other operations can use.",
   params={"geometry": {"type": "string", "required": True,
                        "enum": ["point", "linestring", "polygon"]},
           "vertices": {"type": "points", "required": True, "min_points": 1},
           "crs": {"type": "string", "required": True},
           "name": {"type": "string"}},
   execution=EXEC_LOCAL, reversible=True, produces=("layer",))

# -- the field calculator ----------------------------------------------------
#
# Consequential and not reversible: it writes a column into the user's data, and
# once written there is no undo outside QGIS's own edit buffer. The expression
# is parsed by mapdex_qgis.expressions, a grammar that can only express
# arithmetic over the layer's own fields. That grammar, not a prompt
# instruction, is what stops a calculation reaching outside the row.
_c("field.calculate@1", "field", "Add a field computed from the layer's existing fields.",
   params={"layer_id": {"type": "string", "required": True},
           "field": {"type": "string", "required": True},
           "expression": {"type": "string", "required": True},
           "field_type": {"type": "string", "enum": ["number", "integer", "text"], "default": "number"}},
   targets=("vector",), risk=RISK_CONSEQUENTIAL, execution=EXEC_LOCAL,
   produces=("layer_change",), reversible=False, previewable=True)
_c("layer.reorder@1", "layer", "Move a layer up or down in the drawing order.",
   params=dict(_LAYER, **{"position": {"type": "string",
                                       "enum": ["top", "bottom", "up", "down"],
                                       "default": "top"}}),
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",))

_c("system.execute_code@1", "system", "Run arbitrary code.", risk=RISK_FORBIDDEN,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE),
   refusal="Nivo cannot run arbitrary code. Every action it takes comes from its trusted capability list.")
_c("system.execute_sql@1", "system", "Run arbitrary SQL.", risk=RISK_FORBIDDEN,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE),
   refusal=("Nivo does not run free-form SQL. Ask the analytical question instead - it builds a validated "
            "read-only query for you."))


def supported_action_kinds(client: str = CLIENT_QGIS) -> list[str]:
    """The capability ids this client advertises during negotiation."""
    return [capability.id for capability in for_client(client)]


def executable_domains() -> Iterable[str]:
    return sorted({capability.domain for capability in all_capabilities()})


def bind_executor(identifier: str, executor: Callable[..., Any]) -> None:
    """Attach a runtime executor to a registered capability."""
    capability = get(identifier)
    if capability is None:
        raise CapabilityError("cannot bind an executor to an unknown capability")
    setattr(capability, "executor", executor)
