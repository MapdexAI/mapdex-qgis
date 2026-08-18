# SPDX-License-Identifier: MIT
"""Deterministic statistical analytics for Nivo.

Nothing here imports QGIS or calls a model. Every number a Nivo answer states
about a dataset is produced by these functions from real values, so a model can
explain a result but can never invent one. Keeping the maths pure also makes it
testable against fixtures with known answers, which is the only way a claim like
"the mean parcel area is 5 210 m²" can be trusted.

Inputs are plain sequences of Python values; the QGIS and PostGIS adapters are
responsible for producing them. Values that are ``None`` are counted as nulls
and excluded from every statistic rather than coerced to zero - a null area is
not an area of zero, and treating it as one silently corrupts the mean.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

# Analytical results are bounded so a large layer can never produce an unbounded
# payload for the transcript, the model prompt, or a report.
MAX_CATEGORIES = 50
MAX_HISTOGRAM_BINS = 60
MAX_TOP_N = 500
MAX_CLASSES = 12
# Break computation is O(n*k) per class for Jenks; above this the sorted values
# are sampled evenly so a million-feature layer stays interactive.
JENKS_SAMPLE_CAP = 4000


def _finite(value: Any) -> float | None:
    """Return a usable float, or None for nulls, blanks and non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def numeric_values(values: Iterable[Any]) -> tuple[list[float], int]:
    """Split an attribute column into usable numbers and a null/invalid count."""
    numbers: list[float] = []
    nulls = 0
    for value in values:
        number = _finite(value)
        if number is None:
            nulls += 1
        else:
            numbers.append(number)
    return numbers, nulls


def quantile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation quantile (the R type-7 / numpy default definition)."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    fraction = min(1.0, max(0.0, float(fraction)))
    position = fraction * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def numeric_summary(values: Iterable[Any]) -> dict[str, Any]:
    """Full descriptive statistics for one numeric column.

    ``stddev``/``variance`` are the sample (n-1) statistics, which is what a GIS
    analyst comparing subsets expects; with a single value they are reported as
    None rather than zero, because one observation carries no spread.
    """
    numbers, nulls = numeric_values(values)
    if not numbers:
        return {"kind": "numeric", "count": 0, "nulls": nulls, "usable": 0}
    ordered = sorted(numbers)
    count = len(ordered)
    total = math.fsum(ordered)
    mean = total / count
    variance = None
    stddev = None
    if count > 1:
        variance = math.fsum((value - mean) ** 2 for value in ordered) / (count - 1)
        stddev = math.sqrt(variance)
    return {
        "kind": "numeric",
        "count": count + nulls,
        "usable": count,
        "nulls": nulls,
        "min": ordered[0],
        "max": ordered[-1],
        "sum": total,
        "mean": mean,
        "median": quantile(ordered, 0.5),
        "stddev": stddev,
        "variance": variance,
        "p25": quantile(ordered, 0.25),
        "p75": quantile(ordered, 0.75),
        "p90": quantile(ordered, 0.90),
        "iqr": quantile(ordered, 0.75) - quantile(ordered, 0.25),
        "distinct": len(set(ordered)),
    }


def histogram(values: Iterable[Any], bins: int = 12) -> dict[str, Any]:
    """Equal-width histogram over the finite values of a column."""
    bins = max(1, min(int(bins or 12), MAX_HISTOGRAM_BINS))
    numbers, nulls = numeric_values(values)
    if not numbers:
        return {"kind": "histogram", "bins": [], "nulls": nulls}
    low = min(numbers)
    high = max(numbers)
    if high <= low:
        return {
            "kind": "histogram",
            "nulls": nulls,
            "bins": [{"min": low, "max": high, "count": len(numbers)}],
        }
    width = (high - low) / bins
    counts = [0] * bins
    for number in numbers:
        index = int((number - low) / width)
        # The maximum value lands exactly on the upper edge; keep it in the last
        # bin instead of creating a spurious empty bin beyond the range.
        counts[min(index, bins - 1)] += 1
    return {
        "kind": "histogram",
        "nulls": nulls,
        "bins": [
            {"min": low + index * width, "max": low + (index + 1) * width, "count": count}
            for index, count in enumerate(counts)
        ],
    }


