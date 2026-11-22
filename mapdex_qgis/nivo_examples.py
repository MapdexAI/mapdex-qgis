"""What the Nivo start screen offers to try, from the layer that is open.

The start screen used to be the opening reading: a card per finding with its
actions stacked under it. The approved design keeps the measured part and
changes the shape: a title, one sentence, the one finding that blocks work (as
a status line), and at most three starting points written as things a person
would actually ask about THIS layer.

Every starting point is one of the opening reading's own action kinds
(`first_look.LOCAL`, `ASK`, `MAPDEX`, or "capabilities"), so pressing one goes
through exactly the handler a finding's action did. Nothing here is a new
behaviour; it is the same offer, chosen and worded for the layer on screen.

No Qt and no QGIS: the input is the measured state `_first_look_state` builds.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import first_look

MAX_EXAMPLES = 3

START_TITLE = "Ask about your map"
START_BODY = "Nivo measures answers from your layers and asks before it changes anything."
START_LEAD = "Try with the active layer"
START_LEAD_NO_LAYER = "Try one of these"


def _ask(label: str, icon: str, prompt: str = "") -> dict[str, Any]:
    return {"label": label, "icon": icon, "kind": first_look.ASK, "prompt": prompt or label}


def _local(label: str, icon: str, capability: str) -> dict[str, Any]:
    return {"label": label, "icon": icon, "kind": first_look.LOCAL, "capability": capability}


def _mapdex(label: str, icon: str, workflow: str, promise: str = "") -> dict[str, Any]:
    return {"label": label, "icon": icon, "kind": first_look.MAPDEX, "workflow": workflow,
            "promise": promise}


def geometry_family(value: Any) -> str:
    """point, line or polygon from a geometry type name, or ""."""
    text = str(value or "").lower()
    if "polygon" in text:
        return "polygon"
    if "line" in text:
        return "line"
    if "point" in text:
        return "point"
    return ""


def examples(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Up to three starting points for the open layer, best first."""
    state = state or {}
    layer = state.get("layer") if isinstance(state.get("layer"), Mapping) else {}
    layer = layer or {}
    name = str(layer.get("name") or "")
    kind = str(layer.get("kind") or "")
    out: list[dict[str, Any]] = []

    if not layer:
        out = [
            _local("Add an OpenStreetMap basemap", "map", "map.basemap@1"),
            _mapdex("Place a scanned map on the map", "crosshair", "georeference"),
        ]
    elif kind == "raster":
        if first_look._is_service(layer):  # noqa: SLF001 - one definition of a backdrop
            out = [_mapdex("Place a scanned map on the map", "crosshair", "georeference")]
        elif not layer.get("georeferenced"):
            out = [
                _mapdex("Place {} on the map".format(name), "crosshair", "georeference",
                        "Mapdex reads the sheet's grid and coordinates and places it."),
                _ask("Why can't I measure an unplaced scan?", "circle-help",
                     "Why can't I use a raster that is not georeferenced?"),
            ]
        else:
            out = [
                _local("Zoom to {}".format(name), "zoom-in", "map.zoom_layer@1"),
                _mapdex("Extract parcels from {}".format(name), "layers-2", "digitize_parcels",
                        "Mapdex traces the parcel outlines into a vector layer for your review."),
            ]
    elif kind == "vector":
        family = geometry_family(layer.get("geometry"))
        if not str(layer.get("crs") or ""):
            out.append(_ask("Which CRS should {} be in?".format(name), "circle-help",
                            "This layer has no CRS. How do I work out which one it should be?"))
        if family == "polygon":
            out += [
                _ask("What is the total area of {}?".format(name), "ruler"),
                _ask("Color {} by area".format(name), "palette"),
                _local("Check {} for invalid geometry".format(name), "shield-check",
                       "geoprocessing.validate@1"),
            ]
        elif family == "line":
            out += [
                _ask("What is the total length of {}?".format(name), "ruler"),
                _ask("Buffer {} by 25 meters".format(name), "layers"),
                _local("Check {} for invalid geometry".format(name), "shield-check",
                       "geoprocessing.validate@1"),
            ]
        else:
            out += [
                _local("Profile the fields of {}".format(name), "table", "analytics.profile@1"),
                _ask("Color {} by a field".format(name), "palette",
                     "Color {} by its most useful field".format(name)),
                _local("Check {} for invalid geometry".format(name), "shield-check",
                       "geoprocessing.validate@1"),
            ]
    return out[:MAX_EXAMPLES]


