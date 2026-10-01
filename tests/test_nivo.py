import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import (
    ALLOWED_ACTIONS,
    SUPPORTED_QGIS_PROCESSING_OPERATIONS,
    allowed_actions,
    companion_context,
    confirmation_action_problem,
    confirmation_actions,
    local_processing_action,
    transition,
)
from mapdex_qgis._vendor.nivo.processing import (
    build_algorithm_parameters,
    resolve_processing_algorithm,
    safe_processing_params,
)


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
        {
            "action_id": "act_table",
            "kind": "qgis:open_attribute_table@1",
            "target": "layer_1",
            "params": "not-an-object",
        },
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


def test_confirmation_accepts_legacy_response_without_unused_server_tokens():
    actions = confirmation_actions({"companion_actions": [{
        "action_id": "act_buffer",
        "kind": "qgis:processing_operation@1",
        "requires_confirmation": True,
        "target": "layer_abc",
        "params": {"operation": "buffer", "distance": 200000},
    }]})
    assert len(actions) == 1
    assert actions[0]["params"]["distance"] == 200000.0


def test_rejected_confirmation_explains_the_missing_target():
    response = {"companion_actions": [{
        "action_id": "act_buffer",
        "kind": "qgis:processing_operation@1",
        "requires_confirmation": True,
        "params": {"operation": "buffer", "distance": 25},
    }]}
    assert "target layer" in confirmation_action_problem(response)


def test_confirmation_recovers_transport_dropped_action_from_grounded_trace():
    response = {
        "id": "cmp_buffer",
        "mode": "direct_ui_command",
        "text": "Nivo prepared a QGIS Processing operation for confirmation.",
        "trace": {"steps": [{
            "id": "step_action",
            "capability": "qgis:processing_operation@1",
            "surface": "client",
            "risk": "consequential",
            "status": "succeeded",
            "params": {
                "operation": "buffer",
                "distance": 3000,
                "input_layer": "layer_chile",
            },
        }]},
    }
    actions = confirmation_actions(response)
    assert len(actions) == 1
    assert actions[0]["target"] == "layer_chile"
    assert actions[0]["params"] == {"operation": "buffer", "distance": 3000.0}


def test_confirmation_recovers_named_buffer_capability_from_live_trace():
    response = {
        "text": "Nivo prepared a QGIS Processing operation for confirmation.",
        "companion_actions": [{
            "kind": "geoprocessing.buffer@1",
            "target": "contours_1000m",
            "params": {"distance": 2000, "operation": "buffer"},
        }],
        "trace": {"turn_id": "turn_live", "steps": [{
            "id": "turn_live.s2",
            "capability": "geoprocessing.buffer@1",
            "surface": "client",
            "risk": "consequential",
            "status": "succeeded",
            "params": {
                "operation": "buffer",
                "input_layer": "contours_1000m",
                "distance": 2000,
            },
        }]},
    }

    actions = confirmation_actions(response)

    assert len(actions) == 1
    assert actions[0]["tool"] == "qgis:processing_operation@1"
    assert actions[0]["target"] == "contours_1000m"
    assert actions[0]["params"]["distance"] == 2000.0


def test_confirmation_recovers_every_named_geoprocessing_capability():
    capabilities = {
        "geoprocessing.{}@1".format(operation): operation
        for operation in (
            "buffer", "clip", "intersection", "union", "difference",
            "dissolve", "merge", "centroid", "convex_hull", "reproject",
            "spatial_join", "simplify", "repair", "validate", "split",
            "zonal_statistics",
        )
    }
    capabilities.update({
        "terrain.{}@1".format(operation): operation
        for operation in (
            "slope", "aspect", "hillshade", "ruggedness", "roughness",
            "flow_accumulation", "watershed", "viewshed", "contours",
        )
    })
    two_layer = {
        "clip", "intersection", "union", "difference", "merge",
        "spatial_join", "split", "zonal_statistics",
    }
    for capability, operation in capabilities.items():
        params = {"input_layer": "layer_source"}
        if operation in two_layer:
            params["target_layer"] = "layer_overlay"
        if operation == "buffer":
            params["distance"] = 2000
        if operation == "simplify":
            params["tolerance"] = 10
        if operation == "reproject":
            params["target_crs"] = "EPSG:4326"
        if operation == "contours":
            params["interval"] = 100
        response = {
            "trace": {"turn_id": "turn_all", "steps": [{
                "id": "turn_all.{}".format(operation),
                "capability": capability,
                "surface": "client",
                "risk": "consequential",
                "params": params,
            }]},
        }

        actions = confirmation_actions(response)

        assert len(actions) == 1, capability
        assert actions[0]["params"]["operation"] == operation


