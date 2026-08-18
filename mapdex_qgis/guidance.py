"""User-facing guidance for the single-task QGIS companion lifecycle."""
from __future__ import annotations

from .results import GEOREFERENCE_REQUIRED


ACTIVE_STATES = {"created", "queued", "pending", "running", "cancelling"}

# How long a task may sit unstarted before the panel stops implying that work
# is happening. Dispatch normally takes seconds, so minutes without a start
# means the processing service is not consuming the queue.
STALL_SECONDS = 120


# A refusal that names something the user can do, and where they do it. The
# route is a workflow name `workspace.task_workspace_path` already understands,
# so answering a refusal reuses the destination the panel builds for every other
# handoff instead of inventing a second URL shape for failures.
#
# Only codes whose fix is established belong here. A code absent from this table
# is reported as the failure it is; softening one whose remedy nobody has
# settled would replace an honest error with a guess.
FAILURE_NEXT_STEPS = {
    GEOREFERENCE_REQUIRED: {
        "phase": "Georeference needed first",
        "hint": (
            "This scan has no real-world position yet, so there is nowhere to put "
            "what extraction finds. Georeference it in Mapdex first and the same "
            "sheet is then ready to extract."
        ),
        "action": "Georeference in Mapdex",
        "route": "georeference",
    },
}


def failure_next_step(code: str) -> dict:
    """What a typed failure asks of the user, or ``{}`` when it asks for nothing.

    The scan a user sent is fine; extraction refused because nothing has placed
    it yet. Reporting that as a failure spends their upload and hands back no
    move, which is how the prerequisite stayed invisible until it broke.
    """
    step = FAILURE_NEXT_STEPS.get(str(code or "").strip().upper())
    return dict(step) if step else {}


def with_failure_guidance(summary: dict | None) -> dict:
    """Restate the run listing's failures as next steps where a code names one.

    The server's sentence says what the gate refused. It does not say what to do
    about it, and this listing is exactly where a user asks.
    """
    if not isinstance(summary, dict):
        return {}
    listed = summary.get("runs")
    if not isinstance(listed, list):
        return summary
    rows = []
    for row in listed:
        if not isinstance(row, dict):
            continue
        step = failure_next_step(row.get("code"))
        rows.append({**row, "error": step["hint"]} if step else row)
    return {**summary, "runs": rows}


def task_guidance(
    state: str,
    counts: dict | None = None,
    backend_started: bool | None = None,
    waiting_seconds: int = 0,
    failure_code: str = "",
) -> dict:
    """Describe the current task for the panel.

    ``backend_started`` is what the server reports about the child run: True
    once execution began, False while the job is still queued, None when it is
    unknown. A queued job that is never picked up used to render as
    "Processing in Mapdex" forever, which hid an unavailable processing
    service from the user.

    ``failure_code`` is the canonical code behind a failed item. Without it the
    failed branch could only count failures, so a refusal that names a next step
    read the same as one that does not.
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
        next_step = failure_next_step(failure_code)
        if next_step:
            return {"phase": next_step["phase"], "progress": 100, "busy": False,
                    "hint": next_step["hint"], "action": next_step["action"]}
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
