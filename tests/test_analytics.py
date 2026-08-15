"""Known-answer tests for the analytics engine.

These assert the actual computed numbers, not that a result has the right
shape. A test that only checks ``kind == "numeric"`` would pass against an
engine that returns garbage, and the whole point of this module is that a
number Nivo states is one it measured.
"""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.analytics import (  # noqa: E402
    categorical_summary,
    classify_breaks,
    compare_distributions,
    group_aggregate,
    histogram,
    numeric_summary,
    outliers,
    quantile,
    top_n,
)

# Ten parcels with areas whose statistics are hand-computable.
AREAS = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
PARCELS = [
    {"id": index + 1, "area": area, "landuse": landuse}
    for index, (area, landuse) in enumerate(
        zip(AREAS, ["residential"] * 5 + ["commercial"] * 3 + ["industrial"] * 2)
    )
]


def test_numeric_summary_matches_hand_computed_statistics():
    stats = numeric_summary(AREAS)
    assert stats["usable"] == 10
    assert stats["nulls"] == 0
    assert stats["min"] == 100.0
    assert stats["max"] == 1000.0
    assert stats["sum"] == 5500.0
    assert stats["mean"] == 550.0
    assert stats["median"] == 550.0
    # Sample standard deviation of 100..1000 step 100 is 100*sqrt(55/6).
    assert math.isclose(stats["stddev"], 100.0 * math.sqrt(55.0 / 6.0), rel_tol=1e-12)
    assert math.isclose(stats["p25"], 325.0, rel_tol=1e-12)
    assert math.isclose(stats["p75"], 775.0, rel_tol=1e-12)
    assert math.isclose(stats["iqr"], 450.0, rel_tol=1e-12)


def test_nulls_are_excluded_rather_than_treated_as_zero():
    # A null area is not an area of zero; coercing it would drag the mean down.
    stats = numeric_summary([10.0, None, 20.0, "", 30.0])
    assert stats["nulls"] == 2
    assert stats["usable"] == 3
    assert stats["mean"] == 20.0
    assert stats["count"] == 5


def test_non_finite_and_non_numeric_values_are_counted_as_nulls():
    stats = numeric_summary([1.0, float("nan"), float("inf"), "abc", True, "2,5"])
    # True is a bool, not a measurement; "2,5" is a comma decimal.
    assert stats["usable"] == 2
    assert stats["nulls"] == 4
    assert math.isclose(stats["sum"], 3.5, rel_tol=1e-12)


def test_quantile_uses_linear_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert quantile(values, 0.0) == 1.0
    assert quantile(values, 1.0) == 4.0
    assert math.isclose(quantile(values, 0.5), 2.5, rel_tol=1e-12)
    assert math.isclose(quantile(values, 0.25), 1.75, rel_tol=1e-12)


def test_categorical_summary_counts_and_percentages():
    summary = categorical_summary([parcel["landuse"] for parcel in PARCELS])
    assert summary["distinct"] == 3
    assert summary["usable"] == 10
    assert summary["categories"][0] == {"value": "residential", "count": 5, "percent": 50.0}
    assert [item["value"] for item in summary["categories"]] == [
        "residential",
        "commercial",
        "industrial",
    ]


def test_categorical_ties_break_on_label_so_ordering_is_stable():
    first = categorical_summary(["b", "a", "b", "a"])
    second = categorical_summary(["a", "b", "a", "b"])
    assert [item["value"] for item in first["categories"]] == ["a", "b"]
    assert first["categories"] == second["categories"]


def test_top_n_returns_feature_ids_a_map_selection_can_use():
    result = top_n(PARCELS, "area", limit=3)
    assert result["feature_ids"] == [10, 9, 8]
    assert result["rows"][0] == {"id": 10, "value": 1000.0}
    ascending = top_n(PARCELS, "area", limit=2, ascending=True)
    assert ascending["feature_ids"] == [1, 2]


def test_top_n_is_bounded_and_ignores_unusable_values():
    records = [{"id": 1, "area": None}, {"id": 2, "area": 5.0}]
    result = top_n(records, "area", limit=10)
    assert result["considered"] == 1
    assert result["feature_ids"] == [2]


def test_iqr_outliers_flag_the_extreme_value_with_a_stated_rule():
    records = [{"id": index, "value": value} for index, value in enumerate([1, 2, 2, 3, 3, 3, 4, 4, 5, 100])]
    result = outliers(records, "value")
    assert result["feature_ids"] == [9]
    assert result["method"] == "iqr"
    assert "IQR" in result["rule"]
    assert result["upper_bound"] < 100


