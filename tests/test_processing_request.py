"""What a Processing request is allowed to carry, and what it must not lose.

Two parameters were being stripped between the assistant and the algorithm, and
neither loss produced an error. The operation ran, reported success, and did
something other than what was asked:

  - a dissolve whose `field` was dropped merges the whole layer into one shape;
  - a reprojection's TARGET_CRS was filled with the LAYER'S OWN crs, so it
    produced a duplicate in the system it started in.

Both are worse than a refusal, because the user is told it worked.
"""

from mapdex_qgis._vendor.nivo.processing import (
    build_algorithm_parameters,
    normalize_crs_reference,
    safe_processing_params,
)


class _Definition:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Algorithm:
    def __init__(self, *names):
        self._names = names

    def parameterDefinitions(self):
        return [_Definition(n) for n in self._names]


class _Layer:
    def crs(self):
        return "EPSG:4326"


def test_a_reprojection_without_a_destination_is_refused():
    # It cannot be answered: there is nothing to reproject TO. The old default
    # was the layer's own system, which is why this needs a test rather than a
    # comment.
    assert safe_processing_params({"operation": "reproject"}) == {}


def test_a_reprojection_carries_the_destination_it_was_given():
    safe = safe_processing_params({"operation": "reproject", "target_crs": "EPSG:5254"})
    assert safe["target_crs"] == "EPSG:5254"

    payload = build_algorithm_parameters(
        _Algorithm("INPUT", "TARGET_CRS", "OUTPUT"), "reproject", _Layer(), safe)
    assert payload["TARGET_CRS"] == "EPSG:5254"
    # The specific failure: the layer's own system reaching the algorithm as
    # the destination.
    assert payload["TARGET_CRS"] != _Layer().crs()


def test_an_operation_that_is_not_a_reprojection_still_defaults_its_crs():
    # TARGET_CRS is incidental output metadata on some algorithms rather than
    # the request, so removing the fallback would break them.
    payload = build_algorithm_parameters(
        _Algorithm("INPUT", "TARGET_CRS"), "buffer", _Layer(),
        safe_processing_params({"operation": "buffer", "distance": 10}))
    assert payload["TARGET_CRS"] == "EPSG:4326"


def test_a_dissolve_keeps_the_field_it_groups_by():
    safe = safe_processing_params({"operation": "dissolve", "field": "district"})
    assert safe["field"] == "district"
    payload = build_algorithm_parameters(
        _Algorithm("INPUT", "FIELD", "OUTPUT"), "dissolve", _Layer(), safe)
    assert payload["FIELD"] == "district"


def test_a_crs_reference_must_look_like_one():
    # Only the form is checked; whether the code exists is QGIS's question.
    # Accepting free text would turn a typo into a silently wrong projection.
    assert normalize_crs_reference("epsg:3857") == "EPSG:3857"
    assert normalize_crs_reference(" ESRI:102100 ") == "ESRI:102100"
    for bad in ("", None, "4326", "EPSG:", ":3857", "EPSG:abc", "the national grid",
                "EPSG:12345678901234"):
        assert normalize_crs_reference(bad) == "", bad


def test_a_malformed_destination_refuses_the_whole_request():
    # Dropping just the bad parameter would run the operation without it, which
    # is the failure this file is about.
    assert safe_processing_params({"operation": "reproject", "target_crs": "national grid"}) == {}
