import json
import pathlib
import sys
from urllib import error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI, MapdexAPIError, device_verification_url, normalize_api_base
from mapdex_qgis.qt_compat import enum_member
from mapdex_qgis.results import (
    batch_is_terminal,
    collect_geojson_artifact_urls,
    collect_raster_layer_imports,
    collect_layer_imports,
    collect_vector_layer_imports,
    first_batch_error,
    review_run_ids,
    succeeded_run_ids,
)


class _FakeQt:
    class DockWidgetArea:
        LeftDockWidgetArea = 1
        RightDockWidgetArea = 2


class _FakeQt5Only:
    LeftDockWidgetArea = 1
    RightDockWidgetArea = 2


def test_enum_member_prefers_scoped_qt6_form():
    assert enum_member(_FakeQt, "DockWidgetArea", "LeftDockWidgetArea") == 1
    assert enum_member(_FakeQt, "DockWidgetArea", "RightDockWidgetArea") == 2


def test_enum_member_falls_back_to_flat_qt5_form():
    assert enum_member(_FakeQt5Only, "DockWidgetArea", "LeftDockWidgetArea") == 1


def test_base_url_is_normalized():
    assert MapdexAPI("https://api.mapdex.ai/").base_url == "https://api.mapdex.ai"
    assert MapdexAPI("http://127.0.0.1:8080/v1").base_url == "http://127.0.0.1:8080"
    assert normalize_api_base("http://localhost:8080/v1/") == "http://localhost:8080"


def test_production_device_flow_never_opens_localhost():
    assert device_verification_url(
        "http://localhost:3000/device", "https://mapdex.ai", "https://api.mapdex.ai"
    ) == "https://mapdex.ai/device"
    assert device_verification_url(
        "http://localhost:3000/device", "http://localhost:3000", "http://localhost:8080"
    ) == "http://localhost:3000/device"


def test_projects_accepts_bare_list_and_envelope():
    api = MapdexAPI("https://api.mapdex.ai", "token")

    def as_list(method, path, payload=None, project_id=""):
        return [{"id": "proj_1", "name": "Alpha"}]

    def as_envelope(method, path, payload=None, project_id=""):
        return {"projects": [{"id": "proj_2", "name": "Beta"}]}

    api._request = as_list  # type: ignore[method-assign]
    assert api.projects() == [{"id": "proj_1", "name": "Alpha"}]
    api._request = as_envelope  # type: ignore[method-assign]
    assert api.projects() == [{"id": "proj_2", "name": "Beta"}]


