import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.results import geojson_truncation_notice


def test_reports_truncation_with_counts():
    geojson = {
        "type": "FeatureCollection",
        "features": [],
        "properties": {"truncated": True, "returned_count": 5000, "total_count": 14000},
    }
    notice = geojson_truncation_notice(geojson, "Newark parcels")
    assert "Newark parcels" in notice
    assert "5000" in notice
    assert "14000" in notice


def test_no_notice_when_not_truncated():
    geojson = {
        "type": "FeatureCollection",
        "features": [],
        "properties": {"truncated": False, "returned_count": 42, "total_count": 42},
    }
    assert geojson_truncation_notice(geojson, "Small layer") == ""


def test_no_notice_when_server_omits_truncation_metadata():
    # Older/degraded servers that don't send properties.truncated must not
    # produce a false warning.
    geojson = {"type": "FeatureCollection", "features": []}
    assert geojson_truncation_notice(geojson, "Legacy layer") == ""


def test_no_notice_for_non_dict_payload():
    assert geojson_truncation_notice([], "Whatever") == ""


def test_falls_back_to_generic_notice_without_counts():
    geojson = {"type": "FeatureCollection", "features": [], "properties": {"truncated": True}}
    notice = geojson_truncation_notice(geojson, "Odd layer")
    assert "Odd layer" in notice
    assert "truncated" in notice
