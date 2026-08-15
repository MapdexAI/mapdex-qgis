"""Safe Nivo companion payloads and closed desktop action handling.

This module deliberately has no QGIS imports.  Keeping the admission and
allowlist logic pure makes it testable on QGIS 3 and 4, and prevents a model
response from becoming executable Python by accident.
"""
from __future__ import annotations

from typing import Any

COMPANION_VERSION = "companion.qgis.v1"
MAX_FIELDS = 64
MAX_TEXT = 256

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
})

TARGETED_ACTIONS = frozenset({
    "qgis:zoom_to_layer@1", "qgis:set_layer_visibility@1",
    "qgis:preview_filter@1", "qgis:semantic_style@1",
    "qgis:open_attribute_table@1", "qgis:inspect_layer@1",
})


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
        "active_layer": layer if layer["name"] else None,
        "connections": [{"id": _text(item.get("id"), 128), "name": _text(item.get("name"))}
                        for item in (snapshot.get("connections") or [])[:16]
                        if isinstance(item, dict) and _text(item.get("id"), 128)],
    }
    context["supported_action_kinds"] = sorted(ALLOWED_ACTIONS)
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
        target = _text(action.get("target"), 128)
        if action["kind"] in TARGETED_ACTIONS and not target:
            continue
        # These fields are opaque server-issued identifiers. Never accept a model
        # supplied SQL, Python, path or confirmation executable payload.
        result.append({"tool": action["kind"], "params": params, "summary": _text(action.get("summary")), "undo": bool(action.get("undo")), "undo_token": _text(action.get("undo_token"), 128), "target": target, "correlation_id": _text(action.get("correlation_id"), 128)})
    return result
