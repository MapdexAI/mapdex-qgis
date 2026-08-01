"""Build project-scoped Mapdex workspace destinations for QGIS tasks."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


def task_workspace_path(project_id: str, workflow: str, detail: dict[str, Any] | None = None) -> str:
    """Return the most useful workspace path for a live or completed batch."""
    project = quote(str(project_id or "").strip(), safe="")
    if not project:
        return "/workspace"

    items = (detail or {}).get("items") or []
    item = next((value for value in items if isinstance(value, dict) and value.get("file_id")), {})
    file_id = quote(str(item.get("file_id") or "").strip(), safe="")
    state = str(item.get("state") or "").lower()

    if not file_id:
        return "/workspace/{}".format(project)
    if state in {"needs_review", "review_required"}:
        return "/workspace/{}/review/{}".format(project, file_id)
    if str(workflow or "").lower() == "georeference":
        return "/workspace/{}/georeference/{}".format(project, file_id)
    if str(workflow or "").lower() == "digitize_parcels":
        return "/workspace/{}/extract/{}".format(project, file_id)
    return "/workspace/{}".format(project)