def blocking_finding(state: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The one finding worth a status line above the examples, or None.

    Only a blocking or warning finding: an informational one ("polygon1 - 3
    features") is already the context line under the conversation, and saying
    it twice is how a start screen turns back into a report.
    """
    reading = first_look.opening_reading(state or {})
    for finding in reading.get("findings") or []:
        if finding.get("severity") not in (first_look.BLOCKING, first_look.WARNING):
            continue
        # Amber, not red: these wait on the user. Red is for what failed or
        # destroys something, and an unplaced scan has done neither.
        word = _FINDING_WORDS.get(str(finding.get("id") or ""), "Check this")
        return {"tone": "warn", "word": word, "detail": str(finding.get("headline") or "")}
    return None


_FINDING_WORDS = {
    "raster_not_georeferenced": "Not placed",
    "vector_no_crs": "No CRS",
    "crs_mismatch": "Check this",
}


def context_line(layer: Mapping[str, Any] | None, selection_count: Any = 0) -> str:
    """The line above the composer: which layer a question is about.

    "polygon1 · 3 features · EPSG:4326". The CRS is shown as the code, because
    it is data; an absent one is said as absent rather than dropped.
    """
    layer = layer or {}
    name = str(layer.get("name") or layer.get("id") or "")
    if not name:
        return "No active layer"
    parts = [name]
    if str(layer.get("kind") or "") == "raster":
        parts.append("raster")
    else:
        try:
            count = int(layer.get("feature_count") or 0)
        except (TypeError, ValueError):
            count = 0
        parts.append("1 feature" if count == 1 else "{:,} features".format(count))
    try:
        selected = int(selection_count or 0)
    except (TypeError, ValueError):
        selected = 0
    if selected:
        parts.append("{:,} selected".format(selected))
    parts.append(str(layer.get("crs") or "") or "No CRS")
    return " · ".join(parts)


# Capability domains whose result is a MEASUREMENT of the user's data. An
# answer that came from one of these says "Measured" above it; an answer that
# came from nothing but the model says nothing, because a status line is a
# claim and that one would be false.
MEASURING_DOMAINS = frozenset({
    "analytics", "measure", "inspect", "spatial", "survey", "coordinates", "postgis",
})


def capability_domain(capability: Any) -> str:
    text = str(capability or "")
    for separator in (":", "."):
        if separator in text:
            return text.split(separator, 1)[0]
    return ""


def is_measurement(capability: Any) -> bool:
    return capability_domain(capability) in MEASURING_DOMAINS


def _lower_first(text: str) -> str:
    text = str(text or "").strip().rstrip(".")
    return text[:1].lower() + text[1:] if text else ""


def turn_status(turn: Mapping[str, Any] | None, describe=None) -> dict[str, str] | None:
    """The status line above an assistant answer, or None for plain prose.

    Severity a turn was said with comes first (a refusal is not a measurement),
    then what its steps did. `describe(capability_id)` names a capability run
    locally, which has no step row of its own.
    """
    turn = turn or {}
    severity = str(turn.get("severity") or "")
    if severity == "blocking":
        return {"tone": "err", "word": "Failed", "detail": ""}
    if severity == "warning":
        return {"tone": "warn", "word": "Not done", "detail": ""}
    if severity == "success":
        return {"tone": "ok", "word": "Done", "detail": ""}
    steps = [row for row in (turn.get("steps") or []) if isinstance(row, Mapping)]
    for row in steps:
        if row.get("status") == "failed":
            return {"tone": "err", "word": "Failed", "detail": _lower_first(row.get("title"))}
    for row in steps:
        if row.get("status") in ("awaiting_input", "awaiting_confirmation"):
            return {"tone": "warn", "word": "Needs you", "detail": _lower_first(row.get("title"))}
    capability = str(turn.get("capability") or "")
    if is_measurement(capability):
        detail = describe(capability) if callable(describe) else ""
        return {"tone": "ok", "word": "Measured", "detail": _lower_first(detail)}
    for row in steps:
        if row.get("status") == "succeeded" and is_measurement(row.get("capability")):
            return {"tone": "ok", "word": "Measured", "detail": _lower_first(row.get("title"))}
    return None


def steps_summary(steps) -> str:
    """"2 steps · 0.4 s" for the collapsed trace, "" when there are none."""
    rows = [row for row in (steps or []) if isinstance(row, Mapping)]
    if not rows:
        return ""
    total = 0
    for row in rows:
        try:
            total += max(0, int(row.get("duration_ms") or 0))
        except (TypeError, ValueError):
            continue
    words = "1 step" if len(rows) == 1 else "{} steps".format(len(rows))
    if total <= 0:
        return words
    if total < 1000:
        return "{} · {} ms".format(words, total)
    seconds = total / 1000.0
    return "{} · {:.1f} s".format(words, seconds) if seconds < 10 else "{} · {:.0f} s".format(words, seconds)


def trace_worth_showing(steps) -> bool:
    """Whether a turn's steps earn a "N steps" disclosure.

    Only when a capability or tool actually ran, or a step failed or waits on
    the person. The assistant's own bookkeeping ("nivo:classify", "nivo:plan")
    under a plain reply is noise.
    """
    for row in steps or []:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "")
        if status in ("failed", "awaiting_input", "awaiting_confirmation"):
            return True
        capability = str(row.get("capability") or "")
        if not capability or capability.startswith(("nivo:", "nivo.")):
            continue
        if status in ("succeeded", "running", "refused", ""):
            return True
    return False
