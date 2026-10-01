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


# -- Label legibility ---------------------------------------------------------
#
# The single cartographic decision that matters most and that no default makes
# for you. QGIS labels a layer with plain text and no halo, which is legible on
# a white page and unreadable the moment the map has a satellite basemap, a
# hillshade, or a choropleth under it - which is to say, on every map anybody
# publishes. Adding one is not a preference; it is the difference between a map
# and a screenshot.

# Placements a caller may ask for. Closed, and each is the sensible arrangement
# for one geometry kind: a road label follows the road, a parcel label sits
# inside the parcel, a point label sits beside the point.
LABEL_PLACEMENTS = ("around_point", "over_point", "along_line", "horizontal", "inside_polygon")

GEOMETRY_DEFAULT_PLACEMENT = {
    "point": "around_point",
    "line": "along_line",
    "polygon": "horizontal",
}

# Millimetres. Under about 0.2 the halo stops separating the glyph from what is
# behind it; over about 3 it becomes a blob with a letter in it.
MIN_HALO_MM = 0.2
MAX_HALO_MM = 3.0
DEFAULT_HALO_MM = 1.0

DEFAULT_LABEL_COLOR = "#1A1A1A"


def _channel(value: float) -> float:
    """One sRGB channel, linearised. The step every luminance formula needs."""
    value = value / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a hex colour, 0 for black and 1 for white.

    The coefficients are the standard ones and are not interchangeable with a
    simple average: the eye is far more sensitive to green than to blue, so
    averaging calls pure blue a mid-tone and puts a white halo on it.
    """
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError("a colour must be a hex triple, got {!r}".format(color))
    try:
        red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError("a colour must be a hex triple, got {!r}".format(color)) from None
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrasting_halo(color: str) -> str:
    """The halo colour for this text, computed rather than asked for.

    Dark text takes a white halo and light text a near-black one. This is the
    decision a cartographer makes without thinking and that a form never asks,
    so making it here is most of the value: a caller who says nothing about the
    halo still gets a legible label.

    The threshold is 0.5 rather than a WCAG contrast ratio because both
    candidates are fixed - the question is only which of two is further away,
    and luminance answers it directly.
    """
    return "#FFFFFF" if relative_luminance(color) < 0.5 else "#1A1A1A"


def label_settings(
    field: str,
    size: float = 9.0,
    color: str = DEFAULT_LABEL_COLOR,
    halo: bool = True,
    halo_size_mm: Any = None,
    halo_color: str = "",
    placement: str = "",
    geometry: str = "",
) -> dict[str, Any]:
    """A complete, legible label specification from what little a caller said.

    Pure, so both surfaces can be tested without a canvas and can be checked
    against each other.

    The halo defaults to ON. That reverses QGIS's own default deliberately: its
    default produces text that disappears over any basemap, and a person asking
    to label a layer is asking to be able to read the labels.
    """
    if not str(field or "").strip():
        raise ValueError("a label needs a field to read")
    size = max(4.0, min(48.0, float(size)))
    color = str(color or DEFAULT_LABEL_COLOR).strip() or DEFAULT_LABEL_COLOR
    relative_luminance(color)  # refuses a colour that is not a hex triple

    resolved_placement = str(placement or "").strip().lower()
    if resolved_placement and resolved_placement not in LABEL_PLACEMENTS:
        raise ValueError(
            "unknown label placement {!r}; available: {}".format(
                placement, ", ".join(LABEL_PLACEMENTS)))
    if not resolved_placement:
        # From the geometry when the caller did not say. A road label sitting
        # horizontally beside the road instead of following it is the second
        # most common way a map reads as automatic.
        resolved_placement = GEOMETRY_DEFAULT_PLACEMENT.get(
            str(geometry or "").strip().lower(), "around_point")

    halo_mm = DEFAULT_HALO_MM if halo_size_mm is None else float(halo_size_mm)
    halo_mm = max(MIN_HALO_MM, min(MAX_HALO_MM, halo_mm))
    resolved_halo_color = str(halo_color or "").strip() or contrasting_halo(color)
    relative_luminance(resolved_halo_color)

    return {
        "field": str(field).strip(),
        "size": size,
        "color": color,
        "halo": bool(halo),
        "halo_size_mm": halo_mm if halo else 0.0,
        "halo_color": resolved_halo_color,
        "placement": resolved_placement,
    }


# -- Scale-dependent visibility ----------------------------------------------
#
# A layer drawn at every zoom is a layer that turns a city map into a smear of
# labels. Naming the range it belongs in is what separates a map somebody
# designed from a stack of everything they had.

# Scale denominators: 1:N. The bounds are the practical ends of a web map, not
# a limit of the arithmetic.
MIN_SCALE_DENOMINATOR = 1
MAX_SCALE_DENOMINATOR = 500000000


def scale_range(minimum: Any, maximum: Any) -> dict[str, Any]:
    """The zoom band a layer is drawn in, as scale DENOMINATORS.

    Named the way a cartographer names them - 1:1000 is a LARGER scale than
    1:100000 and shows less ground - so `minimum` here is the smallest
    denominator, meaning the most zoomed in. Getting that backwards hides the
    layer at exactly the zooms it was meant for, and it looks like nothing
    happened.
    """
    try:
        low = float(minimum)
        high = float(maximum)
    except (TypeError, ValueError):
        raise ValueError("a scale range needs two numbers") from None
    if low <= 0 or high <= 0:
        raise ValueError("a scale denominator is greater than zero")
    if low > high:
        # Swapping silently would be guessing at intent. A person who typed
        # them the other way round meant the same band and should be told, so
        # the next one they type is right.
        raise ValueError(
            "the first number is the most zoomed-in end, so it must be smaller: "
            "1:{:g} to 1:{:g} is inverted".format(low, high))
    if low < MIN_SCALE_DENOMINATOR or high > MAX_SCALE_DENOMINATOR:
        raise ValueError(
            "a scale denominator must be between {} and {}".format(
                MIN_SCALE_DENOMINATOR, MAX_SCALE_DENOMINATOR))
    return {"minimum_scale": low, "maximum_scale": high}


def describe_scale_range(band: Mapping[str, Any]) -> str:
    """One line a person can check the band against."""
    return "Drawn between 1:{:,.0f} and 1:{:,.0f}.".format(
        band["minimum_scale"], band["maximum_scale"])


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
