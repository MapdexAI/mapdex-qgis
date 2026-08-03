from mapdex_qgis.guidance import task_guidance


def test_running_task_keeps_qgis_user_oriented():
    guidance = task_guidance("running", {"total": 1, "running": 1})
    assert guidance["phase"] == "Processing in Mapdex"
    assert guidance["busy"] is True
    assert "continue mapping" in guidance["hint"]
    assert guidance["action"] == ""


def test_queued_task_guidance():
    guidance = task_guidance("pending")
    assert "continue mapping" not in guidance["hint"]
    assert guidance["action"] == ""


def test_review_task_explains_round_trip():
    guidance = task_guidance("needs_review", {"total": 1, "needs_review": 1})
    assert guidance["phase"] == "Review required"
    # The round trip is explained without asking the user to come back and
    # press a button: the panel now imports the approved layer on its own.
    assert "adds the approved layer by itself" in guidance["hint"]
    # The second route must be offered too: review the draft inside QGIS.
    assert "draft" in guidance["hint"]
    assert guidance["action"] == "Review in Mapdex"


def test_completed_task_prioritizes_qgis_import():
    guidance = task_guidance("completed", {"total": 1, "succeeded": 1})
    assert guidance["phase"] == "Ready for QGIS"
    assert "Add it" in guidance["hint"]
    assert guidance["progress"] == 100
    assert guidance["action"] == "Open result in Mapdex"


def test_queued_but_unstarted_run_is_not_reported_as_processing():
    """The failure this covers: a job nobody picks up showed "Processing" forever."""
    guidance = task_guidance("running", {"total": 1, "running": 1}, backend_started=False)
    assert guidance["phase"] == "Queued for processing"
    assert guidance["progress"] < 30


def test_long_wait_names_the_processing_service():
    guidance = task_guidance(
        "running", {"total": 1, "running": 1}, backend_started=False, waiting_seconds=600
    )
    assert guidance["phase"] == "Waiting for Mapdex processing"
    assert "has not picked this task up" in guidance["hint"]
    assert guidance["busy"] is False


def test_started_run_keeps_the_processing_wording():
    guidance = task_guidance("running", {"total": 1, "running": 1}, backend_started=True)
    assert guidance["phase"] == "Processing in Mapdex"


def test_unknown_start_state_changes_nothing():
    assert task_guidance("running", {"total": 1, "running": 1}) == task_guidance(
        "running", {"total": 1, "running": 1}, backend_started=None
    )


def test_run_start_detection_reads_the_server_payload():
    from mapdex_qgis.guidance import run_has_started

    assert run_has_started({"job": {"state": "queued"}}) is False
    assert run_has_started({"job": {"state": "running"}}) is True
    assert run_has_started({"job": {"state": "completed"}}) is True
    # A step already reporting progress outranks a stale queued job row.
    assert run_has_started({"job": {"state": "queued"}, "steps": [{"status": "succeeded"}]}) is True
    # Nothing reported: stay neutral rather than invent a state.
    assert run_has_started({}) is None
    assert run_has_started(None) is None
