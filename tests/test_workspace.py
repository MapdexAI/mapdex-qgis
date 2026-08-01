from mapdex_qgis.workspace import task_workspace_path


def test_live_georeference_opens_the_file_workspace():
    detail = {"items": [{"file_id": "file_123", "state": "running"}]}
    assert task_workspace_path("proj_123", "georeference", detail) == (
        "/workspace/proj_123/georeference/file_123"
    )


def test_review_item_opens_review_in_the_same_project():
    detail = {"items": [{"file_id": "file_123", "state": "needs_review"}]}
    assert task_workspace_path("proj_123", "georeference", detail) == (
        "/workspace/proj_123/review/file_123"
    )


def test_missing_file_falls_back_to_project_workspace():
    assert task_workspace_path("proj_123", "georeference", {"items": []}) == "/workspace/proj_123"
