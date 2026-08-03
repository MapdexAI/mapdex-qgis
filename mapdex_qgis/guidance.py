"""User-facing guidance for the single-task QGIS companion lifecycle."""
from __future__ import annotations


ACTIVE_STATES = {"created", "queued", "pending", "running", "cancelling"}

# How long a task may sit unstarted before the panel stops implying that work
# is happening. Dispatch normally takes seconds, so minutes without a start
# means the processing service is not consuming the queue.
STALL_SECONDS = 120


def task_guidance(
    state: str,
    counts: dict | None = None,
    backend_started: bool | None = None,
    waiting_seconds: int = 0,
) -> dict:
    """Describe the current task for the panel.

    ``backend_started`` is what the server reports about the child run: True
    once execution began, False while the job is still queued, None when it is
    unknown. A queued job that is never picked up used to render as
    "Processing in Mapdex" forever, which hid an unavailable processing
    service from the user.
    """
    counts = counts or {}
    total = max(0, int(counts.get("total", 0) or 0))
    succeeded = max(0, int(counts.get("succeeded", 0) or 0))
    review = max(0, int(counts.get("needs_review", 0) or 0))
    failed = max(0, int(counts.get("failed", 0) or 0))
    cancelled = max(0, int(counts.get("cancelled", 0) or 0))
    completed = succeeded + review + failed + cancelled

    if state in ACTIVE_STATES and backend_started is False:
        if waiting_seconds >= STALL_SECONDS:
            minutes = max(1, int(waiting_seconds // 60))
            return {
                "phase": "Waiting for Mapdex processing",
                "progress": 0,
                "busy": False,
                "hint": (
                    "Queued for {} minute(s) without starting. The Mapdex "
                    "processing service has not picked this task up yet. It "
                    "resumes on its own once the service is back; contact "
                    "support if this persists.".format(minutes)
                ),
                "action": "",
            }
        return {
            "phase": "Queued for processing",
            "progress": 10,
            "busy": True,
            "hint": "Waiting for a Mapdex processing slot. You can keep working in QGIS.",
            "action": "",
        }

    if state in {"created", "queued", "pending"}:
        return {"phase": "Queued", "progress": 20, "busy": False,
                "hint": "You can keep working in QGIS. Mapdex is processing your task in the background.",
                "action": ""}
    if state in {"running", "cancelling"}:
        progress = min(95, max(30, int(completed * 100 / total))) if total else 55
        return {"phase": "Processing in Mapdex", "progress": progress,
                "busy": total == 0 or completed == 0,
                "hint": "Mapdex is analyzing the source. You can leave this panel open and continue mapping.",
                "action": ""}
    if review:
        return {"phase": "Review required", "progress": 100, "busy": False,
                "hint": "Review it in Mapdex and QGIS adds the approved layer by itself, "
                        "or get the draft now and review it here.",
                "action": "Review in Mapdex"}
    if failed:
        return {"phase": "Action required", "progress": 100, "busy": False,
                "hint": "Open the task for the exact error, or retry after correcting the source.",
                "action": "Inspect failure in Mapdex"}
    if succeeded:
        return {"phase": "Ready for QGIS", "progress": 100, "busy": False,
                "hint": "The verified vector result is ready. Add it to this QGIS project.",
                "action": "Open result in Mapdex"}
    return {"phase": "Task finished", "progress": 100, "busy": False,
            "hint": "Open the task in Mapdex to inspect its final state.",
            "action": "Open result in Mapdex"}


def run_has_started(run: dict | None) -> bool | None:
    """Has the server actually begun executing this run?

    Returns None when the payload does not say, so the caller keeps the
    neutral wording instead of inventing a state.
    """
    if not isinstance(run, dict):
        return None
    job = run.get("job")
    state = ""
    if isinstance(job, dict):
        state = str(job.get("state") or job.get("status") or "").lower()
    if not state:
        state = str(run.get("status") or "").lower()
    if not state:
        return None
    if state in {"queued", "pending", "created"}:
        # A step that already reported progress outranks a stale job row.
        for step in run.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").lower() not in {"", "pending", "queued"}:
                return True
        return False
    return True
