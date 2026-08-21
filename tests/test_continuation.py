import ast
import pathlib

from mapdex_qgis.continuation import (
    MAX_CLIENT_ROUND_TRIPS,
    action_result,
    continuation_budget,
    continuation_expected,
    should_continue,
)

PLUGIN = (pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py").read_text(
    encoding="utf-8"
)


def _response(expected=True, used=1, total=8):
    return {"text": "ok", "continuation": {"expected": expected, "steps_used": used, "steps_max": total}}


def test_a_server_that_did_not_ask_ends_the_turn():
    assert continuation_expected(_response()) is True
    assert continuation_expected(_response(expected=False)) is False
    # An older server sends no continuation at all, and the turn must end
    # exactly as it did before rather than looping on a field that is absent.
    assert continuation_expected({"text": "ok"}) is False
    assert continuation_expected(None) is False


def test_the_budget_travels_so_the_panel_can_show_it():
    assert continuation_budget(_response(used=3, total=8)) == (3, 8)
    assert continuation_budget({"text": "ok"}) == (0, 0)


def test_nothing_to_report_is_not_a_continuation():
    # An action that never ran leaves nothing to say, and sending an empty
    # report would ask the server to decide again from the same facts.
    assert should_continue(_response(), [], 0) is False


def test_a_reported_failure_ends_the_objective():
    # The desktop already said why. Another attempt at a question the client
    # refused is not an answer.
    failed = [action_result("analytics.numeric@1", False, error="no active layer")]
    assert should_continue(_response(), failed, 0) is False


def test_the_client_holds_its_own_ceiling():
    # The server carries a budget and states it. This exists anyway: a client
    # that trusts a peer to end a loop has no bound of its own, and the peer is
    # reachable over a network.
    ok = [action_result("analytics.geometry@1", True, summary="24 features")]
    assert should_continue(_response(), ok, MAX_CLIENT_ROUND_TRIPS - 1) is True
    assert should_continue(_response(), ok, MAX_CLIENT_ROUND_TRIPS) is False


def test_a_successful_action_continues():
    ok = [action_result("analytics.geometry@1", True, summary="total area 7,627.77 m2")]
    assert should_continue(_response(), ok, 0) is True


def test_a_reported_result_is_bounded_before_it_is_sent():
    entry = action_result(
        "analytics.numeric@1", True,
        summary="x" * 900,
        result={"mean": 42, "values": list(range(500)), "nested": {"a": 1}, "note": "y" * 900},
    )
    assert len(entry["summary"]) <= 403
    assert entry["result"]["values"] == "[500 items]"
    assert entry["result"]["nested"] == "{1 fields}"
    assert len(entry["result"]["note"]) <= 203
    # A number the desktop measured survives intact: bounding is about size,
    # not about discarding the measurement.
    assert entry["result"]["mean"] == 42


def test_the_plugin_reports_what_an_action_did_and_continues():
    # A module reachable only from its own test is not in the product.
    assert "from .continuation import action_result, continuation_budget, should_continue" in PLUGIN
    tree = ast.parse(PLUGIN)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "_continue_objective" in names
    assert "_report_action_result" in names
    body = PLUGIN[PLUGIN.index("def _continue_objective") :]
    body = body[: body.index("\n    @")] if "\n    @" in body else body
    assert "should_continue(response, results" in body
    # The objective is re-sent, not re-read from the input box, which the user
    # may have typed into since.
    assert "self._nivo_objective" in body


def test_a_new_question_starts_a_new_objective():
    # Carrying the previous objective's reported outcomes into a new question
    # would tell the server work had just happened that had not.
    ask = PLUGIN[PLUGIN.index("def ask_nivo") :]
    ask = ask[: ask.index("\n    def _start_hosted_turn")]
    assert "self._nivo_objective = message" in ask
    assert "self._nivo_round_trips = 0" in ask


def test_the_continuation_is_a_guarded_qt_entry_point():
    # It runs from a compose completion callback inside QGIS, where an escaping
    # exception is not a log line but a broken host application.
    assert "@guarded\n    def _continue_objective(" in PLUGIN
