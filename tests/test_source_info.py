import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.source_info import human_size, inspect_paths


def test_human_size():
    assert human_size(1536) == "1.5 KB"


def test_raster_workflow_rejects_vector_before_upload(tmp_path):
    source = tmp_path / "parcels.geojson"
    source.write_text("{}", encoding="utf-8")
    result = inspect_paths([str(source)], "georeference")
    assert not result["valid"]
    assert "raster/PDF" in result["error"]


def test_validate_accepts_vector_and_summarizes_size(tmp_path):
    source = tmp_path / "parcels.gpkg"
    source.write_bytes(b"x" * 2048)
    result = inspect_paths([str(source)], "validate_deliver")
    assert result == {"valid": True, "summary": "Vector · 2.0 KB", "error": ""}