def categorical_summary(values: Iterable[Any], limit: int = MAX_CATEGORIES) -> dict[str, Any]:
    """Frequency table for one categorical column, most frequent first.

    Ties are broken by the category label so the same dataset always produces the
    same ordering; an unstable order would make two runs of the same analysis
    disagree and would make a categorized renderer reshuffle its colours.
    """
    limit = max(1, min(int(limit or MAX_CATEGORIES), MAX_CATEGORIES))
    counts: dict[str, int] = {}
    # A null and an empty string are two different facts about a dataset and
    # were being counted as one. "Nobody recorded a land use here" and "somebody
    # recorded that it has none" call for different work: the first is a gap to
    # chase, the second is data. Merging them reported three categories where
    # the fixture deliberately holds five, and a reviewer who trusts the number
    # of buckets is told a decision was made on their behalf.
    #
    # They stay together in `nulls` for the caller that only wants "how many
    # values could not be used", so nothing that reads that number changes.
    nulls = 0
    missing = 0
    blank = 0
    total = 0
    for value in values:
        total += 1
        if value is None:
            nulls += 1
            missing += 1
            continue
        if isinstance(value, str) and not value.strip():
            nulls += 1
            blank += 1
            continue
        key = str(value).strip()
        counts[key] = counts.get(key, 0) + 1
    usable = total - nulls
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    categories = [
        {
            "value": key,
            "count": count,
            "percent": (count * 100.0 / usable) if usable else 0.0,
        }
        for key, count in ordered[:limit]
    ]
    return {
        "kind": "categorical",
        "count": total,
        "usable": usable,
        "nulls": nulls,
        # Reported separately so the reader can tell a gap from a recorded
        # absence. `nulls` remains their sum for callers that only need the
        # count of values that could not be categorised.
        "missing": missing,
        "blank": blank,
        "distinct": len(counts),
        "truncated": len(counts) > len(categories),
        "categories": categories,
    }


def top_n(
    records: Sequence[Mapping[str, Any]],
    field: str,
    limit: int = 10,
    ascending: bool = False,
    id_key: str = "id",
) -> dict[str, Any]:
    """Rank records by a numeric field and return their identifiers.

    The identifiers are what makes "show me the 20 largest parcels" a map
    operation rather than a paragraph: the caller feeds them straight into a
    feature selection.
    """
    limit = max(1, min(int(limit or 10), MAX_TOP_N))
    scored = []
    for record in records:
        number = _finite(record.get(field))
        if number is None:
            continue
        scored.append((number, record.get(id_key)))
    scored.sort(key=lambda item: item[0], reverse=not ascending)
    selected = scored[:limit]
    return {
        "kind": "top_n",
        "field": field,
        "ascending": bool(ascending),
        "requested": limit,
        "matched": len(selected),
        "considered": len(scored),
        "feature_ids": [identifier for _value, identifier in selected if identifier is not None],
        "rows": [{"id": identifier, "value": value} for value, identifier in selected],
    }


def outliers(
    records: Sequence[Mapping[str, Any]],
    field: str,
    method: str = "iqr",
    threshold: float = 1.5,
    id_key: str = "id",
) -> dict[str, Any]:
    """Flag unusually high/low values with a transparent, stated rule.

    Two methods only, both explainable in one sentence to a GIS analyst: Tukey
    fences (default, distribution-free) and a z-score cut. The rule and its
    bounds travel with the result so a report can say *why* a feature was
    called an outlier instead of asserting that it is one.
    """
    method = str(method or "iqr").lower()
    if method not in {"iqr", "zscore"}:
        method = "iqr"
    threshold = float(threshold or (1.5 if method == "iqr" else 3.0))
    pairs = []
    for record in records:
        number = _finite(record.get(field))
        if number is not None:
            pairs.append((number, record.get(id_key)))
    if len(pairs) < 4:
        return {
            "kind": "outliers",
            "field": field,
            "method": method,
            "matched": 0,
            "feature_ids": [],
            "reason": "not_enough_values",
        }
    values = sorted(value for value, _ in pairs)
    if method == "iqr":
        p25 = quantile(values, 0.25)
        p75 = quantile(values, 0.75)
        spread = p75 - p25
        low = p25 - threshold * spread
        high = p75 + threshold * spread
        rule = "Tukey fences at {:.4g} x IQR".format(threshold)
    else:
        mean = math.fsum(values) / len(values)
        variance = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
        deviation = math.sqrt(variance)
        low = mean - threshold * deviation
        high = mean + threshold * deviation
        rule = "z-score beyond {:.4g} standard deviations".format(threshold)
    flagged = [(value, identifier) for value, identifier in pairs if value < low or value > high]
    flagged.sort(key=lambda item: abs(item[0] - (low + high) / 2.0), reverse=True)
    flagged = flagged[:MAX_TOP_N]
    return {
        "kind": "outliers",
        "field": field,
        "method": method,
        "rule": rule,
        "lower_bound": low,
        "upper_bound": high,
        "considered": len(pairs),
        "matched": len(flagged),
        "feature_ids": [identifier for _value, identifier in flagged if identifier is not None],
        "rows": [{"id": identifier, "value": value} for value, identifier in flagged],
    }


