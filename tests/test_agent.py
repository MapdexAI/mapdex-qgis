"""Capability registry and agent-loop tests.

The security assertions prove the model cannot reach anything that is not a
registered capability; the behaviour assertions prove the loop actually chains
steps and carries measured facts rather than model prose.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.agent import (  # noqa: E402
    STATE_CLARIFY,
    STATE_COMPLETED,
    STATE_CONFIRM,
    STATE_FAILED,
    AgentError,
    AgentSession,
    parse_decision,
)
from mapdex_qgis.capabilities import (  # noqa: E402
    CLIENT_QGIS,
    CLIENT_WORKSPACE,
    RISK_FORBIDDEN,
    Capability,
    CapabilityError,
    catalog_for_prompt,
    for_client,
    get,
    register,
    supported_action_kinds,
    validate_request,
)


class ScriptedProvider:
    """Replays a fixed list of model replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.last_system = ""

    def complete(self, system, messages):
        self.last_system = system
        self.calls += 1
        if not self.replies:
            return json.dumps({"action": "answer", "message": "done"})
        return self.replies.pop(0)


def call(capability, **params):
    return json.dumps({"action": "call", "capability": capability, "params": params, "why": "step"})


def answer(message):
    return json.dumps({"action": "answer", "message": message})


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_the_registry_exposes_a_machine_readable_catalogue_to_the_agent():
    catalog = catalog_for_prompt(CLIENT_QGIS)
    entry = next(item for item in catalog if item["id"] == "analytics.group@1")
    assert entry["domain"] == "analytics"
    assert entry["risk"] == "safe"
    assert "statistic" in entry["params"]


def test_consequential_capabilities_require_confirmation_by_derivation():
    assert get("processing.run@1").requires_confirmation is True
    assert get("mapdex.workflow@1").requires_confirmation is True
    # Safe reversible presentation changes must not nag the user.
    assert get("style.graduated@1").requires_confirmation is False
    assert get("analytics.numeric@1").requires_confirmation is False


def test_forbidden_capabilities_are_hidden_from_the_catalogue_and_refuse_on_call():
    ids = {item["id"] for item in catalog_for_prompt(CLIENT_QGIS)}
    assert "postgis.write@1" not in ids
    assert "system.execute_sql@1" not in ids
    with pytest.raises(CapabilityError) as info:
        validate_request("postgis.write@1", {}, CLIENT_QGIS)
    assert "read-only" in str(info.value)


def test_an_unregistered_capability_cannot_be_called():
    for identifier in ("shell:run@1", "qgis.python@1", "", None, "analytics.group@2"):
        with pytest.raises(CapabilityError):
            validate_request(identifier, {}, CLIENT_QGIS)


def test_unexpected_parameters_are_rejected_rather_than_silently_dropped():
    # A smuggled sql/python key must fail loudly, not be normalised away.
    with pytest.raises(CapabilityError):
        validate_request("analytics.numeric@1", {"layer_id": "l1", "field": "a", "sql": "drop table x"}, CLIENT_QGIS)


def test_parameters_are_range_checked_and_enum_checked():
    with pytest.raises(CapabilityError):
        validate_request("layer.opacity@1", {"layer_id": "l1", "opacity": 500}, CLIENT_QGIS)
    with pytest.raises(CapabilityError):
        validate_request("style.graduated@1", {"layer_id": "l1", "field": "a", "method": "magic"}, CLIENT_QGIS)
    with pytest.raises(CapabilityError):
        validate_request("analytics.numeric@1", {"layer_id": "l1"}, CLIENT_QGIS)


def test_defaults_are_applied_for_omitted_optional_parameters():
    request = validate_request("style.graduated@1", {"layer_id": "l1", "field": "pop"}, CLIENT_QGIS)
    assert request["params"]["classes"] == 5
    assert request["params"]["method"] == "quantile"
    assert request["reversible"] is True


def test_a_bbox_parameter_must_be_four_ordered_numbers():
    ok = validate_request("map.zoom_extent@1", {"bbox": [0, 0, 1, 1]}, CLIENT_QGIS)
    assert ok["params"]["bbox"] == [0.0, 0.0, 1.0, 1.0]
    for bad in ([1, 1, 0, 0], [0, 0, 1], "0,0,1,1", None):
        with pytest.raises(CapabilityError):
            validate_request("map.zoom_extent@1", {"bbox": bad}, CLIENT_QGIS)


