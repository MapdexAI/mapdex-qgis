"""Extraction refuses an unplaced scan, and the desktop said nothing useful.

`extract:features@2` will not run on a raster with no real-world placement and
returns canonical `GEOREFERENCE_REQUIRED`. The plugin had no handler for it, so a
QGIS user who sent an ungeoreferenced scan paid a full upload round trip and got
"Task failed. Retry it, or open Mapdex for details." plus a Retry button that
re-sends the identical request and earns the identical refusal. The prerequisite
was invisible until it failed and then failed uninformatively.

Three things carry the risk here and are what these tests hold:

* the canonical CODE decides, never the message. The message is human copy the
  server may reword or localize, and matching it would be a keyword list in
  disguise - so the code the plugin matches is also asserted against the
  generated error registry, or a contract rename leaves this matching a string
  that no longer exists,
* the answer is a route, not a nicer failure. The scan is fine and Mapdex already
  holds it, so the destination is the georeference desk for that same file - a
  handoff, not a second upload,
* nothing re-sends. A refusal the request cannot satisfy must not be offered as
  something to try again, whether by the panel or by a button.

`plugin.py` imports QGIS at module scope and there is no QGIS in CI, so the two
handlers that carry this are lifted out of its AST and executed against a fake
panel, the way `test_mapdex_runs.py` does. That is a real behavioural test of the
part that has behaviour.
"""
import ast
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.generated_contracts import ERROR_REGISTRY  # noqa: E402
from mapdex_qgis.guard import guarded  # noqa: E402
from mapdex_qgis.guidance import (  # noqa: E402
    failure_next_step,
    task_guidance,
    with_failure_guidance,
)
from mapdex_qgis.results import (  # noqa: E402
    GEOREFERENCE_REQUIRED,
    batch_failure,
    batch_is_terminal,
    batch_state,
    every_failure_needs_placement,
    failed_item_codes,
    first_batch_error,
    review_run_ids,
    succeeded_run_ids,
    summarize_runs,
)
from mapdex_qgis.workspace import task_workspace_path  # noqa: E402

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


# The refusal as the server actually sends it: a `JobError` on the batch item,
# carrying the canonical code, the human message and the file it is about.
REFUSAL = {
    "code": "GEOREFERENCE_REQUIRED",
    "type": "validation",
    "message": (
        "The raster must be georeferenced with embedded metadata or control "
        "points before spatial extraction."
    ),
    "retryable": False,
    "correlation_id": "corr_77",
}


def _refused_batch(file_id="file_9"):
    return {
        "state": "completed_with_failures",
        "counts": {"total": 1, "succeeded": 0, "needs_review": 0, "failed": 1},
        "items": [
            {
                "id": "bitem_1",
                "file_id": file_id,
                "state": "failed",
                "run_id": "run_1",
                "error": dict(REFUSAL),
            }
        ],
    }


def _broken_batch():
    """A failure with no established remedy: still a failure, still reported."""
    return {
        "state": "completed_with_failures",
        "counts": {"total": 1, "succeeded": 0, "needs_review": 0, "failed": 1},
        "items": [
            {
                "id": "bitem_1",
                "file_id": "file_9",
                "state": "failed",
                "run_id": "run_1",
                "error": {
                    "code": "PROCESSING_SERVICE_UNREACHABLE",
                    "type": "processing",
                    "message": "The processing service at worker:8000 could not be resolved.",
                    "correlation_id": "corr_78",
                },
            }
        ],
    }


# --------------------------------------------------------------------------
# The code the plugin matches is the code the contract defines
# --------------------------------------------------------------------------

def test_the_matched_code_still_exists_in_the_generated_registry():
    codes = {str(entry.get("code") or "") for entry in ERROR_REGISTRY}
    assert GEOREFERENCE_REQUIRED in codes, (
        "the plugin routes on a code the contract no longer defines"
    )


def test_the_registry_entry_is_the_validation_refusal_this_answers():
    entry = next(e for e in ERROR_REGISTRY if e.get("code") == GEOREFERENCE_REQUIRED)
    assert entry["type"] == "validation"
    assert entry["http"] == 422


# --------------------------------------------------------------------------
# The typed error survives the trip out of the payload
# --------------------------------------------------------------------------