def _sample(values: Sequence[float], cap: int) -> list[float]:
    """Evenly sample a sorted sequence, always keeping the two extremes."""
    if len(values) <= cap:
        return list(values)
    step = len(values) / float(cap)
    sampled = [values[min(len(values) - 1, int(index * step))] for index in range(cap)]
    sampled[0] = values[0]
    sampled[-1] = values[-1]
    return sampled


def _jenks_breaks(values: Sequence[float], classes: int) -> list[float]:
    """Fisher-Jenks natural breaks over sorted values.

    Standard dynamic programme minimising the within-class sum of squared
    deviations. Values are pre-sampled so the O(n*k) table stays bounded on a
    large layer; the extremes are preserved, so the first and last class edges
    are always the real min and max.
    """
    data = _sample(values, JENKS_SAMPLE_CAP)
    count = len(data)
    if classes >= count:
        return sorted(set(data))
    # variance[i][j] = lowest within-class deviation for the first i values in j classes
    matrix = [[0] * (classes + 1) for _ in range(count + 1)]
    deviation = [[float("inf")] * (classes + 1) for _ in range(count + 1)]
    for cls in range(1, classes + 1):
        matrix[1][cls] = 1
        deviation[1][cls] = 0.0
        for index in range(2, count + 1):
            deviation[index][cls] = float("inf")
    for index in range(2, count + 1):
        total = 0.0
        squares = 0.0
        size = 0
        for back in range(1, index + 1):
            position = index - back + 1
            value = data[position - 1]
            size += 1
            total += value
            squares += value * value
            spread = squares - (total * total) / size
            previous = position - 1
            if previous == 0:
                continue
            for cls in range(2, classes + 1):
                candidate = spread + deviation[previous][cls - 1]
                if deviation[index][cls] >= candidate:
                    matrix[index][cls] = position
                    deviation[index][cls] = candidate
        deviation[index][1] = squares - (total * total) / size
        matrix[index][1] = 1
    breaks = [0.0] * (classes + 1)
    breaks[classes] = data[count - 1]
    breaks[0] = data[0]
    index = count
    for cls in range(classes, 1, -1):
        edge = int(matrix[index][cls]) - 1
        # Report the LAST value of the lower class, not the first value of the
        # upper one. Renderer ranges are inclusive on both ends, so a break at
        # the upper cluster's first value would put that value in the lower
        # class - the cluster boundary would land one feature on the wrong side.
        breaks[cls - 1] = data[max(0, edge - 1)]
        index = edge
    return breaks


def classify_breaks(values: Iterable[Any], classes: int = 5, method: str = "quantile") -> dict[str, Any]:
    """Compute class edges for a graduated renderer.

    This is the bridge between "analyse this column" and "show it on the map":
    the caller turns the returned edges directly into QGIS renderer ranges. The
    method is stated in the result so the legend can be honest about how the
    classes were derived.
    """
    classes = max(2, min(int(classes or 5), MAX_CLASSES))
    method = str(method or "quantile").lower()
    if method not in {"quantile", "equal_interval", "natural_breaks", "stddev"}:
        method = "quantile"
    numbers, nulls = numeric_values(values)
    if len(numbers) < 2:
        return {"kind": "breaks", "method": method, "breaks": [], "nulls": nulls, "usable": len(numbers)}
    ordered = sorted(numbers)
    low, high = ordered[0], ordered[-1]
    if high <= low:
        return {"kind": "breaks", "method": method, "breaks": [low, high], "nulls": nulls, "usable": len(ordered)}
    if method == "equal_interval":
        width = (high - low) / classes
        edges = [low + width * index for index in range(classes + 1)]
    elif method == "natural_breaks":
        edges = _jenks_breaks(ordered, classes)
    elif method == "stddev":
        mean = math.fsum(ordered) / len(ordered)
        variance = math.fsum((value - mean) ** 2 for value in ordered) / (len(ordered) - 1)
        step = math.sqrt(variance)
        half = classes // 2
        edges = [mean + step * (index - half) for index in range(classes + 1)]
        edges[0] = min(edges[0], low)
        edges[-1] = max(edges[-1], high)
    else:
        edges = [quantile(ordered, index / float(classes)) for index in range(classes + 1)]
    # Degenerate distributions can repeat an edge; a renderer needs strictly
    # increasing ranges or a class silently swallows nothing.
    cleaned = [edges[0]]
    for edge in edges[1:]:
        if edge > cleaned[-1]:
            cleaned.append(edge)
    if len(cleaned) < 2:
        cleaned = [low, high]
    cleaned[0] = min(cleaned[0], low)
    cleaned[-1] = max(cleaned[-1], high)
    return {
        "kind": "breaks",
        "method": method,
        "classes": len(cleaned) - 1,
        "requested_classes": classes,
        "usable": len(ordered),
        "nulls": nulls,
        "min": low,
        "max": high,
        "breaks": cleaned,
    }


