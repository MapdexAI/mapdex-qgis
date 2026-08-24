"""Connecting QGIS to Mapdex the way every other connector connects.

Until now this plugin authenticated with a device grant: it asked the server for
a code, the person typed that code into a browser, and the plugin was handed a
full Mapdex session token. It worked, and it had two problems that were not
about whether it worked.

The token was a SESSION. It reached every route a signed-in person reaches, so a
desktop tool that uploads files and starts runs also held the credential for
billing, API keys and member management. And it was invisible: a person could
see their assistants on the Connectors screen and disconnect any of them, while
the program on their own machine appeared nowhere and could be withdrawn from
nowhere.

The authorization-code flow with PKCE fixes both, and the fix is not this file.
It is that the token comes back scoped to the workspace surface and carrying a
connection row, so QGIS shows up beside Claude and is revoked the same way. What
this file does is the client half: build the request, hold the verifier, receive
the code on a loopback socket, and swap it for tokens.

Everything here is a plain function or a small object with no Qt and no network
in the construction path, so the parts with a security consequence - the
verifier, the state check, the callback parsing - are testable by argument.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib import error, parse, request

# The client id shipped inside the plugin. Not a secret: a public client has
# none, which is exactly why PKCE is mandatory rather than optional. It is fixed
# because the plugin is distributed and cannot be told a new one.
CLIENT_ID = "mapdex-qgis"

# What the plugin asks for. Offline access is not optional: without a refresh
# token an hour of work is the whole session, and being sent back to a browser
# mid-task is worse than the device grant this replaces.
SCOPES = ("runs:read", "runs:write", "offline_access")


class ConnectorAuthError(Exception):
    """A connection attempt that did not produce a token, with a reason a
    person can act on. The OAuth error code rides along when the server sent
    one, because `invalid_grant` and "the browser was closed" need different
    responses from the user and identical ones look like the same bug."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def make_verifier() -> str:
    """A fresh PKCE code verifier.

    RFC 7636 allows 43 to 128 characters from an unreserved alphabet. 64 random
    bytes of urlsafe base64 lands inside that and is generated from the system
    CSPRNG, which is the only property that matters: a guessable verifier is a
    code interception away from being no protection at all.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    """The S256 challenge. Plain is never offered: it proves nothing against an
    attacker who can already see the authorization request."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    authorize_endpoint: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    resource: str = "",
) -> str:
    """The URL the person's browser is sent to.

    `resource` is RFC 8707. It names which Mapdex the token is for, and the
    server refuses a token whose audience is not the server being called, so
    omitting it produces a token that authenticates nowhere.
    """
    params = [
        ("response_type", "code"),
        ("client_id", CLIENT_ID),
        ("redirect_uri", redirect_uri),
        ("scope", " ".join(SCOPES)),
        ("state", state),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
    ]
    if resource:
        params.append(("resource", resource))
    separator = "&" if "?" in authorize_endpoint else "?"
    return authorize_endpoint + separator + parse.urlencode(params)


def parse_callback(path: str, expected_state: str) -> str:
    """The authorization code out of the browser's callback, or a refusal.

    Two checks, and the first is the one that matters. A callback whose state
    does not match the one this process generated did not come from the flow
    this process started, so it is discarded WITHOUT reading the code: acting on
    it is how a code from somebody else's session gets redeemed into this
    person's plugin.
    """
    query = parse.parse_qs(parse.urlsplit(path).query)
    state = (query.get("state") or [""])[0]
    if not expected_state or state != expected_state:
        raise ConnectorAuthError(
            "The reply from the browser did not belong to this connection attempt. "
            "Start the connection again.",
            "state_mismatch",
        )
    # The server reports a refusal here rather than at the token endpoint, and
    # the commonest one by far is a person pressing Cancel.
    if query.get("error"):
        code = (query.get("error") or [""])[0]
        described = (query.get("error_description") or [""])[0]
        if code == "access_denied":
            raise ConnectorAuthError("The connection was declined in the browser.", code)
        raise ConnectorAuthError(described or "Mapdex refused the connection.", code)
    code = (query.get("code") or [""])[0]
    if not code:
        raise ConnectorAuthError("The browser returned no authorization code.", "no_code")
    return code


