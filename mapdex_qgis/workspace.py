"""Build project-scoped Mapdex workspace destinations for QGIS tasks."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


def task_workspace_path(
    project_id: str,
    workflow: str,
    detail: dict[str, Any] | None = None,
    file_id: str = "",
) -> str:
    """Return the most useful workspace path for a live or completed batch.

    ``file_id`` names the file the destination is about when the caller already
    knows it. Without it the first item carrying a file is used, which is the
    right answer for the single-source task the companion submits and the wrong
    one when one item of a mixed batch owns the destination.
    """
    project = quote(str(project_id or "").strip(), safe="")
    if not project:
        return "/workspace"

    items = [value for value in ((detail or {}).get("items") or []) if isinstance(value, dict)]
    wanted = str(file_id or "").strip()
    item = next(
        (value for value in items if str(value.get("file_id") or "").strip() == wanted),
        {},
    ) if wanted else next((value for value in items if value.get("file_id")), {})
    file_id = quote(str(item.get("file_id") or wanted).strip(), safe="")
    state = str(item.get("state") or "").lower()

    if not file_id:
        return "/workspace/{}".format(project)
    if state in {"needs_review", "review_required"}:
        return "/workspace/{}/review/{}".format(project, file_id)
    normalized_workflow = str(workflow or "").strip().lower().replace(" ", "_")
    if normalized_workflow in {"georeference", "georeference_maps"}:
        return "/workspace/{}/georeference/{}".format(project, file_id)
    if normalized_workflow in {"digitize_parcels", "digitize_parcels_maps"}:
        return "/workspace/{}/extract/{}".format(project, file_id)
    return "/workspace/{}".format(project)


def review_workspace_path(project_id: str, run: dict[str, Any] | None) -> str:
    """The review destination for one run, mirroring the web's `reviewPathForRun`.

    A review link needs the SOURCE file, not the run: `/review/run_…` resolves no
    run, so the Studio treats it as an orphan and bounces the reviewer straight
    back to the project map. The run id rides in `?run=` beside the file so the
    cockpit hydrates that run's queue rather than whatever the file was last used
    for. With no source file on the run there is no file segment to build, so the
    project opens with review intent instead of on a fabricated path.
    """
    project = quote(str(project_id or "").strip(), safe="")
    base = "/workspace/{}".format(project) if project else "/workspace"
    detail = run if isinstance(run, dict) else {}
    run_id = quote(str(detail.get("id") or "").strip(), safe="")
    sources = detail.get("input_file_ids")
    first = ""
    if isinstance(sources, (list, tuple)):
        first = next((str(value) for value in sources if str(value or "").strip()), "")
    file_id = quote(first.strip(), safe="")
    if not file_id:
        return "{}?open=review&run={}".format(base, run_id) if run_id else base
    path = "{}/review/{}".format(base, file_id)
    return "{}?run={}".format(path, run_id) if run_id else path


def default_project_id(projects, remembered: str = "") -> str:
    """The project work goes to, now that the panel has no project selector.

    The web app removed project choice: work lands in the workspace's default
    project. A remembered project that still exists wins (a resumed task names
    one, and a conversation is bound to its project); then the project the
    server marks `is_default`; then the first one listed. "" when there is none.
    """
    rows = [row for row in (projects or []) if isinstance(row, dict) and row.get("id")]
    wanted = str(remembered or "")
    if wanted and any(str(row.get("id")) == wanted for row in rows):
        return wanted
    for row in rows:
        if row.get("is_default"):
            return str(row["id"])
    return str(rows[0]["id"]) if rows else ""


def identity_from_me(payload) -> tuple:
    """(workspace name, account email) from GET /v1/auth/me, "" when absent.

    The active workspace is the membership whose org matches `org_id`; the
    first membership when none does, which is the server's own fallback.
    """
    payload = payload if isinstance(payload, dict) else {}
    memberships = [row for row in (payload.get("memberships") or []) if isinstance(row, dict)]
    active = str(payload.get("org_id") or "")
    chosen = next((row for row in memberships if str(row.get("org_id") or "") == active), None)
    if chosen is None and memberships:
        chosen = memberships[0]
    name = str((chosen or {}).get("org_name") or "").strip()
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    email = str(user.get("email") or "").strip()
    return name, email
