"""The operations a professional expects from any GIS.

Seven of these existed in neither surface's vocabulary, so Nivo could not name
dissolve, merge, split, centroid, convex hull, union or difference at all. Every
one is an installed native algorithm: the gap was the allowlist, not the
capability.
"""
import pathlib

import pytest

from mapdex_qgis.processing import (
    PROCESSING_OPERATION_CATALOG,
    TWO_LAYER_OPERATIONS,
    build_algorithm_parameters,
    describe_empty_input,
    describe_processing_outcome,
    operation_label,
    safe_processing_params,
)

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py"


class FakeDefinition:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class FakeAlgorithm:
    def __init__(self, *names):
        self._names = names

    def parameterDefinitions(self):
        return [FakeDefinition(name) for name in self._names]


class FakeLayer:
    def crs(self):
        return "EPSG:4326"


# The operations named in the sprint package that had no route at all.
@pytest.mark.parametrize("operation", [
    "dissolve", "union", "difference", "merge", "centroid", "convex_hull", "split",
])
def test_the_previously_missing_operations_are_reachable(operation):
    assert operation in PROCESSING_OPERATION_CATALOG
    assert PROCESSING_OPERATION_CATALOG[operation], "no algorithm id is mapped"


# A two-layer operation with one layer is not a smaller version of the same
# request; it is a different question nobody asked, and the algorithm would
# answer it with an empty result rather than an error.
@pytest.mark.parametrize("operation", sorted(TWO_LAYER_OPERATIONS))
def test_a_two_layer_operation_refuses_a_single_layer(operation):
    assert safe_processing_params({"operation": operation}) == {}
    accepted = safe_processing_params({"operation": operation, "target_layer": "layer_b"})
    assert accepted.get("target_layer") == "layer_b"


def test_a_single_layer_operation_needs_no_target():
    for operation in ("dissolve", "centroid", "convex_hull", "buffer", "reproject"):
        assert safe_processing_params({"operation": operation})["operation"] == operation


def test_an_unregistered_operation_is_refused():
    assert safe_processing_params({"operation": "drop_database"}) == {}
    assert safe_processing_params({"operation": ""}) == {}


# native:mergevectorlayers takes a list rather than INPUT/OVERLAY, so a
# parameter builder that only knew the common shape would silently merge one
# layer with nothing.
def test_merge_passes_both_layers_as_a_list():
    algorithm = FakeAlgorithm("LAYERS", "OUTPUT")
    payload = build_algorithm_parameters(
        algorithm, "merge", FakeLayer(), {"operation": "merge", "target_layer": "layer_b"})
    assert payload["LAYERS"] == [payload["LAYERS"][0], "layer_b"]
    assert len(payload["LAYERS"]) == 2


def test_split_receives_the_cutting_lines():
    algorithm = FakeAlgorithm("INPUT", "LINES", "OUTPUT")
    payload = build_algorithm_parameters(
        algorithm, "split", FakeLayer(), {"operation": "split", "target_layer": "layer_cuts"})
    assert payload["LINES"] == "layer_cuts"


def test_zonal_statistics_carries_the_field():
    algorithm = FakeAlgorithm("INPUT", "FIELD", "OUTPUT")
    payload = build_algorithm_parameters(
        algorithm, "zonal_statistics", FakeLayer(),
        {"operation": "zonal_statistics", "target_layer": "raster", "field": "elevation"})
    assert payload["FIELD"] == "elevation"


# "Completed buffer with native:buffer." was the whole reply. The algorithm id
# is developer data: the person who asked for a buffer cannot act on it, and it
# is the same string in every language.
def test_the_reply_never_names_the_native_algorithm():
    assert operation_label("buffer") == "buffer"
    assert operation_label("select_by_location") == "selection by location"
    for operation in PROCESSING_OPERATION_CATALOG:
        for line in (
            describe_processing_outcome(operation, "Buffered", 42, "Parcels"),
            describe_processing_outcome(operation, "", None, "Parcels"),
            describe_empty_input(operation, "Parcels"),
        ):
            assert "native:" not in line and "qgis:" not in line and "_" not in line


def test_a_run_that_produced_no_layer_does_not_claim_completion():
    line = describe_processing_outcome("buffer", "", None, "Parcels")
    assert "no layer" in line and "Completed" not in line


def test_an_empty_result_says_which_layer_was_empty():
    line = describe_processing_outcome("buffer", "Buffered", 0, "New polygon layer")
    assert "empty" in line and "New polygon layer" in line


def test_a_real_result_states_the_feature_count():
    assert "42 features" in describe_processing_outcome("buffer", "Buffered", 42, "Parcels")
    assert "1 feature " in describe_processing_outcome("buffer", "Buffered", 1, "Parcels")


def test_an_empty_input_is_refused_before_the_run_instead_of_producing_nothing():
    line = describe_empty_input("buffer", "New polygon layer")
    assert "New polygon layer" in line and "no features" in line and "buffer" in line


def test_the_plugin_does_not_read_an_algorithm_id_back_to_the_user():
    source = PLUGIN.read_text(encoding="utf-8")
    for line in source.splitlines():
        if "_nivo_turns.append" in line or "_set_status(" in line:
            assert "algorithm_id" not in line, line.strip()


# The output always goes somewhere temporary rather than over an existing file.
def test_output_is_always_temporary():
    algorithm = FakeAlgorithm("INPUT", "OUTPUT")
    payload = build_algorithm_parameters(
        algorithm, "dissolve", FakeLayer(), {"operation": "dissolve"})
    assert payload["OUTPUT"] == "TEMPORARY_OUTPUT"
