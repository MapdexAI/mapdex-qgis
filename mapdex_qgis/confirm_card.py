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

from typing import Any, Mapping

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
