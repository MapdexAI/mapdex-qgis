import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.results import split_review_buckets


def feature(**properties):
    return {"type": "Feature", "properties": properties, "geometry": None}


def test_real_parcel_output_lands_in_the_review_bucket():
    # Shape taken from a live extraction: no calibrated confidence exists, so
    # the split has to rely on what the validators reported.
    buckets = split_review_buckets(
        {
            "features": [
                feature(review_required=True, validation_status="valid", quality_status="uncalibrated"),
                feature(review_required=False, validation_status="valid"),
            ]
        }
    )
    assert [b["key"] for b in buckets] == ["needs_review", "clean"]
    assert len(buckets[0]["features"]) == 1
    assert buckets[0]["label"] == "Needs review"


def test_invalid_geometry_outranks_review():
    buckets = split_review_buckets(
        {"features": [feature(review_required=True, validation_status="invalid")]}
    )
    assert [b["key"] for b in buckets] == ["invalid"]


def test_empty_buckets_are_dropped():
    buckets = split_review_buckets({"features": [feature(review_required=False)]})
    assert [b["key"] for b in buckets] == ["clean"]


def test_review_status_string_is_honoured():
    buckets = split_review_buckets({"features": [feature(review_status="needs_review")]})
    assert [b["key"] for b in buckets] == ["needs_review"]


def test_malformed_payloads_never_raise():
    assert split_review_buckets({}) == []
    assert split_review_buckets({"features": None}) == []
    assert split_review_buckets({"features": [{}, None]})[0]["key"] == "clean"
