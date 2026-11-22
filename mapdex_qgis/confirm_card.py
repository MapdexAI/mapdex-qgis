"""What a "Needs your OK" card says before a Processing operation runs.

The confirmation used to be a modal dialog reading "Target layer: layer_3f2a /
Operation: buffer", which covered the conversation it belonged to and named an
id the person never sees. The card stays in the transcript, under the question
that caused it, and states the three things a person checks before saying yes:
what goes in, with which setting, and what comes out.

Every value here is measured or taken from the request, never inferred: the
layer's own name and feature count, the distance the request carries, and the
coordinate system the metric plan (`processing_units`) will measure it in.

No Qt and no QGIS.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from ._vendor.nivo.processing import operation_label

# Operations whose result is a new layer, which is what the confirm button says.
_CHECKS = {"validate", "check_validity"}

# Distance-like parameters, in metres unless the request says otherwise. Read
# from the request, never from text: a parameter this list does not know is
# simply not shown, rather than guessed at.
_DISTANCE_KEYS = ("distance", "distance_m", "buffer_distance", "tolerance", "radius")

_GENERIC_SUMMARIES = {
    "",
    "nivo prepared a qgis processing operation.",
    "nivo prepared a qgis action.",
}


def _noun(family: str, count: int) -> str:
    one = {"polygon": "polygon", "line": "line", "point": "point"}.get(family, "feature")
    return one if count == 1 else one + "s"


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return "{:,}".format(int(number))
    return "{:,.2f}".format(number).rstrip("0").rstrip(".")


def processing_card(
    action: Mapping[str, Any],
    layer: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The card for one Processing confirmation.

    ``layer``: {"name", "count", "family"} measured from the target layer.
    ``plan``: what `plan_metric_operation` decided, when it was computed.
    """
    params = dict((action or {}).get("params") or {})
    operation = str(params.get("operation") or "")
    label = operation_label(operation)
    layer = dict(layer or {})
    name = str(layer.get("name") or "the active layer")
    rows: list[tuple[str, str]] = []

    count = layer.get("count")
    if isinstance(count, int) and count >= 0:
        rows.append(("Input", "{} · {} {}".format(name, _number(count), _noun(str(layer.get("family") or ""), count))))
    else:
        rows.append(("Input", name))

    for key in _DISTANCE_KEYS:
        if key in params and params[key] not in (None, ""):
            value = "{} m".format(_number(params[key]))
            plan = plan or {}
            if plan.get("strategy") == "reproject" and plan.get("crs"):
                value += ", measured in {}".format(plan["crs"])
            rows.append(("Distance" if key != "tolerance" else "Tolerance", value))
            break

    check = operation in _CHECKS
    rows.append(("Result", "A list of any invalid shapes" if check else "A new layer, {} {}".format(name, label)))

    summary = str((action or {}).get("summary") or "").strip()
    if summary.lower() in _GENERIC_SUMMARIES:
        summary = ""
    title = summary or "Run the {} on {}?".format(label, name)
    if title and not title.endswith("?") and not summary:
        title += "?"
    return {
        "title": title,
        "rows": rows,
        "confirm": "Run check" if check else "Create layer",
        "cancel": "Cancel",
    }


# What a card says once it has been answered. "Approved, running in QGIS" used
# to stay on the card forever, including when nothing ran because the layer had
# gone or the algorithm was missing: an approval is not an outcome. Every
# answered card now ends in one of these, each a solid icon and a bold word
# (theme.TONES) with the reason or the result beside it.
_OUTCOMES = {
    "pending": ("warn", "Needs your OK"),
    "running": ("working", "Running"),
    "done": ("ok", "Done"),
    "empty": ("warn", "No result"),
    "not_run": ("err", "Not run"),
    "failed": ("err", "Failed"),
    "cancelled": ("neutral", "Cancelled"),
}


