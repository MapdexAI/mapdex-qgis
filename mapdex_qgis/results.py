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


# Reviewing inside QGIS means separating what the validators actually flagged.
# The extractor reports `quality_status: uncalibrated` for parcels, so there is
# no calibrated confidence to grade features by; splitting on an invented score
# would claim an accuracy the pipeline explicitly refuses to claim. These
# buckets use only reported facts, in decreasing order of urgency.
REVIEW_BUCKETS = (
    ("invalid", "Invalid geometry"),
    ("needs_review", "Needs review"),
    ("clean", "Clean"),
)


def _bucket_for(properties: dict[str, Any]) -> str:
    validation = str(properties.get("validation_status") or "").strip().lower()
    if validation and validation not in {"valid", "ok", "passed"}:
        return "invalid"
    if properties.get("review_required") is True:
        return "needs_review"
    review_state = str(properties.get("review_status") or "").strip().lower()
    if review_state in {"needs_review", "review_required", "pending"}:
        return "needs_review"
    return "clean"


def split_review_buckets(geojson: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a result layer into the review layers a QGIS user can work through.

    Returns ``[{key, label, features}]`` for the non-empty buckets only, in
    urgency order, so an unreviewed draft arrives as separate QGIS layers
    instead of one undifferentiated blob.
    """
    features = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(features, list):
        return []
    grouped: dict[str, list[Any]] = {key: [] for key, _ in REVIEW_BUCKETS}
    for feature in features:
        properties = feature.get("properties") if isinstance(feature, dict) else None
        grouped[_bucket_for(properties if isinstance(properties, dict) else {})].append(feature)
    return [
        {"key": key, "label": label, "features": grouped[key]}
        for key, label in REVIEW_BUCKETS
        if grouped[key]
    ]


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
