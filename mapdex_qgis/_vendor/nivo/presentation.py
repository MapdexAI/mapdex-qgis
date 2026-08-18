# SPDX-License-Identifier: MIT
"""Turn computed analysis into map visualization and into an honest report.

Two jobs, both pure so they can be tested without QGIS:

* :func:`visualization_for` converts an analytics result into a *specification*
  a renderer can apply - class edges, colours, legend labels, the feature ids to
  select, the extent to focus. This is the bridge that stops an analysis ending
  as a paragraph. The QGIS runtime consumes the spec; it never asks the model
  what the map should look like.

* :func:`build_report` assembles a report strictly from measured observations.
  A section can only exist if a capability produced it, and a check that did not
  run is printed as *not checked* rather than silently scoring full marks. That
  rule is inherited from the validation-report work: full marks are a claim, and
  a claim needs a measurement behind it.

Colour ramps are declared as ordered hex lists rather than resolved through a
QGIS style name, so a legend rendered here and a legend rendered on the canvas
always agree, and a missing ramp on a user's install cannot silently change the
meaning of a map.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

MAX_LEGEND_ENTRIES = 12
MAX_REPORT_SECTIONS = 24

# Colour-blind-safe sequential and categorical ramps. Sequential ramps are
# ordered light -> dark so "more" always reads as "darker" on the page and on
# the canvas; a reversed ramp inverts the meaning of every choropleth.
RAMPS: dict[str, list[str]] = {
    "Viridis": ["#440154", "#414487", "#2A788E", "#22A884", "#7AD151", "#FDE725"],
    "Blues": ["#EFF3FF", "#C6DBEF", "#9ECAE1", "#6BAED6", "#3182BD", "#08519C"],
    "Oranges": ["#FEEDDE", "#FDD0A2", "#FDAE6B", "#FD8D3C", "#E6550D", "#A63603"],
    "Spectral": ["#D53E4F", "#FC8D59", "#FEE08B", "#E6F598", "#99D594", "#3288BD"],
    # Okabe-Ito: the standard categorical palette that survives every common
    # form of colour vision deficiency.
    "Categorical": [
        "#0072B2", "#E69F00", "#009E73", "#CC79A7",
        "#56B4E9", "#D55E00", "#F0E442", "#000000",
    ],
}
DEFAULT_SEQUENTIAL = "Viridis"
DEFAULT_CATEGORICAL = "Categorical"


def _interpolate(ramp: Sequence[str], count: int) -> list[str]:
    """Sample ``count`` evenly spaced colours from a ramp."""
    if count <= 0:
        return []
    if count == 1:
        return [ramp[len(ramp) // 2]]
    colors = []
    for index in range(count):
        position = index * (len(ramp) - 1) / float(count - 1)
        colors.append(ramp[int(round(position))])
    return colors


def ramp_colors(name: str, count: int, categorical: bool = False) -> list[str]:
    ramp = RAMPS.get(str(name or ""), RAMPS[DEFAULT_CATEGORICAL if categorical else DEFAULT_SEQUENTIAL])
    if categorical:
        # Cycle rather than interpolate: adjacent categories must stay
        # distinguishable, which interpolation destroys past a few classes.
        return [ramp[index % len(ramp)] for index in range(count)]
    return _interpolate(ramp, count)


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e15:
        return "{:,}".format(int(number))
    return "{:,.2f}".format(number)


def graduated_visualization(
    layer_id: str,
    field: str,
    breaks_result: Mapping[str, Any],
    ramp: str = DEFAULT_SEQUENTIAL,
) -> dict[str, Any]:
    """A graduated renderer spec with a legend that states its own method."""
    edges = list(breaks_result.get("breaks") or [])
    if len(edges) < 2:
        return {"kind": "none", "reason": "not_enough_distinct_values"}
    classes = []
    colors = ramp_colors(ramp, len(edges) - 1)
    for index in range(len(edges) - 1):
        classes.append({
            "lower": edges[index],
            "upper": edges[index + 1],
            "color": colors[index],
            "label": "{} – {}".format(_format_number(edges[index]), _format_number(edges[index + 1])),
        })
    return {
        "kind": "graduated",
        "layer_id": layer_id,
        "field": field,
        "method": breaks_result.get("method"),
        "classes": classes,
        # The legend says how the classes were derived, so a reader is never
        # left guessing whether the breaks are quantiles or equal intervals.
        "legend_note": "{} classes, {} breaks".format(len(classes), breaks_result.get("method")),
        "reversible": True,
    }


def categorized_visualization(
    layer_id: str,
    field: str,
    categorical_result: Mapping[str, Any],
    ramp: str = DEFAULT_CATEGORICAL,
) -> dict[str, Any]:
    """A categorized renderer spec, bounded so a legend stays readable."""
    categories = list(categorical_result.get("categories") or [])[:MAX_LEGEND_ENTRIES]
    if not categories:
        return {"kind": "none", "reason": "no_categories"}
    colors = ramp_colors(ramp, len(categories), categorical=True)
    entries = [
        {
            "value": category.get("value"),
            "color": colors[index],
            "label": "{} ({})".format(category.get("value"), _format_number(category.get("count"))),
        }
        for index, category in enumerate(categories)
    ]
    return {
        "kind": "categorized",
        "layer_id": layer_id,
        "field": field,
        "categories": entries,
        "other_hidden": bool(categorical_result.get("truncated"))
        or int(categorical_result.get("distinct") or 0) > len(entries),
        "reversible": True,
    }


def selection_visualization(layer_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """Turn ranked or flagged features into an actual map selection."""
    ids = [str(item) for item in (result.get("feature_ids") or []) if item is not None]
    if not ids:
        return {"kind": "none", "reason": "no_matching_features"}
    return {
        "kind": "selection",
        "layer_id": layer_id,
        "feature_ids": ids,
        "zoom_to_selection": True,
        "reversible": True,
        "matched": len(ids),
    }


def visualization_for(
    analysis: Mapping[str, Any],
    layer_id: str,
    field: str = "",
    ramp: str = "",
) -> dict[str, Any]:
    """Choose the right map expression for a given analytical result.

    Deterministic on purpose: the same analysis always produces the same kind of
    map. Letting a model choose here would make two runs of one analysis look
    different for no measured reason.
    """
    kind = str(analysis.get("kind") or "")
    if kind == "breaks":
        return graduated_visualization(layer_id, field, analysis, ramp or DEFAULT_SEQUENTIAL)
    if kind == "categorical":
        return categorized_visualization(layer_id, field, analysis, ramp or DEFAULT_CATEGORICAL)
    if kind in {"top_n", "outliers"}:
        return selection_visualization(layer_id, analysis)
    if kind == "group_aggregate":
        # A group-by is a choropleth once its values are joined back to the
        # polygons; the caller supplies the join, we supply the classes.
        values = [item.get("value") for item in analysis.get("groups") or [] if item.get("value") is not None]
        if len(values) < 2:
            return {"kind": "none", "reason": "not_enough_groups"}
        return {
            "kind": "group_choropleth",
            "layer_id": layer_id,
            "field": analysis.get("group_field"),
            "value_field": analysis.get("value_field") or analysis.get("statistic"),
            "groups": [
                {"group": item.get("group"), "value": item.get("value")}
                for item in analysis.get("groups") or []
            ][:MAX_LEGEND_ENTRIES * 4],
            "reversible": True,
        }
    if kind == "density":
        values = {key: value for key, value in (analysis.get("values") or {}).items() if value is not None}
        if not values:
            return {"kind": "none", "reason": "no_density_values"}
        return {
            "kind": "group_choropleth",
            "layer_id": layer_id,
            "field": field,
            "value_field": "density",
            "unit": analysis.get("unit"),
            "groups": [{"group": key, "value": value} for key, value in values.items()],
            "reversible": True,
        }
    return {"kind": "none", "reason": "not_spatially_expressible"}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _section_for(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    """Render one measured observation as a report section, or nothing."""
    capability = str(evidence.get("capability") or "")
    result = evidence.get("result")
    if not evidence.get("ok") or result is None:
        return None
    if not isinstance(result, Mapping):
        return {"title": capability, "kind": "note", "body": str(result)[:2000], "capability": capability}
    kind = str(result.get("kind") or "")
    if kind == "numeric":
        rows = [
            (label, _format_number(result.get(key)))
            for label, key in (
                ("Features", "count"), ("Values used", "usable"), ("Missing values", "nulls"),
                ("Minimum", "min"), ("Maximum", "max"), ("Mean", "mean"), ("Median", "median"),
                ("Standard deviation", "stddev"),
            )
            if result.get(key) is not None
        ]
        return {"title": "Distribution", "kind": "metrics", "rows": rows, "capability": capability}
    if kind == "categorical":
        rows = [
            (str(item.get("value")), "{} ({:.1f}%)".format(item.get("count"), item.get("percent") or 0.0))
            for item in (result.get("categories") or [])[:MAX_LEGEND_ENTRIES]
        ]
        note = ""
        if result.get("truncated"):
            note = "Showing the {} most common of {} distinct values.".format(len(rows), result.get("distinct"))
        return {"title": "Categories", "kind": "metrics", "rows": rows, "note": note, "capability": capability}
    if kind == "group_aggregate":
        rows = [
            (str(item.get("group")), _format_number(item.get("value")))
            for item in (result.get("groups") or [])[:MAX_LEGEND_ENTRIES]
        ]
        note = ""
        if result.get("skipped_null_values"):
            # State the exclusion; an average over fewer rows than the reader
            # assumes is exactly how a report becomes quietly wrong.
            note = "{} features were excluded because the value was missing.".format(
                result.get("skipped_null_values")
            )
        return {"title": "Grouped totals", "kind": "metrics", "rows": rows, "note": note, "capability": capability}
    if kind == "outliers":
        if not result.get("matched"):
            reason = result.get("reason") or "none found"
            return {"title": "Outliers", "kind": "note",
                    "body": "No outliers were flagged ({}).".format(reason), "capability": capability}
        return {
            "title": "Outliers",
            "kind": "metrics",
            "rows": [(str(row.get("id")), _format_number(row.get("value"))) for row in (result.get("rows") or [])[:10]],
            "note": "Rule: {}. Bounds {} to {}.".format(
                result.get("rule"), _format_number(result.get("lower_bound")),
                _format_number(result.get("upper_bound"))),
            "capability": capability,
        }
    if kind == "top_n":
        return {
            "title": "Ranked features",
            "kind": "metrics",
            "rows": [(str(row.get("id")), _format_number(row.get("value"))) for row in (result.get("rows") or [])[:10]],
            "note": "Ranked by {} across {} features with a value.".format(
                result.get("field"), result.get("considered")),
            "capability": capability,
        }
    if kind == "points_in_polygons":
        rows = sorted(result.get("counts", {}).items(), key=lambda item: -item[1])[:MAX_LEGEND_ENTRIES]
        note = ""
        if result.get("unmatched"):
            note = "{} of {} features fell outside every polygon.".format(
                result.get("unmatched"), result.get("points"))
        return {"title": "Features per area", "kind": "metrics",
                "rows": [(key, _format_number(value)) for key, value in rows],
                "note": note, "capability": capability}
    if kind == "density":
        rows = sorted(
            ((key, value) for key, value in (result.get("values") or {}).items() if value is not None),
            key=lambda item: -item[1],
        )[:MAX_LEGEND_ENTRIES]
        return {"title": "Density ({})".format(result.get("unit") or ""), "kind": "metrics",
                "rows": [(key, "{:,.2f}".format(value)) for key, value in rows], "capability": capability}
    if kind == "comparison":
        delta = result.get("delta") or {}
        rows = [
            (label, _format_number(delta.get(key)))
            for label, key in (("Mean difference", "mean"), ("Median difference", "median"),
                               ("Feature count difference", "count"))
            if delta.get(key) is not None
        ]
        return {"title": "Comparison", "kind": "metrics", "rows": rows, "capability": capability}
    return None


def build_report(
    title: str,
    objective: str,
    evidence: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a report from measured observations only.

    Failed and unmeasured steps are not dropped in silence - they are listed as
    "not checked", because a report that quietly omits what it could not do
    reads as a clean bill of health.
    """
    sections = []
    not_checked = []
    for item in evidence:
        section = _section_for(item)
        if section is not None:
            sections.append(section)
        elif item.get("capability") and not str(item.get("capability", "")).startswith("("):
            not_checked.append({
                "capability": item.get("capability"),
                "reason": item.get("error") or "produced no measurable result",
            })
    return {
        "title": str(title or "Analysis report").strip()[:200],
        "objective": str(objective or "").strip()[:1000],
        "context": dict(context or {}),
        "sections": sections[:MAX_REPORT_SECTIONS],
        "not_checked": not_checked[:MAX_REPORT_SECTIONS],
        "measured_sections": len(sections),
        # An empty report is a real outcome and must say so, rather than
        # rendering an impressive-looking shell around nothing.
        "empty": not sections,
    }


def render_report_text(report: Mapping[str, Any]) -> str:
    """Plain-text rendering for the transcript and for a saved artefact."""
    lines = [str(report.get("title") or "Analysis report"), "=" * len(str(report.get("title") or "Analysis report"))]
    if report.get("objective"):
        lines += ["", "Question: {}".format(report["objective"])]
    if report.get("empty"):
        lines += ["", "Nothing was measured for this report, so it states no findings."]
    for section in report.get("sections") or []:
        lines += ["", str(section.get("title") or "Section")]
        if section.get("kind") == "metrics":
            for label, value in section.get("rows") or []:
                lines.append("  {:<28} {}".format(str(label)[:28], value))
        elif section.get("body"):
            lines.append("  " + str(section["body"]))
        if section.get("note"):
            lines.append("  Note: {}".format(section["note"]))
    if report.get("not_checked"):
        lines += ["", "Not checked"]
        for item in report["not_checked"]:
            lines.append("  {} - {}".format(item.get("capability"), item.get("reason")))
    return "\n".join(lines)
