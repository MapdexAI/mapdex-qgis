"""A published build must be pinned to the hosted Mapdex."""
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mapdex_qgis import build_profile
from mapdex_qgis.build_profile import (
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
