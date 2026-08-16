"""Placing real features.

"türkiyeye 20 tane nokta at" used to resolve to CREATE_LAYER - the nearest
capability that existed - and produced an empty layer. Doing something adjacent
silently is worse than refusing: the layer appeared, so the request looked
handled while nothing had been placed.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.features import (  # noqa: E402
    MAX_FEATURES,
    can_place,
    describe_placement,
    describe_unplaceable_geometry,
    plan_points,
    scatter_in_rectangle,
    valid_pair,
)
from mapdex_qgis.nivo import IMPLEMENTED_ACTIONS, SAFE_PARAM_KEYS, allowed_actions  # noqa: E402

TURKEY = [25.6, 35.8, 44.8, 42.1]


def test_explicit_coordinates_are_used_exactly():
    plan = plan_points({"count": 1, "coordinates": [[28.97, 41.01]]})
    assert plan["points"] == [(28.97, 41.01)]
    assert plan["source"] == "coordinates"


def test_more_features_than_coordinates_repeats_the_given_points():
    # Inventing nearby locations would be fabricating positions the user never
    # gave; repeating what they did give is honest.
    plan = plan_points({"count": 4, "coordinates": [[10.0, 20.0], [11.0, 21.0]]})
    assert plan["points"] == [(10.0, 20.0), (11.0, 21.0), (10.0, 20.0), (11.0, 21.0)]


def test_points_scatter_inside_the_requested_area():
    plan = plan_points({"count": 20, "bbox": TURKEY}, seed="turkey")
    assert len(plan["points"]) == 20
    for lon, lat in plan["points"]:
        assert TURKEY[0] <= lon <= TURKEY[2]
        assert TURKEY[1] <= lat <= TURKEY[3]


def test_placement_is_reproducible_for_the_same_request():
    # The same question twice must give the same answer, or nothing about the
    # result is checkable.
    first = plan_points({"count": 5, "bbox": TURKEY}, seed="abc")
    second = plan_points({"count": 5, "bbox": TURKEY}, seed="abc")
    assert first["points"] == second["points"]
    assert plan_points({"count": 5, "bbox": TURKEY}, seed="xyz")["points"] != first["points"]


def test_a_centre_without_an_extent_scatters_around_it():
    plan = plan_points({"count": 3, "center": [28.97, 41.01]}, seed="s")
    assert len(plan["points"]) == 3
    for lon, lat in plan["points"]:
        assert abs(lon - 28.97) <= 0.05
        assert abs(lat - 41.01) <= 0.05


def test_a_degenerate_extent_does_not_stack_infinite_precision_noise():
    plan = plan_points({"count": 3, "bbox": [10.0, 20.0, 10.0, 20.0]})
    assert plan["points"] == [(10.0, 20.0)] * 3
    assert plan["source"] == "point_extent"


def test_no_area_and_no_coordinates_places_nothing():
    plan = plan_points({"count": 5})
    assert plan["points"] == []
    assert plan["reason"] == "no_area"


def test_counts_are_bounded_and_never_below_one():
    assert len(plan_points({"count": 10**6, "bbox": TURKEY})["points"]) == MAX_FEATURES
    assert len(plan_points({"count": 0, "bbox": TURKEY})["points"]) == 1
    assert len(plan_points({"count": "abc", "bbox": TURKEY})["points"]) == 1


def test_malformed_coordinates_are_rejected_not_clamped():
    for bad in ([1], [1, 2, 3], ["a", "b"], [200.0, 10.0], [10.0, 100.0], None):
        assert valid_pair(bad) is None
    # A malformed pair must not become an area either.
    assert plan_points({"count": 2, "coordinates": [[500.0, 900.0]]})["points"] == []


def test_an_inverted_bbox_is_refused():
    assert plan_points({"count": 2, "bbox": [10, 10, 0, 0]})["points"] == []


def test_viewport_scatter_stays_in_the_extent_it_was_given():
    points = scatter_in_rectangle([0.0, 0.0, 100.0, 50.0], 25, seed="v")
    assert len(points) == 25
    for x, y in points:
        assert 0.0 <= x <= 100.0 and 0.0 <= y <= 50.0


def test_the_reply_states_what_landed_not_what_was_asked_for():
    assert describe_placement(20, 20, "Türkiye") == "Added 20 features in Türkiye."
    assert describe_placement(1, 1, "") == "Added 1 feature."
    assert "3 of the 20" in describe_placement(3, 20, "Türkiye")
    assert describe_placement(0, 5, "Türkiye") == "I could not place any features."


# A polygon request used to create "New polygon layer" and then feed it point
# geometry, so QGIS refused every feature ("Could not add feature with geometry
# type Point to layer of type Polygon") and an empty layer stayed on the map.
def test_only_positions_can_be_placed_shapes_are_refused():
    assert can_place("point") and can_place("multipoint") and can_place("POINT")
    for geometry in ("polygon", "multipolygon", "linestring", "multilinestring", ""):
        assert not can_place(geometry)


def test_the_refusal_names_the_shape_and_offers_a_real_next_step():
    assert "polygon" in describe_unplaceable_geometry("polygon")
    assert "polygon" in describe_unplaceable_geometry("multipolygon")
    assert "line" in describe_unplaceable_geometry("linestring")
    assert "Draw" in describe_unplaceable_geometry("polygon")


def test_the_plugin_refuses_before_it_creates_a_layer():
    # Order matters: creating the layer first and failing afterwards is what
    # left an empty polygon layer behind, looking like a completed request.
    source = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
    body = source.split("def _add_features", 1)[1].split("\n    def ", 1)[0]
    assert body.index("can_place(geometry)") < body.index("_layer_for_features(")


def test_the_action_is_advertised_and_its_parameters_are_allowlisted():
    assert "qgis:add_features@1" in IMPLEMENTED_ACTIONS
    actions = allowed_actions({"companion_actions": [{
        "action_id": "act_add",
        "kind": "qgis:add_features@1",
        "params": {"geometry": "point", "count": 20, "bbox": TURKEY, "crs": "EPSG:4326", "place": "Türkiye"},
    }]})
    assert len(actions) == 1
    assert actions[0]["params"]["count"] == 20


def test_an_unexpected_parameter_still_rejects_the_action():
    actions = allowed_actions({"companion_actions": [{
        "action_id": "act_add",
        "kind": "qgis:add_features@1",
        "params": {"geometry": "point", "python": "import os"},
    }]})
    assert actions == []
    assert "python" not in SAFE_PARAM_KEYS["qgis:add_features@1"]
