"""User-facing guidance for the single-task QGIS companion lifecycle."""
from __future__ import annotations


ACTIVE_STATES = {"created", "queued", "pending", "running", "cancelling"}


def task_guidance(state: str, counts: dict | None = None) -> dict:
    counts = counts or {}
    total = max(0, int(counts.get("total", 0) or 0))
    succeeded = max(0, int(counts.get("succeeded", 0) or 0))
    review = max(0, int(counts.get("needs_review", 0) or 0))
    failed = max(0, int(counts.get("failed", 0) or 0))
    cancelled = max(0, int(counts.get("cancelled", 0) or 0))
    completed = succeeded + review + failed + cancelled

    if state in {"created", "queued", "pending"}:
        return {"phase": "Queued", "progress": 20, "busy": False,
                "hint": "You can keep working in QGIS. Open the live task for detailed progress.",
                "action": "Open project"}
    if state in {"running", "cancelling"}:
        progress = min(95, max(30, int(completed * 100 / total))) if total else 55
        return {"phase": "Processing in Mapdex", "progress": progress,
                "busy": total == 0 or completed == 0,
                "hint": "Mapdex is analyzing the source. You can leave this panel open and continue mapping.",
                "action": "Open project"}
    if review:
        return {"phase": "Review required", "progress": 100, "busy": False,
                "hint": "Approve or correct the result in Mapdex, then return here and press Resume.",
                "action": "Review in Mapdex"}
    if failed:
        return {"phase": "Action required", "progress": 100, "busy": False,
                "hint": "Open the task for the exact error, or retry after correcting the source.",
                "action": "Inspect failure in Mapdex"}
    if succeeded:
        return {"phase": "Ready for QGIS", "progress": 100, "busy": False,
                "hint": "The verified vector result is ready. Add it to this QGIS project.",
                "action": "Open Mapdex"}
    return {"phase": "Task finished", "progress": 100, "busy": False,
            "hint": "Open the task in Mapdex to inspect its final state.",
            "action": "Open Mapdex"}
