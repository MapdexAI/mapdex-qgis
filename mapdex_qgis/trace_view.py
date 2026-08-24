"""Turn a compose response's execution trace into rows the panel can draw.

The QGIS transcript used to be a list of ``(sender, text)`` pairs. A turn that
ran four capabilities and a turn that answered from memory looked identical,
so the only honest answer to "what did it just do to my project" was to look at
the layer tree and guess.

This module is deliberately free of Qt and of QGIS: it is the presentation rule,
and the rule is worth testing without a running application. ``plugin.py`` draws
what it returns.

Two rules carry over from the server record and are enforced here.

* A step's outcome line comes from its ``message``, which the server builds from
  the observation. The model's ``reason`` is rendered separately and labelled as
  its claim, because a reason that reads like a measurement is how a trail stops
  being evidence.
* Status is text, never colour alone. Every row carries a ``status_label`` a
  screen reader and a monochrome theme can both read.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

MAX_PARAM_CHARS = 160
MAX_PARAMS_SHOWN = 6

_STATUS_LABELS = {
    "planned": "queued",
    "running": "running",
    "succeeded": "done",
    "failed": "failed",
    "skipped": "skipped",
    "refused": "declined",
    "awaiting_input": "waiting for you",
    "awaiting_confirmation": "waiting for your approval",
}

_TONES = {
    "failed": "danger",
    "refused": "muted",
    "skipped": "muted",
    "running": "active",
    "planned": "active",
    "awaiting_input": "active",
    "awaiting_confirmation": "active",
}


def step_rows(response: Any) -> list[dict[str, Any]]:
    """Rows for the steps a turn recorded, in order.

    An empty list means the turn recorded nothing, and the caller must draw
    nothing rather than an empty heading: a trail that is present and blank
    reads as "it did nothing", which is a claim.
    """
    trace = _trace_of(response)
    if not trace:
        return []
    steps = trace.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        return []
    rows: list[dict[str, Any]] = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            continue
        rows.append(_row(step, position))
    return rows


def budget_notice(response: Any) -> str:
    """What the person needs to know about the step budget, once a turn is over.

    Which is: nothing, unless the turn was cut short.

    This used to render "Step 3 of 8" for every finished turn and leave it in
    the status line, so the panel sat there reading "Step 2 of 32" with nothing
    running. That is worse than saying nothing twice over. It looks like work in
    progress, and it invites the reading that thirty steps remain, when in fact
    the answer is already on screen and the count is a spent budget.

    A loop that stopped because it ran out of room DID reach a different outcome
    from one that finished, and that difference is the only part worth a line -
    it is the case where the reply may be incomplete. It says so in words rather
    than leaving the person to notice that two numbers are equal.

    Mid-turn progress is a separate question with a separate source: the
    continuation loop reports its own steps_used and steps_max under
    response["continuation"], which continuation.continuation_budget reads. The
    same numbers are information while work is happening and noise once it has
    stopped, and that distinction is the whole reason this function exists.
    """
    trace = _trace_of(response)
    if not trace:
        return ""
    budget = trace.get("budget")
    if not isinstance(budget, Mapping):
        return ""
    if not budget.get("exhausted"):
        return ""
    used = _int(budget.get("steps_used"))
    total = _int(budget.get("steps_max"))
    if total <= 0:
        # Exhausted with no ceiling reported is still exhausted. Saying so
        # without the arithmetic beats saying nothing, and beats "step 0 of 0".
        return "Stopped after running out of steps"
    return "Stopped after {} of {} steps".format(used, total)


def _row(step: Mapping[str, Any], position: int) -> dict[str, Any]:
    status = str(step.get("status") or "succeeded")
    capability = str(step.get("capability") or "")
    title = str(step.get("title") or "").strip() or capability or "Step"
    index = _int(step.get("index")) or position
    error = step.get("error") if isinstance(step.get("error"), Mapping) else {}
    detail = str(step.get("message") or "").strip()
    if not detail and error:
        detail = str(error.get("message") or "").strip()
    return {
        "id": str(step.get("id") or "{}".format(index)),
        "index": index,
        "title": title,
        "capability": capability,
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "tone": _TONES.get(status, "quiet"),
        "detail": detail,
        "duration": format_duration(step.get("duration_ms")),
        "params": format_params(step.get("params")),
        "reason": str(step.get("reason") or "").strip(),
        "error_code": str(error.get("code") or "") if error else "",
    }


def format_duration(duration_ms: Any) -> str:
    """Milliseconds under a second, seconds above it.

    Nothing is rendered for a missing or zero duration: "0 ms" claims a
    measurement that was never taken.
    """
    value = _int(duration_ms)
    if value <= 0:
        return ""
    if value < 1000:
        return "{} ms".format(value)
    seconds = value / 1000.0
    if seconds < 10:
        return "{:.1f} s".format(seconds)
    return "{:.0f} s".format(seconds)


def format_params(params: Any) -> str:
    """Render the arguments a capability was called with as ``key=value``.

    The server already bounded and redacted them; this bounds the *rendering*,
    because a panel line that wraps for ten rows is unreadable however honest it
    is. What was cut is stated rather than silently dropped.
    """
    if not isinstance(params, Mapping) or not params:
        return ""
    keys = sorted(str(key) for key in params)
    shown = keys[:MAX_PARAMS_SHOWN]
    parts = ["{}={}".format(key, _value(params[key])) for key in shown]
    rendered = ", ".join(parts)
    if len(keys) > MAX_PARAMS_SHOWN:
        rendered += ", +{} more".format(len(keys) - MAX_PARAMS_SHOWN)
    if len(rendered) > MAX_PARAM_CHARS:
        rendered = rendered[:MAX_PARAM_CHARS] + "..."
    return rendered


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, Mapping):
        return "{{{} fields}}".format(len(value))
    if isinstance(value, Sequence):
        return "[{} items]".format(len(value))
    return str(value)


def _trace_of(response: Any) -> Mapping[str, Any] | None:
    if not isinstance(response, Mapping):
        return None
    trace = response.get("trace")
    if isinstance(trace, Mapping):
        return trace
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