_DONE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading}</title>
<style>
body{{margin:0;padding:64px 32px;font:15px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",sans-serif;color:#f2f2f2;background:#111318}}main{{max-width:520px}}
h1{{font-size:20px;line-height:1.3;font-weight:600;margin:0 0 8px}}
p{{margin:0;color:#a9adb7}}
</style>
</head>
<body><main>
<h1>{heading}</h1><p>{detail}</p>
</main></body></html>"""


class LoopbackReceiver:
    """A one-request HTTP server on loopback, to catch the redirect.

    RFC 8252 is explicit that this is how a native application receives an
    authorization code, and it is bound to 127.0.0.1 with a port the operating
    system chooses. Both halves matter: binding a fixed port fails whenever
    something else on the machine has it, and binding anything other than
    loopback would put the code on the network.
    """

    def __init__(self):
        self._result: Dict[str, Any] = {}
        self._event = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server virtual name
                heading = "Connection complete"
                detail = "Return to QGIS — Mapdex will finish setting up your workspace."
                try:
                    receiver._result["code"] = parse_callback(self.path, receiver.state)
                except ConnectorAuthError as failure:
                    receiver._result["error"] = failure
                    heading, detail = "Connection not completed", str(failure)
                body = _DONE_PAGE.format(
                    heading=html.escape(heading),
                    detail=html.escape(detail),
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                receiver._event.set()

            def log_message(self, *_args):
                """Silence. http.server writes to stderr by default, and in QGIS
                that is the Python console, where a callback URL carrying an
                authorization code would be printed for anyone looking."""

        self.state = secrets.token_urlsafe(24)
        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._serving = False

    @property
    def redirect_uri(self) -> str:
        return "http://127.0.0.1:{}/callback".format(self._server.server_port)

    def start(self):
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        self._serving = True
        return self

    def wait(self, timeout: float = 120.0, cancel_event=None) -> str:
        """Block until the browser comes back, or say plainly that it did not.

        The timeout is generous because a person may have to sign in, pick a
        workspace and read a consent screen. It is not unbounded because a
        thread waiting forever on a window somebody closed is a leak that
        outlives the plugin.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise ConnectorAuthError("The Mapdex connection was cancelled.", "cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectorAuthError(
                    "Mapdex did not hear back from the browser in time. Try connecting again.",
                    "timeout",
                )
            self._event.wait(min(0.2, remaining))
        if "error" in self._result:
            raise self._result["error"]
        return str(self._result.get("code") or "")

    def close(self):
        # shutdown() blocks until serve_forever's loop acknowledges it, so
        # calling it on a server that was never started hangs the caller
        # forever. `loopback_available` does exactly that - it binds a socket to
        # find out whether it can and closes it again - so this guard is not
        # defensive tidiness, it is the difference between a capability probe
        # and a frozen QGIS.
        if self._serving:
            self._server.shutdown()
            self._serving = False
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()
        return False


def discover_endpoints(
    api_base: str,
    opener: Optional[Callable] = None,
    timeout: float = 30.0,
) -> Dict[str, str]:
    """Read the authorization server's own metadata rather than guessing paths.

    RFC 8414. Guessing works right up until a deployment moves an endpoint, and
    then it fails as "couldn't reach the server" with nothing naming the cause.
    """
    url = api_base.rstrip("/") + "/.well-known/oauth-authorization-server"
    raw = _fetch(url, opener=opener, timeout=timeout)
    document = json.loads(raw.decode("utf-8"))
    endpoints = {
        "authorize": str(document.get("authorization_endpoint") or ""),
        "token": str(document.get("token_endpoint") or ""),
        "issuer": str(document.get("issuer") or ""),
    }
    if not endpoints["authorize"] or not endpoints["token"]:
        raise ConnectorAuthError(
            "Mapdex did not publish the addresses this connection needs.",
            "metadata_incomplete",
        )
    return endpoints


def server_knows_this_client(
    authorization_url: str,
    opener: Optional[Callable] = None,
    timeout: float = 15.0,
) -> bool:
    """Would this server accept the flow, before anybody opens a browser?

    A plugin is distributed and a server is deployed, so the two are routinely
    different ages. A Mapdex that predates the connector work has an
    authorization server (assistants connect to it) and no registration for
    THIS client, and without this check the person is sent to a browser that
    shows an error page and then waits five minutes for a redirect that will
    never come.

    The probe is the real authorization request, issued as a plain GET with
    redirects not followed. That is not a trick: `BeginAuthorization` is pure
    validation and creates nothing - no code, no consent, no state - so asking
    it twice costs one request and changes nothing. A server that would run the
    flow answers with the consent page or a redirect to sign-in; one that does
    not know the client refuses with a 4xx before any of that.

    Unreachable is reported as "yes". The caller is about to make the same
    request for real and will get a better error from it than this probe can
    invent, and answering "no" here would silently downgrade a working server
    to the fallback flow on one dropped packet.
    """

    class _NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    call = opener or request.build_opener(_NoRedirect).open
    try:
        with call(authorization_url, timeout=timeout) as response:  # nosec B310
            return int(getattr(response, "status", 200) or 200) < 400
    except error.HTTPError as answer:
        # A redirect to a sign-in page is the server saying "yes, but log in
        # first", which is exactly the case the browser handles.
        return answer.code < 400 or answer.code in (301, 302, 303, 307, 308)
    except error.URLError:
        return True


def exchange_code(
    token_endpoint: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    resource: str = "",
    opener: Optional[Callable] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Swap the authorization code for tokens."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }
    if resource:
        form["resource"] = resource
    return _post_form(token_endpoint, form, opener=opener, timeout=timeout)


def refresh_tokens(
    token_endpoint: str,
    refresh_token: str,
    resource: str = "",
    opener: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Trade a refresh token for a new pair.

    The server rotates: the refresh token that comes back replaces the one sent,
    and re-presenting the old one revokes the whole chain as a theft signal. So
    the caller MUST store what this returns, and storing only the access token
    is how a connection dies at the next refresh.
    """
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    if resource:
        form["resource"] = resource
    return _post_form(token_endpoint, form, opener=opener)


def _post_form(
    url: str,
    form: Dict[str, str],
    opener: Optional[Callable] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    body = parse.urlencode(form).encode("ascii")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    raw = _fetch(req, opener=opener, timeout=timeout)
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("access_token"):
        raise ConnectorAuthError("Mapdex returned no access token.", "no_token")
    return payload


def _fetch(target, opener: Optional[Callable] = None, timeout: float = 30.0) -> bytes:
    call = opener or request.urlopen
    try:
        with call(target, timeout=timeout) as response:  # nosec B310 - https/loopback only
            return response.read()
    except error.HTTPError as failure:
        # An OAuth error body is JSON with a code in it (RFC 6749 5.2), and that
        # code is the actionable part. Reporting only "HTTP 400" leaves a person
        # unable to tell a declined consent from a misconfigured server.
        document: Dict[str, Any] = {}
        try:
            document = json.loads(failure.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
            # Proxies and older servers can return HTML or an empty body. The
            # HTTP status fallback below still explains the refusal without
            # hiding unrelated runtime failures behind a blanket exception.
            document = {}
        code = str(document.get("error") or "")
        detail = str(document.get("error_description") or "")
        raise ConnectorAuthError(
            detail or "Mapdex refused the connection ({}).".format(failure.code), code
        ) from failure
    except error.URLError as failure:
        raise ConnectorAuthError(
            "Mapdex could not be reached at this address.", "unreachable"
        ) from failure


def loopback_available() -> Tuple[bool, str]:
    """Can this machine receive the redirect at all?

    Asked before the flow starts rather than discovered halfway through it. A
    locked-down or headless machine that cannot bind a loopback socket has to
    fall back to the device grant, and finding that out AFTER sending somebody
    to a browser wastes the trip and leaves an authorization pending.
    """
    try:
        receiver = LoopbackReceiver()
    except OSError as failure:
        return False, str(failure)
    receiver.close()
    return True, ""


def expiry_from(payload: Dict[str, Any], now: float) -> float:
    """When the access token stops working, as an absolute time.

    Absolute rather than a duration because it is stored and read back in a
    later session, and a duration written down is a duration measured from
    whenever somebody happens to read it. A response with no `expires_in` is
    treated as already expired rather than as eternal: refreshing something that
    was still good costs one request, and trusting a token that is not is a
    request that fails in front of the user.
    """
    try:
        seconds = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return now
    # A minute of slack, so a token is not presented in the second it dies.
    return now + max(0, seconds - 60)


def is_headless() -> bool:
    """No display means no browser, so the loopback flow cannot complete."""
    if os.name == "nt" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    return True
