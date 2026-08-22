"""The plugin must identify itself on every request to the Mapdex API.

The API classifies each request into a bounded `client` label for
mapdex_http_requests_total and already has a qgis_plugin bucket: it looks for
"qgis" in the User-Agent, or for `X-Client-Surface: qgis`. This client sent
neither, so urllib's default "Python-urllib/3.x" matched the api_client rule
instead, and every request the plugin had ever made was counted as a generic
API caller. Heavy real QGIS use showed as nothing on the QGIS panels.

The classification rule lives in the API
(apps/api/internal/transport/http/middleware.go). These tests restate it here
deliberately: if somebody narrows the server rule, this suite is what says the
desktop client went invisible, on the surface that would otherwise only be
noticed as a quiet dashboard weeks later.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI
from mapdex_qgis.build_version import PLUGIN_VERSION


def classify_like_the_api(headers):
    """The server's own rule, reproduced from middleware.go.

    Order matters: the qgis test runs first, so a client that satisfies both
    rules is counted as qgis rather than as a generic API caller.
    """
    user_agent = str(headers.get("User-Agent", "")).lower()
    if "qgis" in user_agent or headers.get("X-Client-Surface") == "qgis":
        return "qgis_plugin"
    if "curl" in user_agent or "python" in user_agent or headers.get("X-API-Key"):
        return "api_client"
    return "web_studio"


def test_headers_identify_the_surface():
    headers = MapdexAPI("https://api.mapdex.ai", token="t")._headers(project_id="proj_1")

    assert headers["X-Client-Surface"] == "qgis"
    assert headers["User-Agent"] == f"mapdex-qgis/{PLUGIN_VERSION}"
    assert "qgis" in headers["User-Agent"].lower()


def test_the_api_would_count_these_requests_as_qgis():
    api = MapdexAPI("https://api.mapdex.ai", token="t")

    for content_type in ("application/json", None, "multipart/form-data; boundary=x"):
        headers = api._headers(project_id="proj_1", content_type=content_type)
        assert classify_like_the_api(headers) == "qgis_plugin", (
            f"content_type={content_type!r} would be counted as "
            f"{classify_like_the_api(headers)}"
        )


def test_without_identification_the_api_would_have_counted_us_as_a_generic_caller():
    """The control. Without this test the one above could pass against a rule
    that classifies everything as qgis, which would prove nothing."""
    assert classify_like_the_api({"User-Agent": "Python-urllib/3.11"}) == "api_client"
    assert classify_like_the_api({}) == "web_studio"


def test_identification_carries_nothing_about_the_user():
    headers = MapdexAPI("https://api.mapdex.ai", token="secret-token")._headers(project_id="proj_1")

    # A metric label is a bounded, low-cardinality value. The surface marker
    # must never carry a token, a user, or a project.
    assert "secret-token" not in headers["User-Agent"]
    assert "proj_1" not in headers["User-Agent"]
    assert headers["X-Client-Surface"] == "qgis"


def test_the_credential_rule_is_unchanged_by_identification():
    """Identification is added to every request; the token still is not.

    A pre-signed object-storage URL is a different origin and needs no Mapdex
    credential, and sending one there would leak it to the storage provider.
    """
    api = MapdexAPI("https://api.mapdex.ai", token="secret-token")

    same_origin = api._headers(url="https://api.mapdex.ai/v1/files")
    assert same_origin["Authorization"] == "Bearer secret-token"
    assert same_origin["X-Client-Surface"] == "qgis"

    other_origin = api._headers(url="https://storage.example.com/signed-put")
    assert "Authorization" not in other_origin