def test_the_refusal_keeps_its_code_and_its_file():
    failure = batch_failure(_refused_batch())
    assert failure["code"] == GEOREFERENCE_REQUIRED
    assert failure["file_id"] == "file_9"
    assert failure["correlation_id"] == "corr_77"
    assert "georeferenced" in failure["message"]


def test_a_string_error_from_an_older_server_still_yields_a_message():
    detail = {"items": [{"state": "failed", "file_id": "file_9", "error": "it broke"}]}
    assert batch_failure(detail) == {
        "code": "", "message": "it broke", "correlation_id": "", "file_id": "file_9"
    }


def test_an_item_that_reported_nothing_is_skipped_for_one_that_did():
    detail = {
        "items": [
            {"state": "failed", "file_id": "file_1"},
            {"state": "failed", "file_id": "file_2", "error": dict(REFUSAL)},
        ]
    }
    assert batch_failure(detail)["file_id"] == "file_2"


def test_a_clean_batch_reports_no_failure():
    detail = {"items": [{"state": "succeeded", "file_id": "file_9"}]}
    assert batch_failure(detail)["code"] == ""
    assert failure_next_step(batch_failure(detail).get("code")) == {}


def test_the_status_sentence_is_unchanged_for_callers_that_want_the_message():
    # first_batch_error is display copy and keeps its old shape; the structured
    # reader was added beside it rather than instead of it.
    assert first_batch_error(_refused_batch()) == (
        "The raster must be georeferenced with embedded metadata or control "
        "points before spatial extraction. (reference corr_77)"
    )


# --------------------------------------------------------------------------
# The refusal becomes a next step, and only this one does
# --------------------------------------------------------------------------

def test_the_refusal_names_what_the_user_should_do():
    step = failure_next_step(GEOREFERENCE_REQUIRED)
    assert step["route"] == "georeference"
    assert step["action"] == "Georeference in Mapdex"
    hint = step["hint"]
    # It has to say the thing to do, not only that something is missing.
    assert "Georeference it in Mapdex first" in hint
    # And it has to say the upload was not wasted.
    assert "the same sheet is then ready to extract" in hint


def test_a_code_with_no_established_remedy_gets_no_invented_one():
    assert failure_next_step("PROCESSING_SERVICE_UNREACHABLE") == {}
    assert failure_next_step("UNSUPPORTED_RASTER_PROFILE") == {}
    assert failure_next_step("") == {}
    assert failure_next_step(None) == {}


def test_the_next_step_cannot_be_mutated_by_a_caller():
    first = failure_next_step(GEOREFERENCE_REQUIRED)
    first["hint"] = "something else"
    assert failure_next_step(GEOREFERENCE_REQUIRED)["hint"] != "something else"


# --------------------------------------------------------------------------
# The panel's phase, hint and action
# --------------------------------------------------------------------------

def test_the_panel_asks_for_placement_instead_of_reporting_a_failure():
    guidance = task_guidance(
        "completed_with_failures",
        {"total": 1, "failed": 1},
        failure_code=GEOREFERENCE_REQUIRED,
    )
    assert guidance["phase"] == "Georeference needed first"
    assert guidance["action"] == "Georeference in Mapdex"
    assert "Georeference it in Mapdex first" in guidance["hint"]
    assert "retry" not in guidance["hint"].lower()


def test_a_different_failure_keeps_the_generic_failure_guidance():
    guidance = task_guidance(
        "completed_with_failures",
        {"total": 1, "failed": 1},
        failure_code="PROCESSING_SERVICE_UNREACHABLE",
    )
    assert guidance["phase"] == "Action required"
    assert guidance["action"] == "Inspect failure in Mapdex"


def test_a_failure_with_no_code_keeps_the_generic_failure_guidance():
    assert task_guidance("completed_with_failures", {"total": 1, "failed": 1}) == task_guidance(
        "completed_with_failures", {"total": 1, "failed": 1}, failure_code=""
    )


