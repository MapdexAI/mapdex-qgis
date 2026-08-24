"""The OAuth connector flow QGIS uses, and the checks with a consequence."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from mapdex_qgis.connector_auth import (
    CLIENT_ID,
    SCOPES,
    ConnectorAuthError,
    LoopbackReceiver,
    build_authorization_url,
    challenge_for,
    discover_endpoints,
    exchange_code,
    expiry_from,
    make_verifier,
    parse_callback,
    refresh_tokens,
)


# --------------------------------------------------------------------------
# PKCE. The verifier is the only thing standing between an intercepted code
# and a token, so it gets measured rather than assumed.
# --------------------------------------------------------------------------

def test_the_verifier_is_inside_the_length_the_rfc_allows():
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128


def test_two_verifiers_are_never_the_same():
    # Generated from the system CSPRNG. A predictable verifier is no protection
    # at all, and the cheapest way to be wrong here is a seeded generator.
    assert len({make_verifier() for _ in range(64)}) == 64


def test_the_challenge_is_the_sha256_of_the_verifier_and_is_not_the_verifier():
    verifier = make_verifier()
    challenge = challenge_for(verifier)
    assert challenge != verifier
    assert challenge == challenge_for(verifier)
    assert "=" not in challenge  # base64url, unpadded, per RFC 7636


def test_the_authorization_url_never_offers_plain_pkce():
    url = build_authorization_url("https://api.example/oauth/authorize", "http://127.0.0.1:1/cb",
                                  "st", "ch", "https://mcp.example")
    assert "code_challenge_method=S256" in url
    assert "plain" not in url
    assert "client_id=" + CLIENT_ID in url
    # RFC 8707. Without it the token names no audience and authenticates nowhere.
    assert "resource=https" in url
    for scope in SCOPES:
        assert scope.replace(":", "%3A") in url or scope in url


def test_the_verifier_itself_is_never_put_in_the_authorization_url():
    # The whole point of PKCE: the browser leg carries the challenge, and only
    # the token request carries the secret it was derived from.
    verifier = make_verifier()
    url = build_authorization_url("https://api.example/oauth/authorize", "http://127.0.0.1:1/cb",
                                  "st", challenge_for(verifier))
    assert verifier not in url


# --------------------------------------------------------------------------
# The callback. This is where somebody else's code would arrive.
# --------------------------------------------------------------------------

def test_a_callback_with_the_wrong_state_is_refused_without_reading_its_code():
    with pytest.raises(ConnectorAuthError) as caught:
        parse_callback("/callback?code=stolen&state=somebody_elses", "mine")
    assert caught.value.code == "state_mismatch"


def test_a_callback_with_no_state_is_refused_even_when_none_was_expected():
    # An empty expected state means this process never started a flow. Matching
    # empty against empty would accept any callback at all.
    with pytest.raises(ConnectorAuthError):
        parse_callback("/callback?code=abc", "")


def test_a_declined_consent_says_it_was_declined():
    with pytest.raises(ConnectorAuthError) as caught:
        parse_callback("/callback?error=access_denied&state=st", "st")
    assert caught.value.code == "access_denied"
    assert "declined" in str(caught.value).lower()


def test_a_matching_callback_yields_its_code():
    assert parse_callback("/callback?code=abc123&state=st", "st") == "abc123"


def test_a_callback_with_neither_code_nor_error_is_a_failure_not_an_empty_token():
    with pytest.raises(ConnectorAuthError) as caught:
        parse_callback("/callback?state=st", "st")
    assert caught.value.code == "no_code"


# --------------------------------------------------------------------------
# The loopback receiver, driven end to end against a real socket.
# --------------------------------------------------------------------------

def test_the_receiver_binds_loopback_on_a_port_the_system_chose():
    with LoopbackReceiver() as receiver:
        assert receiver.redirect_uri.startswith("http://127.0.0.1:")
        port = int(receiver.redirect_uri.split(":")[2].split("/")[0])
        # Never a fixed port: RFC 8252, and a fixed one fails whenever anything
        # else on the machine already holds it.
        assert port > 0


def test_the_receiver_hands_back_the_code_the_browser_delivered():
    with LoopbackReceiver() as receiver:
        answer = {}

        def deliver():
            url = receiver.redirect_uri + "?code=live_code&state=" + receiver.state
            with urllib.request.urlopen(url, timeout=5) as response:
                answer["body"] = response.read().decode("utf-8")

        thread = threading.Thread(target=deliver)
        thread.start()
        assert receiver.wait(timeout=5) == "live_code"
        thread.join(timeout=5)
    # The person still has to be told to go back to QGIS.
    assert "QGIS" in answer["body"]


def test_the_receiver_refuses_a_callback_from_another_flow():
    with LoopbackReceiver() as receiver:
        def deliver():
            url = receiver.redirect_uri + "?code=stolen&state=not_this_flow"
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:  # noqa: BLE001 - the page still renders; this is belt and braces
                pass

        thread = threading.Thread(target=deliver)
        thread.start()
        with pytest.raises(ConnectorAuthError) as caught:
            receiver.wait(timeout=5)
        thread.join(timeout=5)
    assert caught.value.code == "state_mismatch"


def test_waiting_for_a_browser_that_never_returns_ends_with_a_reason():
    with LoopbackReceiver() as receiver:
        with pytest.raises(ConnectorAuthError) as caught:
            receiver.wait(timeout=0.2)
    assert caught.value.code == "timeout"


def test_two_receivers_never_share_a_state():
    first, second = LoopbackReceiver(), LoopbackReceiver()
    try:
        assert first.state != second.state
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------
# The token endpoint, with the network stubbed by argument.
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def close(self):
        """urllib closes what it hands back, and HTTPError treats its body as a
        file. A double missing this raises during interpreter cleanup, which
        surfaces as an unrelated warning in whichever test ran last."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _opener_returning(payload, captured=None):
    def opener(target, timeout=None):  # noqa: ARG001
        if captured is not None:
            captured.append(target)
        return _Response(payload)
    return opener


