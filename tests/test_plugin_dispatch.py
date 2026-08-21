"""One dispatch path, and a transcript that states what was measured.

Two defects motivate this file.

The dispatcher was an eighteen-branch ``tool == "qgis:..."`` chain of
hand-written QGIS calls sitting next to a runtime that implements the same
operations properly. The chain and the runtime disagreed: the visibility branch
would raise on a layer that is not in the layer tree, the opacity branch pushed
nothing onto the undo stack, and ``inspect_layer`` returned three fields where
``inspect.layer@1`` returns a full profile. Whether a user got the good version
depended on which vocabulary their server happened to speak.

And a capability that ran reported ``"<summary> (numeric)"`` - the analytics
kernel measured a mean, a median and an outlier rule, and the transcript printed
the name of the result type. Measuring something and then not saying it is
indistinguishable, to the user, from not measuring it.

``plugin.py`` cannot be imported here: it imports ``qgis`` at module scope and
there is no QGIS in CI. The pure reporting functions are therefore lifted out of
its AST and executed on their own, which is a real behavioural test of the part
that has behaviour, and the dispatcher's shape is asserted against the tree.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo.capabilities import CapabilityError, validate_request  # noqa: E402

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _method(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} not found in plugin.py".format(name))


def _body(name, until):
    start = SOURCE.index("def {}(".format(name))
    return SOURCE[start:SOURCE.index(until, start)]


def _reporting_namespace():
    """Execute only plugin.py's pure reporting helpers, free of QGIS."""
    wanted = (
        "SIMPLE_RESULT_PHRASES", "RESULT_DESCRIBERS",
        "_pretty_number", "_joined", "describe_capability_result",
    )
    selected = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and (
            node.name in wanted or node.name.startswith("_describe_")
        ):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted for target in node.targets
        ):
            selected.append(node)
    assert selected, "the reporting helpers are gone from plugin.py"
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "plugin-reporting", "exec"), namespace)
    return namespace


REPORTING = _reporting_namespace()
describe = REPORTING["describe_capability_result"]


