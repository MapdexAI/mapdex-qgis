"""The desktop half of a multi-step objective.

A hosted turn was one request that produced one action, so "select the roads,
buffer them, find the parcels inside and total their area" was four messages.
The server can now say "run this, then tell me what happened"; this module is
the rule for whether the client should, and what it should say.

Free of Qt and of QGIS on purpose: this is the decision, and the decision is
worth testing without a running application. `plugin.py` performs it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# The client's own ceiling on round trips.
#
# The server carries a budget and states it, and this exists anyway: a client
# that trusts a peer to end a loop has no loop bound of its own, and the peer is
# reachable over a network. It is deliberately one above the server's eight, so
# in normal operation the server's honest "I ran out of steps" is what the user
# sees, and this only fires when the server never said anything at all.
MAX_CLIENT_ROUND_TRIPS = 9


def continuation_expected(response: Any) -> bool:
    """Does the server want to be told what happened?"""
    if not isinstance(response, Mapping):
        return False
    continuation = response.get("continuation")
    if not isinstance(continuation, Mapping):
        return False
    return bool(continuation.get("expected"))


def continuation_budget(response: Any) -> tuple[int, int]:
    """(used, max) as the server reported them, or (0, 0)."""
    if not isinstance(response, Mapping):
        return 0, 0
    continuation = response.get("continuation")
    if not isinstance(continuation, Mapping):
        return 0, 0
    return _int(continuation.get("steps_used")), _int(continuation.get("steps_max"))


def action_result(capability: str, ok: bool, summary: str = "", error: str = "",
                  action_id: str = "", result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One reported outcome, in the shape the server accepts.

    `summary` is the sentence the desktop produced from its own measurement. On
    this surface that IS the observation: an analytics capability runs inside
    QGIS against the real layer, so the desktop is the measuring instrument.
    """
    entry: dict[str, Any] = {"capability": str(capability or ""), "ok": bool(ok)}
    if action_id:
        entry["action_id"] = str(action_id)
    if summary:
        entry["summary"] = str(summary)[:400]
    if error:
        entry["error"] = str(error)[:400]
    if isinstance(result, Mapping) and result:
        entry["result"] = _bounded_result(result)
    return entry


def should_continue(response: Any, results: Sequence[Mapping[str, Any]], round_trips: int) -> bool:
    """Whether to send another turn carrying these results.

    Four conditions, and each blocks a different way of looping forever:
    the server has to have asked; something has to have run, or there is nothing
    to report; the client's own ceiling has to hold even if the server stops
    answering sensibly; and a reported failure ends the objective, because the
    desktop already said why and another attempt at a refused question is not an
    answer.
    """
    if not continuation_expected(response):
        return False
    if not results:
        return False
    if round_trips >= MAX_CLIENT_ROUND_TRIPS:
        return False
    for result in results:
        if not result.get("ok"):
            return False
    return True


def _bounded_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the shape small enough to travel and to read.

    The server bounds this again on arrival. Doing it here as well keeps a
    large QGIS result from being sent at all, which is cheaper than sending it
    and having it trimmed.
    """
    bounded: dict[str, Any] = {}
    for key in sorted(str(name) for name in result):
        if len(bounded) >= 12:
            break
        value = result[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 200:
                value = value[:200] + "..."
            bounded[key] = value
        elif isinstance(value, Sequence):
            bounded[key] = "[{} items]".format(len(value))
        elif isinstance(value, Mapping):
            bounded[key] = "{{{} fields}}".format(len(value))
    return bounded


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
