"""What happens when a connector's access token runs out mid-session.

A connector access token lives an hour; a QGIS session does not. Every one of
these tests is about the same thing from a different angle: the renewal must be
invisible when it works, and must end as "sign in again" when it does not,
without ever becoming a loop.
"""

import io
import json
import pathlib
import sys
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI, MapdexAPIError  # noqa: E402


class _Recorded:
    """One scripted HTTP exchange: what the client sent, and what it gets back."""

    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {}
        self.status = status


def _transport(script, seen):
    """Replace the client's opener with a scripted one.

    The Authorization header of every attempt is recorded, because "did the
    retry actually carry the NEW token" is the question a renewal test exists
    to answer, and a passing test that never looked would be satisfied by a
    retry with the same expired credential.
    """
    attempts = iter(script)

    def opener(req, timeout=None):  # noqa: ARG001
        seen.append(req.get_header("Authorization"))
        step = next(attempts)
        if step.status == 401:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(json.dumps({"code": "UNAUTHENTICATED"}).encode("utf-8")),
            )
        body = json.dumps(step.payload).encode("utf-8")

        class Response:
            def read(self_inner):
                return body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

        return Response()

    return opener


@pytest.fixture(autouse=True)
def _patch_urlopen(monkeypatch):
    holder = {}

    def install(opener):
        holder["opener"] = opener

    def dispatch(req, timeout=None):
        return holder["opener"](req, timeout)

    monkeypatch.setattr("mapdex_qgis.api_client._urlopen", dispatch)
    return install


def test_an_expired_token_is_renewed_and_the_request_retried(_patch_urlopen):
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401), _Recorded({"projects": []})], seen))

    api = MapdexAPI("https://api.example", token="stale")
    api.on_token_expired = lambda: "fresh"
    assert api.projects() == []

    assert seen == ["Bearer stale", "Bearer fresh"]
    # And the client keeps the renewed one, or the next request repeats the
    # whole round trip.
    assert api.token == "fresh"


def test_a_renewal_that_produces_nothing_reports_the_original_refusal(_patch_urlopen):
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401)], seen))

    api = MapdexAPI("https://api.example", token="stale")
    # This is what a browser-side Disconnect looks like from here. It must end
    # as a refusal the person can act on, not as a retry.
    api.on_token_expired = lambda: ""
    with pytest.raises(MapdexAPIError):
        api.projects()
    assert len(seen) == 1


def test_a_client_with_no_renewal_path_behaves_exactly_as_before(_patch_urlopen):
    # An API-key caller and an older device-grant install both land here. The
    # 401 must reach them unchanged.
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401)], seen))

    api = MapdexAPI("https://api.example", token="stale")
    with pytest.raises(MapdexAPIError):
        api.projects()
    assert len(seen) == 1


def test_a_renewed_token_that_is_also_refused_does_not_loop(_patch_urlopen):
    # The dangerous shape: a server refusing every token would otherwise be an
    # infinite recursion, and the symptom is a frozen QGIS rather than an error.
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401), _Recorded(status=401)], seen))

    calls = []

    def renew():
        calls.append(1)
        return "fresh"

    api = MapdexAPI("https://api.example", token="stale")
    api.on_token_expired = renew
    with pytest.raises(MapdexAPIError):
        api.projects()
    assert seen == ["Bearer stale", "Bearer fresh"]
    assert len(calls) == 1


def test_a_renewal_that_raises_is_a_failed_request_not_a_crash(_patch_urlopen):
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401)], seen))

    def renew():
        raise RuntimeError("the authentication database is locked")

    api = MapdexAPI("https://api.example", token="stale")
    api.on_token_expired = renew
    with pytest.raises(MapdexAPIError):
        api.projects()


def test_a_failed_renewal_does_not_wedge_the_client_for_the_session(_patch_urlopen):
    # The guard against a refresh loop is a flag, and a flag left set is a
    # client that never tries again. A person who reconnects in the browser
    # must be able to keep working without restarting QGIS.
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401)], seen))
    api = MapdexAPI("https://api.example", token="stale")
    api.on_token_expired = lambda: ""
    with pytest.raises(MapdexAPIError):
        api.projects()

    seen2 = []
    _patch_urlopen(_transport([_Recorded(status=401), _Recorded({"projects": []})], seen2))
    api.on_token_expired = lambda: "fresh"
    assert api.projects() == []
    assert seen2 == ["Bearer stale", "Bearer fresh"]


def test_a_successful_renewal_leaves_the_client_able_to_renew_again(_patch_urlopen):
    # An hour later it happens again. The flag cleared on the retry path has to
    # actually be cleared, and the only way to know is to do it twice.
    seen = []
    _patch_urlopen(_transport([_Recorded(status=401), _Recorded({"projects": []})], seen))
    api = MapdexAPI("https://api.example", token="t1")
    api.on_token_expired = lambda: "t2"
    api.projects()

    seen2 = []
    _patch_urlopen(_transport([_Recorded(status=401), _Recorded({"projects": []})], seen2))
    api.on_token_expired = lambda: "t3"
    assert api.projects() == []
    assert seen2 == ["Bearer t2", "Bearer t3"]


def test_a_403_is_not_a_token_problem_and_is_never_renewed(_patch_urlopen):
    # A desktop connector refused by the surface check gets 403. Renewing there
    # would spend a refresh token on a request that will be refused identically,
    # and would hide a policy decision behind an authentication one.
    calls = []

    def opener(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            io.BytesIO(json.dumps({"code": "FORBIDDEN_SCOPE"}).encode("utf-8")),
        )

    _patch_urlopen(opener)
    api = MapdexAPI("https://api.example", token="fine")
    api.on_token_expired = lambda: calls.append(1) or "fresh"
    with pytest.raises(MapdexAPIError):
        api.projects()
    assert calls == []