def _table(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("plugin.py has no {} table".format(name))


LEGACY_CAPABILITY_IDS = _table("LEGACY_CAPABILITY_IDS")
PLUGIN_NATIVE_ACTIONS = _table("PLUGIN_NATIVE_ACTIONS")


# --------------------------------------------------------------------------
# There is one dispatch path
# --------------------------------------------------------------------------

def test_the_if_elif_chain_is_gone():
    body = _body("_apply_nivo_action", "def _capability_request")
    assert 'tool == "' not in body, "the hand-written dispatch chain is back"
    assert "LEGACY_CAPABILITY_IDS" in SOURCE
    assert "PLUGIN_NATIVE_ACTIONS" in body


def test_the_dispatcher_routes_through_validation_and_the_executor():
    body = _body("_run_capability", "\n    @staticmethod")
    assert "validate_request(" in body
    assert "_capability_executor()(request)" in body


def test_every_legacy_id_the_plugin_advertises_can_be_routed():
    from mapdex_qgis.nivo import LEGACY_ACTIONS

    routed = set(LEGACY_CAPABILITY_IDS) | set(PLUGIN_NATIVE_ACTIONS)
    # Processing runs through the separate confirmation flow.
    missing = sorted(LEGACY_ACTIONS - routed - {"qgis:processing_operation@1"})
    assert not missing, "advertised with no route: {}".format(missing)


# The payload each legacy action carries, taken from nivo.SAFE_PARAM_KEYS, after
# the dispatcher has injected the target layer. The registry has to accept every
# one of these, or a translated action fails validation in front of the user -
# which is the one way this change could be worse than the chain it replaced.
TRANSLATED_PAYLOADS = {
    "qgis:zoom_to_layer@1": {"layer_id": "layer_1"},
    "qgis:zoom_to_selection@1": {},
    # POST-translation, which is the payload validation actually sees. The
    # legacy wire shape carries `bbox`; the plugin resolves it and emits the
    # capability's declared `bounds`, and the test below covers that step
    # directly. Putting the legacy name here would have the test bypass the
    # very translation it exists to cover - which is how it passed for as long
    # as the two names happened to coincide.
    "qgis:zoom_to_extent@1": {"bounds": [25.0, 35.0, 45.0, 43.0], "crs": "EPSG:4326"},
    "qgis:refresh_canvas@1": {},
    "qgis:previous_extent@1": {},
    "qgis:add_xyz_basemap@1": {"provider": "osm"},
    "qgis:inspect_layer@1": {"layer_id": "layer_1"},
    "qgis:open_attribute_table@1": {"layer_id": "layer_1"},
    "qgis:set_layer_visibility@1": {"layer_id": "layer_1", "visible": False},
    "qgis:set_layer_opacity@1": {"layer_id": "layer_1", "opacity": 60},
    "qgis:select_all@1": {"layer_id": "layer_1"},
    "qgis:clear_selection@1": {"layer_id": "layer_1"},
    "qgis:invert_selection@1": {"layer_id": "layer_1"},
}


def test_every_translated_legacy_payload_passes_registry_validation():
    for legacy, canonical in LEGACY_CAPABILITY_IDS.items():
        assert legacy in TRANSLATED_PAYLOADS, "no payload covered for {}".format(legacy)
        validate_request(canonical, TRANSLATED_PAYLOADS[legacy])


def test_a_smuggled_parameter_is_still_refused_after_translation():
    # Translation must not become a way around the registry's closed parameter
    # set: the point of routing here is that legacy actions gain the checks,
    # not that canonical ids lose them.
    try:
        validate_request("layer.opacity@1", {"layer_id": "layer_1", "opacity": 60, "sql": "DROP TABLE t"})
    except CapabilityError:
        return
    raise AssertionError("an unexpected parameter was accepted")


# --------------------------------------------------------------------------
# A consequential capability is gated by the registry, not by the server
# --------------------------------------------------------------------------

def test_the_registry_marks_the_field_calculator_consequential():
    request = validate_request(
        "field.calculate@1",
        {"layer_id": "layer_1", "field": "area_m2", "expression": "1 + 1"},
    )
    assert request["requires_confirmation"] is True


def test_the_dispatcher_asks_before_a_consequential_capability():
    """A compose response that omits the flag must not run this silently.

    `field.calculate@1` writes a column into the user's own data and there is no
    undo for it outside QGIS's edit buffer. Before this, `_run_capability`
    executed whatever validated, so the only thing standing between a model and
    a written column was the server remembering to set a flag.
    """
    body = _body("_run_capability", "\n    @staticmethod")
    assert 'request.get("requires_confirmation")' in body
    assert "_confirm_capability" in body
    gate = body.index('request.get("requires_confirmation")')
    execute = body.index("_capability_executor()(request)")
    assert gate < execute, "the confirmation is asked after the capability already ran"


def test_the_confirmation_reads_the_registry_not_the_action():
    body = _body("_confirm_capability", "\n    def _plugin_capability_handlers")
    assert "get_capability(" in body
    assert "QMessageBox.question" in body


# --------------------------------------------------------------------------
# The executor table carries both halves
# --------------------------------------------------------------------------

def test_the_plugin_handlers_are_chained_onto_the_runtime_table():
    body = _body("_capability_executor", "\n    def _run_capability")
    assert "build_executor(" in body
    assert "_plugin_capability_handlers()" in body
    assert "runtime(request)" in body, "the runtime table must still be consulted"
    assert "execute.capabilities" in body, "the composed executor must advertise both halves"


def test_the_executor_is_still_built_lazily():
    body = _body("_capability_executor", "\n    def _run_capability")
    assert "_nivo_executor" in body and "if executor is None" in body


# --------------------------------------------------------------------------
# The transcript states the measurement
# --------------------------------------------------------------------------

def test_a_numeric_summary_reports_its_numbers():
    line = describe("Average parcel area", {
        "kind": "numeric", "usable": 128, "count": 130, "nulls": 2,
        "mean": 1234.5, "median": 900.0, "min": 12.0, "max": 9800.0, "scope": "all",
    })
    for expected in ("Average parcel area", "128 values", "1,234.5", "900", "12", "9,800", "2 empty"):
        assert expected in line, "{!r} missing from {!r}".format(expected, line)


def test_a_truncated_read_says_so_instead_of_describing_a_sample_as_the_layer():
    line = describe("", {
        "kind": "numeric", "usable": 200000, "nulls": 0, "mean": 1.0,
        "median": 1.0, "min": 0.0, "max": 2.0, "truncated": True,
    })
    assert "stopped at the row limit" in line
    assert "part of the layer" in line


def test_an_empty_column_is_not_reported_as_a_measurement():
    line = describe("Average area", {"kind": "numeric", "count": 4, "usable": 0, "nulls": 4})
    assert "no usable numbers" in line
    assert "mean" not in line


def test_a_frequency_table_names_the_top_categories():
    line = describe("", {
        "kind": "categorical", "distinct": 3, "usable": 10, "nulls": 0, "truncated": False,
        "categories": [
            {"value": "residential", "count": 6, "percent": 60.0},
            {"value": "commercial", "count": 3, "percent": 30.0},
            {"value": "industrial", "count": 1, "percent": 10.0},
        ],
    })
    assert "3 distinct values in 10" in line
    assert "residential (6)" in line


def test_outliers_carry_the_rule_that_flagged_them():
    line = describe("", {
        "kind": "outliers", "field": "area", "method": "iqr", "matched": 4, "considered": 120,
        "rule": "Tukey fences at 1.5 x IQR", "lower_bound": -10.0, "upper_bound": 3000.0,
    })
    assert "4 of 120 flagged" in line
    assert "Tukey fences" in line
    assert "3,000" in line


def test_too_few_values_is_stated_rather_than_reported_as_zero_outliers():
    line = describe("", {
        "kind": "outliers", "field": "area", "method": "iqr", "matched": 0,
        "feature_ids": [], "reason": "not_enough_values",
    })
    assert "too few values" in line


def test_a_group_aggregate_reports_the_groups():
    line = describe("", {
        "kind": "group_aggregate", "group_field": "district", "value_field": "area",
        "statistic": "mean", "group_count": 2, "skipped_null_values": 3,
        "groups": [{"group": "Kadikoy", "value": 812.0, "features": 9},
                   {"group": "Besiktas", "value": 640.5, "features": 4}],
    })
    assert "mean of area by district" in line
    assert "Kadikoy 812" in line
    assert "3 rows had no value" in line


def test_an_analysis_drawn_on_the_map_reports_the_analysis():
    line = describe("Colour by type", {
        "analysis": {"kind": "categorical", "distinct": 2, "usable": 5, "nulls": 0,
                     "categories": [{"value": "a", "count": 3}, {"value": "b", "count": 2}]},
        "map": {"kind": "style_applied", "style": "categorized", "classes": 2},
    })
    assert "2 distinct values in 5" in line
    assert "shown on the map" in line


def test_a_layer_profile_reads_as_a_profile():
    line = describe("", {
        "kind": "layer_profile", "name": "Parcels", "type": "vector", "crs": "EPSG:5254",
        "feature_count": 4210, "selected": 0, "fields": [{"name": "id"}, {"name": "area"}],
    })
    assert "Parcels" in line and "4,210 features" in line and "EPSG:5254" in line


def test_an_ungeoreferenced_raster_is_named_as_such():
    line = describe("", {
        "kind": "layer_profile", "name": "Scan", "type": "raster", "crs": "",
        "width": 8000, "height": 6000, "bands": 3, "georeferenced": False,
    })
    assert "not georeferenced" in line
    assert "no CRS" in line


def test_a_measurement_states_which_method_produced_it():
    line = describe("", {"kind": "measurement", "metres": 1523.44, "method": "ellipsoidal"})
    assert "1,523.44" in line and "ellipsoidal" in line


def test_an_export_states_where_the_file_went():
    line = describe("", {"kind": "export", "path": "/tmp/mapdex-exports/Parcels.gpkg",
                         "format": "gpkg", "features": 4210, "bytes": 91234})
    assert "4,210 features" in line and "Parcels.gpkg" in line


def test_a_plain_effect_is_a_plain_sentence():
    assert describe("Zoom in", {"kind": "zoomed", "layer": "Parcels"}) == "Zoomed to the layer."
    assert describe("", {"kind": "selection_cleared"}) == "Cleared the selection."


def test_an_unknown_result_kind_is_not_dressed_up_as_a_measurement():
    line = describe("Did the thing", {"kind": "something_new", "count": 7})
    assert "7" not in line, "an unrecognised shape must not have numbers read out of it"
    assert line == "Did the thing"


def test_a_non_dict_result_falls_back_to_the_summary():
    assert describe("Did the thing", None) == "Did the thing"
    assert describe("", None) == "Done."


def test_numbers_are_readable_and_never_invented():
    pretty = REPORTING["_pretty_number"]
    assert pretty(4210) == "4,210"
    assert pretty(1234.5) == "1,234.5"
    assert pretty(0.0) == "0"
    assert pretty(None) == "None"
    assert pretty(float("nan")) == "nan"


def test_the_extent_resolver_reads_either_name_for_the_box():
    """The renaming step the payload fixture above depends on.

    A plugin newer than the server it talks to still receives `bbox`, and a
    resolver that refused it would break the pairing it was renamed to fix.
    """
    from mapdex_qgis._vendor.nivo.viewport import resolve_extent

    legacy = resolve_extent({"bbox": [25.0, 35.0, 45.0, 43.0], "crs": "EPSG:4326"})
    canonical = resolve_extent({"bounds": [25.0, 35.0, 45.0, 43.0], "crs": "EPSG:4326"})
    assert legacy == canonical
    assert canonical["bbox"] == [25.0, 35.0, 45.0, 43.0]
    # And a payload carrying neither is still refused rather than guessed at.
    assert resolve_extent({"crs": "EPSG:4326"}) is None
