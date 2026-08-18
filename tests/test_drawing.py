"""Drawing a shape on the canvas, and that shape becoming a layer.

Two gaps meet here. `features.py` will place points and refuses to invent a
polygon - correctly, because corners nobody supplied are fabricated geometry -
so the product could produce made-up points and could not accept a boundary a
person traced. And the plugin's answer to "draw an area" was to create an empty
layer and tell the user to toggle editing, which leaves the third release-gate
scenario, draw an AOI then analyse it, with no first step.

What these tests can prove is the part that has behaviour without a canvas: the
state machine, the geometry floor, and the registry's refusal of anything that
is not a real coordinate. Whether `DrawMapTool` actually binds to a QGIS canvas
and whether a click lands where the user pointed are questions only a running
QGIS answers, and `testdata/verify_qgis_end_to_end.py` is where they are asked.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.capabilities import CapabilityError, get, validate_request  # noqa: E402
from mapdex_qgis.maptools import (  # noqa: E402
    MINIMUM_VERTICES,
    DrawState,
    distinct_vertices,
    refusal_for,
)
from mapdex_qgis.qgis_runtime import PLUGIN_BOUND_CAPABILITIES, bound_capability_ids  # noqa: E402

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------

def test_a_point_is_finished_by_its_own_click():
    # One click IS the whole shape. Requiring a finishing gesture would give the
    # simplest case a hidden extra step.
    state = DrawState("point")
    outcome = state.click(28.9784, 41.0082)
    assert outcome["kind"] == "finish"
    assert outcome["vertices"] == [[28.9784, 41.0082]]


def test_a_line_collects_until_it_is_finished():
    state = DrawState("linestring")
    first = state.click(0, 0)
    assert first["kind"] == "vertex"
    assert first["still_needed"] == 1
    second = state.click(1, 1)
    assert second["kind"] == "vertex"
    assert second["still_needed"] == 0
    assert state.finish()["vertices"] == [[0.0, 0.0], [1.0, 1.0]]


def test_finishing_too_early_keeps_the_points_already_placed():
    # The user has not finished. Discarding their clicks in order to say so
    # would cost them the corners as well as the shape.
    state = DrawState("polygon")
    state.click(0, 0)
    state.click(1, 0)
    outcome = state.finish()
    assert outcome["kind"] == "incomplete"
    assert outcome["count"] == 2
    assert "3 distinct" in outcome["reason"]
    assert len(state.vertices) == 2


def test_a_double_click_does_not_place_two_corners_on_one_spot():
    state = DrawState("polygon")
    state.click(5, 5)
    repeated = state.click(5, 5)
    assert repeated["kind"] == "repeat"
    assert state.vertices == [[5.0, 5.0]]


def test_a_finished_shape_does_not_leak_into_the_next_one():
    # The failure this guards is the measure tool's: a vertex from an abandoned
    # shape pairing with the first click of the next one, minutes later and
    # somewhere else entirely.
    state = DrawState("linestring")
    state.click(0, 0)
    state.click(1, 1)
    state.finish()
    assert state.vertices == []
    assert state.click(9, 9)["count"] == 1


def test_cancelling_reports_what_it_threw_away():
    state = DrawState("polygon")
    for x, y in SQUARE:
        state.click(x, y)
    outcome = state.cancel()
    assert outcome == {"kind": "cancelled", "discarded": 4}
    assert state.vertices == []


def test_an_unknown_geometry_never_finishes():
    state = DrawState("circle")
    state.click(0, 0)
    assert state.finish()["kind"] == "incomplete"


# --------------------------------------------------------------------------
# The geometry floor
# --------------------------------------------------------------------------

def test_repeated_positions_are_not_separate_corners():
    assert distinct_vertices([[1, 1], [1, 1], [2, 2], [1, 1]]) == [(1.0, 1.0), (2.0, 2.0)]


def test_a_polygon_needs_three_distinct_corners_not_three_clicks():
    # QGIS accepts a degenerate polygon and the layer then holds a shape whose
    # area every later measurement reports as zero: a wrong answer with no error
    # in front of it.
    assert refusal_for("polygon", [[0, 0], [0, 0], [0, 0]])
    assert refusal_for("polygon", [[0, 0], [1, 0], [1, 1]]) == ""


def test_a_closed_ring_is_still_three_corners():
    assert refusal_for("polygon", [[0, 0], [1, 0], [1, 1], [0, 0]]) == ""


def test_each_geometry_states_its_own_minimum():
    assert MINIMUM_VERTICES == {"point": 1, "linestring": 2, "polygon": 3}
    assert refusal_for("linestring", [[0, 0]])
    assert refusal_for("point", [])
    assert refusal_for("point", [[0, 0]]) == ""


def test_a_shape_this_tool_does_not_draw_is_named_in_the_refusal():
    assert "multipolygon" in refusal_for("multipolygon", SQUARE)


# --------------------------------------------------------------------------
# The registry boundary
# --------------------------------------------------------------------------

def _request(**overrides):
    params = {"geometry": "polygon", "vertices": SQUARE, "crs": "EPSG:3857"}
    params.update(overrides)
    return params


def test_a_drawn_polygon_validates_and_keeps_its_numbers():
    request = validate_request("draw.geometry@1", _request())
    assert request["params"]["vertices"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    assert request["params"]["crs"] == "EPSG:3857"
    # Adding a layer nobody had is not a change to anybody's data.
    assert request["requires_confirmation"] is False
    assert request["reversible"] is True


def test_true_is_not_a_coordinate():
    # bool subclasses int, so True would otherwise arrive as 1.0 and put a
    # vertex on the equator.
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(vertices=[[True, 0], [1, 0], [1, 1]]))


def test_a_numeric_string_is_not_a_coordinate():
    # A caller sending "41.0" has sent text. Reading it as a position is how a
    # shape ends up somewhere nobody chose.
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(vertices=[["0", "0"], [1, 0], [1, 1]]))


def test_an_infinite_coordinate_is_refused():
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(vertices=[[float("inf"), 0], [1, 0], [1, 1]]))


def test_a_vertex_must_be_a_pair():
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(vertices=[[0, 0, 0], [1, 0], [1, 1]]))


def test_the_vertex_list_is_bounded():
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(vertices=[[1, 1]] * 501))


def test_the_crs_is_required_and_has_no_default():
    # The vertices are in whatever the canvas is projected to. Defaulting to
    # WGS84 would let a shape claim a place it was not drawn in.
    assert "default" not in get("draw.geometry@1").params["crs"]
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", {"geometry": "polygon", "vertices": SQUARE})


def test_only_the_shapes_the_canvas_can_collect_are_offered():
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(geometry="multipolygon"))
    assert set(get("draw.geometry@1").params["geometry"]["enum"]) == set(MINIMUM_VERTICES)


def test_a_smuggled_parameter_is_refused():
    with pytest.raises(CapabilityError):
        validate_request("draw.geometry@1", _request(expression="1 == 1"))


# --------------------------------------------------------------------------
# Reachable from the running plugin, not only from this file
# --------------------------------------------------------------------------

def _method(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} is not defined in plugin.py".format(name))


def _plugin_handler_ids():
    """The capability ids `_plugin_capability_handlers` actually routes."""
    for node in ast.walk(_method("_plugin_capability_handlers")):
        if isinstance(node, ast.Dict):
            return {
                key.value for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("_plugin_capability_handlers no longer builds a table")


def test_drawing_is_advertised_only_because_something_executes_it():
    assert "draw.geometry@1" in PLUGIN_BOUND_CAPABILITIES
    assert "draw.geometry@1" in bound_capability_ids()


def test_the_declaration_and_the_handler_table_name_the_same_ids():
    # The plugin asserts this at runtime too. Asserting it here means a drift
    # fails the suite rather than the first user who asks for the capability.
    assert _plugin_handler_ids() == set(PLUGIN_BOUND_CAPABILITIES)


def test_the_handler_exists_and_asks_the_geometry_floor():
    body = ast.dump(_method("_draw_geometry_capability"))
    assert "refusal_for" in body, (
        "the executor does not re-check how many corners the shape needs, so a "
        "request arriving from the capability channel could store a polygon "
        "with no area")


def test_the_draw_tool_is_installed_when_qgis_loads_the_plugin():
    # A map tool nothing arms is a module reachable only from its own test.
    assert "_install_draw_action" in ast.dump(_method("initGui"))
    assert "DrawMapTool" in ast.dump(_method("_toggle_draw_tool"))


def test_the_drawn_shape_goes_through_the_same_validated_path_as_a_measurement():
    body = ast.dump(_method("_drew_geometry"))
    assert "_run_capability" in body
    assert "draw.geometry@1" in body


def test_a_canvas_with_no_authority_code_is_refused_with_its_reason():
    # The registry can only say "crs is required". The user needs the reason,
    # and the reason is their project rather than their request.
    body = ast.dump(_method("_drew_geometry"))
    assert "authority code" in body


def test_arming_a_tool_puts_the_other_one_away_first():
    # Two checkable canvas tools now exist. Arming one while the other is active
    # would otherwise record OUR tool as "previous", so finishing a measurement
    # would drop the user back into drawing - in a mode whose menu item reads
    # unchecked, because _restore_map_tool clears both checkmarks.
    for name in ("_toggle_draw_tool", "_toggle_measure_tool"):
        body = ast.dump(_method(name))
        assert body.count("_restore_map_tool") >= 2, (
            "{} does not put the other tool away before recording the previous "
            "one".format(name))
        assert "setChecked" in body, (
            "{} arms a tool without checking its own action, so the menu item "
            "disagrees with the canvas".format(name))


def test_the_tool_is_put_away_when_the_plugin_unloads():
    # A map tool outlives the plugin that set it: a click on the canvas after an
    # unload would call into a dead plugin.
    body = ast.dump(_method("_restore_map_tool"))
    assert "_draw_tool" in body
    assert "_draw_action" in body


# --------------------------------------------------------------------------
# What the transcript says
# --------------------------------------------------------------------------

def _reporting_namespace():
    """plugin.py's pure reporting helpers, executed free of QGIS."""
    wanted = ("SIMPLE_RESULT_PHRASES", "RESULT_DESCRIBERS", "_pretty_number",
              "_joined", "describe_capability_result")
    selected = [
        node for node in TREE.body
        if (isinstance(node, ast.FunctionDef)
            and (node.name in wanted or node.name.startswith("_describe_")))
        or (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in wanted
                    for target in node.targets))
    ]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "plugin-reporting", "exec"), namespace)
    return namespace


def test_the_transcript_names_the_layer_and_the_crs_it_carries():
    describe = _reporting_namespace()["describe_capability_result"]
    line = describe("Draw an area on the map", {
        "kind": "geometry_drawn",
        "layer_id": "aoi_1",
        "layer_name": "New polygon layer",
        "geometry": "polygon",
        "crs": "EPSG:5254",
        "vertices": 4,
    })
    # A drawn shape is data, and a layer whose frame the user cannot see is one
    # they cannot check against anything else.
    assert "EPSG:5254" in line
    assert "New polygon layer" in line
    assert "polygon" in line
