from __future__ import annotations

import json
import mimetypes
import os
import uuid
from urllib import request, error


class MapdexAPIError(RuntimeError):
    def __init__(self, message: str, status: int = 0, correlation_id: str = ""):
        super().__init__(message)
        self.status = status
        self.correlation_id = correlation_id


class MapdexAPI:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload=None, project_id: str = ""):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if project_id:
            headers["X-Project-ID"] = project_id
        req = request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=45) as response:
                body = response.read()
                return json.loads(body) if body else None
        except error.HTTPError as exc:
            raw = exc.read()
            try:
                envelope = json.loads(raw)
            except Exception:
                envelope = {}
            raise MapdexAPIError(envelope.get("message") or envelope.get("error") or str(exc), exc.code, envelope.get("correlation_id", "")) from exc

    def authorize_device(self):
        return self._request("POST", "/v1/auth/device/authorization", {"client_name": "Mapdex for QGIS"})

    def poll_device_token(self, device_code: str):
        return self._request("POST", "/v1/auth/device/token", {"device_code": device_code})

    def projects(self):
        response = self._request("GET", "/v1/projects")
        return response.get("projects") or response.get("data") or []

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
        headers = {"Authorization": f"Bearer {self.token}", "X-Project-ID": project_id, "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"}
        req = request.Request(self.base_url + "/v1/files", data=body, method="POST", headers=headers)
        with request.urlopen(req, timeout=180) as response:
            return json.loads(response.read())

    def start_batch(self, project_id: str, file_id: str, kind: str):
        return self._request("POST", "/v1/batches", {"kind": kind, "sources": [{"file_id": file_id}]}, project_id)

    def batch(self, project_id: str, batch_id: str):
        return self._request("GET", f"/v1/batches/{batch_id}", project_id=project_id)

    def cancel_batch(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/cancel", {}, project_id)

    def retry_failed(self, project_id: str, batch_id: str):
        return self._request("POST", f"/v1/batches/{batch_id}/retry-failed", {}, project_id)
