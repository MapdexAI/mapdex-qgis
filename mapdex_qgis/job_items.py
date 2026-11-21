"""The items of a batch, ordered and described for a person working through it.

A batch of three hundred sheets behind one progress bar is a number, not a
surface. What a reviewer needs is the opposite of a summary: which sheet failed,
which one is waiting on them, and which are done and can be ignored. This module
turns the server's item list into that, and it does it in plain functions so the
ordering and the wording are testable without a running QGIS.

The ordering rule is the product's, not this file's: docs/UX.md §10.1 states the
queue orders work for the reviewer as failed, then needs_review, then active,
then done. A list sorted by index instead buries the twenty sheets that need a
person among the two hundred and eighty that do not.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

# The closed set the server emits (contracts.BatchItemState). Anything outside
# it is shown as itself rather than guessed at: a state this build has not heard
# of is a fact, and rendering it as "done" would be an invention.
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_NEEDS_REVIEW = "needs_review"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# Attention first. Two groups need a person and two do not, and the two that do
# are not interchangeable: a failure is finished and wrong, a review is waiting.
_ORDER = {
    STATE_FAILED: 0,
    STATE_NEEDS_REVIEW: 1,
    STATE_RUNNING: 2,
    STATE_PENDING: 3,
    STATE_SUCCEEDED: 4,
    STATE_CANCELLED: 5,
}
_UNKNOWN_ORDER = 6

# Icon plus label, never colour alone (DESIGN.md §4.2). These are the plain
# characters a Qt label renders in any theme; the label beside them carries the
# meaning on its own.
_MARK = {
    STATE_FAILED: "!",
    STATE_NEEDS_REVIEW: "?",
    STATE_RUNNING: "*",
    STATE_PENDING: "-",
    STATE_SUCCEEDED: "+",
    STATE_CANCELLED: "x",
}

# The web app's words (My maps): what waits on the person is "Needs you", what
# is finished and usable is "Ready", what is in flight is "Working".
_LABEL = {
    STATE_FAILED: "Failed",
    STATE_NEEDS_REVIEW: "Needs you",
    STATE_RUNNING: "Working",
    STATE_PENDING: "Queued",
    STATE_SUCCEEDED: "Ready",
    STATE_CANCELLED: "Cancelled",
}

# The status tone each state is drawn with (`theme.TONES`): a solid icon and a
# coloured word. Amber waits on you, red failed, green is done, blue is working.
_TONE = {
    STATE_FAILED: "err",
    STATE_NEEDS_REVIEW: "warn",
    STATE_RUNNING: "working",
    STATE_PENDING: "neutral",
    STATE_SUCCEEDED: "ok",
    STATE_CANCELLED: "neutral",
}


def item_state(item: Dict[str, Any]) -> str:
    return str((item or {}).get("state") or "").strip().lower()


def order_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attention first, then the order the batch was submitted in.

    Stable within a group on purpose: a reviewer who works down the failures
    expects sheet 4 before sheet 9, and a list that reshuffles on every poll is
    one nobody can keep their place in.
    """

    def key(pair):
        index, item = pair
        state = item_state(item)
        try:
            submitted = int(item.get("index", index))
        except (TypeError, ValueError):
            submitted = index
        return (_ORDER.get(state, _UNKNOWN_ORDER), submitted, index)

    return [item for _, item in sorted(enumerate(items or []), key=key)]


def item_title(item: Dict[str, Any], names: Optional[Dict[str, str]] = None) -> str:
    """What to call this item, in the words the person recognises.

    The file name when we know it, because that is what they chose in the file
    dialog. A page number when the item is one page of a document, because
    otherwise every page of a three-hundred-page PDF has the same title and the
    list cannot be read at all.
    """
    item = item or {}
    file_id = str(item.get("file_id") or "")
    label = ""
    if names:
        label = str(names.get(file_id) or "")
    if label:
        label = os.path.basename(label)
    if not label:
        try:
            position = int(item.get("index", 0)) + 1
        except (TypeError, ValueError):
            position = 1
        label = "Item {}".format(position)

    page = item.get("page")
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        page_number = 0
    # 0 means "not paged" in the contract, so it is not a page one.
    if page_number > 0:
        return "{} · page {}".format(label, page_number)
    return label


