from __future__ import annotations

import json
import mimetypes
import os
import re
from typing import Any, Optional
from urllib import error, parse, request


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# Server-supplied ids end up in temp file names; keep them to an id alphabet so
# a hostile or buggy response cannot walk out of the download directory.
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename_part(value: str, fallback: str = "result") -> str:
    """Reduce an untrusted id to a path-safe file-name component."""
    cleaned = SAFE_ID.sub("_", str(value or "")).strip("._")
    return cleaned[:120] or fallback


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
    ):
        super().__init__(message)
        self.status = status
        self.correlation_id = correlation_id
        self.url = url
        self.method = method
        self.retry_after = retry_after


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
    fallback = "{}/device".format(str(web_base or "").rstrip("/"))
    scheme, host, _ = _origin(candidate)
    if scheme not in {"http", "https"} or not host:
        return fallback
    if not is_transport_secure(candidate):
        return fallback
    production_api = not is_local_host(api_base)
    if production_api and host in LOCAL_HOSTS:
        return fallback
    return candidate


class MapdexAPI:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = normalize_api_base(base_url)
        self.token = token

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
        if isinstance(envelope, dict):
            message = envelope.get("message")
            corr = envelope.get("correlation_id") or ""
            err = envelope.get("error")
            if isinstance(err, dict):
                message = message or err.get("message")
                corr = err.get("correlation_id") or corr
            elif isinstance(err, str):
                message = message or err
        if not message:
            if exc.code == 404:
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
            str(message), exc.code, corr, url=url, method=method, retry_after=retry_after
        ) from exc

    def _request(self, method: str, path: str, payload=None, project_id: str = ""):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = self._headers(project_id=project_id, content_type="application/json" if data is not None else None)
        if data is None:
            headers.pop("Content-Type", None)
        url = self.base_url + path
        req = request.Request(url, data=data, method=method, headers=headers)
        try:
            with _urlopen(req, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else None
        except error.HTTPError as exc:
            self._raise_http(method, url, exc)
        except error.URLError as exc:
            raise MapdexAPIError(
                "Could not reach Mapdex API at {url}: {reason}".format(url=url, reason=exc.reason),
                url=url,
                method=method,
            ) from exc

    def download_bytes(self, path: str, project_id: str = "") -> bytes:
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
            with _urlopen(req, timeout=180) as response:
                return response.read()
        except error.HTTPError as exc:
            self._raise_http("GET", url, exc)
        except error.URLError as exc:
            raise MapdexAPIError(
                "Could not download from {url}: {reason}".format(url=url, reason=exc.reason),
                url=url,
                method="GET",
            ) from exc

    def authorize_device(self):
        return self._request("POST", "/v1/auth/device/authorization", {"client_name": "Mapdex for QGIS"})

    def poll_device_token(self, device_code: str):
        return self._request("POST", "/v1/auth/device/token", {"device_code": device_code})

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
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        url = self.base_url + "/v1/files"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Project-ID": project_id,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
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

    def start_batch(self, project_id: str, file_ids, kind: str):
        if isinstance(file_ids, str):
            file_ids = [file_ids]
        clean_kind = kind.replace("_", " ").title()
        return self._request(
            "POST",
            "/v1/batches",
            {
                "kind": kind,
                "name": f"[QGIS] {clean_kind}",
                "sources": [{"file_id": file_id} for file_id in file_ids],
            },
            project_id,
        )

    def batch(self, project_id: str, batch_id: str):
        return self._request("GET", f"/v1/batches/{batch_id}", project_id=project_id)

    def cancel_batch(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/cancel", {}, project_id)

    def retry_failed(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/retry-failed", {}, project_id)

    def run(self, run_id: str, project_id: str = "") -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}", project_id=project_id)

    def layer_geojson(self, layer_id: str, project_id: str, limit: int = 5000) -> bytes:
        return self.download_bytes(f"/v1/layers/{layer_id}/geojson?limit={limit}", project_id=project_id)

    def layer(self, layer_id: str, project_id: str = "") -> dict[str, Any]:
        value = self._request("GET", f"/v1/layers/{layer_id}", project_id=project_id)
        return value if isinstance(value, dict) else {}

    def file_bytes(self, file_id: str, project_id: str = "") -> bytes:
        return self.download_bytes(f"/v1/files/{file_id}/download", project_id=project_id)
