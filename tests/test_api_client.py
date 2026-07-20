import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI


def test_base_url_is_normalized():
    assert MapdexAPI("https://api.mapdex.ai/").base_url == "https://api.mapdex.ai"


def test_workflow_payload_uses_server_batch_contract(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}
    def fake(method, path, payload=None, project_id=""):
        captured.update(method=method, path=path, payload=payload, project_id=project_id)
        return {"id": "batch_1"}
    monkeypatch.setattr(api, "_request", fake)
    api.start_batch("proj_1", "file_1", "digitize_parcels")
    assert captured == {"method": "POST", "path": "/v1/batches", "payload": {"kind": "digitize_parcels", "sources": [{"file_id": "file_1"}]}, "project_id": "proj_1"}