def group_aggregate(
    records: Sequence[Mapping[str, Any]],
    group_field: str,
    value_field: str | None = None,
    statistic: str = "count",
) -> dict[str, Any]:
    """Group-by aggregation - the typed answer to "X per Y" questions.

    ``count`` needs no value field; every other statistic requires one and
    ignores records whose value is null, reporting how many were skipped so the
    caller can say so rather than quietly averaging over fewer rows.
    """
    statistic = str(statistic or "count").lower()
    if statistic not in {"count", "sum", "mean", "min", "max", "median", "distinct"}:
        statistic = "count"
    if statistic != "count" and not value_field:
        statistic = "count"
    buckets: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    distinct: dict[str, set] = {}
    skipped = 0
    for record in records:
        raw_group = record.get(group_field)
        key = "" if raw_group is None else str(raw_group).strip()
        if not key:
            key = "(no value)"
        counts[key] = counts.get(key, 0) + 1
        if statistic == "count":
            continue
        if statistic == "distinct":
            distinct.setdefault(key, set()).add(str(record.get(value_field)))
            continue
        number = _finite(record.get(value_field))
        if number is None:
            skipped += 1
            continue
        buckets.setdefault(key, []).append(number)
    groups = []
    for key in counts:
        if statistic == "count":
            value: float | None = float(counts[key])
        elif statistic == "distinct":
            value = float(len(distinct.get(key, ())))
        else:
            numbers = sorted(buckets.get(key, ()))
            if not numbers:
                value = None
            elif statistic == "sum":
                value = math.fsum(numbers)
            elif statistic == "mean":
                value = math.fsum(numbers) / len(numbers)
            elif statistic == "min":
                value = numbers[0]
            elif statistic == "max":
                value = numbers[-1]
            else:
                value = quantile(numbers, 0.5)
        groups.append({"group": key, "value": value, "features": counts[key]})
    groups.sort(key=lambda item: (item["value"] is None, -(item["value"] or 0.0), item["group"]))
    return {
        "kind": "group_aggregate",
        "group_field": group_field,
        "value_field": value_field or "",
        "statistic": statistic,
        "groups": groups[:MAX_CATEGORIES],
        "group_count": len(groups),
        "truncated": len(groups) > MAX_CATEGORIES,
        "skipped_null_values": skipped,
    }


def compare_distributions(left: Iterable[Any], right: Iterable[Any]) -> dict[str, Any]:
    """Compare one numeric column across two feature sets.

    Used for "how does this viewport differ from the whole dataset?" and for
    comparing a selection against its parent layer. Only the differences that
    can actually be computed are reported; a missing side yields None, never a
    fabricated zero.
    """
    left_stats = numeric_summary(left)
    right_stats = numeric_summary(right)
    delta: dict[str, Any] = {}
    for key in ("count", "usable", "mean", "median", "min", "max", "sum", "stddev"):
        first = left_stats.get(key)
        second = right_stats.get(key)
        if isinstance(first, (int, float)) and isinstance(second, (int, float)):
            delta[key] = first - second
            if key in {"mean", "median", "sum"} and second:
                delta[key + "_percent"] = (first - second) * 100.0 / abs(second)
        else:
            delta[key] = None
    return {"kind": "comparison", "left": left_stats, "right": right_stats, "delta": delta}
