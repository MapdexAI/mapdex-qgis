"""What a compose turn is offering to run, in the words a person decides with.

The desktop panel could not run a plan. It rendered the reply, the steps that
had already happened, a survey drawing and its own companion actions, and a
compose turn that needed a Run arrived as a description of work with no way to
start it. Every server capability that produces a Run - a traverse, a resection,
a geoid height, validating a layer - was understood, planned, and then sat
there.

This module is the first half of closing that: it turns a compose response into
an offer, or into nothing, without touching Qt or the network, so the decision
can be tested. Three rules carry it.

A plan is only offerable WITH its hash. `docs/MEMORY.md` says a confirmed plan
is an exact plan: the server returns the complete configured DAG plus a
`plan_hash`, and `POST /v1/runs` verifies that hash before executing. A plan
with no hash cannot be submitted unchanged, and rebuilding one from tool names
is the thing that rule exists to forbid, so a hashless plan is not an offer.

A step is named by its title. The server already resolves the manifest title;
an executor string like `worker:convert@2` is developer data and hard rule 22
keeps it out of an end-user surface. A step carrying no title falls back to a
cleaned operation name rather than its raw id.

An absent cost is stated as unknown, never as zero. A person deciding whether
to spend credits on a 300-sheet archive is worse off being told it is free than
being told nobody measured it.
"""

from __future__ import annotations

from typing import Any


# A turn that produced these modes has already acted or already answered.
# Offering to run something on top of an answer the user already has is how a
# panel talks somebody into paying twice for the same sentence.
_ALREADY_ANSWERED = frozenset({
    "direct_answer",
    "direct_ui_command",
    "contextual_observation",
    "lightweight_action",
})


def _text(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit]


def _step_label(step: dict[str, Any]) -> str:
    """The step's own title, or a readable fallback - never a tool id."""
    title = _text(step.get("title"), 120)
    if title:
        return title
    # `name` is the plan-local step name (`step_1`, `reproject`), which is
    # closer to language than `type` (`worker:reproject@2`) and carries no
    # executor family or version.
    name = _text(step.get("name"), 120)
    if name:
        return name.replace("_", " ").strip()
    return "unnamed step"


def plan_offer(response: Any) -> dict[str, Any] | None:
    """Return what this turn offers to run, or None when it offers nothing.

    The caller renders the result and asks the person. Nothing here decides to
    run anything: a function that both describes an offer and accepts it is one
    the panel cannot present without committing.
    """
    if not isinstance(response, dict):
        return None
    if _text(response.get("mode"), 64) in _ALREADY_ANSWERED:
        return None

    plan = response.get("plan")
    if not isinstance(plan, dict):
        return None
    plan_hash = _text(response.get("plan_hash"), 128)
    if not plan_hash:
        # Refusing here rather than submitting the plan alone: without the hash
        # the server cannot verify that what runs is what was shown, which is
        # the whole guarantee the confirmation is worth anything for.
        return None

    raw_steps = plan.get("steps")
    steps = [_step_label(step) for step in raw_steps if isinstance(step, dict)] \
        if isinstance(raw_steps, list) else []
    if not steps:
        # A plan with no steps runs nothing. Offering it would ask somebody to
        # approve an empty act and then report a completed run.
        return None

    estimate = response.get("estimate")
    estimate = estimate if isinstance(estimate, dict) else {}
    credits = estimate.get("estimated_credits")
    seconds = estimate.get("estimated_duration_seconds")
    seconds = seconds if isinstance(seconds, dict) else {}

    return {
        "plan": plan,
        "plan_hash": plan_hash,
        "steps": steps,
        # None means nobody measured it. Zero means free, and a free run is a
        # real thing here: a deterministic georeference apply costs 0 credits.
        "credits": credits if isinstance(credits, int) and not isinstance(credits, bool) else None,
        "seconds_min": seconds.get("min") if isinstance(seconds.get("min"), int) else None,
        "seconds_max": seconds.get("max") if isinstance(seconds.get("max"), int) else None,
        "inputs": [_text(item, 120) for item in estimate.get("input_summary") or []
                   if _text(item, 120)],
        "outputs": [_text(item, 120) for item in estimate.get("output_summary") or []
                    if _text(item, 120)],
        "thread_id": _text(response.get("thread_id"), 128),
    }


def offer_prompt(offer: dict[str, Any]) -> str:
    """The sentence the confirmation dialog asks.

    It states the work, then what it costs, because the cost is the part a
    person is being asked to accept and a dialog that buries it under a step
    list is asking for a click rather than a decision.
    """
    if not isinstance(offer, dict):
        return ""
    steps = offer.get("steps") or []
    lines = ["Mapdex will run this in your workspace:", ""]
    lines.extend("  {}. {}".format(index, step) for index, step in enumerate(steps, 1))
    lines.append("")

    credits = offer.get("credits")
    if credits is None:
        lines.append("Cost: not estimated for this plan.")
    elif credits == 0:
        lines.append("Cost: no credits.")
    else:
        lines.append("Cost: {} credit{}.".format(credits, "" if credits == 1 else "s"))

    low, high = offer.get("seconds_min"), offer.get("seconds_max")
    if isinstance(low, int) and isinstance(high, int):
        lines.append("Expected to take {} to {} seconds.".format(low, high))

    outputs = offer.get("outputs") or []
    if outputs:
        lines.append("Produces: {}.".format(", ".join(outputs)))
    return "\n".join(lines)


# Terminal states a plan run can reach. `needs_review` is terminal for the
# desktop and NOT a failure: the work exists and a person has to look at it in
# the browser, so reporting it as an error would send somebody to debug a run
# that did what it was asked.
_TERMINAL = frozenset({"completed", "succeeded", "failed", "cancelled", "needs_review"})


def plan_run_report(run: Any) -> dict[str, Any]:
    """What to tell the user about a plan run, and whether to stop polling.

    Kept beside the offer because they are two halves of one promise: the panel
    said what it would run, and this says what happened. A run whose state the
    desktop does not recognise is reported as still running rather than as
    finished, because a poll that stops early leaves a person believing a run
    ended when it is still spending their credits.
    """
    from .results import collect_layer_imports, run_state

    if not isinstance(run, dict):
        return {"terminal": False, "state": "", "message": "", "layers": []}
    state = run_state(run)
    terminal = state in _TERMINAL
    if state in {"completed", "succeeded"}:
        message = "Done."
    elif state == "needs_review":
        # Not "finished". A run in this state has stopped in front of a
        # decision - which of several places was meant, or an approval before a
        # step that changes risk - and calling that finished-but-unapproved
        # made ordinary data acquisition read as though somebody had to go and
        # audit it. Any result it did produce is still imported below; what is
        # outstanding is an answer, not an inspection.
        message = ("That run stopped and is waiting for a decision. "
                   "Open it in Mapdex to answer; anything it already produced is below.")
    elif state == "failed":
        message = "That run failed in Mapdex."
    elif state == "cancelled":
        message = "That run was cancelled."
    else:
        message = ""
    return {
        "terminal": terminal,
        "state": state,
        "message": message,
        "layers": (collect_layer_imports(run)
                   if terminal and state in {"completed", "succeeded", "needs_review"}
                   else []),
    }
