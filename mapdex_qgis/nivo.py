"""Safe Nivo companion payloads and closed desktop action handling.

This module deliberately has no QGIS imports.  Keeping the admission and
allowlist logic pure makes it testable on QGIS 3 and 4, and prevents a model
response from becoming executable Python by accident.
"""
from __future__ import annotations

from datetime import datetime
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
    "qgis:add_features@1",
})

# What this build can actually carry out. ALLOWED_ACTIONS above is the protocol
# allowlist - the shapes we are willing to parse - which is deliberately wider
# during development. Capability negotiation must advertise only what the
# dispatcher really implements: telling the server we support an action and then
# doing nothing produces "Nivo prepared a QGIS action" followed by a map that
# never changes, which reads as a broken assistant rather than a missing feature.
# test_action_parity.py fails if this drifts from the dispatcher.
#
# The canonical half is derived rather than listed. Every capability with a
# bound executor is advertised, so adding one to the runtime table makes it
# reachable without anyone remembering to edit this file. The `qgis:*` entries
# below stay hand-written because they are the legacy vocabulary: a fixed,
# closed set that will only ever shrink.
LEGACY_ACTIONS = frozenset({
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
    "qgis:add_features@1",
    # Runs through the confirmation path rather than the direct dispatcher.
    "qgis:processing_operation@1",
})


def _bound_capabilities() -> frozenset:
    """Capabilities the runtime implements, or nothing if it cannot be read.

    Imported lazily and defensively: capability negotiation happening at all
    matters more than it being complete, and a plugin that fails to advertise
    is degraded while a plugin that fails to load is broken.
    """
    try:
        from .qgis_runtime import bound_capability_ids

        return bound_capability_ids()
    except Exception:  # noqa: BLE001 - advertisement must never break the plugin
        return frozenset()


IMPLEMENTED_ACTIONS = LEGACY_ACTIONS | _bound_capabilities()

# The protocol allowlist has to admit the canonical vocabulary too, or the
# parser rejects the very actions the negotiation just advertised.
ALLOWED_ACTIONS = ALLOWED_ACTIONS | _bound_capabilities()

TARGETED_ACTIONS = frozenset({
    "qgis:zoom_to_layer@1", "qgis:set_layer_visibility@1",
    "qgis:preview_filter@1", "qgis:semantic_style@1",
    "qgis:open_attribute_table@1", "qgis:inspect_layer@1",
    "qgis:select_all@1", "qgis:clear_selection@1", "qgis:invert_selection@1",
    "qgis:set_layer_opacity@1",
    "qgis:processing_operation@1",
})

# The geometry choice offered when a new-layer request did not state one.
# Presenting it as a picker rather than a chat question matters: a question
# asked in the transcript has nowhere to go, because the user's reply starts a
# fresh turn in which "polygon" is a bare word with no intent attached.
GEOMETRY_CHOICES = (
    ("Point", "point"),
    ("Line", "linestring"),
    ("Polygon", "polygon"),
)


def geometry_from_choice(label: str) -> str:
    """Map a picker label to its OGC type, or "" when nothing was chosen."""
    for choice, geometry in GEOMETRY_CHOICES:
        if str(label or "").strip().lower() == choice.lower():
            return geometry
    return ""