def test_placement_does_not_leak_into_a_review_or_a_success():
    # The refusal branch is the failed branch. A code arriving alongside review
    # or a ready result must not rewrite either of those.
    review = task_guidance(
        "completed", {"total": 2, "needs_review": 1, "failed": 1},
        failure_code=GEOREFERENCE_REQUIRED,
    )
    assert review["phase"] == "Review required"
    ready = task_guidance(
        "completed", {"total": 1, "succeeded": 1}, failure_code=GEOREFERENCE_REQUIRED
    )
    assert ready["phase"] == "Ready for QGIS"


# --------------------------------------------------------------------------
# Nothing re-sends the request that was refused
# --------------------------------------------------------------------------

def test_every_failure_needing_placement_is_recognised():
    assert every_failure_needs_placement(_refused_batch()) is True


def test_a_mixed_set_of_failures_still_offers_retry():
    detail = _refused_batch()
    detail["items"].append(
        {"id": "bitem_2", "file_id": "file_10", "state": "failed",
         "error": {"code": "PROCESSING_SERVICE_UNREACHABLE", "message": "gone"}}
    )
    # One of the two could come back differently, so retry is still worth having.
    assert every_failure_needs_placement(detail) is False


def test_a_failure_that_reported_no_code_does_not_count_as_placement():
    detail = {"items": [
        {"state": "failed", "file_id": "file_1", "error": dict(REFUSAL)},
        {"state": "failed", "file_id": "file_2"},
    ]}
    assert failed_item_codes(detail) == [GEOREFERENCE_REQUIRED, ""]
    assert every_failure_needs_placement(detail) is False


def test_no_failures_is_not_a_placement_block():
    assert every_failure_needs_placement({"items": [{"state": "succeeded"}]}) is False
    assert every_failure_needs_placement({}) is False


def _method_body(name, until):
    start = SOURCE.index("def {}(".format(name))
    return SOURCE[start:SOURCE.index(until, start)]


def test_the_retry_button_is_withdrawn_when_the_request_cannot_be_satisfied():
    body = _method_body("_refresh_ui", "def _task")
    assert "every_failure_needs_placement" in body
    assert "self.retry_button.setVisible(" in body
    line = next(
        text for text in body.splitlines() if "retry_button.setVisible(" in text
    )
    assert "not unsatisfiable" in line, "retry is still offered for a refusal it cannot satisfy"


def test_the_only_thing_that_re_sends_is_a_button_the_user_presses():
    # Automatic re-submission is the failure mode this guards: a refusal that
    # cannot succeed, re-sent by the panel, forever. `self.api.retry_failed` is
    # the request itself and is expected inside the handler; what must have
    # exactly one caller is the handler.
    callers = [
        text.strip() for text in SOURCE.splitlines()
        if "self.retry_failed" in text
    ]
    assert callers == ["self.retry_button.clicked.connect(self.retry_failed)"]


def test_the_refusal_branch_does_not_start_a_task_or_a_timer():
    body = _method_body("_batch_updated", "def import_results")
    branch = body[body.index("and next_step:"):body.index("elif failed and not ok and not review:")]
    assert "self._task(" not in branch
    assert "progress_timer.start" not in branch


# --------------------------------------------------------------------------
# The route is the georeference desk for the file Mapdex already holds
# --------------------------------------------------------------------------

def test_the_route_opens_the_georeference_desk_for_that_file():
    path = task_workspace_path(
        "proj_1", failure_next_step(GEOREFERENCE_REQUIRED)["route"],
        _refused_batch(), file_id="file_9",
    )
    assert path == "/workspace/proj_1/georeference/file_9"


def test_the_route_follows_the_refused_file_through_a_mixed_batch():
    detail = _refused_batch(file_id="file_9")
    detail["items"].insert(
        0, {"id": "bitem_0", "file_id": "file_1", "state": "succeeded", "run_id": "run_0"}
    )
    detail["counts"] = {"total": 2, "succeeded": 1, "needs_review": 0, "failed": 1}
    path = task_workspace_path("proj_1", "georeference", detail, file_id="file_9")
    assert path == "/workspace/proj_1/georeference/file_9", (
        "the destination followed the first item instead of the refused one"
    )


def test_an_id_that_could_repoint_the_route_is_encoded():
    path = task_workspace_path("proj_1", "georeference", {"items": []}, file_id="../../admin")
    assert path == "/workspace/proj_1/georeference/..%2F..%2Fadmin"


