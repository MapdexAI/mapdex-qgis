"""Safe Nivo companion payloads and closed desktop action handling.

This module deliberately has no QGIS imports.  Keeping the admission and
allowlist logic pure makes it testable on QGIS 3 and 4, and prevents a model
response from becoming executable Python by accident.
"""
from __future__ import annotations

from typing import Any

from .processing import safe_processing_params

COMPANION_VERSION = "companion.qgis.v1"
MAX_FIELDS = 64
MAX_TEXT = 256

TURN_STATES = frozenset({
    "idle", "composing", "clarification_required", "action_ready",
    "confirmation_required", "executing", "cancelling", "completed", "failed",
})


def transition(state: str, event: str) -> str:
    """Closed Nivo turn state machine; stale/unknown events fail closed."""
    table = {
        ("idle", "send"): "composing", ("composing", "clarify"): "clarification_required",
        ("composing", "action"): "action_ready", ("composing", "confirm"): "confirmation_required",
        ("composing", "error"): "failed", ("action_ready", "execute"): "executing",
        ("confirmation_required", "apply"): "executing", ("executing", "cancel"): "cancelling",
        ("executing", "done"): "completed", ("executing", "error"): "failed",
        ("cancelling", "done"): "completed", ("clarification_required", "send"): "composing",
        ("completed", "send"): "composing", ("failed", "send"): "composing",
    }
    return table.get((state, event), state if state in TURN_STATES else "idle")

# These names are product contracts, not model suggestions.  The plugin maps
# them to a small set of native QGIS UI operations after compose returns.
ALLOWED_ACTIONS = frozenset({
    "qgis:zoom_to_layer@1",
    "qgis:zoom_to_selection@1",
    "qgis:zoom_to_extent@1",
    "qgis:set_layer_visibility@1",
    "qgis:preview_filter@1",
    "qgis:semantic_style@1",
    "qgis:open_attribute_table@1",
    "qgis:open_processing@1",
    "qgis:open_review@1",
    "qgis:open_results@1",
    "qgis:inspect_layer@1",
    "qgis:refresh_canvas@1",
    "qgis:previous_extent@1",
    "qgis:next_extent@1",
    "qgis:select_all@1",
    "qgis:clear_selection@1",
    "qgis:invert_selection@1",
    "qgis:set_layer_opacity@1",
    "qgis:processing_operation@1",
    "qgis:add_xyz_basemap@1",
    "qgis:create_layer@1",
})

# What this build can actually carry out. ALLOWED_ACTIONS above is the protocol
# allowlist - the shapes we are willing to parse - which is deliberately wider
# during development. Capability negotiation must advertise only what the
# dispatcher really implements: telling the server we support an action and then
# doing nothing produces "Nivo prepared a QGIS action" followed by a map that
# never changes, which reads as a broken assistant rather than a missing feature.
# test_action_parity.py fails if this drifts from the dispatcher.
IMPLEMENTED_ACTIONS = frozenset({
    "qgis:zoom_to_layer@1",
    "qgis:zoom_to_selection@1",
    "qgis:zoom_to_extent@1",
    "qgis:set_layer_visibility@1",
    "qgis:set_layer_opacity@1",
    "qgis:open_attribute_table@1",
    "qgis:open_processing@1",
    "qgis:inspect_layer@1",
    "qgis:refresh_canvas@1",
    "qgis:previous_extent@1",
    "qgis:next_extent@1",
    "qgis:select_all@1",
    "qgis:clear_selection@1",
    "qgis:invert_selection@1",
    "qgis:add_xyz_basemap@1",
    "qgis:create_layer@1",
    # Runs through the confirmation path rather than the direct dispatcher.
    "qgis:processing_operation@1",
})

TARGETED_ACTIONS = frozenset({
    "qgis:zoom_to_layer@1", "qgis:set_layer_visibility@1",
    "qgis:preview_filter@1", "qgis:semantic_style@1",
    "qgis:open_attribute_table@1", "qgis:inspect_layer@1",
    "qgis:select_all@1", "qgis:clear_selection@1", "qgis:invert_selection@1",
    "qgis:set_layer_opacity@1",
    "qgis:processing_operation@1",
})

