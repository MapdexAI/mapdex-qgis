"""What a survey answer asks the canvas for, and what it must never ask.

The payloads here are the shapes `spatial:cogo@1` actually returns, so a change
on the server that this module cannot read fails here rather than in somebody's
QGIS.
"""
from mapdex_qgis.survey_drawing import (
    MAX_FEATURES,
    attribute_names,
    layer_specs,
)


def traverse_result():
    return {
        "operation": "traverse",
        "render": "drawing",
        "drawing": {
            "type": "FeatureCollection",
            "crs": "EPSG:4326",
            "ellipsoid": "WGS84",
            "features": [
                {"type": "Feature",
                 "geometry": {"type": "LineString",
                              "coordinates": [[32.8, 39.9], [32.801, 39.901]]},
                 "properties": {"role": "computed"}},
                {"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [32.8, 39.9]},
                 "properties": {"station": 0, "role": "computed"}},
                {"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [32.801, 39.901]},
                 "properties": {"station": 1, "role": "computed", "label": "A"}},
            ],
        },
    }


def test_a_traverse_becomes_one_layer_per_geometry_type():
    """A QGIS memory layer holds one geometry type, so an answer that mixes
    them cannot be one layer however much we would like it to be."""
    specs = layer_specs(traverse_result())
    kinds = {spec.geometry_type for spec in specs}
    assert kinds == {"Point", "LineString"}, kinds

    points = next(spec for spec in specs if spec.geometry_type == "Point")
    lines = next(spec for spec in specs if spec.geometry_type == "LineString")
    assert len(points.features) == 2
    assert len(lines.features) == 1
    # Named for the operation as a person says it, not for the wire value.
    assert points.name == "Traverse stations"
    assert lines.name == "Traverse lines"


def test_the_order_is_stable():
    """A canvas that reorders itself between identical questions is a surface
    nobody trusts."""
    first = [spec.name for spec in layer_specs(traverse_result())]
    second = [spec.name for spec in layer_specs(traverse_result())]
    assert first == second


def test_an_answer_with_nothing_to_draw_asks_for_nothing():
    """The honest majority case. A scale factor is a number, and creating an
    empty layer for it would add noise to a correct answer."""
    for result in (
        {"operation": "scale_factor", "render": "none", "combined_factor": 0.9996},
        {"operation": "format", "render": "none"},
        {},
        None,
        "not a result",
        {"render": "drawing"},
        {"render": "drawing", "drawing": {"type": "FeatureCollection", "features": []}},
    ):
        assert layer_specs(result) == []


def test_a_coordinate_that_is_not_a_position_is_refused():
    """A latitude of 2589 is a pixel row, and this plugin has no business
    drawing one on a map. It is the same rule the server applies."""
    result = traverse_result()
    result["drawing"]["features"].append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1323.0, 2589.0]},
        "properties": {"role": "pixel"},
    })
    points = next(spec for spec in layer_specs(result) if spec.geometry_type == "Point")
    # The two real stations survive; the pixel pair does not.
    assert len(points.features) == 2
    roles = [feature["properties"].get("role") for feature in points.features]
    assert "pixel" not in roles


def test_nested_geometry_is_held_to_the_same_rule():
    result = {
        "operation": "deed_closure",
        "render": "drawing",
        "drawing": {"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Polygon",
                          "coordinates": [[[32.8, 39.9], [32.81, 39.9],
                                           [32.81, 39.91], [32.8, 39.9]]]},
             "properties": {}},
            {"type": "Feature",
             "geometry": {"type": "Polygon",
                          "coordinates": [[[4881.0, 44190.0], [4882.0, 44190.0],
                                           [4882.0, 44191.0], [4881.0, 44190.0]]]},
             "properties": {}},
        ]},
    }
    specs = layer_specs(result)
    assert len(specs) == 1
    assert specs[0].geometry_type == "Polygon"
    assert len(specs[0].features) == 1
    assert specs[0].name == "Deed closure areas"


def test_a_geometry_type_qgis_cannot_hold_is_skipped_not_guessed():
    result = traverse_result()
    result["drawing"]["features"].append({
        "type": "Feature",
        "geometry": {"type": "GeometryCollection", "geometries": []},
        "properties": {},
    })
    kinds = {spec.geometry_type for spec in layer_specs(result)}
    assert kinds == {"Point", "LineString"}


def test_the_feature_count_is_bounded():
    result = traverse_result()
    result["drawing"]["features"] = [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [32.8, 39.9]},
         "properties": {}}
        for _ in range(MAX_FEATURES + 500)
    ]
    specs = layer_specs(result)
    assert len(specs[0].features) == MAX_FEATURES


def test_attribute_names_are_the_union_not_the_first_features_keys():
    """A traverse labels its features unevenly: the start carries no label and
    the stations do. Taking the first feature's keys would drop the label."""
    specs = layer_specs(traverse_result())
    points = next(spec for spec in specs if spec.geometry_type == "Point")
    names = attribute_names(points.features)
    assert "label" in names, names
    assert "station" in names and "role" in names
    assert names == sorted(names)


def test_an_unknown_operation_still_draws_under_a_general_name():
    """A new operation on the server must not silently stop drawing here."""
    result = traverse_result()
    result["operation"] = "some_new_computation"
    specs = layer_specs(result)
    assert specs and specs[0].name.startswith("Survey result")