def test_naming_no_file_keeps_the_previous_first_item_behaviour():
    detail = {"items": [{"file_id": "file_123", "state": "running"}]}
    assert task_workspace_path("proj_1", "georeference", detail) == (
        "/workspace/proj_1/georeference/file_123"
    )


# --------------------------------------------------------------------------
# The run listing answers the same way
# --------------------------------------------------------------------------

def _failed_run(code=""):
    error = {"message": "the raster is not placed", "correlation_id": "corr_5"}
    if code:
        error["code"] = code
    return {
        "id": "run_1", "status": "failed", "prompt": "Digitize sheet 12",
        "job": {"state": "failed", "error": error},
    }


def test_the_run_listing_carries_the_code_beside_the_message():
    summary = summarize_runs([_failed_run(GEOREFERENCE_REQUIRED)], "failed")
    assert summary["runs"][0]["code"] == GEOREFERENCE_REQUIRED
    assert summary["runs"][0]["error"] == "the raster is not placed (reference corr_5)"


def test_the_run_listing_restates_the_refusal_as_the_next_step():
    summary = with_failure_guidance(
        summarize_runs([_failed_run(GEOREFERENCE_REQUIRED)], "failed")
    )
    assert "Georeference it in Mapdex first" in summary["runs"][0]["error"]


def test_the_run_listing_leaves_another_failure_as_the_server_reported_it():
    summary = with_failure_guidance(
        summarize_runs([_failed_run("PROCESSING_SERVICE_UNREACHABLE")], "failed")
    )
    assert summary["runs"][0]["error"] == "the raster is not placed (reference corr_5)"


def test_the_run_listing_survives_a_payload_it_cannot_read():
    assert with_failure_guidance(None) == {}
    assert with_failure_guidance({"kind": "jobs"}) == {"kind": "jobs"}


# --------------------------------------------------------------------------
# The handlers, lifted out of plugin.py and run
# --------------------------------------------------------------------------

class FakeUrl:
    def __init__(self, text):
        self.text = text


class FakeDesktopServices:
    opened: list = []

    @classmethod
    def openUrl(cls, url):
        cls.opened.append(url.text if isinstance(url, FakeUrl) else str(url))


class FakeLocale:
    def name(self):
        return "en_GB"

    @staticmethod
    def system():
        return FakeLocale()


class FakeTimer:
    def __init__(self):
        self.started: list = []
        self.stopped = 0
        self.interval = 0

    def start(self, ms=0):
        self.started.append(ms)

    def stop(self):
        self.stopped += 1

    def setInterval(self, ms):
        self.interval = ms


HANDLERS = ("_batch_updated", "open_review")


def _plugin_namespace():
    """Execute only the two handlers this defect lives in, free of QGIS."""
    selected = [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef) and node.name in HANDLERS
    ]
    found = {node.name for node in selected}
    missing = sorted(set(HANDLERS) - found)
    assert not missing, "plugin.py no longer defines {}".format(missing)
    namespace = {
        "QDesktopServices": FakeDesktopServices,
        "QLocale": FakeLocale,
        "QUrl": FakeUrl,
        "MapdexAPIError": type("MapdexAPIError", (RuntimeError,), {}),
        "REVIEW_POLL_MS": 15000,
        "batch_failure": batch_failure,
        "batch_is_terminal": batch_is_terminal,
        "batch_state": batch_state,
        "failure_next_step": failure_next_step,
        "first_batch_error": first_batch_error,
        "guarded": guarded,
        "review_run_ids": review_run_ids,
        "succeeded_run_ids": succeeded_run_ids,
        "task_workspace_path": task_workspace_path,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "plugin-placement", "exec"), namespace)
    return namespace


PLUGIN = _plugin_namespace()


class FakeComboBox:
    def __init__(self, data=""):
        self._data = data

    def currentData(self):
        return self._data


