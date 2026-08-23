"""Credential-handling guarantees for the published plugin."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import (
    MapdexAPI,
    MapdexAPIError,
    device_verification_url,
    is_transport_secure,
    safe_filename_part,
    same_origin,
)


def _api():
    return MapdexAPI("https://api.mapdex.ai", token="secret-token")


def test_token_only_travels_to_the_configured_api_origin():
    api = _api()
    assert "Authorization" in api._headers(url="https://api.mapdex.ai/v1/files")
    # Pre-signed object storage and any other host must never see the token.
    assert "Authorization" not in api._headers(url="https://storage.example.com/signed")
    assert "Authorization" not in api._headers(url="https://api.mapdex.ai.evil.com/v1")
    assert "Authorization" not in api._headers(url="http://api.mapdex.ai/v1")


def test_third_party_download_carries_neither_token_nor_tenant(monkeypatch):
    api = _api()
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured["headers"] = {key.lower(): value for key, value in req.header_items()}
        return FakeResponse()

    monkeypatch.setattr("mapdex_qgis.api_client._urlopen", fake_urlopen)

    api.download_bytes("https://storage.example.com/result.geojson", project_id="proj_1")
    assert "authorization" not in captured["headers"]
    assert "x-project-id" not in captured["headers"]

    api.download_bytes("/v1/layers/layer_1/geojson", project_id="proj_1")
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["x-project-id"] == "proj_1"


def test_download_refuses_plaintext_third_party_urls():
    api = _api()
    with pytest.raises(MapdexAPIError):
        api.download_bytes("http://storage.example.com/result.geojson")


def test_upload_refuses_plaintext_storage_urls():
    api = _api()
    with pytest.raises(MapdexAPIError):
        api._put_upload(__file__, {"url": "http://storage.example.com/put"})


def test_local_development_stays_allowed():
    assert is_transport_secure("http://127.0.0.1:8080/v1")
    assert is_transport_secure("http://localhost:8080/v1")
    assert is_transport_secure("https://api.mapdex.ai")
    assert not is_transport_secure("http://api.mapdex.ai")


def test_same_origin_compares_scheme_host_and_port():
    assert same_origin("https://api.mapdex.ai/v1/x", "https://api.mapdex.ai")
    assert not same_origin("https://api.mapdex.ai:8443", "https://api.mapdex.ai")
    assert not same_origin("https://evil.example", "https://api.mapdex.ai")
    assert not same_origin("", "https://api.mapdex.ai")


def test_device_url_rejects_non_web_schemes_from_the_server():
    web = "https://mapdex.ai"
    api = "https://api.mapdex.ai"
    app_device = "https://app.mapdex.ai/device"
    assert device_verification_url("javascript:alert(1)", web, api) == app_device
    assert device_verification_url("file:///etc/passwd", web, api) == app_device
    assert device_verification_url("http://mapdex.ai/device", web, api) == app_device
    assert device_verification_url("", web, api) == app_device
    # A production API may not send the user to their own machine.
    assert device_verification_url("http://localhost:3000/device", web, api) == app_device
    # A legitimate https address is kept.
    assert device_verification_url("https://mapdex.ai/device", web, api) == app_device
    # Self-hosted/local development keeps working.
    assert (
        device_verification_url("http://127.0.0.1:3000/device", "http://127.0.0.1:3000", "http://127.0.0.1:8080")
        == "http://127.0.0.1:3000/device"
    )


def test_redirect_off_the_api_origin_drops_the_token():
    """urllib replays headers on 30x; an off-origin hop must not keep the token."""
    from urllib import request as urllib_request

    from mapdex_qgis.api_client import _AuthStrippingRedirectHandler

    handler = _AuthStrippingRedirectHandler()

    def redirected(new_url):
        original = urllib_request.Request(
            "https://api.mapdex.ai/v1/files/file_1/download",
            headers={"Authorization": "Bearer secret-token", "Accept": "*/*"},
        )
        return handler.redirect_request(original, None, 302, "Found", {}, new_url)

    off_origin = redirected("https://evil.example/steal")
    assert off_origin is not None
    assert not off_origin.has_header("Authorization")

    same_host = redirected("https://api.mapdex.ai/v1/files/file_1/download2")
    assert same_host is not None
    assert same_host.get_header("Authorization") == "Bearer secret-token"


def test_server_ids_cannot_escape_the_download_directory():
    assert safe_filename_part("layer_abc123") == "layer_abc123"
    assert "/" not in safe_filename_part("../../etc/passwd")
    assert "\\" not in safe_filename_part("..\\..\\windows\\system32")
    assert safe_filename_part("") == "result"
    assert safe_filename_part("...") == "result"
    assert len(safe_filename_part("x" * 400)) <= 120
