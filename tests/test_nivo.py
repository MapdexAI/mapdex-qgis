import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import ALLOWED_ACTIONS, allowed_actions, companion_context, transition


def test_companion_context_contains_summary_not_attribute_values_or_credentials():
    payload = companion_context({
        "bbox": [1, 2, 3, 4], "crs": "EPSG:4326", "selection_count": 3,
        "active_layer": {"name": "roads", "kind": "vector", "fields": ["name", "owner"], "feature_count": 9},
        "connections": [{"id": "pg_saved", "name": "PostGIS saved", "password": "do-not-copy"}],
    })
    assert payload["version"] == "companion.qgis.v1"
    assert payload["active_layer"]["fields"] == ["name", "owner"]
    assert "password" not in payload["connections"][0]


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
