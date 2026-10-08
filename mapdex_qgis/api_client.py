from __future__ import annotations

import json
import mimetypes
import os
import re
from typing import Any, Callable, Optional
from urllib import error, parse, request

from .build_profile import LOOPBACK_HOSTS, is_production
from .build_version import PLUGIN_VERSION


# One set, owned by build_profile, so the transport check and the build
# profile cannot disagree about what "this computer" means.
LOCAL_HOSTS = LOOPBACK_HOSTS

# Server-supplied ids end up in temp file names; keep them to an id alphabet so
# a hostile or buggy response cannot walk out of the download directory.
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename_part(value: str, fallback: str = "result") -> str:
    """Reduce an untrusted id to a path-safe file-name component."""
    cleaned = SAFE_ID.sub("_", str(value or "")).strip("._")
    return cleaned[:120] or fallback


def _path_segment(value: Any) -> str:
    """Percent-encode an id before it becomes part of a request path.

    A thread id round-trips through QSettings, which is a local file the user
    (or another program) can edit, so it is not trusted to be a bare `th_…`.
    Encoding keeps a stray `/` or `..` from re-pointing the request at another
    endpoint.
    """
    return parse.quote(str(value or ""), safe="")


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """The list in a response, whether bare or wrapped in an envelope."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys + ("items", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _origin(url: str) -> tuple[str, str, int]:
    parsed = parse.urlsplit(str(url or ""))
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    return scheme, host, port


def is_local_host(url: str) -> bool:
    return _origin(url)[1] in LOCAL_HOSTS


def same_origin(first: str, second: str) -> bool:
    """True when both URLs share scheme, host and port."""
    left, right = _origin(first), _origin(second)
    return bool(left[1]) and left == right


def is_transport_secure(url: str) -> bool:
    """The bearer token may only travel over TLS, or to the local machine."""
    scheme, host, _ = _origin(url)
    if scheme == "https":
        return True
    return scheme == "http" and host in LOCAL_HOSTS


class _AuthStrippingRedirectHandler(request.HTTPRedirectHandler):
    """Drop the Authorization header when a redirect leaves the API origin.

    urllib replays request headers on redirects, so without this an API that
    answers 302 to another host would hand that host the user's Mapdex token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None and not same_origin(newurl, req.full_url):
            new_request.remove_header("Authorization")
        return new_request


_OPENER = request.build_opener(_AuthStrippingRedirectHandler())


def _urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


class MapdexAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int = 0,
        correlation_id: str = "",
        url: str = "",
        method: str = "",
        retry_after: int = 0,
        code: str = "",
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status = status
        self.correlation_id = correlation_id
        self.url = url
        self.method = method
        self.retry_after = retry_after
        # The canonical machine code from the error envelope. Callers branch on
        # this and on `status`, never on the message text: the message is
        # human copy that may be localized or reworded, and matching it would
        # be a keyword list in disguise.
        self.code = code
        # The envelope's structured details: for a short balance, `required`
        # and `available` in cents. A refusal shown without them asks the
        # person to pay without saying how much.
        self.details = dict(details) if isinstance(details, dict) else {}


def normalize_api_base(url: str) -> str:
    """Accept ``http://host:8080`` or ``…/v1`` and always store the host root."""
    value = (url or "").strip().rstrip("/")
    if value.lower().endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value


def device_verification_url(response_url: str, web_base: str, api_base: str) -> str:
    """Pick the URL to open for device approval, distrusting the response.

    The value arrives from the API, and it is handed to the desktop URL
    handler — so anything that is not a plain http(s) address (``javascript:``,
    ``file:``, an OS handler scheme) falls back to the configured web app, as
    does a localhost address returned by a production API.
    """
    candidate = str(response_url or "").strip()
    configured_web = str(web_base or "").rstrip("/")
    production_api = not is_local_host(api_base)
    # Device approval is authenticated application state. Repair both an old
    # stored plugin setting and an older API response that still name the
    # marketing host, so existing installs move immediately with new ones.
    configured_parts = parse.urlsplit(configured_web)
    if production_api and (configured_parts.hostname or "").lower() == "mapdex.ai":
        configured_web = "https://app.mapdex.ai"
    fallback = "{}/device".format(configured_web)
    scheme, host, _ = _origin(candidate)
    if scheme not in {"http", "https"} or not host:
        return fallback
    if not is_transport_secure(candidate):
        return fallback
    if production_api and host in LOCAL_HOSTS:
        return fallback
    if production_api and host == "mapdex.ai":
        parts = parse.urlsplit(candidate)
        return parse.urlunsplit(("https", "app.mapdex.ai", parts.path, parts.query, parts.fragment))
    return candidate


