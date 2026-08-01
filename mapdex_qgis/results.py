"""Pure helpers for turning Mapdex run payloads into QGIS-importable targets.

Kept free of PyQGIS so unit tests can run without a QGIS install.
"""
from __future__ import annotations

from typing import Any, Iterable


TERMINAL_BATCH_STATES = frozenset({"completed", "failed", "cancelled", "partial", "completed_with_failures"})
SUCCEEDED_ITEM_STATES = frozenset({"succeeded", "completed"})
REVIEW_ITEM_STATES = frozenset({"needs_review", "review_required"})


def batch_state(detail: dict[str, Any]) -> str:
    return str(detail.get("state") or detail.get("status") or "").lower()


def first_batch_error(detail: dict[str, Any]) -> str:
    """Return one actionable server error without exposing raw envelopes."""
    for item in detail.get("items") or []:
        if str(item.get("state") or "").lower() != "failed":
            continue
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            correlation_id = error.get("correlation_id")
            if message:
                suffix = " (reference {})".format(correlation_id) if correlation_id else ""
                return "{}{}".format(message, suffix)
        if isinstance(error, str) and error.strip():
            return error.strip()
    return ""


def batch_is_terminal(detail: dict[str, Any]) -> bool:
    return batch_state(detail) in TERMINAL_BATCH_STATES


def succeeded_run_ids(detail: dict[str, Any]) -> list[str]:
    runs: list[str] = []
    for item in detail.get("items") or []:
        state = str(item.get("state") or "").lower()
        run_id = item.get("run_id") or ""
        if run_id and state in SUCCEEDED_ITEM_STATES:
            runs.append(run_id)
    return runs


def review_run_ids(detail: dict[str, Any]) -> list[str]:
    runs: list[str] = []
    for item in detail.get("items") or []:
        state = str(item.get("state") or "").lower()
        run_id = item.get("run_id") or ""
        if run_id and state in REVIEW_ITEM_STATES:
            runs.append(run_id)
    return runs


def _iter_result_refs(run: dict[str, Any]) -> Iterable[dict[str, Any]]:
    refs = run.get("result_references")
    if isinstance(refs, list) and refs:
        for ref in refs:
            if isinstance(ref, dict):
                yield ref
        return
    # Fallback: older/output.results shape used by some clients.
    output = run.get("output") or {}
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list):
        return
    for entry in results:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("reference")
        if isinstance(nested, dict):
            yield {
                "kind": nested.get("kind"),
                "id": nested.get("id"),
                "url": entry.get("url"),
                "geometry_type": nested.get("geometry_type") or entry.get("geometry_type"),
                "summary": entry.get("label") or entry.get("description"),
            }


def collect_vector_layer_imports(run: dict[str, Any]) -> list[dict[str, str]]:
    """Return [{layer_id, name}] for vector layers the plugin can load as GeoJSON."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in _iter_result_refs(run):
        kind = str(ref.get("kind") or "").lower()
        layer_id = str(ref.get("id") or "")
        if kind != "layer" or not layer_id or layer_id in seen:
            continue
        geometry = str(ref.get("geometry_type") or "").lower()
        if geometry == "raster":
            continue
        seen.add(layer_id)
        name = str(ref.get("summary") or ref.get("label") or layer_id)
        found.append({"layer_id": layer_id, "name": name})
    return found


def collect_layer_imports(run: dict[str, Any]) -> list[dict[str, str]]:
    """Return all result Layers; authoritative type comes from layer metadata."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in _iter_result_refs(run):
        kind = str(ref.get("kind") or "").lower()
        layer_id = str(ref.get("id") or "")
        if kind != "layer" or not layer_id or layer_id in seen:
            continue
        seen.add(layer_id)
        name = str(ref.get("summary") or ref.get("label") or layer_id)
        found.append(
            {
                "layer_id": layer_id,
                "name": name,
                "geometry_type": str(ref.get("geometry_type") or "").lower(),
            }
        )
    return found


def collect_raster_layer_imports(run: dict[str, Any]) -> list[dict[str, str]]:
    """Return raster result Layers whose backing GeoTIFF QGIS can download."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in _iter_result_refs(run):
        kind = str(ref.get("kind") or "").lower()
        layer_id = str(ref.get("id") or "")
        geometry = str(ref.get("geometry_type") or "").lower()
        if kind != "layer" or geometry != "raster" or not layer_id or layer_id in seen:
            continue
        seen.add(layer_id)
        name = str(ref.get("summary") or ref.get("label") or layer_id)
        found.append({"layer_id": layer_id, "name": name})
    return found


def collect_geojson_artifact_urls(run: dict[str, Any]) -> list[dict[str, str]]:
    """Return [{url, name}] for downloadable GeoJSON artifacts only."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in _iter_result_refs(run):
        kind = str(ref.get("kind") or "").lower()
        url = str(ref.get("url") or "")
        if kind != "artifact" or not url or url in seen:
            continue
        fmt = str(ref.get("format") or "").lower()
        if fmt and fmt not in {"geojson", "json", "geo+json"}:
            continue
        if not fmt and "geojson" not in url.lower():
            continue
        seen.add(url)
        name = str(ref.get("summary") or ref.get("label") or ref.get("id") or "Mapdex result")
        found.append({"url": url, "name": name})
    return found
