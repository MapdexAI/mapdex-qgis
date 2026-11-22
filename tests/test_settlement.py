"""A confirmed run always ends its card in a terminal state, and errors still get reported.

`confirm_card.Settlement` is the one place that decides it. These tests drive
it through the same shape the plugin uses (a guarded wrapper around the steps,
a callback that settles and reports), raising at each stage, and check both
halves: the card's final state, and that the error reached the reporter.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import confirm_card  # noqa: E402
from mapdex_qgis.guard import guarded, report_unexpected  # noqa: E402


class Run:
    """The plugin's run path in miniature: steps, a task manager, a callback."""

    def __init__(self, stage, accept=1):
        self.stage = stage
        self.accept = accept
        self.card = []          # every (state, detail) the card was set to
        self.reports = []       # what reached the error boundary
        self.layers = []

    def _report_unexpected(self, action, exc):
        self.reports.append((action, type(exc).__name__))

    def _settle(self, state, detail):
        self.card.append((state, detail))

    @guarded
    def run(self):
        settlement = confirm_card.Settlement(self._settle)
        with settlement.guarding():
            self._steps(settlement)
        settlement.close_body()

    def _steps(self, settlement):
        if self.stage == "pre_task":
            raise ValueError("planning failed")
        if self.stage == "task_construction":
            raise RuntimeError("no task")

        def finish(successful):
            if self.stage == "callback_before_layer":
                raise RuntimeError("reprojection failed")
            settlement.layer_may_exist = True
            self.layers.append("buffer")
            if self.stage == "callback_after_layer":
                raise RuntimeError("could not activate the layer")
            settlement.settle(*(("done", "added buffer, 3 features") if successful else
                                ("failed", "QGIS stopped the buffer before it finished")))

        def completed(successful):
            try:
                with settlement.guarding():
                    finish(successful)
                settlement.close_callback()
            except Exception as exc:  # noqa: BLE001
                report_unexpected(self, "the Processing result", exc)

        settlement.handed_over = True
        accepted = self.accept
        if not accepted:
            settlement.handed_over = False
            settlement.settle("not_run", "QGIS did not accept the task")
            return
        completed(self.stage != "task_failed")


def _final(run):
    assert run.card, "the card was never settled"
    return run.card[-1]


@pytest.mark.parametrize("stage", ["pre_task", "task_construction"])
def test_an_error_before_the_task_settles_not_run_and_is_reported(stage):
    run = Run(stage)
    run.run()
    state, detail = _final(run)
    assert state == "not_run"
    assert "before QGIS started" in detail and "layer may" not in detail
    assert run.reports and run.reports[0][0] == "run"


def test_a_task_qgis_did_not_accept_settles_not_run():
    run = Run("ok", accept=0)
    run.run()
    assert _final(run) == ("not_run", "QGIS did not accept the task")
    assert run.reports == []


def test_an_error_in_the_callback_before_the_layer_settles_failed_and_is_reported():
    run = Run("callback_before_layer")
    run.run()
    state, detail = _final(run)
    assert state == "failed" and "layer may" not in detail
    assert run.reports == [("the Processing result", "RuntimeError")]


def test_an_error_after_the_layer_was_added_says_the_layer_may_exist():
    run = Run("callback_after_layer")
    run.run()
    state, detail = _final(run)
    assert state == "failed"
    assert "a result layer may already have been added to the project" in detail
    assert run.reports == [("the Processing result", "RuntimeError")]


def test_success_and_task_failure_settle_once_and_never_both():
    ok = Run("ok")
    ok.run()
    assert ok.card == [("done", "added buffer, 3 features")]
    failed = Run("task_failed")
    failed.run()
    assert failed.card == [("failed", "QGIS stopped the buffer before it finished")]


def test_the_first_terminal_state_wins_and_an_exception_is_never_done():
    seen = []
    settlement = confirm_card.Settlement(lambda state, detail: seen.append(state))
    settlement.settle("done", "added x")
    settlement.fail(RuntimeError("late"))
    assert seen == ["done"]
    for started in (False, True):
        for layer in (False, True):
            state, _detail = confirm_card.failure_outcome(RuntimeError("x"), started, layer)
            assert state in ("not_run", "failed")


def test_a_body_that_neither_settled_nor_handed_over_is_not_run():
    seen = []
    settlement = confirm_card.Settlement(lambda state, detail: seen.append((state, detail)))
    settlement.close_body()
    assert seen == [("not_run", "the operation did not start")]
    handed = confirm_card.Settlement(lambda state, detail: seen.append((state, detail)))
    handed.handed_over = True
    handed.close_body()
    assert len(seen) == 1, "after hand-over the callback owns the outcome"


def test_a_callback_that_returned_without_an_outcome_is_failed():
    seen = []
    settlement = confirm_card.Settlement(lambda state, detail: seen.append(state))
    settlement.handed_over = True
    settlement.close_callback()
    assert seen == ["failed"]


def test_a_failing_card_update_does_not_hide_the_run_error():
    def broken(_state, _detail):
        raise RuntimeError("panel gone")

    settlement = confirm_card.Settlement(broken)
    with pytest.raises(ValueError):
        with settlement.guarding():
            raise ValueError("the real error")
    assert settlement.state == "not_run"
    assert isinstance(settlement.settle_error, RuntimeError)
