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


class _PredicateDefinition:
    """A PREDICATE parameter that reports its own option list, as QGIS does."""

    def __init__(self, options):
        self._options = options

    def options(self):
        return self._options


class _PredicateAlgorithm(_Algorithm):
    def __init__(self, options, *names):
        super().__init__(*names)
        self._options = options

    def parameterDefinition(self, name):
        if name == "PREDICATE":
            return _PredicateDefinition(self._options)
        return None


# The two algorithms order their options differently, which is why an index
# table cannot work: index 2 is `equals` in one and `disjoint` in the other.
JOIN_OPTIONS = ["intersects", "contains", "equals", "touches", "overlaps", "within", "crosses"]
SELECT_OPTIONS = ["intersect", "contain", "disjoint", "equal", "touch", "overlap",
                  "are within", "cross"]


def test_the_predicate_is_the_one_that_was_asked_for():
    # It used to be the constant [0], so every spatial relationship question
    # ran as `intersects` whatever was asked: accepted, validated against an
    # enum, and then thrown away.
    payload = build_algorithm_parameters(
        _PredicateAlgorithm(JOIN_OPTIONS, "INPUT", "PREDICATE", "JOIN", "OUTPUT"),
        "spatial_join", _Layer(),
        safe_processing_params({"operation": "spatial_join", "target_layer": "roads",
                                "predicate": "within"}))
    assert payload["PREDICATE"] == [5]


def test_the_same_predicate_lands_on_a_different_index_per_algorithm():
    # The reason the live option list is read rather than a table: `within` is
    # 5 in one algorithm's ordering and 6 in the other's.
    join = build_algorithm_parameters(
        _PredicateAlgorithm(JOIN_OPTIONS, "INPUT", "PREDICATE"), "spatial_join", _Layer(),
        safe_processing_params({"operation": "spatial_join", "target_layer": "x",
                                "predicate": "within"}))
    select = build_algorithm_parameters(
        _PredicateAlgorithm(SELECT_OPTIONS, "INPUT", "PREDICATE"), "select_by_location", _Layer(),
        safe_processing_params({"operation": "select_by_location", "target_layer": "x",
                                "predicate": "within"}))
    assert join["PREDICATE"] == [5]
    assert select["PREDICATE"] == [6]
    assert join["PREDICATE"] != select["PREDICATE"]


def test_a_relationship_the_algorithm_does_not_offer_is_refused():
    # Falling back to the first option would answer a different question
    # confidently, which is the failure this replaces.
    payload = build_algorithm_parameters(
        _PredicateAlgorithm(["intersects", "contains"], "INPUT", "PREDICATE"),
        "spatial_join", _Layer(),
        safe_processing_params({"operation": "spatial_join", "target_layer": "x",
                                "predicate": "crosses"}))
    assert payload == {}


def test_no_predicate_asked_takes_the_algorithm_s_own_first_option():
    payload = build_algorithm_parameters(
        _PredicateAlgorithm(JOIN_OPTIONS, "INPUT", "PREDICATE"), "spatial_join", _Layer(),
        safe_processing_params({"operation": "spatial_join", "target_layer": "x"}))
    assert payload["PREDICATE"] == [0]


def test_simplification_carries_its_tolerance():
    safe = safe_processing_params({"operation": "simplify", "tolerance": 2.5})
    payload = build_algorithm_parameters(
        _Algorithm("INPUT", "TOLERANCE", "OUTPUT"), "simplify", _Layer(), safe)
    assert payload["TOLERANCE"] == 2.5


def test_a_spatial_join_names_the_second_layer_JOIN():
    # joinattributesbylocation calls it JOIN rather than OVERLAY or INTERSECT.
    payload = build_algorithm_parameters(
        _Algorithm("INPUT", "JOIN", "OUTPUT"), "spatial_join", _Layer(),
        safe_processing_params({"operation": "spatial_join", "target_layer": "roads"}))
    assert payload["JOIN"] == "roads"