def outcome_status(state: str, detail: str = "") -> tuple[str, str, str]:
    """(tone, word, detail) for a card in this state.

    A card saved by an earlier build in the old "approved" state carries no
    outcome, and says so rather than claiming it is still running.
    """
    state = str(state or "pending")
    if state == "approved":
        return ("neutral", "Approved", detail or "the outcome was not recorded")
    tone, word = _OUTCOMES.get(state, _OUTCOMES["pending"])
    if not detail:
        detail = {"running": "in QGIS", "cancelled": "nothing was changed"}.get(state, "")
    return (tone, word, str(detail))


def done_detail(output_name: str, produced: int | None) -> str:
    """The result of a finished operation, for the card's status line."""
    if not output_name:
        return "nothing was produced"
    if produced is None:
        return "added {}".format(output_name)
    if produced == 0:
        return "{} is empty".format(output_name)
    return "added {}, {:,} {}".format(output_name, produced, "feature" if produced == 1 else "features")


def check_outcome(has_report: bool, invalid: int | None) -> tuple[str, str]:
    """(state, detail) for a geometry check that QGIS reported as finished.

    Done only when there is something to say: a count of invalid shapes, or a
    report layer whose count could not be read. With no report layer at all the
    check finished but nothing can be said about the shapes, so it is No result,
    never Done.
    """
    if not has_report:
        return ("empty", "the check finished but returned no report, so nothing is known about the shapes")
    if invalid is None:
        return ("done", "the report layer is in the project")
    if invalid == 0:
        return ("done", "no invalid shapes")
    return ("done", "{:,} invalid {}".format(invalid, "shape" if invalid == 1 else "shapes"))


def failure_outcome(error: BaseException, started: bool, layer_may_exist: bool) -> tuple[str, str]:
    """(state, detail) when something raised during a confirmed run.

    Before QGIS accepted the task nothing ran (Not run); after, it is Failed.
    When a result layer may already be in the project the card says so, because
    the person will see it there. Never Done: an exception is not an outcome.
    """
    name = type(error).__name__
    detail = "an unexpected error ({}) stopped it{}; the Mapdex log has the details".format(
        name, "" if started else " before QGIS started")
    if layer_may_exist:
        detail += "; a result layer may already have been added to the project"
    return ("failed" if started else "not_run", detail)


class Settlement:
    """Guarantees that one confirmed run ends in a terminal state.

    Every exit of the run path used to settle the card by hand, and an exit
    nobody listed (an exception from a QGIS call, a task manager that refused
    the task) left the card reading "Running, in QGIS" for good. This object is
    the one place that decision lives:

    - ``settle`` records the first terminal state and ignores later ones;
    - ``guarding()`` settles Failed or Not run when the block raises, then
      re-raises, so the error still reaches the plugin's error boundary;
    - ``handed_over`` marks the task as accepted by QGIS, after which the
      callback owns the outcome; ``close_body`` settles Not run when the body
      returned without settling and without handing the task over;
    - ``close_callback`` settles Failed when the callback returned without an
      outcome.

    No Qt and no QGIS, so a test can drive every path.
    """

    def __init__(self, settle: Callable[[str, str], Any]):
        self._settle = settle
        self.state = ""
        self.handed_over = False
        self.layer_may_exist = False
        self.settle_error: BaseException | None = None

    @property
    def settled(self) -> bool:
        return bool(self.state)

    def settle(self, state: str, detail: str = "") -> None:
        if self.state:
            return
        self.state = state
        try:
            self._settle(state, detail)
        except Exception as error:  # noqa: BLE001 - the card's own failure must not hide the run's
            self.settle_error = error

    def fail(self, error: BaseException) -> None:
        self.settle(*failure_outcome(error, self.handed_over, self.layer_may_exist))

    @contextmanager
    def guarding(self) -> Iterator["Settlement"]:
        try:
            yield self
        except Exception as error:
            self.fail(error)
            raise

    def close_body(self) -> None:
        if not self.handed_over:
            self.settle("not_run", "the operation did not start")

    def close_callback(self) -> None:
        detail = "QGIS finished without reporting a result"
        if self.layer_may_exist:
            detail += "; a result layer may already have been added to the project"
        self.settle("failed", detail)