SAFE_PARAM_KEYS = {
    "qgis:zoom_to_extent@1": frozenset({"bbox", "crs", "center", "zoom"}),
    "qgis:set_layer_visibility@1": frozenset({"visible"}),
    "qgis:set_layer_opacity@1": frozenset({"opacity"}),
    "qgis:semantic_style@1": frozenset({"renderer", "field", "classes", "label_field", "labels"}),
    "qgis:processing_operation@1": frozenset(
        {"operation", "distance", "segments", "predicate", "target_layer", "input_layer"}
    ),
    "qgis:add_xyz_basemap@1": frozenset({"provider"}),
    "qgis:create_layer@1": frozenset({"geometry", "crs", "name"}),
    "qgis:add_features@1": frozenset(
        {"geometry", "count", "coordinates", "bbox", "center", "crs", "area", "place"}
    ),
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
            styling_field = _text(params.get("field") or params.get("label_field"), 128)
            if renderer in {"categorized", "graduated", "labels"} and not styling_field:
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
        result.append({
            "id": action_id,
            "tool": action["kind"],
            "params": params,
            "summary": _text(action.get("summary")),
            "undo": bool(action.get("undo")),
            "undo_token": _text(action.get("undo_token"), 128),
            "target": target,
            "correlation_id": _text(action.get("correlation_id"), 128),
        })
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


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------
#
# `POST /v1/compose` has always accepted a `thread_id`, and with one it replays
# the conversation and carries the earlier source reference forward. The plugin
# sent none, so every QGIS turn arrived with no memory of the last one and
# nothing was ever stored: there was no history to open because none was kept.
#
# These helpers are the pure half of that: which remembered id is still usable,
# what the server's rows mean, and how a stored message becomes a transcript
# turn. They are here rather than in plugin.py because plugin.py imports `qgis`
# at module scope and cannot be imported in CI.

THREAD_ID_SETTING = "mapdex/nivo/thread_id"
THREAD_PROJECT_SETTING = "mapdex/nivo/thread_project_id"

MAX_THREAD_TITLE = 60
DEFAULT_THREAD_TITLE = "QGIS conversation"


def thread_title(message: str) -> str:
    """A History title taken from the user's own first words.

    Mechanical: whitespace collapsed, then truncated. Nothing here inspects
    what the words mean, in any language - the server's own fallback title is
    a timestamp, which tells a reader nothing about which conversation this was.
    """
    text = " ".join(str(message or "").split())
    if not text:
        return DEFAULT_THREAD_TITLE
    if len(text) <= MAX_THREAD_TITLE:
        return text
    return text[: MAX_THREAD_TITLE - 1].rstrip() + "…"


def thread_is_gone(status: Any) -> bool:
    """True when the API says the conversation we named does not exist.

    `prepareCompose` answers 404 for an unknown or stale thread id and for
    nothing else on that path, so this is the signal to open a fresh
    conversation rather than to show the user an error about a thread they
    never knew they had.
    """
    try:
        return int(status) == 404
    except (TypeError, ValueError):
        return False


def remembered_thread(stored_id: Any, stored_project: Any, active_project: Any) -> str:
    """The stored thread id, but only for the project it was opened in.

    Threads are project-scoped on the server. Carrying one into another project
    would ask the API to continue a conversation that project cannot see, so a
    mismatch (or a stored id with no recorded project) simply starts fresh.
    """
    thread_id = str(stored_id or "").strip()
    project = str(stored_project or "").strip()
    active = str(active_project or "").strip()
    if not thread_id or not project or not active or project != active:
        return ""
    return thread_id


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """The list in an API payload, whether bare or wrapped in an envelope."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys + ("items", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _optional_count(value: Any):
    """A non-negative integer, or None when the payload did not carry one.

    None and 0 are different claims: "the server does not report this" must not
    render as "this conversation has no messages".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def thread_list_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize `GET /v1/threads` into rows the History dialog can render.

    Server order is preserved. The API already returns newest-updated first,
    and re-sorting RFC 3339 strings that may carry different UTC offsets would
    reorder them wrongly rather than defensively.
    """
    rows = []
    for item in _rows(payload, "threads"):
        thread_id = _text(item.get("id"), 128)
        if not thread_id:
            continue
        rows.append({
            "id": thread_id,
            "title": _text(item.get("title")) or DEFAULT_THREAD_TITLE,
            "updated_at": _text(item.get("updated_at"), 64),
            "created_at": _text(item.get("created_at"), 64),
            "message_count": _optional_count(item.get("message_count")),
        })
    return rows


def thread_turns(payload: Any) -> list[tuple[str, str]]:
    """Stored messages as the transcript's own (sender, text) pairs.

    Only user and assistant turns, and only their text. Tool calls, references
    and model metadata are record-keeping, not conversation. The text stays a
    plain string and is never marked up: the transcript is native Qt widgets on
    purpose, because assistant text is data.
    """
    turns = []
    for item in _rows(payload, "messages"):
        role = _text(item.get("role"), 32).lower()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        turns.append((role, content))
    return turns


def format_timestamp(value: Any) -> str:
    """`2026-08-17 14:32` in local time, or "" when the value cannot be read.

    An unreadable timestamp yields nothing rather than the raw string: a row
    reading "Parcel areas - 2026-08-17T11:32:04Z" is worse than one that simply
    does not say when.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    text = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
    try:
        moment = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return ""
    if moment.tzinfo is not None:
        try:
            moment = moment.astimezone()
        except (ValueError, OSError, OverflowError):
            pass
    return moment.strftime("%Y-%m-%d %H:%M")


def describe_thread(item: dict[str, Any]) -> str:
    """One History row: what it was about, when, and how long it ran."""
    parts = [str((item or {}).get("title") or DEFAULT_THREAD_TITLE)]
    when = format_timestamp((item or {}).get("updated_at") or (item or {}).get("created_at"))
    if when:
        parts.append(when)
    count = (item or {}).get("message_count")
    if isinstance(count, int) and count > 0:
        parts.append("1 message" if count == 1 else "{} messages".format(count))
    return " · ".join(parts)
