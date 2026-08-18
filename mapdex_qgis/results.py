"""Pure helpers for turning Mapdex run payloads into QGIS-importable targets.

Kept free of PyQGIS so unit tests can run without a QGIS install.
"""
from __future__ import annotations

from typing import Any, Iterable


TERMINAL_BATCH_STATES = frozenset({"completed", "failed", "cancelled", "partial", "completed_with_failures"})
SUCCEEDED_ITEM_STATES = frozenset({"succeeded", "completed"})
REVIEW_ITEM_STATES = frozenset({"needs_review", "review_required"})

# The canonical code extraction returns for a raster with no real-world
# placement. Callers branch on the code and never on the message: the message is
# human copy the server may reword or localize, and matching it would be a
# keyword list in disguise. `tests/test_mapdex_runs.py` asserts this string is
# still in the generated error registry, so a contract rename cannot leave the
# plugin quietly matching a code that no longer exists.
GEOREFERENCE_REQUIRED = "GEOREFERENCE_REQUIRED"


def batch_state(detail: dict[str, Any]) -> str:
    return str(detail.get("state") or detail.get("status") or "").lower()


def _failed_items(detail: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for item in detail.get("items") or []:
        if isinstance(item, dict) and str(item.get("state") or "").lower() == "failed":
            yield item


def _error_code(error: Any) -> str:
    """The canonical code on one item's error, or "" when it carries none."""
    if isinstance(error, dict):
        return str(error.get("code") or "").strip().upper()
    return ""


def first_batch_error(detail: dict[str, Any]) -> str:
    """Return one actionable server error without exposing raw envelopes."""
    for item in _failed_items(detail):
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


def batch_failure(detail: dict[str, Any]) -> dict[str, str]:
    """The first failed item's typed error, kept whole.

    `first_batch_error` reduces the same error to one sentence for the status
    line. That is right for display and wrong for deciding what to do next: the
    canonical code and the file the refusal is about are both on the wire and
    both were being dropped, so a refusal that names a next step arrived as a
    generic failure with nowhere to go.
    """
    for item in _failed_items(detail):
        error = item.get("error")
        code = _error_code(error)
        message = ""
        correlation_id = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            correlation_id = str(error.get("correlation_id") or "").strip()
        elif isinstance(error, str):
            message = error.strip()
        if not code and not message:
            continue
        return {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
            "file_id": str(item.get("file_id") or "").strip(),
        }
    return {"code": "", "message": "", "correlation_id": "", "file_id": ""}


def failed_item_codes(detail: dict[str, Any]) -> list[str]:
    """One entry per failed item: its canonical code, or "" if it reported none.

    The empty entries are the point. They keep the list the same length as the
    failures, so a caller asking whether every failure shares one code cannot
    get a true answer out of the items that never said.
    """
    return [_error_code(item.get("error")) for item in _failed_items(detail)]


def every_failure_needs_placement(detail: dict[str, Any]) -> bool:
    """Whether every failed item was refused for want of real-world placement.

    Retry re-sends all failed items, so it is worth offering while any one of
    them could come back differently. When they were all refused for want of
    placement, the identical request produces the identical refusal, and
    offering it teaches the user that the button does nothing.
    """
    codes = failed_item_codes(detail)
    return bool(codes) and all(code == GEOREFERENCE_REQUIRED for code in codes)


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


# `GET /v1/runs` reports the task status and, when a job exists, the job state on
# top of it. The two vocabularies overlap but are not identical, so both are
# folded into the four buckets `mapdex.jobs@1` offers rather than asking the user
# to know which surface produced the word they are reading.
FAILED_RUN_STATES = frozenset({"failed", "cancelled"})
COMPLETED_RUN_STATES = frozenset({"completed", "succeeded"})

# The listing is bounded because it lands in a chat transcript, not a table. A
# project with three hundred sheets would otherwise print three hundred lines.
MAX_LISTED_RUNS = 10


def run_state(run: dict[str, Any]) -> str:
    """The state to report for one run.

    The job state wins when there is a job: the task row can still read
    `dispatched` while its job has already failed, and reporting the older of
    two known facts is how a failure stays invisible.
    """
    job = run.get("job")
    if isinstance(job, dict):
        state = str(job.get("state") or "").strip().lower()
        if state:
            return "needs_review" if state == "review_required" else state
    return str(run.get("status") or run.get("state") or "").strip().lower()


def run_matches_state(run: dict[str, Any], wanted: str) -> bool:
    """Whether a run belongs in the requested bucket."""
    state = run_state(run)
    if wanted in ("", "all"):
        return True
    if wanted == "review_required":
        return state in REVIEW_ITEM_STATES
    if wanted == "failed":
        return state in FAILED_RUN_STATES
    if wanted == "completed":
        return state in COMPLETED_RUN_STATES
    if wanted == "active":
        # Active is everything that has not stopped. Defined as the complement
        # of the terminal sets rather than as its own list, so a state this
        # build has never seen is reported as still running instead of being
        # dropped from every bucket and vanishing from the answer.
        return state not in FAILED_RUN_STATES | COMPLETED_RUN_STATES | REVIEW_ITEM_STATES
    return False


def run_failure(run: dict[str, Any]) -> str:
    """The failure the server reported for this run, or "" when it reported none.

    A run listed as failed with no reason is the case the capability's own
    summary promises to explain, so the message is carried through rather than
    reduced to the state word.
    """
    job = run.get("job") if isinstance(run.get("job"), dict) else {}
    error = job.get("error") if isinstance(job, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        correlation_id = str(error.get("correlation_id") or "").strip()
        if message and correlation_id:
            return "{} (reference {})".format(message, correlation_id)
        if message:
            return message
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def run_failure_code(run: dict[str, Any]) -> str:
    """The canonical code behind this run's failure, or "" when it carries none.

    Reported beside the message rather than folded into it, because the listing
    is where a user asks what went wrong and some codes name a next step the
    sentence alone cannot route them to.
    """
    job = run.get("job") if isinstance(run.get("job"), dict) else {}
    return _error_code(job.get("error") if isinstance(job, dict) else None)


def summarize_runs(
    runs: Any, state: str = "all", limit: int = MAX_LISTED_RUNS
) -> dict[str, Any]:
    """Turn a `GET /v1/runs` payload into a bounded, structured answer.

    Counts are taken over every run the project has, not over the truncated
    list: "3 of 47 runs" is a different fact from "3 runs", and reporting the
    page size as the total would be a number the product invented.
    """
    rows = [run for run in (runs if isinstance(runs, list) else []) if isinstance(run, dict)]
    wanted = str(state or "all").strip().lower()
    counts: dict[str, int] = {}
    for run in rows:
        key = run_state(run) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    matched = [run for run in rows if run_matches_state(run, wanted)]
    listed = []
    for run in matched[: max(1, int(limit))]:
        listed.append(
            {
                "run_id": str(run.get("id") or ""),
                "state": run_state(run) or "unknown",
                "title": str(run.get("prompt") or "").strip()[:120],
                "created_at": str(run.get("created_at") or ""),
                "error": run_failure(run),
                "code": run_failure_code(run),
            }
        )
    return {
        "kind": "jobs",
        "state": wanted,
        "total": len(rows),
        "matched": len(matched),
        "counts": counts,
        "runs": listed,
    }


def select_review_run(runs: Any, run_id: str = "") -> dict[str, Any] | None:
    """The run a review should open, or None when there is nothing to review.

    Without an explicit id this picks the most recent run that needs review.
    `GET /v1/runs` returns newest first, so the first match is that run; sorting
    on `created_at` here would re-derive an order the server already decided and
    would silently reorder runs whose timestamps are missing.
    """
    rows = [run for run in (runs if isinstance(runs, list) else []) if isinstance(run, dict)]
    wanted = str(run_id or "").strip()
    if wanted:
        for run in rows:
            if str(run.get("id") or "") == wanted:
                return run
        return None
    for run in rows:
        if run_state(run) in REVIEW_ITEM_STATES:
            return run
    return None


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


def geojson_truncation_notice(geojson: dict[str, Any], layer_name: str) -> str:
    """Return a warning string if a `/v1/layers/{id}/geojson` response was
    truncated (FN-003), or "" when it wasn't or the server didn't report it.

    The endpoint's default page size is 5000 features; a layer larger than
    that returned exactly 5000 features with nothing distinguishing it from a
    genuinely small layer, so a QGIS import could silently miss most of a
    dataset. The server now stamps `properties.truncated`/`returned_count`/
    `total_count` on the FeatureCollection (RFC 7946 §7 foreign members) when
    it knows the layer's true size; older servers that don't send these
    fields simply produce no notice here.
    """
    if not isinstance(geojson, dict):
        return ""
    properties = geojson.get("properties")
    if not isinstance(properties, dict) or not properties.get("truncated"):
        return ""
    returned = properties.get("returned_count")
    total = properties.get("total_count")
    if isinstance(returned, int) and isinstance(total, int) and total > 0:
        return (
            "{}: only {} of {} features were imported. Re-run with a higher limit "
            "or use the GeoJSON export instead of the QGIS import to get the rest."
        ).format(layer_name, returned, total)
    return "{}: this layer was truncated on import.".format(layer_name)


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