def test_raster_result_uses_layer_metadata_and_file_download(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    calls = []

    def fake_request(method, path, payload=None, project_id=""):
        calls.append((method, path, project_id))
        return {"id": "layer_r", "geometry_type": "raster", "source_file_id": "file_result"}

    def fake_download(path, project_id=""):
        calls.append(("DOWNLOAD", path, project_id))
        return b"geotiff"

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(api, "download_bytes", fake_download)

    assert api.layer("layer_r", "proj_1")["source_file_id"] == "file_result"
    assert api.file_bytes("file_result", "proj_1") == b"geotiff"
    assert calls == [
        ("GET", "/v1/layers/layer_r", "proj_1"),
        ("DOWNLOAD", "/v1/files/file_result/download", "proj_1"),
    ]


def test_workflow_payload_uses_server_batch_contract(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    def fake(method, path, payload=None, project_id=""):
        captured.update(method=method, path=path, payload=payload, project_id=project_id)
        return {"id": "batch_1"}

    monkeypatch.setattr(api, "_request", fake)
    api.start_batch("proj_1", "file_1", "digitize_parcels")
    assert captured == {
        "method": "POST",
        "path": "/v1/batches",
        "payload": {"kind": "digitize_parcels", "name": "[QGIS] Digitize Parcels", "sources": [{"file_id": "file_1"}]},
        "project_id": "proj_1",
    }


def test_nivo_compose_uses_canonical_endpoint_and_companion_context(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    def fake(method, path, payload=None, project_id=""):
        captured.update(method=method, path=path, payload=payload, project_id=project_id)
        return {"kind": "message"}
    monkeypatch.setattr(api, "_request", fake)
    api.compose("proj_1", "inspect selection", {"version": "companion.qgis.v1", "client": "qgis"})
    assert captured == {
        "method": "POST",
        "path": "/v1/compose",
        "project_id": "proj_1",
        "payload": {
            "input": "inspect selection",
            "companion_context": {"version": "companion.qgis.v1", "client": "qgis"},
        },
    }


def test_upload_file_streams_to_signed_storage_and_finalizes(monkeypatch, tmp_path):
    source = tmp_path / "map.tif"
    source.write_bytes(b"TIFFCONTENT")
    api = MapdexAPI("https://api.mapdex.ai", "token")
    calls = []

    def fake(method, path, payload=None, project_id=""):
        calls.append((method, path, payload, project_id))
        if path == "/v1/files":
            return {
                "file": {"id": "file_123"},
                "upload": {"url": "https://storage.mapdex.ai/upload/123", "method": "PUT", "headers": {}},
            }
        if path == "/v1/files/file_123/complete":
            return {"id": "file_123", "status": "ready"}
        return {}

    monkeypatch.setattr(api, "_request", fake)
    monkeypatch.setattr(api, "_put_upload", lambda path, upload: None)

    res = api.upload_file(str(source), "proj_1")
    assert res == {"id": "file_123", "status": "ready"}
    assert calls == [
        (
            "POST",
            "/v1/files",
            {
                "filename": "map.tif",
                "content_type": "image/tiff",
                "byte_size": 11,
                # Required by every POST /v1/files. Omitting it means the API
                # answers CONTENT_POLICY_ATTESTATION_REQUIRED and the desktop
                # cannot upload anything at all, so this belongs in the
                # assertion rather than being tolerated as an extra key.
                "policy_acknowledged": True,
            },
            "proj_1",
        ),
        ("POST", "/v1/files/file_123/complete", {}, "proj_1"),
    ]


def test_signed_upload_does_not_send_mapdex_authorization(monkeypatch, tmp_path):
    source = tmp_path / "parcels.geojson"
    source.write_bytes(b"{}")
    api = MapdexAPI("https://api.mapdex.ai", "secret-token")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=0):
        captured.update(headers=dict(req.header_items()), timeout=timeout, data=req.data)
        return FakeResponse()

    monkeypatch.setattr("mapdex_qgis.api_client._urlopen", fake_urlopen)
    api._put_upload(
        str(source),
        {"url": "https://objects.example.test/file", "headers": {"X-Signed": "yes"}},
    )

    lowered = {key.lower(): value for key, value in captured["headers"].items()}
    assert lowered["content-length"] == "2"
    assert lowered["x-signed"] == "yes"
    assert "authorization" not in lowered
    assert hasattr(captured["data"], "read")


def test_multipart_fallback_carries_the_policy_attestation(monkeypatch, tmp_path):
    """The compatibility route posts the file directly, so it must attest too.

    The API enforces the acknowledgement as a form field on this branch. Sending
    it only on the signed-upload path would leave the fallback refused with
    CONTENT_POLICY_ATTESTATION_REQUIRED on exactly the deployments that need it.
    """
    source = tmp_path / "plan.dxf"
    source.write_bytes(b"DXFBODY")
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured.update(data=req.data, headers=dict(req.header_items()))
        return FakeResponse()

    monkeypatch.setattr("mapdex_qgis.api_client._urlopen", fake_urlopen)
    api._upload_file_multipart(str(source), "proj_1")

    body = captured["data"]
    boundary = dict(
        (key.lower(), value) for key, value in captured["headers"].items()
    )["content-type"].split("boundary=")[1]

    assert b'name="policy_acknowledged"' in body
    assert b"\r\n\r\ntrue\r\n" in body
    # The file part must survive intact alongside the new field, and the body
    # must still terminate with the closing boundary.
    assert b"DXFBODY" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert body.count(f"--{boundary}".encode()) == 3


def test_multiple_files_create_one_real_batch(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    def fake(method, path, payload=None, project_id=""):
        captured.update(payload=payload, project_id=project_id)
        return {"id": "batch_2"}

    monkeypatch.setattr(api, "_request", fake)
    api.start_batch("proj_1", ["file_1", "file_2"], "georeference")
    assert captured == {
        "payload": {
            "kind": "georeference",
            "name": "[QGIS] Georeference",
            "sources": [{"file_id": "file_1"}, {"file_id": "file_2"}],
        },
        "project_id": "proj_1",
    }


def test_run_and_layer_geojson_paths(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")
    calls = []

    def fake_request(method, path, payload=None, project_id=""):
        calls.append((method, path, project_id))
        return {"id": "run_1", "result_references": []}

    def fake_download(path, project_id=""):
        calls.append(("GET_BYTES", path, project_id))
        return b'{"type":"FeatureCollection","features":[]}'

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(api, "download_bytes", fake_download)
    assert api.run("run_1", "proj_1")["id"] == "run_1"
    assert api.layer_geojson("layer_1", "proj_1")[:1] == b"{"
    assert calls[0] == ("GET", "/v1/runs/run_1", "proj_1")
    assert calls[1] == ("GET_BYTES", "/v1/layers/layer_1/geojson?limit=5000", "proj_1")


def test_run_list_is_project_scoped_by_the_header_and_survives_an_envelope(monkeypatch):
    # The server scopes `GET /v1/runs` by `X-Project-ID` and answers with a bare
    # array. Reading only the array shape would turn a server that later wraps
    # the list into an empty job list on the desktop, which reads as "you have
    # no runs" rather than as a shape this client cannot parse.
    api = MapdexAPI("https://api.mapdex.ai", "token")
    calls = []

    def bare(method, path, payload=None, project_id=""):
        calls.append((method, path, project_id))
        return [{"id": "run_1"}, "not a run"]

    monkeypatch.setattr(api, "_request", bare)
    assert api.runs("proj_1") == [{"id": "run_1"}]
    assert calls[0] == ("GET", "/v1/runs", "proj_1")

    monkeypatch.setattr(api, "_request", lambda *a, **k: {"runs": [{"id": "run_2"}]})
    assert api.runs("proj_1") == [{"id": "run_2"}]


def test_api_error_parses_nested_envelope(monkeypatch):
    api = MapdexAPI("https://api.mapdex.ai", "token")

    class FakeResponse:
        def read(self):
            return json.dumps(
                {"error": {"message": "Batch not found", "correlation_id": "cor_1"}}
            ).encode()

        def close(self):
            return None

    class FakeHTTPError(error.HTTPError):
        def __init__(self):
            super().__init__(
                "https://api.mapdex.ai/v1/x",
                404,
                "Not Found",
                hdrs={"Retry-After": "17"},
                fp=FakeResponse(),
            )

    def boom(*_args, **_kwargs):
        raise FakeHTTPError()

    monkeypatch.setattr("mapdex_qgis.api_client._urlopen", boom)
    try:
        api.batch("proj_1", "batch_missing")
        raise AssertionError("expected MapdexAPIError")
    except MapdexAPIError as exc:
        assert exc.status == 404
        assert "Batch not found" in str(exc)
        assert exc.correlation_id == "cor_1"
        assert exc.retry_after == 17


def test_succeeded_and_review_run_ids():
    detail = {
        "status": "partial",
        "items": [
            {"id": "i1", "state": "succeeded", "run_id": "run_ok"},
            {"id": "i2", "state": "needs_review", "run_id": "run_rev"},
            {"id": "i3", "state": "failed", "run_id": "run_bad"},
            {"id": "i4", "state": "succeeded"},
        ],
    }
    assert batch_is_terminal(detail)
    assert succeeded_run_ids(detail) == ["run_ok"]
    assert review_run_ids(detail) == ["run_rev"]


def test_first_batch_error_returns_message_and_reference():
    detail = {
        "items": [{
            "state": "failed",
            "error": {"message": "The source has no CRS.", "correlation_id": "cor_42"},
        }]
    }
    assert first_batch_error(detail) == "The source has no CRS. (reference cor_42)"


def test_collect_vector_layer_imports_skips_raster_and_duplicates():
    run = {
        "result_references": [
            {
                "kind": "layer",
                "id": "layer_a",
                "summary": "Parcels",
                "geometry_type": "Polygon",
            },
            {
                "kind": "layer",
                "id": "layer_a",
                "summary": "Parcels again",
                "geometry_type": "Polygon",
            },
            {
                "kind": "layer",
                "id": "layer_r",
                "summary": "COG",
                "geometry_type": "raster",
            },
            {"kind": "export", "id": "exp_1", "url": "/v1/exports/exp_1"},
        ]
    }
    assert collect_vector_layer_imports(run) == [
        {"layer_id": "layer_a", "name": "Parcels"}
    ]


def test_collect_raster_layer_imports_selects_georeferenced_result():
    run = {
        "result_references": [
            {"kind": "layer", "id": "layer_r", "summary": "Georeferenced map", "geometry_type": "raster"},
            {"kind": "layer", "id": "layer_v", "summary": "Parcels", "geometry_type": "Polygon"},
            {"kind": "layer", "id": "layer_r", "summary": "Duplicate", "geometry_type": "raster"},
        ]
    }
    assert collect_raster_layer_imports(run) == [
        {"layer_id": "layer_r", "name": "Georeferenced map"}
    ]


def test_collect_layer_imports_keeps_layer_when_run_omits_geometry_type():
    run = {
        "result_references": [
            {"kind": "layer", "id": "layer_raster", "summary": "Georeferenced map"}
        ]
    }

    assert collect_layer_imports(run) == [
        {
            "layer_id": "layer_raster",
            "name": "Georeferenced map",
            "geometry_type": "",
        }
    ]


def test_collect_vector_layer_imports_from_output_results_fallback():
    run = {
        "output": {
            "results": [
                {
                    "reference": {"kind": "layer", "id": "layer_b"},
                    "label": "Validated",
                    "url": "/v1/layers/layer_b",
                }
            ]
        }
    }
    assert collect_vector_layer_imports(run) == [
        {"layer_id": "layer_b", "name": "Validated"}
    ]


def test_collect_geojson_artifact_urls():
    run = {
        "result_references": [
            {
                "kind": "artifact",
                "id": "art_1",
                "url": "/v1/artifacts/art_1/download",
                "format": "geojson",
                "summary": "Preview",
            },
            {
                "kind": "artifact",
                "id": "art_2",
                "url": "/v1/artifacts/art_2/download",
                "format": "zip",
                "summary": "Delivery",
            },
        ]
    }
    assert collect_geojson_artifact_urls(run) == [
        {"url": "/v1/artifacts/art_1/download", "name": "Preview"}
    ]