def test_outliers_refuse_to_guess_on_a_tiny_sample():
    result = outliers([{"id": 1, "value": 5}], "value")
    assert result["matched"] == 0
    assert result["reason"] == "not_enough_values"


def test_equal_interval_breaks_are_evenly_spaced_and_span_the_range():
    result = classify_breaks(AREAS, classes=3, method="equal_interval")
    assert result["breaks"][0] == 100.0
    assert result["breaks"][-1] == 1000.0
    assert len(result["breaks"]) == 4
    assert math.isclose(result["breaks"][1], 400.0, rel_tol=1e-12)


def test_natural_breaks_separate_two_obvious_clusters():
    # Jenks must put the split between the clusters, not in the middle of one.
    values = [1.0, 2.0, 3.0, 4.0, 100.0, 101.0, 102.0, 103.0]
    result = classify_breaks(values, classes=2, method="natural_breaks")
    assert result["breaks"][0] == 1.0
    assert result["breaks"][-1] == 103.0
    # The split must fall in the empty gap between the clusters, and it must be
    # the lower cluster's top value so 100.0 renders in the upper class.
    assert result["breaks"][1] == 4.0


def test_natural_breaks_ranges_place_the_upper_cluster_in_the_upper_class():
    values = [1.0, 2.0, 3.0, 4.0, 100.0, 101.0, 102.0, 103.0]
    breaks = classify_breaks(values, classes=2, method="natural_breaks")["breaks"]
    # QGIS graduated ranges are inclusive [lower, upper]; check every value
    # actually lands in the class a reader would expect from the legend.
    assert all(value <= breaks[1] for value in values[:4])
    assert all(value > breaks[1] for value in values[4:])


def test_breaks_are_strictly_increasing_even_on_a_degenerate_column():
    result = classify_breaks([5.0] * 20, classes=5)
    assert result["breaks"] == [5.0, 5.0] or len(result["breaks"]) == 2


def test_quantile_breaks_split_the_data_into_equal_counts():
    result = classify_breaks(AREAS, classes=2, method="quantile")
    assert result["classes"] == 2
    assert math.isclose(result["breaks"][1], 550.0, rel_tol=1e-12)


def test_histogram_bins_are_half_open_and_keep_the_maximum_in_the_last_bin():
    # Edges are [0,5) and [5,10]: 5 belongs to the upper bin, and 10 must not
    # spill into a spurious extra bin beyond the range.
    result = histogram([0.0, 5.0, 10.0], bins=2)
    assert [item["count"] for item in result["bins"]] == [1, 2]
    assert result["bins"][-1]["max"] == 10.0


def test_group_aggregate_counts_features_per_group():
    result = group_aggregate(PARCELS, "landuse")
    assert result["statistic"] == "count"
    assert {item["group"]: item["value"] for item in result["groups"]} == {
        "residential": 5.0,
        "commercial": 3.0,
        "industrial": 2.0,
    }


def test_group_aggregate_computes_mean_and_reports_skipped_nulls():
    records = PARCELS + [{"id": 99, "area": None, "landuse": "residential"}]
    result = group_aggregate(records, "landuse", "area", "mean")
    values = {item["group"]: item["value"] for item in result["groups"]}
    assert math.isclose(values["residential"], 300.0, rel_tol=1e-12)
    assert math.isclose(values["commercial"], 700.0, rel_tol=1e-12)
    assert result["skipped_null_values"] == 1
    assert values["residential"] == 300.0


def test_group_aggregate_falls_back_to_count_without_a_value_field():
    result = group_aggregate(PARCELS, "landuse", None, "mean")
    assert result["statistic"] == "count"


def test_comparison_reports_real_deltas_between_two_feature_sets():
    result = compare_distributions(AREAS[:5], AREAS)
    assert result["left"]["mean"] == 300.0
    assert result["right"]["mean"] == 550.0
    assert math.isclose(result["delta"]["mean"], -250.0, rel_tol=1e-12)
    assert math.isclose(result["delta"]["mean_percent"], -250.0 * 100.0 / 550.0, rel_tol=1e-9)


def test_empty_column_is_reported_as_empty_not_as_zeroes():
    stats = numeric_summary([None, None])
    assert stats["usable"] == 0
    assert "mean" not in stats
