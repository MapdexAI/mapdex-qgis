import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import ALLOWED_ACTIONS, allowed_actions, companion_context, confirmation_actions, transition
from mapdex_qgis.processing import build_algorithm_parameters, resolve_processing_algorithm, safe_processing_params


def test_companion_context_contains_summary_not_attribute_values_or_credentials():
    payload = companion_context({
        "bbox": [1, 2, 3, 4], "crs": "EPSG:4326", "selection_count": 3,
        "active_layer": {"name": "roads", "kind": "vector", "fields": ["name", "owner"], "feature_count": 9},
        "connections": [{"id": "pg_saved", "name": "PostGIS saved", "password": "do-not-copy"}],
    })
    assert payload["version"] == "companion.qgis.v1"
    assert payload["active_layer"]["fields"] == ["name", "owner"]
    assert "password" not in payload["connections"][0]


def test_companion_context_keeps_stable_layer_id_even_without_name():
    payload = companion_context({
        "active_layer": {"id": "qgis_layer_42", "kind": "vector", "feature_count": 1},
    })
    assert payload["active_layer"]["id"] == "qgis_layer_42"


def test_only_closed_qgis_actions_can_reach_dispatch():
    actions = allowed_actions({"companion_actions": [
        {"action_id": "act_zoom", "kind": "qgis:zoom_to_layer@1", "params": {}},
        {"action_id": "act_shell", "kind": "shell:run@1", "params": {"command": "rm -rf /"}},
        {"action_id": "act_table", "kind": "qgis:open_attribute_table@1", "target": "layer_1", "params": "not-an-object"},
    ]})
    assert [item["tool"] for item in actions] == ["qgis:open_attribute_table@1"]
    assert actions[0]["params"] == {}
    assert "shell:run@1" not in ALLOWED_ACTIONS


def test_targeted_action_requires_stable_layer_identity():
    actions = allowed_actions({"companion_actions": [
        {"action_id": "act_a", "kind": "qgis:zoom_to_layer@1", "target": "layer_abc", "params": {}},
        {"action_id": "act_b", "kind": "qgis:zoom_to_layer@1", "target": "roads", "params": {}},
    ]})
    assert [item["target"] for item in actions] == ["layer_abc", "roads"]


def test_turn_state_machine_rejects_stale_events():
    assert transition("idle", "send") == "composing"
    assert transition("composing", "action") == "action_ready"
    assert transition("action_ready", "execute") == "executing"
    assert transition("executing", "done") == "completed"
    assert transition("completed", "done") == "completed"


def test_confirmation_actions_only_accept_bounded_processing_payloads():
    actions = confirmation_actions({"companion_actions": [
        {
            "action_id": "act_buffer", "confirmation_id": "cnf_buffer", "idempotency_key": "idem_buffer",
            "kind": "qgis:processing_operation@1", "requires_confirmation": True, "target": "layer_abc",
            "params": {"operation": "buffer", "distance": 25, "input_layer": "layer_abc"},
        },
        {
            "action_id": "act_sql", "confirmation_id": "cnf_sql", "idempotency_key": "idem_sql",
            "kind": "qgis:processing_operation@1", "requires_confirmation": True, "target": "layer_abc",
            "params": {"operation": "buffer", "sql": "delete from roads"},
        },
        {
            "action_id": "act_unknown", "confirmation_id": "cnf_unknown", "idempotency_key": "idem_unknown",
            "kind": "qgis:processing_operation@1", "requires_confirmation": True, "target": "layer_abc",
            "params": {"operation": "arbitrary_plugin_algorithm"},
        },
    ]})
    assert len(actions) == 1
    assert actions[0]["params"] == {"operation": "buffer", "distance": 25.0}


def test_processing_registry_resolves_only_known_installed_algorithms():
    class Registry:
        def algorithmById(self, algorithm_id):
            return object() if algorithm_id == "native:buffer" else None

    algorithm_id, algorithm = resolve_processing_algorithm(Registry(), "buffer")
    assert algorithm_id == "native:buffer"
    assert algorithm is not None
    assert resolve_processing_algorithm(Registry(), "arbitrary")[1] is None


def test_processing_parameters_are_built_from_algorithm_definitions():
    class Definition:
        def __init__(self, name):
            self._name = name

        def name(self):
            return self._name

    class Algorithm:
        def parameterDefinitions(self):
            return [Definition("INPUT"), Definition("DISTANCE"), Definition("SEGMENTS"), Definition("OUTPUT")]

    class Layer:
        def crs(self):
            return "EPSG:4326"

    params = build_algorithm_parameters(Algorithm(), "buffer", Layer(), safe_processing_params({
        "operation": "buffer", "distance": "10", "segments": "8",
    }))
    assert params["INPUT"].__class__.__name__ == "Layer"
    assert params["DISTANCE"] == 10.0
    assert params["SEGMENTS"] == 8
    assert params["OUTPUT"] == "TEMPORARY_OUTPUT"