def item_status(item: Dict[str, Any]) -> Dict[str, str]:
    """The state as a mark and a word, plus the reason when there is one.

    A failed item carries its own message. Showing "Failed" alone asks the
    person to go and find out why somewhere else, which for the one item that
    needs them is the wrong place to save a line.
    """
    state = item_state(item)
    detail = ""
    error = (item or {}).get("error")
    if isinstance(error, dict):
        detail = str(error.get("message") or "").strip()
    elif isinstance(error, str):
        detail = error.strip()

    return {
        "state": state,
        "mark": _MARK.get(state, "?"),
        # An unheard-of state is shown as itself. Mapping it onto a known label
        # would report something this build cannot actually know.
        "label": _LABEL.get(state, state.replace("_", " ").capitalize() or "Unknown"),
        "tone": _TONE.get(state, "neutral"),
        "detail": detail,
    }


def item_action(item: Dict[str, Any]) -> Dict[str, str]:
    """The one thing to offer for this item, or nothing.

    One action per row. A row with three buttons is a row nobody reads, and the
    action that matters differs by state: a failure is opened to find out why, a
    review is opened to decide, a finished item is added to the map.
    """
    state = item_state(item)
    run_id = str((item or {}).get("run_id") or "")
    if state == STATE_FAILED:
        return {"key": "open", "label": "See why in Mapdex", "run_id": run_id}
    if state == STATE_NEEDS_REVIEW:
        return {"key": "review", "label": "Open in Mapdex", "run_id": run_id}
    if state == STATE_SUCCEEDED:
        return {"key": "import", "label": "Add to QGIS", "run_id": run_id}
    return {"key": "", "label": "", "run_id": run_id}


def items_summary(items: Sequence[Dict[str, Any]]) -> str:
    """One line above the list, naming what is in it.

    Counted from the items themselves rather than from the batch's own counts,
    because this sentence describes the list directly below it. Two numbers from
    two sources disagreeing on screen is worse than one number.
    """
    items = list(items or [])
    if not items:
        return ""
    total = len(items)
    failed = sum(1 for item in items if item_state(item) == STATE_FAILED)
    review = sum(1 for item in items if item_state(item) == STATE_NEEDS_REVIEW)
    done = sum(1 for item in items if item_state(item) == STATE_SUCCEEDED)

    parts = ["{} items".format(total) if total != 1 else "1 item"]
    if failed:
        parts.append("{} failed".format(failed))
    if review:
        parts.append("{} need review".format(review) if review != 1 else "1 needs review")
    if done:
        parts.append("{} done".format(done))
    return " · ".join(parts)


