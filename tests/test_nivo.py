import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import ALLOWED_ACTIONS, allowed_actions, companion_context


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
    actions = allowed_actions({"ui_commands": [
        {"tool": "qgis:zoom_to_layer@1", "params": {}},
        {"tool": "shell:run@1", "params": {"command": "rm -rf /"}},
        {"tool": "qgis:open_attribute_table@1", "params": "not-an-object"},
    ]})
    assert [item["tool"] for item in actions] == ["qgis:zoom_to_layer@1", "qgis:open_attribute_table@1"]
    assert actions[1]["params"] == {}
    assert "shell:run@1" not in ALLOWED_ACTIONS