class MapdexAPI:
    # The empty default is "not signed in yet", not a hardcoded credential.
    def __init__(self, base_url: str, token: str = ""):  # nosec B107
        self.base_url = normalize_api_base(base_url)
        self.token = token
        # Called when a request is refused for want of a valid token, and only
        # then. It returns the new access token, or "" if the connection cannot
        # be renewed - in which case the refusal is reported to the user as it
        # always was.
        #
        # A connector access token lives an hour and QGIS sessions do not, so
        # without this every long day of work ends in a browser trip. Reactive
        # rather than scheduled on purpose: a timer that refreshes on its own
        # keeps a connection alive in a QGIS somebody left open for a week, and
        # the server's own answer is the only authority on whether a token still
        # works - a locally computed expiry cannot know about a disconnect.
        self.on_token_expired: Optional[Callable[[], str]] = None
        # Guards against a refresh loop. One retry per request: if the renewed
        # token is refused too, the connection is genuinely gone.
        self._renewing = False

    def _headers(
        self,
        project_id: str = "",
        content_type: Optional[str] = "application/json",
        url: str = "",
    ) -> dict[str, str]:
        """Headers for one request. Credentials are bound to the API origin.

        Result payloads carry absolute artifact URLs, and object storage uses
        pre-signed links that need no Mapdex credential — so the token is only
        attached when the target really is the configured API.
        """
        headers = {"Accept": "application/json"}
        # Identify the surface on every request.
        #
        # The API classifies each request into a bounded `client` label for
        # mapdex_http_requests_total, and it already has a qgis_plugin bucket:
        # it looks for "qgis" in the User-Agent or X-Client-Surface: qgis. This
        # client sent neither, so urllib's default "Python-urllib/3.x" matched
        # the api_client rule instead and every request this plugin has ever
        # made was counted as a generic API caller. Heavy real QGIS use showed
        # as nothing on the QGIS panels, which is how it was found.
        #
        # Both are sent: the explicit header is what the API keys on, and the
        # User-Agent is what a proxy, an access log, or a support engineer
        # reads. Neither carries anything about the user.
        headers["X-Client-Surface"] = "qgis"
        headers["User-Agent"] = f"mapdex-qgis/{PLUGIN_VERSION}"
        if content_type:
            headers["Content-Type"] = content_type
        target = url or self.base_url
        if self.token and same_origin(target, self.base_url):
            headers["Authorization"] = f"Bearer {self.token}"
        if project_id:
            headers["X-Project-ID"] = project_id
        return headers

    def _raise_http(self, method: str, url: str, exc: error.HTTPError) -> None:
        raw = exc.read()
        try:
            envelope = json.loads(raw) if raw else {}
        except Exception:
            envelope = {}
        message = None
        corr = ""
        code = ""
        details = {}
        if isinstance(envelope, dict):
            message = envelope.get("message")
            corr = envelope.get("correlation_id") or ""
            code = str(envelope.get("code") or "")
            if isinstance(envelope.get("details"), dict):
                details = envelope["details"]
            err = envelope.get("error")
            if isinstance(err, dict):
                message = message or err.get("message")
                corr = err.get("correlation_id") or corr
                code = code or str(err.get("code") or "")
                if not details and isinstance(err.get("details"), dict):
                    details = err["details"]
            elif isinstance(err, str):
                message = message or err
        if not message:
            if exc.code == 404:
                if is_production():
                    message = (
                        "Mapdex could not find that service. Please update the plugin "
                        "or try again later."
                    )
                else:
                    message = (
                        "{method} {url} returned 404. Check the API URL — for local "
                        "Mapdex use http://127.0.0.1:8080 (device auth must be running)."
                    ).format(method=method, url=url)
            elif exc.code == 501:
                message = "Device authorization is not enabled on this API deployment."
            else:
                message = str(exc)
        try:
            retry_after = max(0, int(exc.headers.get("Retry-After", "0")))
        except (TypeError, ValueError):
            retry_after = 0
        raise MapdexAPIError(
            str(message), exc.code, corr, url=url, method=method, retry_after=retry_after, code=code,
            details=details,
        ) from exc

    def _request(
        self, method: str, path: str, payload=None, project_id: str = "",
        timeout: float = 45.0,
    ):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers(project_id=project_id, content_type="application/json" if data is not None else None)
        if data is None:
            headers.pop("Content-Type", None)
        url = self.base_url + path
        req = request.Request(url, data=data, method=method, headers=headers)
        try:
            with _urlopen(req, timeout=timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except error.HTTPError as exc:
            if exc.code == 401 and self._renew_token():
                # One retry, with the renewed token. The body is rebuilt rather
                # than reused because `data` is a consumed stream on some
                # transports, and a silently empty retry body is worse than the
                # 401 it replaced.
                #
                # The guard stays SET across the retry. Clearing it first was
                # the first version and it disabled the guard completely: the
                # retry's own 401 found the flag down, renewed again, and
                # recursed. A server refusing every token would have frozen
                # QGIS rather than reporting a refusal.
                try:
                    return self._request(method, path, payload, project_id, timeout)
                finally:
                    self._renewing = False
            self._raise_http(method, url, exc)
        except error.URLError as exc:
            raise MapdexAPIError(
                "Could not reach Mapdex API at {url}: {reason}".format(url=url, reason=exc.reason),
                url=url,
                method=method,
            ) from exc

    def _renew_token(self) -> bool:
        """Ask the owner for a fresh access token, once.

        Returns False when there is no renewal path, when one is already in
        flight, or when it produced nothing - and every one of those means the
        original 401 is reported to the person, which is the honest outcome.
        A connection that was disconnected in the browser lands here, and it
        must end as "sign in again" rather than as a silent retry loop.
        """
        if self._renewing or self.on_token_expired is None:
            return False
        self._renewing = True
        try:
            renewed = self.on_token_expired() or ""
        except Exception:  # noqa: BLE001 - a failed renewal is a failed request, not a crash
            renewed = ""
        finally:
            # A failed renewal clears the flag here, or the client would never
            # try again for the rest of the QGIS session - so somebody who
            # reconnects in the browser would have to restart QGIS. A
            # SUCCESSFUL one leaves it set: the caller clears it once its single
            # retry has finished, which is what makes the guard a guard.
            if not renewed:
                self._renewing = False
        if not renewed:
            return False
        self.token = renewed
        return True

    def download_bytes(
        self, path: str, project_id: str = "", timeout: float = 180.0
    ) -> bytes:
        """GET a relative `/v1/...` path or absolute URL and return raw bytes."""
        absolute = path.startswith("http://") or path.startswith("https://")
        url = path if absolute else self.base_url + path
        if absolute and not is_transport_secure(url):
            raise MapdexAPIError(
                "Refusing to download over an insecure connection: {url}".format(url=url),
                url=url,
                method="GET",
            )
        headers = self._headers(project_id=project_id, content_type=None, url=url)
        headers.pop("Content-Type", None)
        if not same_origin(url, self.base_url):
            # A third-party artifact host has no business seeing our tenant.
            headers.pop("X-Project-ID", None)
        if "/geojson" in path or path.endswith(".geojson"):
            headers["Accept"] = "application/geo+json, application/json;q=0.9, */*;q=0.1"
        req = request.Request(url, method="GET", headers=headers)
        try:
            with _urlopen(req, timeout=timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            self._raise_http("GET", url, exc)
        except error.URLError as exc:
            raise MapdexAPIError(
                "Could not download from {url}: {reason}".format(url=url, reason=exc.reason),
                url=url,
                method="GET",
            ) from exc

    def authorize_device(self, timeout: float = 45.0):
        return self._request(
            "POST", "/v1/auth/device/authorization",
            {"client_name": "Mapdex for QGIS"}, timeout=timeout,
        )

    def poll_device_token(self, device_code: str):
        return self._request("POST", "/v1/auth/device/token", {"device_code": device_code})

    def revoke_device(self, device_code: str):
        """Cancel a device authorization the user walked away from.

        Sent without a token on purpose: while the authorization is pending this
        plugin has no token yet, and the device code is the proof — the same
        proof the polling call above exchanges for one.
        """
        return self._request("POST", "/v1/auth/device/revoke", {"device_code": device_code})

    def projects(self):
        response = self._request("GET", "/v1/projects")
        # API returns a bare JSON array (see ListProjects). Older clients used
        # an envelope — accept both so the plugin never calls .get on a list.
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            nested = response.get("projects") or response.get("data") or []
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return []

    def _put_upload(self, path: str, upload: dict[str, Any]) -> None:
        url = str(upload.get("url") or "")
        if not url:
            raise MapdexAPIError("Mapdex did not return an object-storage upload URL.")
        method = str(upload.get("method") or "PUT").upper()
        if not is_transport_secure(url):
            raise MapdexAPIError(
                "Refusing to upload over an insecure connection: {url}".format(url=url),
                url=url,
                method=method,
            )
        # Only the storage provider's own signed headers travel here: a
        # pre-signed URL needs no Mapdex credential, and the storage host is a
        # different origin from the API.
        headers = {str(key): str(value) for key, value in (upload.get("headers") or {}).items()}
        headers.pop("Authorization", None)
        headers["Content-Length"] = str(os.path.getsize(path))
        try:
            with open(path, "rb") as handle:
                req = request.Request(url, data=handle, method=method, headers=headers)
                with _urlopen(req, timeout=1800) as response:
                    response.read()
        except error.HTTPError as exc:
            self._raise_http(method, url, exc)
        except (error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MapdexAPIError(
                "Could not upload the file to Mapdex storage: {reason}".format(reason=reason),
                url=url,
                method=method,
            ) from exc

    def _upload_file_multipart(self, path: str, project_id: str):
        """Compatibility path for tests/small self-hosted deployments only."""
        import uuid

        boundary = "----mapdex-" + uuid.uuid4().hex
        filename = os.path.basename(path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            content = handle.read()
        body = (
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + content
            # Same attestation the presigned path sends; the multipart branch of
            # POST /v1/files enforces it as a form field.
            + (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; "
                "name=\"policy_acknowledged\"\r\n\r\ntrue\r\n"
                f"--{boundary}--\r\n"
            ).encode()
        )
        url = self.base_url + "/v1/files"
        # Built through _headers so this path carries the same identification
        # and credential rules as every other API call. It used to assemble its
        # own dict, which is how one request path silently stayed unidentified
        # while the rest were fixed.
        headers = self._headers(
            project_id=project_id,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        req = request.Request(url, data=body, method="POST", headers=headers)
        try:
            with _urlopen(req, timeout=180) as response:
                return json.loads(response.read())
        except error.HTTPError as exc:
            self._raise_http("POST", url, exc)

    def upload_file(self, path: str, project_id: str):
        """Register, stream directly to object storage, then finalize the file."""
        filename = os.path.basename(path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        registration = self._request(
            "POST",
            "/v1/files",
            {
                "filename": filename,
                "content_type": content_type,
                "byte_size": os.path.getsize(path),
                # Required by every POST /v1/files (docs/API.md §Content policy
                # acknowledgement). Without it the API refuses the registration
                # with CONTENT_POLICY_ATTESTATION_REQUIRED and no file is created.
                "policy_acknowledged": True,
            },
            project_id,
        )
        if not isinstance(registration, dict):
            raise MapdexAPIError("Mapdex returned an invalid upload registration.")
        file_meta = registration.get("file")
        upload = registration.get("upload")
        if not isinstance(file_meta, dict) or not isinstance(upload, dict):
            # Older self-hosted APIs may only support multipart uploads.
            return self._upload_file_multipart(path, project_id)
        file_id = str(file_meta.get("id") or "")
        if not file_id:
            raise MapdexAPIError("Mapdex did not return a file id for the upload.")
        self._put_upload(path, upload)
        return self._request("POST", f"/v1/files/{file_id}/complete", {}, project_id)

    def sheet_prices(self):
        """The published pricing ladder, for the per-sheet batch estimate.

        `GET /v1/plans` is deliberately unauthenticated on the server - it is
        what an anonymous visitor reads on the pricing page - so this works
        before a connection and the Task page can price a workflow while the
        user is still choosing one.

        The plugin does not compute a price from this. It reads the server's
        own per-item `batch_kinds` figure and multiplies by the sheet count;
        `packages/contracts/pricing.go` is the single source of the
        customer-facing price and a second copy of it here is exactly the
        two-price-lists problem that file exists to end.
        """
        return self._request("GET", "/v1/plans", timeout=15.0)

    def credits(self):
        """What this workspace has left to spend, in the same unit as a price.

        `GET /v1/credits` answers with `credits_available`, which is the balance
        minus the holds other runs already have out - the exact figure the
        reservation gate enforces, so a batch checked against it cannot be
        refused for a hold this panel could not see.

        It is authenticated, so it answers only once connected. Its unit is
        cents: a run reserves `estimated_price_cents` against the same column,
        which is why the batch total and this number are comparable at all.
        """
        return self._request("GET", "/v1/credits", timeout=15.0)

    def start_batch(self, project_id: str, file_ids, kind: str, options=None):
        """Create the batch.

        `options` is a `TaskOptions`, and only what differs from the server's
        own defaults is merged in - see `task_options.payload`. The three
        fields it can carry (`expand_pdf_pages`, `skip_completed`,
        `max_concurrency`) have been accepted by this endpoint for as long as
        batches have existed and were never sent from here, so a QGIS user
        submitting a multi-page PDF archive had no way to ask for a sheet per
        page and re-running a partly failed archive paid for the sheets that
        had already succeeded.
        """
        if isinstance(file_ids, str):
            file_ids = [file_ids]
        clean_kind = kind.replace("_", " ").title()
        body = {
            "kind": kind,
            "name": f"[QGIS] {clean_kind}",
            "sources": [{"file_id": file_id} for file_id in file_ids],
        }
        if options is not None:
            body.update(options.payload())
        return self._request("POST", "/v1/batches", body, project_id)

    def compose(
        self,
        project_id: str,
        message: str,
        companion_context: dict[str, Any],
        thread_id: str = "",
        companion_results=None,
    ):
        """Call the canonical compose endpoint with bounded QGIS context.

        The server independently validates this as untrusted companion data;
        this client method exists so no hidden local automation path emerges.

        `thread_id` is what makes a follow-up question work: given one, compose
        replays the conversation and carries the earlier source reference
        forward, so "and the largest one?" resolves against the layer named in
        the previous turn. Omitted when empty, because a turn with no
        conversation is a legitimate first turn, not a malformed request.
        """
        payload: dict[str, Any] = {
            "input": str(message or "").strip(),
            "companion_context": companion_context,
        }
        if thread_id:
            payload["thread_id"] = str(thread_id)
        # The reported outcomes of the previous turn's actions. Their presence
        # is what makes this a continuation of the same objective rather than a
        # new question, and the server counts them as the loop's budget.
        if companion_results:
            payload["companion_results"] = list(companion_results)
        return self._request("POST", "/v1/compose", payload, project_id)

    def list_threads(self, project_id: str) -> list[dict[str, Any]]:
        """Conversations in this project, in the server's newest-first order.

        The API returns a bare JSON array of `ThreadListItem`; an envelope is
        accepted too so an older or proxied deployment does not make the
        History dialog claim there is nothing to show.
        """
        return _rows(self._request("GET", "/v1/threads", project_id=project_id), "threads")

    def create_thread(self, project_id: str, title: str = "") -> dict[str, Any]:
        """Open a conversation. `project_id` is required in the body itself."""
        response = self._request(
            "POST",
            "/v1/threads",
            {"title": str(title or ""), "project_id": project_id},
            project_id,
        )
        return response if isinstance(response, dict) else {}

    def thread_messages(self, thread_id: str, project_id: str = "") -> list[dict[str, Any]]:
        """The stored messages of one conversation, oldest first."""
        path = "/v1/threads/{}/messages".format(_path_segment(thread_id))
        return _rows(self._request("GET", path, project_id=project_id), "messages")

    def delete_thread(self, thread_id: str, project_id: str = "") -> None:
        """Soft-delete a conversation. The API answers 204 with no body."""
        self._request(
            "DELETE", "/v1/threads/{}".format(_path_segment(thread_id)), project_id=project_id
        )

    def batch(self, project_id: str, batch_id: str):
        return self._request("GET", f"/v1/batches/{batch_id}", project_id=project_id)

    def cancel_run(self, project_id: str, run_id: str):
        """Stop a run the panel started.

        The server answers 409 when the run is already terminal, which is the
        ordinary race between pressing Cancel and the run finishing on its own
        rather than a fault. The caller reports that as "it already finished".
        """
        return self._request("POST", f"/v1/runs/{run_id}/cancel", {}, project_id)

    def cancel_batch(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/cancel", {}, project_id)

    def retry_failed(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/retry-failed", {}, project_id)

    def create_run(self, project_id: str, text: str, plan: dict[str, Any],
                   plan_hash: str) -> dict[str, Any]:
        """Execute a plan the server already composed, unchanged.

        The plan and its hash travel together and neither is rebuilt here.
        `docs/MEMORY.md` states the rule: compose returns the complete
        configured DAG plus a content-addressed hash, and the server verifies
        that hash before executing, so a client that reassembles a plan from
        tool names is asking for something nobody approved. This method
        therefore takes the plan as an opaque object and does not inspect it.
        """
        return self._request("POST", "/v1/runs", {
            "input": text,
            "plan": plan,
            "plan_hash": plan_hash,
        }, project_id)

    def run(self, run_id: str, project_id: str = "") -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}", project_id=project_id)

    def runs(self, project_id: str = "") -> list[dict[str, Any]]:
        """The project's runs, newest first, as `GET /v1/runs` returns them.

        The server scopes the list by the `X-Project-ID` header rather than a
        path segment, and answers with a bare array. `_rows` accepts the
        envelope shape too, so a server that later wraps the list does not turn
        into an empty job list on the desktop.
        """
        return _rows(self._request("GET", "/v1/runs", project_id=project_id), "runs")

    def layer_geojson(self, layer_id: str, project_id: str, limit: int = 5000) -> bytes:
        return self.download_bytes(f"/v1/layers/{layer_id}/geojson?limit={limit}", project_id=project_id)

    def layer(self, layer_id: str, project_id: str = "") -> dict[str, Any]:
        value = self._request("GET", f"/v1/layers/{layer_id}", project_id=project_id)
        return value if isinstance(value, dict) else {}

    def file_bytes(self, file_id: str, project_id: str = "") -> bytes:
        return self.download_bytes(f"/v1/files/{file_id}/download", project_id=project_id)

    def placement_quote(self, file_id: str, project_id: str = ""):
        """What downloading this file would cost now, charging nothing.

        `GET /v1/credits/placement-quote`: a placed sheet costs one placement
        the first time it leaves Mapdex. Returns the server's quote, or None
        when it could not be read - which `leave_price` treats as unknown,
        never as free.
        """
        if not file_id:
            return None
        try:
            value = self._request(
                "GET",
                "/v1/credits/placement-quote?" + parse.urlencode({"file_id": file_id}),
                project_id=project_id,
                timeout=15.0,
            )
        except MapdexAPIError:
            return None
        return value if isinstance(value, dict) else None
