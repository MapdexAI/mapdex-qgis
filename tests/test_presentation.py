"""Analysis-to-map and analysis-to-report tests."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.analytics import (  # noqa: E402
    categorical_summary,
    classify_breaks,
    group_aggregate,
    numeric_summary,
    outliers,
    top_n,
)
from mapdex_qgis.presentation import (  # noqa: E402
    build_report,
    ramp_colors,
    render_report_text,
    visualization_for,
)

AREAS = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
PARCELS = [
    {"id": index + 1, "area": area, "landuse": landuse}
    for index, (area, landuse) in enumerate(
        zip(AREAS, ["residential"] * 5 + ["commercial"] * 3 + ["industrial"] * 2)
    )
]


# --------------------------------------------------------------------------
# Analysis becomes map state
# --------------------------------------------------------------------------

def test_a_numeric_analysis_becomes_a_graduated_renderer_with_a_real_legend():
    breaks = classify_breaks(AREAS, classes=4, method="quantile")
    spec = visualization_for(breaks, "parcels", "area")
    assert spec["kind"] == "graduated"
    assert len(spec["classes"]) == 4
    # Classes must tile the range without gaps so no feature is unstyled.
    assert spec["classes"][0]["lower"] == 100.0
    assert spec["classes"][-1]["upper"] == 1000.0
    for lower, upper in zip(spec["classes"], spec["classes"][1:]):
        assert lower["upper"] == upper["lower"]
    # The legend states how the classes were derived.
    assert "quantile" in spec["legend_note"]
    assert all(entry["color"].startswith("#") for entry in spec["classes"])


def test_a_categorical_analysis_becomes_a_categorized_renderer():
    summary = categorical_summary([parcel["landuse"] for parcel in PARCELS])
    spec = visualization_for(summary, "parcels", "landuse")
    assert spec["kind"] == "categorized"
    assert [entry["value"] for entry in spec["categories"]] == ["residential", "commercial", "industrial"]
    assert "5" in spec["categories"][0]["label"]
    # Distinct colours, so two categories never read as one.
    assert len({entry["color"] for entry in spec["categories"]}) == 3


def test_top_n_becomes_an_actual_map_selection_that_zooms():
    ranked = top_n(PARCELS, "area", limit=3)
    spec = visualization_for(ranked, "parcels")
    assert spec["kind"] == "selection"
    assert spec["feature_ids"] == ["10", "9", "8"]
    assert spec["zoom_to_selection"] is True
    assert spec["reversible"] is True


def test_outliers_become_a_selection_so_the_user_can_see_which_features():
    records = [{"id": index, "value": value} for index, value in enumerate([1, 2, 2, 3, 3, 3, 4, 4, 5, 100])]
    spec = visualization_for(outliers(records, "value"), "parcels")
    assert spec["kind"] == "selection"
    assert spec["feature_ids"] == ["9"]


def test_a_group_aggregate_becomes_a_choropleth_specification():
    result = group_aggregate(PARCELS, "landuse", "area", "mean")
    spec = visualization_for(result, "districts", "landuse")
    assert spec["kind"] == "group_choropleth"
    assert {item["group"] for item in spec["groups"]} == {"residential", "commercial", "industrial"}


def test_density_becomes_a_choropleth_and_carries_its_unit():
    density = {"kind": "density", "unit": "per km²", "values": {"a": 50.0, "b": 10.0}}
    spec = visualization_for(density, "districts", "name")
    assert spec["kind"] == "group_choropleth"
    assert spec["unit"] == "per km²"


def test_an_empty_result_produces_no_map_change_and_says_why():
    assert visualization_for(top_n([], "area"), "parcels")["reason"] == "no_matching_features"
    assert visualization_for({"kind": "numeric"}, "parcels")["kind"] == "none"
    assert visualization_for(classify_breaks([5.0], classes=3), "l", "f")["kind"] == "none"


def test_sequential_ramps_are_ordered_and_categorical_ramps_cycle():
    sequential = ramp_colors("Viridis", 4)
    assert len(sequential) == 4 and len(set(sequential)) == 4
    # More classes than palette entries must still yield one colour per class.
    categorical = ramp_colors("Categorical", 12, categorical=True)
    assert len(categorical) == 12
    assert len(set(categorical[:8])) == 8


# --------------------------------------------------------------------------
# Reports are built only from measurements
# --------------------------------------------------------------------------

def _evidence(capability, result, ok=True, error=""):
    return {"capability": capability, "params": {}, "result": result, "ok": ok, "error": error}


def test_a_report_states_only_numbers_that_were_measured():
    evidence = [
        _evidence("analytics.numeric@1", numeric_summary(AREAS)),
        _evidence("analytics.categories@1", categorical_summary([p["landuse"] for p in PARCELS])),
    ]
    report = build_report("Parcel report", "analyse parcels", evidence)
    assert report["empty"] is False
    assert report["measured_sections"] == 2
    text = render_report_text(report)
    assert "550" in text          # the real mean
    assert "residential" in text
    assert "Parcel report" in text


def test_a_failed_step_is_listed_as_not_checked_rather_than_omitted():
    evidence = [
        _evidence("analytics.numeric@1", numeric_summary(AREAS)),
        _evidence("spatial.relate@1", None, ok=False, error="the other layer has no geometry"),
    ]
    report = build_report("Report", "objective", evidence)
    assert report["not_checked"]
    assert report["not_checked"][0]["capability"] == "spatial.relate@1"
    assert "no geometry" in render_report_text(report)
    assert "Not checked" in render_report_text(report)


def test_a_report_with_no_measurements_says_so_instead_of_looking_complete():
    report = build_report("Empty", "objective", [])
    assert report["empty"] is True
    assert "states no findings" in render_report_text(report)


def test_a_group_report_discloses_features_excluded_for_missing_values():
    records = PARCELS + [{"id": 99, "area": None, "landuse": "residential"}]
    grouped = group_aggregate(records, "landuse", "area", "mean")
    report = build_report("R", "o", [_evidence("analytics.group@1", grouped)])
    assert "1 features were excluded" in render_report_text(report)


def test_an_outlier_section_prints_the_rule_that_produced_it():
    records = [{"id": index, "value": value} for index, value in enumerate([1, 2, 2, 3, 3, 3, 4, 4, 5, 100])]
    report = build_report("R", "o", [_evidence("analytics.outliers@1", outliers(records, "value"))])
    text = render_report_text(report)
    assert "IQR" in text
    assert "Bounds" in text


def test_a_no_outliers_result_is_reported_as_a_finding_not_as_silence():
    report = build_report("R", "o", [_evidence("analytics.outliers@1", outliers([{"id": 1, "value": 2}], "value"))])
    assert "No outliers were flagged" in render_report_text(report)


def test_points_in_polygons_report_discloses_features_that_matched_nothing():
    result = {"kind": "points_in_polygons", "counts": {"a": 2}, "points": 3, "unmatched": 1}
    report = build_report("R", "o", [_evidence("spatial.count_in_polygons@1", result)])
    assert "1 of 3 features fell outside every polygon" in render_report_text(report)


def test_rejected_agent_steps_do_not_pollute_the_not_checked_list():
    # "(rejected)" is an internal loop artefact, not a GIS check the user asked
    # for; listing it would read as a failed analysis.
    report = build_report("R", "o", [_evidence("(rejected)", None, ok=False, error="unknown capability")])
    assert report["not_checked"] == []
