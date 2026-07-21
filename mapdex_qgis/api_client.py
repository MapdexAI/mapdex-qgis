from __future__ import annotations

import json
import mimetypes
import os
import uuid
from typing import Any, Optional
from urllib import error, request


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


class MapdexAPI:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = normalize_api_base(base_url)
        self.token = token

    def _headers(self, project_id: str = "", content_type: Optional[str] = "application/json") -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.token:
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
            with request.urlopen(req, timeout=45) as response:
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
        url = path if path.startswith("http://") or path.startswith("https://") else self.base_url + path
        headers = self._headers(project_id=project_id, content_type=None)
        headers.pop("Content-Type", None)
        if "/geojson" in path or path.endswith(".geojson"):
            headers["Accept"] = "application/geo+json, application/json;q=0.9, */*;q=0.1"
        req = request.Request(url, method="GET", headers=headers)
        try:
            with request.urlopen(req, timeout=180) as response:
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

    def upload_file(self, path: str, project_id: str):
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
            with request.urlopen(req, timeout=180) as response:
                return json.loads(response.read())
        except error.HTTPError as exc:
            self._raise_http("POST", url, exc)

    def start_batch(self, project_id: str, file_id: str, kind: str):
        return self._request(
            "POST",
            "/v1/batches",
            {"kind": kind, "sources": [{"file_id": file_id}]},
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