def test_discovery_reads_the_published_endpoints_rather_than_guessing_paths():
    captured = []
    endpoints = discover_endpoints(
        "https://api.example",
        opener=_opener_returning(
            {"authorization_endpoint": "https://api.example/oauth/authorize",
             "token_endpoint": "https://api.example/oauth/token",
             "issuer": "https://api.example"},
            captured,
        ),
    )
    assert endpoints["token"] == "https://api.example/oauth/token"
    assert captured[0].endswith("/.well-known/oauth-authorization-server")


def test_a_metadata_document_missing_an_endpoint_is_a_named_failure():
    with pytest.raises(ConnectorAuthError) as caught:
        discover_endpoints("https://api.example",
                           opener=_opener_returning({"issuer": "https://api.example"}))
    assert caught.value.code == "metadata_incomplete"


def test_the_code_exchange_sends_the_verifier_and_the_client_id():
    captured = []
    payload = exchange_code(
        "https://api.example/oauth/token", "the_code", "the_verifier",
        "http://127.0.0.1:5/cb", "https://mcp.example",
        opener=_opener_returning({"access_token": "at", "refresh_token": "rt",
                                  "expires_in": 3600}, captured),
    )
    body = captured[0].data.decode("ascii")
    assert "code_verifier=the_verifier" in body
    assert "client_id=" + CLIENT_ID in body
    assert "grant_type=authorization_code" in body
    assert payload["access_token"] == "at"


def test_a_refresh_sends_the_refresh_grant_and_returns_the_rotated_pair():
    captured = []
    payload = refresh_tokens(
        "https://api.example/oauth/token", "old_rt", "https://mcp.example",
        opener=_opener_returning({"access_token": "at2", "refresh_token": "rt2",
                                  "expires_in": 3600}, captured),
    )
    body = captured[0].data.decode("ascii")
    assert "grant_type=refresh_token" in body
    assert "refresh_token=old_rt" in body
    # The server rotates. A caller that stored only the access token would
    # present a revoked refresh token next time and lose the whole chain.
    assert payload["refresh_token"] == "rt2"


def test_a_token_response_with_no_access_token_is_a_failure():
    with pytest.raises(ConnectorAuthError) as caught:
        exchange_code("https://api.example/oauth/token", "c", "v", "http://127.0.0.1:5/cb",
                      opener=_opener_returning({"token_type": "Bearer"}))
    assert caught.value.code == "no_token"


def test_an_oauth_error_body_is_reported_by_its_code_not_as_http_400():
    # RFC 6749 5.2. `invalid_grant` means start over; a generic "HTTP 400"
    # leaves a person retrying something that will never work again.
    def opener(target, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(
            "https://api.example/oauth/token", 400, "Bad Request", {},
            _Response({"error": "invalid_grant",
                       "error_description": "This connection was disconnected."}),
        )

    with pytest.raises(ConnectorAuthError) as caught:
        refresh_tokens("https://api.example/oauth/token", "rt", opener=opener)
    assert caught.value.code == "invalid_grant"
    assert "disconnected" in str(caught.value)


def test_an_unreachable_server_says_so_rather_than_raising_a_url_error():
    def opener(target, timeout=None):  # noqa: ARG001
        raise urllib.error.URLError("no route to host")

    with pytest.raises(ConnectorAuthError) as caught:
        discover_endpoints("https://api.example", opener=opener)
    assert caught.value.code == "unreachable"


# --------------------------------------------------------------------------
# Expiry.
# --------------------------------------------------------------------------

def test_expiry_is_absolute_and_carries_a_minute_of_slack():
    # Stored and read back in a later session, so a duration written down is a
    # duration measured from whenever somebody reads it.
    assert expiry_from({"expires_in": 3600}, now=1000.0) == 1000.0 + 3540


def test_a_response_with_no_expiry_is_treated_as_already_expired():
    # Refreshing something still good costs one request. Trusting a token that
    # is not costs a failure in front of the user.
    assert expiry_from({}, now=1000.0) == 1000.0
    assert expiry_from({"expires_in": "nonsense"}, now=1000.0) == 1000.0


def test_closing_a_receiver_that_never_started_does_not_hang():
    # Found by hanging. HTTPServer.shutdown waits for serve_forever to
    # acknowledge it, so calling it on a server that was never started blocks
    # forever - and loopback_available does precisely that on every call, to
    # find out whether a socket can be bound at all. The probe would have
    # frozen QGIS before the person ever reached a browser.
    finished = threading.Event()

    def probe():
        receiver = LoopbackReceiver()
        receiver.close()
        finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    assert finished.wait(timeout=5), "closing an unstarted receiver blocked"


def test_the_capability_probe_answers_and_releases_its_socket():
    from mapdex_qgis.connector_auth import loopback_available

    finished = threading.Event()
    answer = {}

    def probe():
        answer["ok"], answer["why"] = loopback_available()
        finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    assert finished.wait(timeout=5), "the loopback probe blocked"
    assert answer["ok"] is True