class FakePanel:
    """Only the surface the two lifted handlers touch."""

    web_base = "https://app.mapdex.ai"
    dock = object()

    def __init__(self, workflow="digitize_parcels", batch_id="batch_1"):
        self.batch_id = batch_id
        self.workflow_box = FakeComboBox(workflow)
        self.progress_timer = FakeTimer()
        self.progress_pending = True
        self._last_batch = None
        self._backend_started = None
        self._announced_state = ""
        self.statuses: list = []
        self.announced: list = []
        self.tasks: list = []
        self.errors: list = []
        self.refreshed = 0
        for name in HANDLERS:
            setattr(self, name, types.MethodType(PLUGIN[name], self))

    # -- the surface --
    def _active_project_id(self):
        return "proj_1"

    def _set_status(self, text, toast=False):
        self.statuses.append(text)

    def _announce(self, text, level=0, duration=6):
        self.announced.append((text, level))

    def _refresh_ui(self):
        self.refreshed += 1

    def _show_error(self, title, exc):
        self.errors.append((title, exc))

    def _task(self, description, work, done, busy=True):
        self.tasks.append(description)

    def _note_backend_start(self, started):
        self._backend_started = started

    def _recent_tasks(self):
        return []

    def _fetch_result_files(self, detail, include_review=False):
        return []

    def _results_imported(self, exception, payload):
        return None

    def _report_unexpected(self, action, exc):
        raise AssertionError("{} escaped the handler: {!r}".format(action, exc))


def test_a_refused_scan_is_reported_as_a_next_step_not_a_failure():
    panel = FakePanel()
    panel._batch_updated(None, _refused_batch())
    status = panel.statuses[-1]
    assert "Georeference it in Mapdex first" in status
    assert "Task failed" not in status
    assert "Retry it" not in status


def test_a_refused_scan_is_said_where_qgis_users_look():
    panel = FakePanel()
    panel._batch_updated(None, _refused_batch())
    assert len(panel.announced) == 1
    text, level = panel.announced[0]
    assert "Georeference it in Mapdex first" in text
    assert level == 1, "a prerequisite the user must act on is not an info notice"


def test_the_refusal_is_announced_once_however_often_status_is_polled():
    panel = FakePanel()
    for _ in range(4):
        panel._batch_updated(None, _refused_batch())
    assert len(panel.announced) == 1


def test_a_refused_scan_starts_nothing_and_stops_polling():
    panel = FakePanel()
    panel._batch_updated(None, _refused_batch())
    assert panel.tasks == [], "the refusal queued work instead of asking the user"
    assert panel.progress_timer.started == [], "the panel kept polling a finished refusal"
    assert panel.progress_timer.stopped == 1


def test_a_different_failure_still_reports_as_a_failure():
    panel = FakePanel()
    panel._batch_updated(None, _broken_batch())
    status = panel.statuses[-1]
    assert "could not be resolved" in status
    assert "Georeference" not in status
    assert panel.announced == []


def test_a_failure_with_no_reported_error_still_reports_as_a_failure():
    panel = FakePanel()
    panel._batch_updated(None, {
        "state": "completed_with_failures",
        "counts": {"total": 1, "failed": 1},
        "items": [{"file_id": "file_9", "state": "failed"}],
    })
    assert "Task failed" in panel.statuses[-1]


def test_a_succeeded_batch_is_untouched_by_any_of_this():
    panel = FakePanel()
    panel._batch_updated(None, {
        "state": "completed",
        "counts": {"total": 1, "succeeded": 1},
        "items": [{"file_id": "file_9", "state": "succeeded", "run_id": "run_1"}],
    })
    assert "ready to add to QGIS" in panel.statuses[-1]
    assert panel.tasks == ["Import Mapdex results into QGIS"]


def test_the_action_opens_the_georeference_desk_not_the_extract_desk():
    FakeDesktopServices.opened.clear()
    panel = FakePanel(workflow="digitize_parcels")
    panel._batch_updated(None, _refused_batch())
    panel.open_review()
    assert FakeDesktopServices.opened == [
        "https://app.mapdex.ai/workspace/proj_1/georeference/file_9"
    ], "the refused sheet opened on the desk that already refused it"


def test_the_action_keeps_the_submitted_workflow_for_any_other_failure():
    FakeDesktopServices.opened.clear()
    panel = FakePanel(workflow="digitize_parcels")
    panel._batch_updated(None, _broken_batch())
    panel.open_review()
    assert FakeDesktopServices.opened == [
        "https://app.mapdex.ai/workspace/proj_1/extract/file_9"
    ]
