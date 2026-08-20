"""Cancelling a device authorization the user walked away from.

A connect shows a code and waits for the browser. If the user disconnects
instead of approving, the plugin stops polling — and the authorization stays
pending on the server until it expires, so a code still on screen can be
approved by anyone who reads it.

`/v1/auth/device/revoke` existed for exactly this and had no caller that could
reach it: it sat behind Auth, while the only party holding the device code is
this plugin, which has no token until the flow completes. The route moved to the
public band beside `/v1/auth/device/token`, whose proof is the same device code
and which is strictly more powerful — it issues a bearer token on it.

`plugin.py` imports `qgis` at module scope and cannot be imported in CI, so its
wiring is asserted at source level, the way the rest of these tests do it.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")


def _method_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("method not found: " + name)


def test_the_client_sends_the_device_code_to_the_revoke_route():
    sent = {}

    def fake_request(method, path, payload=None, project_id=""):
        sent.update({"method": method, "path": path, "payload": payload})
        return None

    api = MapdexAPI("https://api.mapdex.ai")
    api._request = fake_request
    api.revoke_device("dev_abc")

    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/auth/device/revoke"
    # The device code is the whole proof, so it has to be in the body.
    assert sent["payload"] == {"device_code": "dev_abc"}


def test_disconnect_cancels_a_pending_authorization():
    source = _method_source(PLUGIN, "disconnect")
    assert "revoke_device" in source, "disconnect must cancel a pending authorization"
    # Guarded on the code: once a token has been issued the authorization is
    # already `consumed`, so there is nothing left to cancel and no call to make.
    assert "if self.device_code:" in source
    # And it must not be able to strand a half-disconnected session.
    assert "except Exception" in source


def test_disconnect_clears_the_session_after_revoking_not_before():
    source = _method_source(PLUGIN, "disconnect")
    revoke_at = source.index("revoke_device")
    cleared_at = source.index('self.device_code = ""')
    assert revoke_at < cleared_at, "the code is the argument; clearing it first sends nothing"