def test_client_scoping_keeps_desktop_only_capabilities_out_of_the_workspace():
    workspace_ids = {capability.id for capability in for_client(CLIENT_WORKSPACE)}
    assert "processing.run@1" not in workspace_ids  # QGIS Processing is desktop-only
    assert "analytics.group@1" in workspace_ids     # analytics is shared
    assert "postgis.analyze@1" in workspace_ids     # PostGIS analytics is shared
    with pytest.raises(CapabilityError):
        validate_request("processing.run@1", {"operation": "buffer", "layer_id": "l"}, CLIENT_WORKSPACE)


def test_both_clients_share_the_same_analytics_semantics():
    # The same typed intent must exist on both surfaces, so an answer means the
    # same thing whether it was computed in QGIS or in the workspace.
    shared = {"analytics.group@1", "analytics.numeric@1", "analytics.top_n@1", "spatial.relate@1"}
    qgis_ids = {capability.id for capability in for_client(CLIENT_QGIS)}
    workspace_ids = {capability.id for capability in for_client(CLIENT_WORKSPACE)}
    assert shared <= qgis_ids
    assert shared <= workspace_ids


def test_contributors_add_a_capability_without_touching_the_agent():
    register(Capability("custom.hillshade@1", "style", "Render a hillshade.",
                        params={"layer_id": {"type": "string", "required": True}},
                        clients=(CLIENT_QGIS,)))
    assert "custom.hillshade@1" in supported_action_kinds(CLIENT_QGIS)
    assert validate_request("custom.hillshade@1", {"layer_id": "l1"}, CLIENT_QGIS)["risk"] == "safe"


def test_a_capability_id_must_be_versioned():
    with pytest.raises(ValueError):
        register(Capability("unversioned", "misc", "x"))


def test_forbidden_risk_capabilities_carry_a_stated_refusal():
    forbidden = [item for item in for_client(CLIENT_QGIS, include_forbidden=True) if item.risk == RISK_FORBIDDEN]
    assert forbidden
    assert all(item.refusal for item in forbidden)


# --------------------------------------------------------------------------
# Decision parsing
# --------------------------------------------------------------------------

def test_a_decision_survives_prose_and_code_fences_around_the_json():
    reply = 'Sure!\n```json\n{"action":"call","capability":"map.refresh@1","params":{}}\n```\nHope that helps.'
    assert parse_decision(reply)["capability"] == "map.refresh@1"
    bare = 'I will do this: {"action":"answer","message":"hello"} ok?'
    assert parse_decision(bare)["message"] == "hello"


def test_nested_json_objects_are_extracted_whole():
    reply = json.dumps({"action": "call", "capability": "map.zoom_extent@1", "params": {"bbox": [0, 0, 1, 1]}})
    assert parse_decision(reply)["params"]["bbox"] == [0.0, 0.0, 1.0, 1.0]


def test_a_reply_with_no_decision_is_an_error_not_a_guess():
    for reply in ("I think you should buffer the roads.", "", "```python\nprint(1)\n```"):
        with pytest.raises(AgentError):
            parse_decision(reply)


def test_a_model_cannot_invent_an_action_verb():
    with pytest.raises(AgentError):
        parse_decision(json.dumps({"action": "exec", "command": "rm -rf /"}))


def test_a_model_cannot_smuggle_python_through_a_known_capability():
    with pytest.raises(CapabilityError):
        parse_decision(json.dumps({
            "action": "call", "capability": "map.refresh@1", "params": {"python": "import os"},
        }))


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def test_the_agent_chains_steps_and_carries_measured_facts():
    provider = ScriptedProvider([
        call("inspect.fields@1", layer_id="parcels"),
        call("analytics.group@1", layer_id="parcels", group_field="landuse", statistic="count"),
        answer("Residential dominates with 5 of 10 parcels."),
    ])
    results = {
        "inspect.fields@1": {"fields": [{"name": "landuse", "type": "text"}]},
        "analytics.group@1": {"groups": [{"group": "residential", "value": 5.0}]},
    }
    session = AgentSession(provider, lambda request: results[request["capability"]])
    outcome = session.run("landuse breakdown yap")
    assert outcome["state"] == STATE_COMPLETED
    assert outcome["message"].startswith("Residential")
    assert [item["capability"] for item in outcome["evidence"]] == ["inspect.fields@1", "analytics.group@1"]
    # The stated number came from an executed capability, not from the model.
    assert outcome["evidence"][1]["result"]["groups"][0]["value"] == 5.0


def test_the_objective_reaches_the_model_verbatim_in_any_language():
    provider = ScriptedProvider([answer("Tamam")])
    session = AgentSession(provider, lambda request: None)
    session.run("seçili parsellere git")
    # No keyword table touched the text; the model sees exactly what was typed,
    # and the shared contract tells it to reply in the user's language.
    normalized = " ".join(provider.last_system.lower().split())
    assert "answer in the language the user wrote in" in normalized


