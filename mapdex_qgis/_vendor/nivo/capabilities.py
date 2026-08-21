# SPDX-License-Identifier: MIT
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
# A durable Run on the host application's own server. Nivo does not name a
# vendor: a host registers its remote capabilities with this execution site and
# `offline_capability_ids` then reports, as a registry fact, exactly which part
# of the catalogue needs an account behind it.
EXEC_REMOTE_SERVICE = "remote_service"
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


def offline_capability_ids(client: str = CLIENT_QGIS) -> frozenset:
    """What this client can do with no host account behind it.

    Everything except the capabilities whose execution site is a remote
    service. That is the split stated as a registry fact rather than a list
    somebody maintains: whatever a host registers as server-side work stays
    account-gated, and everything else keeps working offline.

    Read-only PostGIS is deliberately included. It executes on the user's own
    database, which they already own, and excluding it would gate a local
    capability behind an account for no reason but tidiness.
    """
    return frozenset(
        capability.id
        for capability in for_client(client)
        if capability.execution != EXEC_REMOTE_SERVICE
    )


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
    if kind == "legs":
        return _coerce_legs(name, spec, value)
    raise CapabilityError("unsupported parameter type for {}".format(name))


def _coerce_legs(name: str, spec: Mapping[str, Any], value: Any) -> list[dict]:
    """A traverse's legs: bearing and length, both finite, nothing else.

    The same discipline as ``_coerce_points`` and for the same reason. A leg is
    about to be walked across the ellipsoid, so a bearing that is text or a
    length that is infinite has to be refused rather than coerced into
    something plausible - a plausible leg puts a station somewhere nobody
    measured.
    """
    if not isinstance(value, (list, tuple)):
        raise CapabilityError("{} must be a list of legs".format(name))
    legs: list[dict] = []
    for index, item in enumerate(value[:MAX_PARAM_LIST]):
        if not isinstance(item, Mapping):
            raise CapabilityError("{} leg {} must be an object".format(name, index + 1))
        try:
            azimuth = float(item.get("azimuth_deg"))
            distance = float(item.get("distance_m"))
        except (TypeError, ValueError):
            raise CapabilityError(
                "{} leg {} needs a numeric azimuth_deg and distance_m".format(name, index + 1))
        if not (math.isfinite(azimuth) and math.isfinite(distance)):
            raise CapabilityError("{} leg {} is not finite".format(name, index + 1))
        if distance <= 0:
            raise CapabilityError("{} leg {} has no length".format(name, index + 1))
        legs.append({"azimuth_deg": azimuth, "distance_m": distance})
    if not legs:
        raise CapabilityError("{} needs at least one leg".format(name))
    return legs


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
# The parameter names are the SERVER's, deliberately.
#
# All three used to differ - `group_field` for `group_by`, `value_field` for
# `field`, `statistic` for `stat` - so an assistant that learned a grouped
# aggregation on one surface got every argument wrong on the other, and both
# registries refuse an unexpected parameter, so the call was rejected outright
# rather than degraded. `packages/contracts` is the source of truth for the
# vocabulary (RULES.md), so the desktop adopts it rather than the other way
# round, and the drift gate no longer has to carry the divergence as accepted.
_c("analytics.group@1", "analytics", "Aggregate a field by group: count, sum, mean, min, max or median.",
   params=dict(_LAYER, **{"group_by": {"type": "string", "required": True},
                          "field": {"type": "string"},
                          "stat": {"type": "string",
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
# `k` rather than `limit`, for the same reason the grouped aggregation adopted
# the server's names: an assistant that learned one surface should not have to
# unlearn the count's name on the other. What remains different is real and not
# a spelling - the server takes an explicit lon/lat and the desktop takes the
# point from the current selection, because a QGIS user has clicked - and the
# drift gate carries that difference with its reason.
_c("spatial.nearest@1", "spatial", "Find the N nearest features to a point or to the current selection.",
   params={"layer_id": {"type": "string", "required": True},
           "k": {"type": "integer", "min": 1, "max": 500, "default": 10}},
   targets=("vector",), produces=("analysis", "selection"), reversible=True,
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# -- map navigation and presentation (safe, reversible) --------------------
_c("map.zoom_layer@1", "map", "Zoom the map to a layer's extent.", params=_LAYER,
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
_c("map.zoom_selection@1", "map", "Zoom the map to the current selection.",
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
# `bounds` is the server's name for the same box (map:fit_bounds@1). The
# resolver still reads `bbox`, so an older payload is not refused by a newer
# plugin; what changed is the name an assistant is taught.
#
# The `crs` beside it is a real difference and stays: the desktop can be handed
# a projected extent and the server assumes WGS84, so this surface can express
# something the other cannot.
_c("map.zoom_extent@1", "map", "Zoom to an explicit bounding box.",
   params={"bounds": {"type": "bbox", "required": True}, "crs": {"type": "string"}},
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
# The halo is on by default and its colour is COMPUTED from the text colour.
# QGIS labels a layer with plain text and no halo, which is legible on a white
# page and invisible over a satellite basemap, a hillshade or a choropleth - and
# a person asking to label a layer is asking to be able to read the labels.
_c("style.labels@1", "style", "Label a layer from one of its existing fields, legibly: "
   "a contrasting halo by default and a placement chosen for the geometry.",
   params=dict(_LAYER, **{"field": {"type": "string", "required": True},
                          "size": {"type": "number", "min": 4, "max": 48, "default": 9},
                          "enabled": {"type": "boolean", "default": True},
                          "color": {"type": "string"},
                          "halo": {"type": "boolean", "default": True},
                          "halo_size": {"type": "number", "min": 0.2, "max": 3},
                          "halo_color": {"type": "string"},
                          "placement": {"type": "string"}}),
   execution=EXEC_CLIENT_UI, reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# Scale-dependent visibility. A layer drawn at every zoom is what turns a city
# map into a smear of labels.
_c("layer.scale_range@1", "layer", "Draw a layer only between two map scales.",
   params=dict(_LAYER, **{
       "minimum_scale": {"type": "number", "required": True, "min": 1, "max": 500000000},
       "maximum_scale": {"type": "number", "required": True, "min": 1, "max": 500000000}}),
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
# The catalogue, which is the one question here that names no table: "which of
# my tables has no spatial index" is about the set, so asking it one table at a
# time is not a narrower version of it. It therefore takes no identifier from
# the caller at all - the narrowest surface in this domain rather than the
# widest, despite reading the whole database.
_c("postgis.diagnose@1", "postgis", "Read a PostGIS connection's catalogue: which tables have no spatial index, "
   "which declare no SRID, which were never analysed, and how large each one is.",
   params={"connection_id": {"type": "string", "required": True}},
   execution=EXEC_POSTGIS, produces=("analysis",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))
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

# -- Survey computation: the numbers a surveyor types -----------------------
#
# Every one of these answers a question about coordinates in the REQUEST rather
# than about a layer, so none of them needs a project or a network and all of
# them work with the laptop offline in a field hut. Until this module existed
# the desktop had to ask the server for a bearing.
#
# The ellipsoid travels with every one of them, and an unrecognised name is
# refused rather than defaulted: the same two coordinates on WGS84 and on ED50
# are different places on the ground, and an answer that does not say which
# surface it was computed on is not a survey answer.
_ELLIPSOID = {"ellipsoid": {"type": "string"}}
_POINT_PAIR = {
    "from_lat": {"type": "number", "required": True, "min": -90, "max": 90},
    "from_lon": {"type": "number", "required": True, "min": -180, "max": 180},
}

# One position, written every way somebody else's system asks for it. The
# precision is the caller's: an MGRS reference truncated to three digits is a
# 100 m square, and handing back five when three were asked for claims a
# precision the request did not.
# Which paths exist between two datums and how good each one is. A
# reprojection between datums is not one operation with one answer: PROJ
# usually knows several, they differ by metres, and which one runs depends on
# whether a grid file is installed.
# Contradictions only: a file whose declared system cannot hold the numbers in
# it is wrong whatever anyone intended, and saying so needs no external
# knowledge. Guessing what the system SHOULD be needs a hint, and without one a
# ranked list of candidates is a list of coincidences.
# A GPS height and a levelled height are different numbers, and the difference
# is the geoid separation - thirty to forty metres around the Mediterranean.
# The conversion REFUSES when the grid is not installed rather than returning
# the input unchanged, which is what PROJ does on its own and reports as
# success.
_c("crs.geoid_height@1", "survey", "Convert between an ellipsoidal height and an orthometric one.",
   params={"lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "height_m": {"type": "number", "required": True, "min": -500, "max": 20000},
           "direction": {"type": "string",
                         "enum": ["ellipsoidal_to_orthometric", "orthometric_to_ellipsoidal"],
                         "default": "ellipsoidal_to_orthometric"},
           "model": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("crs.diagnose@1", "survey", "Check whether a layer's declared reference system can hold its coordinates.",
   params=dict(_LAYER),
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("crs.transformations@1", "survey", "List the datum transformations between two systems, with their accuracies.",
   params={"source_crs": {"type": "string", "required": True},
           "target_crs": {"type": "string", "required": True}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("coordinate.write@1", "survey", "Write a position as DMS, UTM and MGRS.",
   params={"lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "mgrs_digits": {"type": "integer", "min": 0, "max": 5, "default": 5},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# Reading is where the refusal lives: a coordinate with no sign and no
# hemisphere is a position AND its mirror image.
_c("coordinate.read@1", "survey", "Read a coordinate written in degrees, minutes and seconds.",
   params={"latitude": {"type": "string", "required": True},
           "longitude": {"type": "string", "required": True}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# The two factors are reported apart because they are different problems: a
# discrepancy at a zone edge is fixed by a projection and one on a mountain by
# a height.
_c("survey.scale_factor@1", "survey", "Grid scale, elevation factor and their combination at a point.",
   params={"lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "height_m": {"type": "number", "min": -500, "max": 20000, "default": 0},
           "height_reference": {"type": "string",
                                "enum": ["orthometric", "ellipsoidal"],
                                "default": "orthometric"},
           "geoid_separation_m": {"type": "number", "min": -200, "max": 200, "default": 0},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("survey.inverse@1", "survey", "Bearing and distance between two coordinates, on a named ellipsoid.",
   params=dict(_POINT_PAIR, **_ELLIPSOID, **{
       "to_lat": {"type": "number", "required": True, "min": -90, "max": 90},
       "to_lon": {"type": "number", "required": True, "min": -180, "max": 180}}),
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("survey.forward@1", "survey", "The point at a bearing and distance from a coordinate: setting out.",
   params=dict(_POINT_PAIR, **_ELLIPSOID, **{
       "azimuth_deg": {"type": "number", "required": True, "min": -360, "max": 360},
       "distance_m": {"type": "number", "required": True, "min": 0, "max": 20_000_000}}),
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# `closed` is the whole difference between a traverse that can report a
# misclosure and one that cannot: an open traverse has nothing to close onto.
_c("survey.traverse@1", "survey", "Walk a traverse and report its misclosure and precision ratio.",
   params=dict(_POINT_PAIR, **_ELLIPSOID, **{
       "legs": {"type": "legs", "required": True},
       "closed": {"type": "boolean", "default": False}}),
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("survey.intersect@1", "survey", "Fix a point from two bearings, on the ellipsoid.",
   params={"first_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "first_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "first_azimuth_deg": {"type": "number", "required": True, "min": -360, "max": 360},
           "second_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "second_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "second_azimuth_deg": {"type": "number", "required": True, "min": -360, "max": 360},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# Station and offset. A point past the end of the line is reported as past the
# end rather than clamped to the endpoint: it is a real measurement and the
# surveyor needs to know where it actually fell.
# A taped fix has TWO answers and both come back with the side of the baseline
# they fall on. Returning one would be choosing for the surveyor, and only they
# know which side of the line they were standing on.
_c("survey.trilaterate@1", "survey", "Fix a point from two taped distances; both answers are returned.",
   params={"first_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "first_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "first_distance_m": {"type": "number", "required": True, "min": 0, "max": 1_000_000},
           "second_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "second_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "second_distance_m": {"type": "number", "required": True, "min": 0, "max": 1_000_000},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# Resection carries its danger-circle margin with the answer. On that circle
# every station observes the same two angles, so the observations name no
# single place - and the margin is what says how close to that the fix is.
_c("survey.resection@1", "survey", "Fix the instrument's own position from angles to three known points.",
   params={"first_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "first_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "second_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "second_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "third_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "third_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "angle_first_second_deg": {"type": "number", "required": True, "min": 0, "max": 180},
           "angle_second_third_deg": {"type": "number", "required": True, "min": 0, "max": 180},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("survey.station_offset@1", "survey", "How far along a line a point sits, and how far off it.",
   params={"start_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "start_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "end_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "end_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "point_lat": {"type": "number", "required": True, "min": -90, "max": 90},
           "point_lon": {"type": "number", "required": True, "min": -180, "max": 180},
           "ellipsoid": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

_c("survey.closure@1", "survey", "Close a deed description and report its area and misclosure.",
   params=dict(_ELLIPSOID, **{"legs": {"type": "legs", "required": True}}),
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("analysis",), reversible=True,
   clients=(CLIENT_QGIS,))

# -- QGIS Processing: consequential, confirmation required -----------------
_c("processing.run@1", "processing", "Run an installed QGIS Processing algorithm and load its output.",
   params={"operation": {"type": "string", "required": True},
           "layer_id": {"type": "string", "required": True},
           "other_layer_id": {"type": "string"},
           "distance": {"type": "number", "min": 0, "max": 1_000_000},
           "unit": {"type": "string", "enum": ["m", "km", "ft", "mi"], "default": "m"},
           "segments": {"type": "integer", "min": 1, "max": 96},
           "predicate": {"type": "string",
                         "enum": ["intersects", "within", "contains", "overlaps", "touches", "crosses", "disjoint"]},
           # Which attribute a dissolve groups by. Without it the operation
           # merges the whole layer into one shape, which is a different
           # request from the one that named a field.
           "field": {"type": "string"},
           # Where a reprojection is going. Required for it in practice:
           # `safe_processing_params` refuses a reproject without one,
           # because the default was the layer's OWN system and made the
           # operation a no-op that reported success.
           "target_crs": {"type": "string"}},
   risk=RISK_CONSEQUENTIAL, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=False)
# -- Named geoprocessing: one capability per operation ----------------------
#
# These ten were reachable only through `processing.run@1`, the generic bridge,
# which is risk-classified consequential and therefore asks before every single
# run. That is right for a bridge that can reach any allowlisted algorithm and
# wrong for the operations themselves: each of these READS one or two layers and
# writes a NEW one, changing nothing that already exists. Removing a layer you
# did not want is one click, so a confirmation buys nothing and costs a step
# every time.
#
# Naming them also lets each declare the parameters it actually takes instead of
# the union of all of them. `processing.run@1` accepts distance, segments and
# predicate whatever the operation is, so a request could name a buffer distance
# for a centroid and be accepted.
#
# `operation` is deliberately absent from every one of these: the capability id
# IS the operation. A named capability that still takes an operation name is the
# generic bridge wearing ten hats.
_LAYER_IN = {"layer_id": {"type": "string", "required": True}}
_OTHER_LAYER = {"other_layer_id": {"type": "string", "required": True}}

_c("geoprocessing.buffer@1", "geoprocessing", "Grow or shrink features by a distance, as a new layer.",
   params=dict(_LAYER_IN, **{
       "distance": {"type": "number", "required": True, "min": 0, "max": 1_000_000},
       "unit": {"type": "string", "enum": ["m", "km", "ft", "mi"], "default": "m"},
       "segments": {"type": "integer", "min": 1, "max": 96}}),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.clip@1", "geoprocessing", "Keep only the parts of one layer that fall inside another.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.intersection@1", "geoprocessing", "Keep the overlapping parts of two layers, with both sets of attributes.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.union@1", "geoprocessing", "Combine two layers, splitting them where they overlap.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.difference@1", "geoprocessing", "Remove from one layer everything the other covers.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# The field is optional and its absence is a real request rather than a missing
# argument: no field merges the whole layer into one shape.
_c("geoprocessing.dissolve@1", "geoprocessing", "Merge features into one shape per value of a field, or into a single shape.",
   params=dict(_LAYER_IN, **{"field": {"type": "string"}}),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# -- Terrain -----------------------------------------------------------------
#
# The five core-QGIS surface measurements. `targets=("raster",)` because every
# one of them reads an elevation grid; offering them for a vector layer would
# produce an algorithm failure where a refusal belongs.
#
# Flow accumulation, watershed and viewshed are deliberately NOT here. They live
# in the GRASS and SAGA providers, which a given install may not have, and a
# capability that usually cannot resolve is worse than one that is honestly
# absent - the user is told the action exists and then it does not run.
_TERRAIN_IN = {
    "layer_id": {"type": "string"},
    "z_factor": {"type": "number", "min": 0.000001, "max": 1000000},
    "band": {"type": "integer", "min": 1, "max": 512},
}

_c("terrain.slope@1", "terrain", "How steep the ground is, in degrees, from an elevation surface.",
   params=dict(_TERRAIN_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

_c("terrain.aspect@1", "terrain", "Which way the ground faces, in degrees clockwise from north.",
   params=dict(_TERRAIN_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

_c("terrain.hillshade@1", "terrain", "A shaded-relief image of the surface, lit from one direction.",
   params=dict(_TERRAIN_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# Roughness and ruggedness are elevation differences alone, so unlike the three
# above they carry no ratio and a grid measured in degrees does not corrupt them.
_c("terrain.ruggedness@1", "terrain", "How broken the ground is: the mean difference from the eight neighbouring cells.",
   params=dict(_TERRAIN_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

_c("terrain.roughness@1", "terrain", "The range of elevation within each cell's neighbourhood.",
   params=dict(_TERRAIN_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

# The one terrain product that leaves as vector data: a layer of lines a person
# can style, label and export, rather than a surface they look at.
_c("terrain.contours@1", "terrain", "Trace contour lines from an elevation surface at a stated height interval.",
   params={"layer_id": {"type": "string"},
           "interval": {"type": "number", "required": True, "min": 0.000001, "max": 1000000},
           "base": {"type": "number"},
           "band": {"type": "integer", "min": 1, "max": 512}},
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("raster",), clients=(CLIENT_QGIS, CLIENT_WORKSPACE))

_c("geoprocessing.merge@1", "geoprocessing", "Stack two layers into one without changing their shapes.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.centroid@1", "geoprocessing", "Reduce each feature to a single point.",
   params=dict(_LAYER_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.convex_hull@1", "geoprocessing", "Wrap the layer in the smallest shape that contains all of it.",
   params=dict(_LAYER_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# The destination is required, and that is the correction this capability
# carries: the bridge used to fill TARGET_CRS with the layer's own system, so a
# reprojection produced a duplicate in the system it started in.
_c("geoprocessing.reproject@1", "geoprocessing", "Rewrite a layer in a different coordinate reference system.",
   params=dict(_LAYER_IN, **{"target_crs": {"type": "string", "required": True}}),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.spatial_join@1", "geoprocessing", "Attach one layer's attributes to another by where the features sit.",
   params=dict(_LAYER_IN, **_OTHER_LAYER, **{
       "predicate": {"type": "string",
                     "enum": ["intersects", "within", "contains", "overlaps", "touches", "crosses"]}}),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# The tolerance is the request: without it the algorithm's own default decides
# how much detail a customer's boundary loses.
_c("geoprocessing.simplify@1", "geoprocessing", "Reduce the number of vertices, keeping the shape within a tolerance.",
   params=dict(_LAYER_IN, **{
       "tolerance": {"type": "number", "required": True, "min": 0, "max": 1_000_000}}),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# Safe because it writes a NEW layer. The amber risk class the server applies to
# repair is about strategies that DELETE features; fixing geometry in a copy
# removes nothing from the original.
_c("geoprocessing.repair@1", "geoprocessing", "Fix invalid geometry into a new layer, leaving the original alone.",
   params=dict(_LAYER_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

# Checking is not repairing, and the two are different requests: this one
# changes nothing and hands back the features that FAILED, which is what a
# reviewer needs. `geoprocessing.repair@1` is the other half.
_c("geoprocessing.validate@1", "geoprocessing", "Check geometry validity and hand back the features that fail.",
   params=dict(_LAYER_IN),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.split@1", "geoprocessing", "Cut every feature along the lines of another layer.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector",), clients=(CLIENT_QGIS,))

_c("geoprocessing.zonal_statistics@1", "geoprocessing", "Summarize a raster's values inside each polygon of a layer.",
   params=dict(_LAYER_IN, **_OTHER_LAYER),
   risk=RISK_SAFE, execution=EXEC_QGIS_PROCESSING, produces=("layer",), reversible=True,
   targets=("vector", "raster"), clients=(CLIENT_QGIS,))

_c("processing.discover@1", "processing", "List the installed Processing algorithms that fit an objective.",
   params={"objective": {"type": "string", "required": True}},
   execution=EXEC_LOCAL, produces=("catalog",))

# -- the host application's server-side work -------------------------------
#
# Deliberately empty. A capability that runs on one vendor's server belongs to
# that vendor, not to this package, so a host registers its own with
# `register(Capability(..., execution=EXEC_REMOTE_SERVICE))`. Everything the
# agent needs then follows from the registry: the catalogue advertises them,
# `offline_capability_ids` excludes them, and a session without an account
# cannot reach them even if a model names one.

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
# `target_format` is the server's name on data:convert@1. Consistency across the
# two surfaces beats the shorter word: the vocabulary exists to be learned once.
_c("export.layer@1", "export", "Write a layer to a file in a standard GIS format.",
   params={"layer_id": {"type": "string", "required": True},
           "target_format": {"type": "string",
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
# is parsed by nivo.expressions, a grammar that can only express
# arithmetic over the layer's own fields. That grammar, not a prompt
# instruction, is what stops a calculation reaching outside the row.
# A field point list becoming a layer. The parameter names are the server's
# `data:field_points@1` vocabulary so one request is one request on both
# surfaces.
#
# `crs` is required and nothing about it is inferred. The numbers in a point
# list cannot say which projected system they are in, and a provider told the
# wrong one places a survey in the Gulf of Guinea without complaining.
_c("field.import_points@1", "field", "Read a coordinate list into a layer, with an explicit reference system.",
   params={"path": {"type": "string", "required": True},
           "crs": {"type": "string", "required": True},
           "easting_field": {"type": "string", "required": True},
           "northing_field": {"type": "string", "required": True},
           "elevation_field": {"type": "string"},
           "name": {"type": "string"},
           "delimiter": {"type": "string"}},
   risk=RISK_SAFE, execution=EXEC_LOCAL, produces=("layer",), reversible=True,
   clients=(CLIENT_QGIS,))
_c("field.calculate@1", "field", "Add a field computed from the layer's existing fields.",
   params={"layer_id": {"type": "string", "required": True},
           "field": {"type": "string", "required": True},
           "expression": {"type": "string", "required": True},
           "field_type": {"type": "string", "enum": ["number", "integer", "text"], "default": "number"}},
   targets=("vector",), risk=RISK_CONSEQUENTIAL, execution=EXEC_LOCAL,
   produces=("layer_change",), reversible=False, previewable=True)
# The layer tree IS the legend in QGIS, so this expands a layer's classes
# rather than opening a panel. It hands the entries back as well as setting the
# state, because "what do these colours mean" is the question underneath.
_c("map.legend@1", "map", "Show or hide a layer's classes in the legend, and read them back.",
   params=dict(_LAYER, **{"visible": {"type": "boolean", "default": True}}),
   execution=EXEC_CLIENT_UI, reversible=True, produces=("map_effect", "analysis"),
   clients=(CLIENT_QGIS,))
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


# What the agent calls the surface it is running in, when it introduces itself to
# the model. A host registers its own name rather than this package hard-coding
# one; the prompt substitutes it for {{CLIENT_NAME}}.
_CLIENT_DISPLAY_NAMES: dict[str, str] = {
    CLIENT_QGIS: "QGIS",
    CLIENT_WORKSPACE: "the workspace",
}


def client_display_name(client: str) -> str:
    """The human name for a client, defaulting to the client id itself."""
    return _CLIENT_DISPLAY_NAMES.get(str(client or ""), str(client or ""))


def set_client_display_name(client: str, name: str) -> None:
    """Name a client for the prompt. Hosts call this once at start-up."""
    _CLIENT_DISPLAY_NAMES[str(client)] = str(name)


def executable_domains() -> Iterable[str]:
    return sorted({capability.domain for capability in all_capabilities()})


def bind_executor(identifier: str, executor: Callable[..., Any]) -> None:
    """Attach a runtime executor to a registered capability."""
    capability = get(identifier)
    if capability is None:
        raise CapabilityError("cannot bind an executor to an unknown capability")
    setattr(capability, "executor", executor)
