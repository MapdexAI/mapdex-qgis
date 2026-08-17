"""A null and an empty string are two different facts about a dataset.

Found by the real-QGIS end-to-end harness against testdata/mapdex-test-parcels.gpkg,
whose kullanim column deliberately holds one SetFieldNull and one "". They were
being counted as one bucket, so the summary reported three categories where the
fixture holds five distinct states. "Nobody recorded a land use here" and
"somebody recorded that it has none" call for different work, and merging them
makes that decision for the reviewer without telling them.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import analytics  # noqa: E402

FIXTURE = ["tarım"] * 8 + ["konut"] * 7 + ["ticari"] * 7 + [None, ""]


def test_a_gap_and_a_recorded_absence_are_counted_apart():
    r = analytics.categorical_summary(FIXTURE)
    assert r["count"] == 24
    assert r["usable"] == 22
    assert r["missing"] == 1, "the SetFieldNull parcel"
    assert r["blank"] == 1, "the empty-string parcel"


def test_nulls_remains_their_sum_so_existing_readers_are_unchanged():
    r = analytics.categorical_summary(FIXTURE)
    assert r["nulls"] == r["missing"] + r["blank"] == 2


def test_the_real_categories_are_untouched():
    r = analytics.categorical_summary(FIXTURE)
    assert [(c["value"], c["count"]) for c in r["categories"]] == [
        ("tarım", 8), ("konut", 7), ("ticari", 7),
    ]


def test_a_column_with_neither_reports_zero_for_both():
    r = analytics.categorical_summary(["a", "b", "a"])
    assert r["missing"] == 0 and r["blank"] == 0 and r["nulls"] == 0