def test_map_effects_are_collected_so_analysis_lands_on_the_canvas():
    provider = ScriptedProvider([
        call("analytics.top_n@1", layer_id="parcels", field="area", limit=3),
        call("selection.by_ids@1", layer_id="parcels", feature_ids=["10", "9", "8"]),
        answer("Selected the three largest parcels."),
    ])
    session = AgentSession(provider, lambda request: {"feature_ids": ["10", "9", "8"]})
    outcome = session.run("show the 3 largest parcels")
    kinds = [effect["capability"] for effect in outcome["map_effects"]]
    assert "selection.by_ids@1" in kinds
    assert all(effect["reversible"] for effect in outcome["map_effects"])


def test_a_consequential_capability_is_not_run_without_confirmation():
    provider = ScriptedProvider([call("processing.run@1", operation="buffer", layer_id="roads", distance=50)])
    executed = []
    session = AgentSession(provider, lambda request: executed.append(request), confirm=lambda request: False)
    outcome = session.run("roads'a 50 metre buffer yap")
    assert outcome["kind"] == "confirmation_required"
    assert session.state == STATE_CONFIRM
    assert executed == []
    assert outcome["pending_confirmation"]["capability"] == "processing.run@1"


def test_a_confirmed_consequential_capability_does_run():
    provider = ScriptedProvider([
        call("processing.run@1", operation="buffer", layer_id="roads", distance=50),
        answer("Buffer complete."),
    ])
    executed = []
    session = AgentSession(
        provider,
        lambda request: executed.append(request["capability"]) or {"layer": "buffered"},
        confirm=lambda request: True,
    )
    outcome = session.run("buffer roads by 50 m")
    assert executed == ["processing.run@1"]
    assert outcome["state"] == STATE_COMPLETED


def test_an_invalid_capability_choice_is_recoverable_within_the_budget():
    provider = ScriptedProvider([
        call("shell.run@1", command="rm -rf /"),
        call("map.refresh@1"),
        answer("Refreshed."),
    ])
    session = AgentSession(provider, lambda request: "ok")
    outcome = session.run("refresh")
    assert outcome["state"] == STATE_COMPLETED
    failures = [item for item in outcome["evidence"] if not item["ok"]]
    assert failures and failures[0]["capability"] == "(rejected)"


def test_the_loop_is_bounded_and_reports_honestly_when_it_runs_out():
    provider = ScriptedProvider([call("analytics.numeric@1", layer_id="l", field="f{}".format(index))
                                 for index in range(20)])
    session = AgentSession(provider, lambda request: {"mean": 1}, max_steps=3)
    outcome = session.run("analyse everything")
    assert outcome["kind"] == "incomplete"
    assert session.state == STATE_FAILED
    assert session.steps == 3
    # It must not fabricate a conclusion it never reached.
    assert "ran out of steps" in outcome["message"]


def test_an_identical_repeated_call_is_blocked_so_the_loop_cannot_spin():
    provider = ScriptedProvider([
        call("map.refresh@1"),
        call("map.refresh@1"),
        answer("Done."),
    ])
    calls = []
    session = AgentSession(provider, lambda request: calls.append(request["capability"]))
    session.run("refresh twice")
    assert calls == ["map.refresh@1"]


def test_an_executor_failure_becomes_an_observation_not_a_crash():
    def failing(request):
        raise RuntimeError("layer is gone")

    provider = ScriptedProvider([call("map.refresh@1"), answer("I could not refresh.")])
    session = AgentSession(provider, failing)
    outcome = session.run("refresh")
    assert outcome["state"] == STATE_COMPLETED
    assert any("layer is gone" in item["error"] for item in outcome["evidence"])


def test_a_provider_failure_surfaces_as_an_agent_error():
    class Broken:
        def complete(self, system, messages):
            raise RuntimeError("provider down")

    session = AgentSession(Broken(), lambda request: None)
    with pytest.raises(AgentError):
        session.run("anything")
    assert session.state == STATE_FAILED


def test_clarification_stops_the_loop_and_asks_one_question():
    provider = ScriptedProvider(['{"action":"clarify","message":"Which layer: parcels or plots?"}'])
    session = AgentSession(provider, lambda request: None)
    outcome = session.run("analyze it")
    assert outcome["kind"] == "clarify"
    assert session.state == STATE_CLARIFY
    assert outcome["message"].endswith("?")


def test_measured_facts_exclude_failures_so_a_report_cannot_cite_them():
    provider = ScriptedProvider([call("map.refresh@1"), answer("ok")])

    def failing(request):
        raise RuntimeError("nope")

    session = AgentSession(provider, failing)
    session.run("refresh")
    assert session.measured_facts() == []