def test_generic_processing_bridge_covers_the_complete_local_catalog():
    two_layer = {
        "clip", "intersection", "union", "difference", "merge",
        "select_by_location", "nearest_neighbor", "split",
        "zonal_statistics", "spatial_join",
    }
    for operation in SUPPORTED_QGIS_PROCESSING_OPERATIONS:
        params = {"operation": operation, "input_layer": "layer_source"}
        if operation in two_layer:
            params["target_layer"] = "layer_overlay"
        if operation == "reproject":
            params["target_crs"] = "EPSG:4326"
        if operation == "contours":
            params["interval"] = 100
        response = {
            "trace": {"turn_id": "turn_catalog", "steps": [{
                "id": "turn_catalog.{}".format(operation),
                "capability": "processing.run@1",
                "surface": "client",
                "risk": "consequential",
                "params": params,
            }]},
        }

        actions = confirmation_actions(response)

        assert len(actions) == 1, operation
        assert actions[0]["params"]["operation"] == operation


def test_named_geoprocessing_capability_rejects_mismatched_operation():
    response = {
        "trace": {"turn_id": "turn_confused", "steps": [{
            "id": "turn_confused.s1",
            "capability": "geoprocessing.buffer@1",
            "surface": "client",
            "risk": "consequential",
            "params": {
                "operation": "dissolve",
                "input_layer": "layer_source",
                "distance": 2000,
            },
        }]},
    }

    assert confirmation_actions(response) == []


def test_local_processing_fallback_recovers_the_live_centroid_sentence():
    action = local_processing_action("Create centroids from this layer", "layer_chile")

    assert action["target"] == "layer_chile"
    assert action["params"] == {"operation": "centroid"}
    assert "undo_token" not in action


def test_local_processing_fallback_maps_numeric_parameters_by_operation():
    assert local_processing_action("Create a 2 km buffer", "layer")["params"]["distance"] == 2000
    assert local_processing_action("Simplify with a 100 meter tolerance", "layer")["params"]["tolerance"] == 100
    assert local_processing_action("Generate contours every 50 metres", "layer")["params"]["interval"] == 50


def test_local_processing_fallback_refuses_ambiguous_or_two_layer_requests():
    assert local_processing_action("make this nicer", "layer") == {}
    assert local_processing_action("clip this layer", "layer") == {}


def test_confirmation_does_not_recover_untrusted_or_incomplete_trace():
    base = {
        "id": "cmp_buffer",
        "mode": "direct_ui_command",
        "trace": {"steps": [{
            "id": "step_action",
            "capability": "qgis:processing_operation@1",
            "surface": "client",
            "risk": "safe",
            "params": {"operation": "buffer", "distance": 3000, "input_layer": "layer_chile"},
        }]},
    }
    assert confirmation_actions(base) == []
    base["trace"]["steps"][0]["risk"] = "consequential"
    base["trace"]["steps"][0]["params"]["python"] = "dangerous()"
    assert confirmation_actions(base) == []


def test_confirmation_recovers_the_legacy_generic_trace_shape():
    response = {
        "id": "cmp_legacy",
        "mode": "direct_ui_command",
        "text": "Nivo prepared a QGIS Processing operation for confirmation.",
        "trace": {"steps": [{
            "id": "step_desktop",
            "capability": "qgis:action",
            "status": "succeeded",
            "params": {
                "operation": "buffer",
                "distance": 3000,
                "input_layer": "layer_chile",
            },
        }]},
    }
    actions = confirmation_actions(response)
    assert len(actions) == 1
    assert actions[0]["target"] == "layer_chile"
    assert actions[0]["params"]["distance"] == 3000.0


def test_confirmation_recovery_does_not_depend_on_the_router_mode_label():
    for mode in ("lightweight_action", "", None):
        response = {
            "id": "cmp_mode",
            "mode": mode,
            "trace": {"steps": [{
                "id": "step_desktop",
                "capability": "qgis:action",
                "params": {
                    "operation": "buffer",
                    "distance": 2000,
                    "input_layer": "layer_chile",
                },
            }]},
        }
        assert len(confirmation_actions(response)) == 1, mode


def test_osm_xyz_basemap_action_is_provider_allowlisted():
    actions = allowed_actions({"companion_actions": [
        {"action_id": "act_osm", "kind": "qgis:add_xyz_basemap@1", "params": {"provider": "osm"}},
        {
            "action_id": "act_custom",
            "kind": "qgis:add_xyz_basemap@1",
            "params": {"provider": "custom", "url": "https://example.com/{z}/{x}/{y}.png"},
        },
    ]})
    assert len(actions) == 1
    assert actions[0]["tool"] == "qgis:add_xyz_basemap@1"
    assert actions[0]["params"] == {"provider": "osm"}


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
