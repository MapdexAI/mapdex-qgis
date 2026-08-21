from mapdex_qgis.trace_view import (
    budget_label,
    format_duration,
    format_params,
    step_rows,
)


def _response(steps, budget=None):
    trace = {"turn_id": "turn_1", "steps": steps}
    if budget is not None:
        trace["budget"] = budget
    return {"text": "done", "trace": trace}


def test_a_turn_with_no_trace_draws_nothing():
    # An empty trail rendered as a heading with no rows reads as "it did
    # nothing", which is a claim. Draw nothing instead.
    assert step_rows({"text": "hello"}) == []
    assert step_rows({"text": "hello", "trace": {}}) == []
    assert step_rows(None) == []


def test_a_failed_step_is_visible_as_failed():
    rows = step_rows(
        _response(
            [
                {"index": 1, "id": "s1", "capability": "nivo:classify", "title": "Understand the request", "status": "succeeded"},
                {
                    "index": 2,
                    "id": "s2",
                    "capability": "nivo:plan",
                    "title": "Draft the workflow plan",
                    "status": "failed",
                    "error": {"code": "AI_OUTPUT_INVALID", "message": "the planner returned no usable steps"},
                },
            ]
        )
    )
    assert [row["status_label"] for row in rows] == ["done", "failed"]
    assert rows[1]["tone"] == "danger"
    # The reason a step failed must reach the panel, not just the fact of it.
    assert rows[1]["detail"] == "the planner returned no usable steps"
    assert rows[1]["error_code"] == "AI_OUTPUT_INVALID"


def test_a_step_states_what_it_was_called_with():
    rows = step_rows(
        _response([
            {
                "index": 1,
                "id": "s1",
                "capability": "spatial:buffer@1",
                "title": "Buffer",
                "status": "succeeded",
                "params": {"layer_id": "layer_7", "distance_m": 500, "units": "m"},
                "duration_ms": 812,
            }
        ])
    )
    assert rows[0]["params"] == "distance_m=500, layer_id=layer_7, units=m"
    assert rows[0]["duration"] == "812 ms"


def test_the_model_reason_stays_separate_from_the_measured_outcome():
    rows = step_rows(
        _response([
            {
                "index": 1,
                "id": "s1",
                "capability": "spatial:aggregate@1",
                "status": "succeeded",
                "message": "1204 features",
                "reason": "the parcels layer is the only polygon layer open",
            }
        ])
    )
    # Two separate fields, so the panel can label one as a claim and print the
    # other as the result. Merging them is how a guess becomes a number.
    assert rows[0]["detail"] == "1204 features"
    assert rows[0]["reason"] == "the parcels layer is the only polygon layer open"


def test_a_step_without_a_title_falls_back_to_its_capability():
    rows = step_rows(_response([{"index": 1, "id": "s1", "capability": "geo:reverse@1", "status": "succeeded"}]))
    assert rows[0]["title"] == "geo:reverse@1"


def test_rows_are_numbered_even_when_the_server_omits_the_index():
    rows = step_rows(_response([{"id": "a", "status": "succeeded"}, {"id": "b", "status": "succeeded"}]))
    assert [row["index"] for row in rows] == [1, 2]


def test_format_duration_reports_nothing_when_nothing_was_measured():
    assert format_duration(None) == ""
    assert format_duration(0) == ""
    assert format_duration(812) == "812 ms"
    assert format_duration(2480) == "2.5 s"
    assert format_duration(41000) == "41 s"


def test_format_params_states_what_it_cut():
    params = {"k{}".format(i): i for i in range(10)}
    rendered = format_params(params)
    assert "+4 more" in rendered
    # A long value is bounded rather than allowed to wrap the panel to pieces.
    assert len(format_params({"note": "x" * 500})) <= 170


def test_budget_says_when_the_loop_ran_out_of_room():
    assert budget_label(_response([], {"steps_used": 3, "steps_max": 8})) == "Step 3 of 8"
    assert budget_label(_response([], {"steps_used": 8, "steps_max": 8, "exhausted": True}) ) == (
        "Step 8 of 8 (out of steps)"
    )
    # No budget reported is not "step 0 of 0"; it is nothing to say.
    assert budget_label(_response([])) == ""


def test_a_malformed_trace_does_not_raise():
    # The trace arrives from the network. A panel that raises on a shape it did
    # not expect takes the whole turn down with it.
    assert step_rows({"trace": {"steps": "not a list"}}) == []
    assert step_rows({"trace": {"steps": ["not a mapping", {"id": "s1", "status": "succeeded"}]}})[0]["id"] == "s1"
