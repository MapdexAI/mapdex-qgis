"""A plan offer is what the desktop asks a person to approve.

Each test here names a way the panel could ask for a click instead of a
decision, or run something other than what it showed.
"""

from mapdex_qgis.plan_offer import offer_prompt, plan_offer


def _proposal(**overrides):
    payload = {
        "kind": "proposal",
        "mode": "workflow_proposal",
        "plan": {
            "definition_key": "vector_hygiene",
            "steps": [
                {"name": "validate", "type": "quality:validate_layer@1",
                 "title": "Validate geometry"},
                {"name": "repair", "type": "data:repair@1", "title": "Repair shapes"},
            ],
        },
        "plan_hash": "sha256:abc",
        "estimate": {"estimated_credits": 4,
                     "estimated_duration_seconds": {"min": 10, "max": 40},
                     "output_summary": ["a repaired layer"]},
    }
    payload.update(overrides)
    return payload


def test_a_proposal_with_its_hash_is_an_offer():
    offer = plan_offer(_proposal())
    assert offer is not None
    assert offer["steps"] == ["Validate geometry", "Repair shapes"]
    assert offer["plan_hash"] == "sha256:abc"
    assert offer["credits"] == 4


def test_a_plan_without_its_hash_is_not_offerable():
    """A confirmed plan is an exact plan.

    Without the hash the server cannot verify that what runs is what the person
    saw, so submitting it would make the confirmation decorative.
    """
    assert plan_offer(_proposal(plan_hash="")) is None


def test_a_turn_that_already_answered_offers_nothing():
    """Offering to run work on top of an answer already given asks the user to
    pay twice for the same sentence."""
    for mode in ("direct_answer", "lightweight_action", "direct_ui_command",
                 "contextual_observation"):
        assert plan_offer(_proposal(mode=mode)) is None


def test_an_empty_plan_is_not_an_offer():
    """Approving an empty act and then being told a run completed is worse than
    being told nothing was proposed."""
    assert plan_offer(_proposal(plan={"definition_key": "x", "steps": []})) is None


def test_a_step_is_never_shown_as_a_tool_id():
    """Hard rule 22: executor strings are developer data."""
    offer = plan_offer(_proposal(plan={
        "definition_key": "x",
        "steps": [{"name": "reproject_layer", "type": "worker:reproject@2"}],
    }))
    assert offer["steps"] == ["reproject layer"]
    assert "worker:reproject@2" not in offer_prompt(offer)


def test_an_unmeasured_cost_is_said_and_not_shown_as_free():
    offer = plan_offer(_proposal(estimate={}))
    assert offer["credits"] is None
    assert "not estimated" in offer_prompt(offer)


def test_a_genuinely_free_run_says_so():
    """A deterministic georeference apply costs nothing, and that is different
    from nobody having measured it."""
    offer = plan_offer(_proposal(estimate={"estimated_credits": 0}))
    assert offer["credits"] == 0
    assert "no credits" in offer_prompt(offer)


def test_the_prompt_states_the_cost_and_the_work():
    text = offer_prompt(plan_offer(_proposal()))
    assert "1. Validate geometry" in text
    assert "4 credits" in text
    assert "10 to 40 seconds" in text
    assert "a repaired layer" in text


def test_one_credit_is_singular():
    assert "1 credit." in offer_prompt(plan_offer(
        _proposal(estimate={"estimated_credits": 1})))


def test_nothing_that_is_not_a_response_is_an_offer():
    for value in (None, "", [], {"kind": "message"}, {"plan": "not a dict"}):
        assert plan_offer(value) is None


def test_a_finished_run_offers_its_layers():
    from mapdex_qgis.plan_offer import plan_run_report

    report = plan_run_report({
        "status": "completed",
        "result_references": [{"kind": "layer", "id": "layer_1", "summary": "Repaired parcels"}],
    })
    assert report["terminal"] is True
    assert report["message"] == "Done."
    assert [item["layer_id"] for item in report["layers"]] == ["layer_1"]
    assert report["layers"][0]["name"] == "Repaired parcels"


def test_needs_review_is_terminal_and_is_not_a_failure():
    from mapdex_qgis.plan_offer import plan_run_report

    report = plan_run_report({"job": {"state": "review_required"}})
    assert report["terminal"] is True
    assert "needs a person" in report["message"]
    assert "fail" not in report["message"].lower()


def test_an_unrecognised_state_keeps_polling():
    """Stopping early tells somebody a run ended while it is still spending
    their credits."""
    from mapdex_qgis.plan_offer import plan_run_report

    assert plan_run_report({"status": "dispatched"})["terminal"] is False
    assert plan_run_report({"status": "some_new_state"})["terminal"] is False


def test_the_run_path_walks_the_closed_state_machine():
    """Every edge the plan-run path uses must exist in the table.

    `transition` fails closed: an event with no edge returns the state
    unchanged. So a wrong verb is silent - the panel says the run failed while
    the turn still reads `action_ready` - and the first version of this path
    used two verbs that were not events at all.
    """
    from mapdex_qgis.nivo import transition

    composing = transition("idle", "send")
    assert composing == "composing"
    ready = transition(composing, "action")
    assert ready == "action_ready"
    executing = transition(ready, "execute")
    assert executing == "executing"
    assert transition(executing, "done") == "completed"
    assert transition(executing, "error") == "failed"
    # The two that were wrong, named so nobody reaches for them again.
    assert transition(ready, "error") == ready
    assert transition(executing, "idle") == executing
