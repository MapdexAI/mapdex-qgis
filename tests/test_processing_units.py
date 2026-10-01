"""A length stated in metres reaches Processing in metres.

The server sends `{"operation": "buffer", "distance": 25}` for "buffer this by
25 m", and the plugin used to hand 25 straight to `native:buffer`, which reads it
in the layer's own units. On EPSG:4326 that grew every side by 25 degrees -
measured in QGIS 4.0.2 by replaying the server's recorded response
(verify_qgis_end_to_end.py, "a buffer in metres on a layer in degrees grows by
metres").
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.processing_units import (  # noqa: E402
    describe_processing_failure,
    describe_textual_fields,
    describe_validation_outcome,
    input_parameter_aliases,
    plan_metric_operation,
    structured_field_names,
    structured_value_as_text,
)

# The plugin's own fixture, in degrees: a few hundred metres near Istanbul.
ISTANBUL = [28.97, 41.00, 28.99, 41.02]


def test_a_buffer_on_a_layer_in_degrees_runs_in_the_local_utm_zone():
    plan = plan_metric_operation("buffer", {"operation": "buffer", "distance": 25},
                                 True, None, ISTANBUL)
    assert plan["strategy"] == "reproject"
    assert plan["crs"] == "EPSG:32635"
    # Metres, never a degree figure: the work happens where a metre is a metre.
    assert plan["params"]["distance"] == 25


def test_a_buffer_on_a_metric_layer_is_the_stated_metres():
    plan = plan_metric_operation("buffer", {"distance": 25}, False, 1.0, None)
    assert plan["strategy"] == "map_units"
    assert plan["params"]["distance"] == 25


def test_a_buffer_on_a_layer_in_feet_is_converted_into_feet():
    plan = plan_metric_operation("buffer", {"distance": 25}, False, 1 / 0.3048, None)
    assert abs(plan["params"]["distance"] - 82.0210) < 1e-3


def test_a_stated_unit_is_honoured_and_then_dropped():
    plan = plan_metric_operation("buffer", {"distance": 2, "unit": "km"}, False, 1.0, None)
    assert plan["params"]["distance"] == 2000
    # `unit` is not an algorithm parameter; leaving it would be a second
    # source of truth about what the number means.
    assert "unit" not in plan["params"]


def test_a_simplify_tolerance_is_a_length_too():
    plan = plan_metric_operation("simplify", {"tolerance": 5}, True, None, ISTANBUL)
    assert plan["strategy"] == "reproject"
    assert plan["params"]["tolerance"] == 5


def test_a_layer_too_wide_for_one_zone_is_refused_not_guessed():
    plan = plan_metric_operation("buffer", {"distance": 25}, True, None, [-10, 35, 30, 60])
    assert plan["strategy"] == "refuse"
    assert "too large for one UTM zone" in plan["reason"]


def test_an_unknown_unit_is_refused():
    plan = plan_metric_operation("buffer", {"distance": 25, "unit": "cubits"}, False, 1.0, None)
    assert plan["strategy"] == "refuse"


def test_a_layer_with_no_length_unit_is_refused():
    plan = plan_metric_operation("buffer", {"distance": 25}, False, None, None)
    assert plan["strategy"] == "refuse"


def test_operations_without_a_ground_length_are_untouched():
    params = {"operation": "contours", "interval": 10}
    plan = plan_metric_operation("contours", params, True, None, ISTANBUL)
    assert plan == {"strategy": "none", "params": params}


def test_a_failure_says_what_failed_and_what_qgis_said():
    text = describe_processing_failure("slope", "parcels", "Loading...\nInput layer is not a raster\n")
    assert "slope" in text and "parcels" in text and "not a raster" in text
    assert describe_processing_failure("buffer", "roads") == "The buffer on 'roads' did not finish."


def test_the_geometry_check_receives_its_input_under_its_own_name():
    layer = object()
    assert input_parameter_aliases(["INPUT_LAYER", "METHOD", "VALID_OUTPUT"], layer) == {
        "INPUT_LAYER": layer}
    assert input_parameter_aliases(["INPUT", "DISTANCE"], layer) == {}


def test_a_clean_geometry_check_is_reported_as_clean_not_as_an_empty_layer():
    assert describe_validation_outcome("parcels", 0) == \
        "The geometry check found no invalid geometries in 'parcels'."
    assert "3 invalid geometries" in describe_validation_outcome("parcels", 3)
    assert "1 invalid geometry " in describe_validation_outcome("parcels", 1)


def test_a_json_field_is_found_by_its_declared_type():
    # What QGIS 4.0.2 reports for vaudherland-cadastre-preview.geojson.
    fields = [("Layer", "String", "Type.QString"),
              ("confidence", "Real", "Type.Double"),
              ("image_corners", "JSON", "Type.QString")]
    assert structured_field_names(fields) == ["image_corners"]


def test_list_and_map_storage_types_are_found_too():
    fields = [("tags", "", "Type.QStringList"), ("props", "", "Type.QVariantMap"),
              ("ids", "IntegerList", "Type.QVariantList"), ("name", "String", "Type.QString")]
    assert structured_field_names(fields) == ["tags", "props", "ids"]


def test_a_nested_value_becomes_the_json_it_was_written_as():
    corners = [[662185.3583333333, 6878169.449444444], [662652.9916666666, 6878169.449444444]]
    text = structured_value_as_text(corners)
    assert text == "[[662185.3583333333, 6878169.449444444], [662652.9916666666, 6878169.449444444]]"
    assert structured_value_as_text({"a": "ş"}) == '{"a": "ş"}'
    assert structured_value_as_text("already text") == "already text"


def test_an_absent_value_stays_absent():
    class Null:
        def isNull(self):
            return True

    null = Null()
    assert structured_value_as_text(None) is None
    assert structured_value_as_text(null) is null


def test_the_outcome_says_which_fields_became_text():
    assert describe_textual_fields([]) == ""
    assert "'image_corners' is carried as text" in describe_textual_fields(["image_corners"])
    assert "'a', 'b' are carried" in describe_textual_fields(["a", "b"])


def _method(name):
    tree = ast.parse((ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("plugin.py has no {}".format(name))


def _calls(node):
    return {
        child.func.attr for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }


def test_the_path_the_server_uses_applies_the_checks():
    # Every operation the server sends arrives behind a confirmation and runs
    # through _run_processing_operation. The raster and degrees checks lived
    # only in the named-capability wrapper, so on that path they did not exist.
    # A guard nothing on the user's path calls is not a guard.
    calls = _calls(_method("_run_processing_operation"))
    assert "_processing_refusal" in calls
    assert "_metric_plan" in calls
    assert "_reprojected_copy" in calls
    assert "_textual_copy" in calls
    assert "_processing_refusal" in _calls(_method("_run_processing_capability"))


def test_the_textual_copy_is_made_before_the_reprojection():
    # The reprojection is itself an algorithm writing a temporary layer, so a
    # JSON field stops it exactly as it stops the buffer.
    source = ast.get_source_segment(
        (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8"),
        _method("_run_processing_operation"))
    assert source.index("self._textual_copy(") < source.index("self._reprojected_copy(")