def _counts(items: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items or []:
        state = item_state(item)
        counts[state] = counts.get(state, 0) + 1
    return counts


def jobs_headline(items: Sequence[Dict[str, Any]]) -> str:
    """The Jobs page title: how many sheets, and how many need the person.

    "4 sheets: 1 needs you" leads with what is waiting, the web app's rule for
    a batch digest. A failure counts as needing the person: nothing else will
    happen to it until somebody looks.
    """
    items = list(items or [])
    if not items:
        return ""
    total = len(items)
    counts = _counts(items)
    waiting = counts.get(STATE_NEEDS_REVIEW, 0) + counts.get(STATE_FAILED, 0)
    sheets = "1 sheet" if total == 1 else "{} sheets".format(total)
    if waiting:
        return "{}: {} {}".format(sheets, waiting, "needs you" if waiting == 1 else "need you")
    if counts.get(STATE_RUNNING, 0) or counts.get(STATE_PENDING, 0):
        return "{}: working".format(sheets)
    if counts.get(STATE_SUCCEEDED, 0) == total:
        return "{}: ready".format(sheets) if total == 1 else "{}: all ready".format(sheets)
    return "{}: finished".format(sheets)


_LINE_WORDS = (
    (STATE_SUCCEEDED, "ready"),
    (STATE_RUNNING, "working"),
    (STATE_PENDING, "queued"),
    (STATE_NEEDS_REVIEW, "waiting for review"),
    (STATE_FAILED, "failed"),
    (STATE_CANCELLED, "cancelled"),
)


def jobs_line(items: Sequence[Dict[str, Any]]) -> str:
    """The line under the title, naming every sheet by state.

    Its counts add up to the total in the title. "4 sheets: 2 need you" over
    "1 ready, 1 working, 1 failed" left the sheet waiting on review out, so the
    line read as three of four. A state this build does not know is counted
    too, rather than dropped.
    """
    counts = _counts(items)
    parts = []
    for state, word in _LINE_WORDS:
        if counts.get(state):
            parts.append("{} {}".format(counts[state], word))
    known = {state for state, _word in _LINE_WORDS}
    other = sum(count for state, count in counts.items() if state not in known)
    if other:
        parts.append("{} in another state".format(other))
    return ", ".join(parts)


# -- the batch as one state, with a clock and a real fraction -----------------

_QUEUED = {"created", "queued", "pending"}
_RUNNING = {"running", "processing"}


def batch_status(state: str, counts: Dict[str, Any], active: bool, uploading: bool = False) -> Dict[str, str]:
    """ONE state for the batch, from the real job state: a tone and a word.

    "Queued" and "Working" are never shown together: the word is what the
    server says the job is doing now.
    """
    state = str(state or "").lower()
    counts = counts or {}

    def _n(key):
        try:
            return max(0, int(counts.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    if uploading:
        return {"tone": "working", "word": "Uploading"}
    if active:
        if state == "cancelling":
            return {"tone": "working", "word": "Cancelling"}
        if state in _QUEUED:
            return {"tone": "working", "word": "Queued"}
        return {"tone": "working", "word": "Working"}
    if _n("failed"):
        return {"tone": "err", "word": "Failed"}
    if _n("needs_review"):
        return {"tone": "warn", "word": "Needs you"}
    if _n("succeeded"):
        return {"tone": "ok", "word": "Ready"}
    if _n("cancelled") or state == "cancelled":
        return {"tone": "neutral", "word": "Cancelled"}
    return {"tone": "neutral", "word": ""}


def sheet_fraction(counts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sheets done of sheets sent, or None when there is no real fraction.

    Only a batch of more than one sheet has one. A percentage made from steps
    is never shown: it sits still and reads as stuck.
    """
    counts = counts or {}
    try:
        total = int(counts.get("total", 0) or 0)
        done = sum(max(0, int(counts.get(key, 0) or 0))
                   for key in ("succeeded", "needs_review", "failed", "cancelled"))
    except (TypeError, ValueError):
        return None
    if total <= 1:
        return None
    done = min(done, total)
    return {"done": done, "total": total, "text": "{} of {} sheets".format(done, total)}


def elapsed_text(seconds: Any) -> str:
    """A live clock: "12 s", "1 min 12 s", "1 h 3 min"."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return "{} s".format(seconds)
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return "{} min {} s".format(minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "{} h {} min".format(hours, minutes)


def running_count(counts: Dict[str, Any]) -> int:
    """How many sheets are still in progress (at least one while active)."""
    counts = counts or {}
    try:
        total = int(counts.get("total", 0) or 0)
        done = sum(max(0, int(counts.get(key, 0) or 0))
                   for key in ("succeeded", "needs_review", "failed", "cancelled"))
    except (TypeError, ValueError):
        return 1
    return max(1, total - done)


def created_at_seconds(value: Any) -> float:
    """Epoch seconds from the server's ISO 8601 `created_at`, or 0.0."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()
