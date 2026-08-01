from mapdex_qgis.guidance import task_guidance


def test_running_task_keeps_qgis_user_oriented():
    guidance = task_guidance("running", {"total": 1, "running": 1})
    assert guidance["phase"] == "Processing in Mapdex"
    assert guidance["busy"] is True
    assert "continue mapping" in guidance["hint"]
    assert guidance["action"] == "Open live task in Mapdex"


def test_review_task_explains_round_trip():
    guidance = task_guidance("needs_review", {"total": 1, "needs_review": 1})
    assert guidance["phase"] == "Review required"
    assert "return here" in guidance["hint"]
    assert guidance["action"] == "Review and approve in Mapdex"


def test_completed_task_prioritizes_qgis_import():
    guidance = task_guidance("completed", {"total": 1, "succeeded": 1})
    assert guidance["phase"] == "Ready for QGIS"
    assert "Add it" in guidance["hint"]
    assert guidance["progress"] == 100