SAFE_PARAM_KEYS = {
    "qgis:zoom_to_extent@1": frozenset({"bbox", "crs", "center", "zoom"}),
    "qgis:set_layer_visibility@1": frozenset({"visible"}),
    "qgis:set_layer_opacity@1": frozenset({"opacity"}),
    "qgis:semantic_style@1": frozenset({"renderer", "field", "classes", "label_field", "labels"}),
    "qgis:processing_operation@1": frozenset({"operation", "distance", "segments", "predicate", "target_layer", "input_layer"}),
    "qgis:add_xyz_basemap@1": frozenset({"provider"}),
    "qgis:create_layer@1": frozenset({"geometry", "crs", "name"}),
}


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def companion_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Serialize a bounded QGIS summary; never copy attributes or credentials."""
    active = snapshot.get("active_layer") if isinstance(snapshot.get("active_layer"), dict) else {}
    fields = active.get("fields") if isinstance(active.get("fields"), list) else []
    layer = {
        "id": _text(active.get("id"), 128),
        "name": _text(active.get("name")),
        "kind": _text(active.get("kind"), 32),
        "crs": _text(active.get("crs"), 128),
        "feature_count": max(0, int(active.get("feature_count") or 0)),
        "geometry_type": _text(active.get("geometry_type"), 64),
        "fields": [_text(field, 128) for field in fields[:MAX_FIELDS] if _text(field, 128)],
    }
    bbox = snapshot.get("bbox")
    viewport = {"bbox": list(bbox)} if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
    context: dict[str, Any] = {
        "version": COMPANION_VERSION,
        "client": "qgis",
        "crs": _text(snapshot.get("crs"), 128),
        "selection_count": max(0, int(snapshot.get("selection_count") or 0)),
        "visible_layer_count": max(0, int(snapshot.get("visible_layer_count") or 0)),
        "active_layer": layer if layer["id"] or layer["name"] else None,
        "connections": [{"id": _text(item.get("id"), 128), "name": _text(item.get("name"))}
                        for item in (snapshot.get("connections") or [])[:16]
                        if isinstance(item, dict) and _text(item.get("id"), 128)],
    }
    # Advertise only what this build can carry out, so the server never chooses
    # an action that would silently do nothing on this desktop.
    context["supported_action_kinds"] = sorted(IMPLEMENTED_ACTIONS)
    context["qgis_version"] = _text(snapshot.get("qgis_version"), 64)
    context["plugin_version"] = _text(snapshot.get("plugin_version"), 64)
    if viewport:
        context["viewport"] = viewport
    return {key: value for key, value in context.items() if value not in (None, "", [], {})}


def allowed_actions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only schema-shaped allowlisted companion actions from compose."""
    raw = response.get("companion_actions") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []
    result = []
    for action in raw:
        if not isinstance(action, dict) or action.get("kind") not in ALLOWED_ACTIONS:
            continue
        if action.get("requires_confirmation") is True:
            continue
        params = action.get("params")
        if not isinstance(params, dict):
            params = {}
        allowed = SAFE_PARAM_KEYS.get(action["kind"])
        if allowed is not None and any(key not in allowed for key in params):
            continue
        if action["kind"] == "qgis:semantic_style@1":
            renderer = params.get("renderer")
            if renderer not in {"single", "categorized", "graduated", "labels"}:
                continue
            if renderer in {"categorized", "graduated", "labels"} and not _text(params.get("field") or params.get("label_field"), 128):
                continue
        if action["kind"] == "qgis:add_xyz_basemap@1" and params.get("provider") != "osm":
            continue
        target = _text(action.get("target"), 128)
        if action["kind"] in TARGETED_ACTIONS and not target:
            continue
        # These fields are opaque server-issued identifiers. Never accept a model
        # supplied SQL, Python, path or confirmation executable payload.
        action_id = _text(action.get("action_id"), 128)
        if not action_id:
            continue
        result.append({"id": action_id, "tool": action["kind"], "params": params, "summary": _text(action.get("summary")), "undo": bool(action.get("undo")), "undo_token": _text(action.get("undo_token"), 128), "target": target, "correlation_id": _text(action.get("correlation_id"), 128)})
    return result


def confirmation_actions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return confirmation-required actions that are safe to present and apply."""
    raw = response.get("companion_actions") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return []
    result = []
    for action in raw:
        if not isinstance(action, dict) or action.get("kind") != "qgis:processing_operation@1":
            continue
        if action.get("requires_confirmation") is not True:
            continue
        target = _text(action.get("target"), 128)
        if not target:
            continue
        params = action.get("params")
        if not isinstance(params, dict):
            continue
        if any(key not in SAFE_PARAM_KEYS["qgis:processing_operation@1"] for key in params):
            continue
        safe_params = safe_processing_params(params)
        if not safe_params:
            continue
        action_id = _text(action.get("action_id"), 128)
        confirmation_id = _text(action.get("confirmation_id"), 128)
        idempotency_key = _text(action.get("idempotency_key"), 128)
        if not action_id or not confirmation_id or not idempotency_key:
            continue
        result.append({
            "id": action_id,
            "confirmation_id": confirmation_id,
            "idempotency_key": idempotency_key,
            "tool": action["kind"],
            "target": target,
            "params": safe_params,
            "summary": _text(action.get("summary")),
            "correlation_id": _text(action.get("correlation_id"), 128),
            "undo": bool(action.get("undo")),
            "undo_token": _text(action.get("undo_token"), 128),
        })
    return result
