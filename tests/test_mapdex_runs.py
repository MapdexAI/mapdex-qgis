"""The two Mapdex surfaces a QGIS user had to leave QGIS to reach.

`mapdex.jobs@1` and `mapdex.open_review@1` were registered, advertised in the
catalogue the model plans against, and bound to nothing - so asking "what is
running" or "open that review" produced "not available in this QGIS build yet",
and the answer was a browser tab the user had to find by hand.

Three things carry the risk here and are what these tests hold:

* the run list is FETCHED, not answered from nothing. Both handlers start a task
  and return an honest "started"; blocking the Qt main thread on `/v1/runs`
  would freeze QGIS, and claiming a listing before the request returns would
  report runs nobody read,
* a review link is built from the run's SOURCE file, never from the run id.
  `/review/run_…` resolves no run, so the Studio closes the cockpit and returns
  the reviewer to the project map,
* an absence is named. No session, no project, an unknown run id and a project
  with nothing waiting are four different facts, and each tells the user what to
  do next.

`plugin.py` imports QGIS at module scope and there is no QGIS in CI, so the
handlers are lifted out of its AST and executed against a fake panel. That is a
real behavioural test of the part that has behaviour.
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo.capabilities import CapabilityError, validate_request  # noqa: E402
from mapdex_qgis.guard import describe_exception, guarded  # noqa: E402
from mapdex_qgis.guidance import with_failure_guidance  # noqa: E402
from mapdex_qgis.nivo import transition  # noqa: E402
from mapdex_qgis.qgis_runtime import PLUGIN_BOUND_CAPABILITIES, bound_capability_ids  # noqa: E402
from mapdex_qgis.results import (  # noqa: E402
    run_failure,
    run_matches_state,
    run_state,
    select_review_run,
    summarize_runs,
)
from mapdex_qgis.workspace import review_workspace_path  # noqa: E402

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


# --------------------------------------------------------------------------
# Lifting the handlers out of plugin.py
# --------------------------------------------------------------------------

class FakeUrl:
    def __init__(self, text):
        self.text = text


class FakeDesktopServices:
    """Records what would have been opened instead of opening it."""

    opened: list = []

    @classmethod
    def openUrl(cls, url):
        cls.opened.append(url.text if isinstance(url, FakeUrl) else str(url))


class FakeLocale:
    language = "en_GB"

    def __init__(self):
        self._name = FakeLocale.language

    def name(self):
        return self._name

    @staticmethod
    def system():
        return FakeLocale()


HANDLERS = (
    "_require_mapdex_session",
    "_list_mapdex_runs",
    "_mapdex_runs_listed",
    "_open_mapdex_review",
    "_mapdex_review_resolved",
    "_nivo_report",
)


def _plugin_namespace():
    """Execute the run handlers and the reporting helpers, free of QGIS."""
    reporting = ("SIMPLE_RESULT_PHRASES", "RESULT_DESCRIBERS",
                 "_pretty_number", "_joined", "describe_capability_result")
    selected = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and (
            node.name in reporting or node.name.startswith("_describe_")
        ):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in reporting for target in node.targets
        ):
            selected.append(node)
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name in HANDLERS:
            selected.append(node)
    found = {node.name for node in selected if isinstance(node, ast.FunctionDef)}
    missing = sorted(set(HANDLERS) - found)
    assert not missing, "plugin.py no longer defines {}".format(missing)

    namespace = {
        "CapabilityError": CapabilityError,
        "QDesktopServices": FakeDesktopServices,
        "QLocale": FakeLocale,
        "QUrl": FakeUrl,
        "describe_exception": describe_exception,
        "guarded": guarded,
        "review_workspace_path": review_workspace_path,
        "run_state": run_state,
        "select_review_run": select_review_run,
        "summarize_runs": summarize_runs,
        "transition": transition,
        "with_failure_guidance": with_failure_guidance,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "plugin-runs", "exec"), namespace)
    return namespace


PLUGIN = _plugin_namespace()
describe = PLUGIN["describe_capability_result"]


class FakeAPI:
    def __init__(self, payload=None, error=None, token="tok"):
        self.token = token
        self._payload = payload if payload is not None else []
        self._error = error
        self.calls = []

    def runs(self, project_id=""):
        self.calls.append(project_id)
        if self._error is not None:
            raise self._error
        return self._payload


class FakePanel:
    """Only the surface the lifted handlers touch."""

    web_base = "https://app.mapdex.ai"

    def __init__(self, api=None, project_id="proj_1", run_tasks=True):
        self.api = api if api is not None else FakeAPI()
        self._project_id = project_id
        self._run_tasks = run_tasks
        self.tasks = []
        self._nivo_turns = []
        self._nivo_state = "executing"
        self.statuses = []
        self.rendered = 0
        for name in HANDLERS:
            setattr(self, name, types.MethodType(PLUGIN[name], self))

    def _active_project_id(self):
        return self._project_id

    def _task(self, description, work, done, busy=True):
        self.tasks.append(description)
        if not self._run_tasks:
            return
        try:
            done(None, work())
        except Exception as error:  # noqa: BLE001 - mirrors _WorkTask capture
            done(error, None)

    def _render_nivo_turns(self):
        self.rendered += 1

    def _set_status(self, text):
        self.statuses.append(text)

    def _report_unexpected(self, action, exc):  # pragma: no cover - guard sink
        raise AssertionError("{} escaped the handler: {}".format(action, exc))

    @property
    def said(self):
        return [entry[1] for entry in self._nivo_turns if entry[0] == "assistant"]


def _run(run_id, state, prompt="", files=("file_1",), error=None):
    job = {"state": state}
    if error is not None:
        job["error"] = error
    return {
        "id": run_id,
        "status": state,
        "prompt": prompt,
        "input_file_ids": list(files),
        "job": job,
        "created_at": "2026-08-17T09:00:00Z",
    }


@pytest.fixture(autouse=True)
def _clear_opened():
    FakeDesktopServices.opened = []
    FakeLocale.language = "en_GB"
    yield
    FakeDesktopServices.opened = []
    FakeLocale.language = "en_GB"


# --------------------------------------------------------------------------
# Reading a run's state
# --------------------------------------------------------------------------

def test_the_job_state_wins_over_the_task_status():
    # A task row can still read `dispatched` while its job has already failed.
    # Reporting the older of two known facts is how a failure stays invisible.
    run = {"id": "run_1", "status": "dispatched", "job": {"state": "failed"}}
    assert run_state(run) == "failed"


def test_review_required_is_reported_in_the_word_the_rest_of_the_product_uses():
    assert run_state({"id": "run_1", "job": {"state": "review_required"}}) == "needs_review"


def test_a_run_without_a_job_still_reports_its_status():
    assert run_state({"id": "run_1", "status": "needs_review"}) == "needs_review"


def test_an_unfamiliar_state_counts_as_active_rather_than_disappearing():
    # Active is the complement of the terminal sets. A state this build has
    # never seen would otherwise match no bucket at all and drop out of every
    # answer the user can ask for.
    run = _run("run_1", "rehydrating")
    assert run_matches_state(run, "active") is True
    assert run_matches_state(run, "completed") is False
    assert run_matches_state(run, "all") is True


def test_a_failure_message_carries_its_support_reference():
    run = _run("run_1", "failed", error={"message": "raster is not georeferenced",
                                         "correlation_id": "corr_9"})
    assert run_failure(run) == "raster is not georeferenced (reference corr_9)"


def test_a_failed_run_with_no_reported_reason_invents_none():
    assert run_failure(_run("run_1", "failed")) == ""


# --------------------------------------------------------------------------
# Summarising the list
# --------------------------------------------------------------------------

def test_counts_are_taken_over_every_run_not_over_the_page():
    runs = [_run("run_1", "needs_review"), _run("run_2", "completed"), _run("run_3", "failed")]
    summary = summarize_runs(runs, "failed")
    assert summary["total"] == 3
    assert summary["matched"] == 1
    assert summary["counts"] == {"needs_review": 1, "completed": 1, "failed": 1}
    assert [row["run_id"] for row in summary["runs"]] == ["run_3"]


def test_the_listing_is_bounded_and_says_how_much_it_left_out():
    runs = [_run("run_{}".format(index), "completed") for index in range(25)]
    summary = summarize_runs(runs, "all", limit=3)
    assert summary["matched"] == 25
    assert len(summary["runs"]) == 3
    assert "22 more not listed" in describe("", summary)


def test_an_empty_project_is_reported_as_empty_rather_than_as_a_filter_miss():
    assert describe("", summarize_runs([], "all")) == "this project has no Mapdex runs yet"


def test_a_filter_that_matches_nothing_names_the_filter():
    line = describe("", summarize_runs([_run("run_1", "completed")], "failed"))
    assert line == "none of the 1 runs in this project are failed"


def test_the_transcript_line_states_the_failure_the_server_reported():
    runs = [_run("run_1", "failed", prompt="Georeference sheet 4",
                 error={"message": "no control points"})]
    line = describe("", summarize_runs(runs, "failed"))
    assert "Georeference sheet 4" in line
    assert "no control points" in line


def test_a_payload_that_is_not_a_list_produces_an_empty_answer_not_a_crash():
    # `/v1/runs` answers with a bare array today. A server that changes shape
    # must not take the desktop down with it.
    assert summarize_runs({"unexpected": True})["total"] == 0
    assert summarize_runs(None)["runs"] == []


# --------------------------------------------------------------------------
# Choosing and addressing the run to review
# --------------------------------------------------------------------------

def test_the_most_recent_run_needing_review_is_chosen_when_none_is_named():
    runs = [_run("run_new", "running"), _run("run_2", "needs_review"), _run("run_1", "needs_review")]
    assert select_review_run(runs)["id"] == "run_2"


def test_a_named_run_is_used_even_when_it_is_not_the_one_waiting():
    runs = [_run("run_2", "needs_review"), _run("run_1", "completed")]
    assert select_review_run(runs, "run_1")["id"] == "run_1"


def test_a_run_id_from_another_project_resolves_to_nothing():
    assert select_review_run([_run("run_1", "needs_review")], "run_elsewhere") is None


def test_a_project_with_nothing_waiting_resolves_to_nothing():
    assert select_review_run([_run("run_1", "completed")]) is None


def test_the_review_link_is_built_from_the_source_file_with_the_run_beside_it():
    path = review_workspace_path("proj_1", _run("run_1", "needs_review", files=("file_9",)))
    assert path == "/workspace/proj_1/review/file_9?run=run_1"


def test_a_run_with_no_source_file_opens_the_project_with_review_intent():
    # `/review/run_…` resolves no run: the Studio treats it as an orphan, closes
    # the cockpit and rewrites the address back to the project map.
    path = review_workspace_path("proj_1", _run("run_1", "needs_review", files=()))
    assert path == "/workspace/proj_1?open=review&run=run_1"


def test_an_id_that_could_repoint_the_link_is_encoded():
    path = review_workspace_path("proj_1", {"id": "run_1", "input_file_ids": ["../../admin"]})
    assert "/workspace/proj_1/review/..%2F..%2Fadmin?run=run_1" == path


# --------------------------------------------------------------------------
# The handlers
# --------------------------------------------------------------------------

def test_listing_runs_refuses_before_it_asks_when_there_is_no_session():
    # `/v1/runs` without a token answers 401, which reaches the user as an HTTP
    # failure for a question whose real answer is "connect first".
    panel = FakePanel(api=FakeAPI(token=""))
    with pytest.raises(CapabilityError):
        panel._list_mapdex_runs({"state": "all"})
    assert panel.tasks == []


def test_listing_runs_refuses_when_no_project_is_chosen():
    panel = FakePanel(project_id="")
    with pytest.raises(CapabilityError):
        panel._list_mapdex_runs({"state": "all"})
    assert panel.tasks == []


def test_listing_runs_starts_a_task_rather_than_blocking_the_main_thread():
    panel = FakePanel(api=FakeAPI([_run("run_1", "completed")]), run_tasks=False)
    started = panel._list_mapdex_runs({"state": "all"})
    assert started == {"kind": "jobs_requested", "state": "all"}
    assert panel.tasks == ["List Mapdex runs"]
    # Nothing was fetched yet, so nothing may be reported yet.
    assert panel.said == []


def test_the_run_list_reaches_the_transcript_when_it_arrives():
    panel = FakePanel(api=FakeAPI([
        _run("run_1", "needs_review", prompt="Digitize sheet 12"),
        _run("run_2", "completed", prompt="Convert parcels"),
    ]))
    panel._list_mapdex_runs({"state": "review_required"})
    assert panel.api.calls == ["proj_1"]
    line = panel.said[-1]
    assert "1 of 2 runs" in line
    assert "Digitize sheet 12" in line
    assert panel._nivo_state == "completed"


def test_a_failed_request_is_reported_instead_of_an_empty_run_list():
    # An empty list and an unreachable server are different facts, and reporting
    # the first for the second tells the user their runs are gone.
    panel = FakePanel(api=FakeAPI(error=RuntimeError("connection refused")))
    panel._list_mapdex_runs({"state": "all"})
    assert "could not read the run list" in panel.said[-1]
    assert "connection refused" in panel.said[-1]
    assert panel._nivo_state == "completed"


def test_opening_a_review_resolves_the_run_before_it_opens_anything():
    panel = FakePanel(api=FakeAPI([_run("run_7", "needs_review", files=("file_3",))]), run_tasks=False)
    started = panel._open_mapdex_review({})
    assert started == {"kind": "review_requested", "run_id": ""}
    assert panel.tasks == ["Open Mapdex review"]
    assert FakeDesktopServices.opened == []


def test_opening_a_review_lands_on_the_run_that_is_waiting():
    panel = FakePanel(api=FakeAPI([
        _run("run_9", "running", files=("file_1",)),
        _run("run_7", "needs_review", files=("file_3",)),
    ]))
    panel._open_mapdex_review({})
    assert FakeDesktopServices.opened == ["https://app.mapdex.ai/workspace/proj_1/review/file_3?run=run_7"]
    assert "run_7" in panel.said[-1]


def test_a_named_run_is_addressed_by_its_own_source_file():
    # The id alone cannot build a review link, which is why the run is fetched
    # even when the caller supplied one.
    panel = FakePanel(api=FakeAPI([
        _run("run_7", "needs_review", files=("file_3",)),
        _run("run_4", "completed", files=("file_8",)),
    ]))
    panel._open_mapdex_review({"run_id": "run_4"})
    assert FakeDesktopServices.opened == ["https://app.mapdex.ai/workspace/proj_1/review/file_8?run=run_4"]


def test_a_non_english_qgis_opens_its_own_locale_of_the_workspace():
    FakeLocale.language = "tr_TR"
    panel = FakePanel(api=FakeAPI([_run("run_7", "needs_review", files=("file_3",))]))
    panel._open_mapdex_review({})
    assert FakeDesktopServices.opened == ["https://app.mapdex.ai/tr/workspace/proj_1/review/file_3?run=run_7"]


def test_nothing_to_review_is_said_out_loud_and_opens_no_tab():
    panel = FakePanel(api=FakeAPI([_run("run_1", "completed")]))
    panel._open_mapdex_review({})
    assert FakeDesktopServices.opened == []
    assert panel.said[-1] == "Nothing in this project is waiting for review."
    assert panel._nivo_state == "completed"


def test_an_unknown_run_id_is_named_rather_than_silently_replaced():
    # Opening the most recent review instead would send the user to a different
    # run than the one they asked for, with nothing saying so.
    panel = FakePanel(api=FakeAPI([_run("run_1", "needs_review")]))
    panel._open_mapdex_review({"run_id": "run_missing"})
    assert FakeDesktopServices.opened == []
    assert "run_missing" in panel.said[-1]


def test_opening_a_review_refuses_before_it_asks_when_there_is_no_session():
    panel = FakePanel(api=FakeAPI(token=""))
    with pytest.raises(CapabilityError):
        panel._open_mapdex_review({})
    assert panel.tasks == []


# --------------------------------------------------------------------------
# Advertisement and validation
# --------------------------------------------------------------------------

def test_both_capabilities_are_declared_plugin_bound():
    assert {"mapdex.jobs@1", "mapdex.open_review@1"} <= set(PLUGIN_BOUND_CAPABILITIES)


def test_both_capabilities_are_advertised_as_executable():
    # `bound_capability_ids` is what the plugin negotiates with. Advertising an
    # id nothing runs is the "Nivo prepared an action" and nothing happens
    # failure the whole parity surface exists to avoid.
    bound = bound_capability_ids()
    assert "mapdex.jobs@1" in bound
    assert "mapdex.open_review@1" in bound


def test_the_handler_table_names_both_ids():
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "_plugin_capability_handlers":
            table = next(child for child in ast.walk(node) if isinstance(child, ast.Dict))
            keys = {key.value for key in table.keys}
            assert keys == set(PLUGIN_BOUND_CAPABILITIES)
            return
    raise AssertionError("plugin.py has no _plugin_capability_handlers")


def test_the_registry_still_gates_what_reaches_the_handlers():
    assert validate_request("mapdex.jobs@1", {})["params"]["state"] == "all"
    assert validate_request("mapdex.jobs@1", {"state": "failed"})["params"]["state"] == "failed"
    with pytest.raises(CapabilityError):
        validate_request("mapdex.jobs@1", {"state": "everything"})
    with pytest.raises(CapabilityError):
        validate_request("mapdex.open_review@1", {"run_id": "run_1", "sql": "DROP TABLE t"})


def test_neither_capability_asks_for_a_confirmation_it_does_not_need():
    # Both only read and open a browser tab. Marking them consequential would
    # put a dialog in front of "what is running", which is a question, not a
    # change.
    assert validate_request("mapdex.jobs@1", {})["requires_confirmation"] is False
    assert validate_request("mapdex.open_review@1", {})["requires_confirmation"] is False
