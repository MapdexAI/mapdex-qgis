"""A published build must be pinned to the hosted Mapdex."""
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")
PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mapdex_qgis import build_profile
from mapdex_qgis.build_profile import (
    DEVELOPMENT_API,
    DEVELOPMENT_WEB,
    PRODUCTION_API,
    PRODUCTION_WEB,
    endpoints_unlocked,
    resolve_endpoints,
)


def test_the_working_tree_stays_a_development_build():
    assert build_profile.CHANNEL == "development"
    assert build_profile.is_production() is False


def test_a_development_build_may_point_anywhere():
    assert endpoints_unlocked("") is True
    api, web, drop = resolve_endpoints("http://127.0.0.1:8080", "http://127.0.0.1:3000", True)
    assert (api, web, drop) == ("http://127.0.0.1:8080", "http://127.0.0.1:3000", False)


def test_a_clean_development_build_defaults_to_the_local_stack():
    api, web, drop = resolve_endpoints("", "", True)
    assert (api, web, drop) == (DEVELOPMENT_API, DEVELOPMENT_WEB, False)


def test_endpoint_fields_follow_the_build_profile():
    assert "endpoint_settings=self._endpoints_unlocked" in PLUGIN
    assert "if not endpoint_settings:" in PANEL
    assert "endpoint_frame.setVisible(False)" in PANEL
    # The choices come from the build profile, not a literal in the panel: a
    # literal is what put 127.0.0.1 first in a released build's Settings.
    assert "endpoint_choices()" in PANEL
    assert "127.0.0.1" not in PANEL


def test_a_released_build_never_resolves_to_this_computer(monkeypatch):
    """The case that shipped: an unlock and a local address left in a profile.

    A development session set `allow_custom_endpoint` and pointed the plugin at
    the local stack. A genuine production package was later installed into the
    same QGIS profile and sent every request to 127.0.0.1, while the build
    itself said production. The unlock is for a self-hosted server; it never
    makes this computer a Mapdex.
    """
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    api, web, drop = resolve_endpoints("http://127.0.0.1:8080", "http://127.0.0.1:3000", True)
    assert (api, web) == (PRODUCTION_API, PRODUCTION_WEB)
    # The session came from the local stack and must not travel to production.
    assert drop is True
    for host in ("http://localhost:8080", "http://[::1]:8080", "HTTP://LOCALHOST"):
        assert resolve_endpoints(host, "", True)[0] == PRODUCTION_API, host


def test_an_unlocked_release_still_honours_a_self_hosted_server(monkeypatch):
    # What the unlock is for, and it has to keep working.
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    api, web, drop = resolve_endpoints(
        "https://mapdex.example.org/api", "https://mapdex.example.org", True)
    assert (api, web, drop) == ("https://mapdex.example.org/api", "https://mapdex.example.org", False)


def test_a_loopback_web_address_alone_keeps_the_session(monkeypatch):
    # The token belongs to the API. A stray local WEB address is ignored but is
    # no reason to sign somebody out of a server that is still the right one.
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    api, web, drop = resolve_endpoints("https://mapdex.example.org", "http://127.0.0.1:3000", True)
    assert (api, web, drop) == ("https://mapdex.example.org", PRODUCTION_WEB, False)


def test_a_released_build_offers_no_address_on_this_computer(monkeypatch):
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    api_choices, web_choices = build_profile.endpoint_choices()
    assert api_choices == (PRODUCTION_API,)
    assert web_choices == (PRODUCTION_WEB,)
    assert not any(build_profile.is_loopback(value) for value in api_choices + web_choices)


def test_a_development_build_still_offers_the_local_stack():
    api_choices, web_choices = build_profile.endpoint_choices()
    assert api_choices[0] == DEVELOPMENT_API
    assert web_choices[0] == DEVELOPMENT_WEB


def test_a_released_build_refuses_a_loopback_address_typed_by_hand(monkeypatch):
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    for url in ("http://127.0.0.1:8080", "http://localhost:3000", "http://[::1]:8080"):
        refusal = build_profile.endpoint_refusal(url)
        assert "released build" in refusal and "this computer" in refusal, url
    assert build_profile.endpoint_refusal("https://mapdex.example.org") == ""


def test_a_development_build_accepts_the_local_stack():
    assert build_profile.endpoint_refusal(DEVELOPMENT_API) == ""


def test_the_transport_check_and_the_profile_agree_on_this_computer():
    # One set with two readers. Two sets is how a check that allows plaintext
    # to "this computer" and a profile that refuses "this computer" would drift.
    from mapdex_qgis import api_client

    assert api_client.LOCAL_HOSTS is build_profile.LOOPBACK_HOSTS


def test_a_pinned_build_ignores_a_stored_development_endpoint():
    api, web, drop = resolve_endpoints("http://127.0.0.1:8080", "http://127.0.0.1:3000", False)
    assert api == PRODUCTION_API
    assert web == PRODUCTION_WEB
    # The token issued by the local stack must not travel to production.
    assert drop is True


def test_a_pinned_build_keeps_a_matching_session():
    _, _, drop = resolve_endpoints(PRODUCTION_API, PRODUCTION_WEB, False)
    assert drop is False


def test_support_can_unlock_a_released_build_deliberately(monkeypatch):
    monkeypatch.setattr(build_profile, "CHANNEL", "production")
    assert endpoints_unlocked("") is False
    assert endpoints_unlocked("false") is False
    assert endpoints_unlocked("true") is True
    assert endpoints_unlocked("1") is True
    api, web, drop = resolve_endpoints("", "", True)
    assert (api, web, drop) == (PRODUCTION_API, PRODUCTION_WEB, False)


def test_release_package_is_stamped_production(tmp_path):
    import package

    built = ROOT / "dist" / "mapdex-qgis.zip"
    if not built.is_file():
        import pytest

        pytest.skip("run scripts/package.py first")
    channel = [
        line
        for line in zipfile.ZipFile(built).read("mapdex/build_profile.py").decode().splitlines()
        if line.startswith("CHANNEL")
    ]
    assert channel and channel[0].split("=")[1].strip().strip("\"") in {"development", "production"}
    assert package.channel_from(["--channel", "production"]) == "production"
    assert package.channel_from([]) == "development"
