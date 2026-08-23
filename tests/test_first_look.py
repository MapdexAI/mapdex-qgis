"""Nivo introduces itself by reading the open project, not by describing itself.

The plugin shipped with no state that explained anything: two URL fields, a
Connect button, and a chat box whose placeholder was the only hint that a
hundred capabilities existed. A stranger typed "hello" and closed it.

A brochure listing the four workflows by their internal names would not have
fixed that, because nobody buys a pipeline from a dropdown. What a GIS person
recognises is a correct statement about the file already on their screen. These
tests hold the three properties that separate that from marketing copy:

* a finding is MEASURED, never inferred from a name or an extension;
* the free action is offered before the paid one, so the demonstration happens
  before the ask;
* an action that needs an account says so and says what it would do.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mapdex_qgis import first_look  # noqa: E402
from mapdex_qgis.first_look import ASK, BLOCKING, LOCAL, MAPDEX, opening_reading  # noqa: E402


def _finding(reading, identifier):
    for item in reading["findings"]:
        if item["id"] == identifier:
            return item
    raise AssertionError(
        "no {!r} finding in {}".format(identifier, [f["id"] for f in reading["findings"]])
    )


def _kinds(finding):
    return [action["kind"] for action in finding["actions"]]


UNPLACED = {"layer": {"name": "cadastre_1943.tif", "kind": "raster",
                      "width": 2759, "height": 2590, "georeferenced": False}, "layer_count": 1}
PLACED = {"layer": {"name": "sheet.tif", "kind": "raster",
                    "width": 4000, "height": 3000, "georeferenced": True}, "layer_count": 1}
PARCELS = {"layer": {"name": "parcels.shp", "kind": "vector",
                     "crs": "EPSG:5254", "feature_count": 1204}, "layer_count": 1}


# -- measured, not guessed ---------------------------------------------------

def test_the_georeference_finding_turns_on_the_measurement_alone():
    """`profile_layer` computes `georeferenced` from a valid CRS and a real extent.

    The same file name, the same extension, the same size: only the measured
    flag decides. A reading that fired on ".tif" would accuse every GeoTIFF in
    the world of being unplaced.
    """
    assert _finding(opening_reading(UNPLACED), "raster_not_georeferenced")["severity"] == BLOCKING
    assert [f["id"] for f in opening_reading(PLACED)["findings"]] == ["raster_placed"]


def test_a_vector_layer_never_produces_a_raster_finding():
    named_like_a_scan = {"layer": {"name": "scan_1943.tif", "kind": "vector",
                                   "crs": "EPSG:4326", "feature_count": 3}, "layer_count": 1}
    ids = [f["id"] for f in opening_reading(named_like_a_scan)["findings"]]
    assert "raster_not_georeferenced" not in ids, ids


def test_a_missing_crs_is_reported_because_qgis_will_not_report_it():
    reading = opening_reading({"layer": {"name": "points.shp", "kind": "vector",
                                         "crs": "", "feature_count": 88}, "layer_count": 1})
    assert _finding(reading, "vector_no_crs")["severity"] == BLOCKING


def test_a_crs_mismatch_is_only_claimed_when_layers_were_named():
    """The warning quotes the layers it found. An empty list is not a finding."""
    assert "crs_mismatch" not in [f["id"] for f in opening_reading(PARCELS)["findings"]]
    mixed = dict(PARCELS, crs_mismatch=["roads.shp", "buildings.shp"])
    finding = _finding(opening_reading(mixed), "crs_mismatch")
    assert "roads.shp" in finding["detail"] and "buildings.shp" in finding["detail"]


# -- the demonstration comes before the ask ---------------------------------

def test_free_work_is_offered_before_paid_work_in_the_same_finding():
    """The order IS the argument: the reader sees what it does before being asked.

    A finding that leads with the account is the brochure this module replaces.
    """
    finding = _finding(opening_reading(PARCELS), "vector_ready")
    kinds = _kinds(finding)
    assert LOCAL in kinds and MAPDEX in kinds, kinds
    assert kinds.index(LOCAL) < kinds.index(MAPDEX), (
        "the account was asked for before anything was demonstrated: {}".format(kinds)
    )


def test_the_free_actions_are_ones_that_run_here():
    """A "free" action bound to no local capability is a button that does nothing."""
    for state in (PARCELS, PLACED, {"layer_count": 0}):
        for finding in opening_reading(state)["findings"]:
            for action in finding["actions"]:
                if action["kind"] == LOCAL:
                    assert action.get("capability"), action


def test_the_unplaced_raster_has_no_free_action_rather_than_a_decorative_one():
    """Nothing local can place a scan, and offering a button that cannot is worse
    than offering none. The finding itself is the free value here."""
    finding = _finding(opening_reading(UNPLACED), "raster_not_georeferenced")
    assert LOCAL not in _kinds(finding), _kinds(finding)
    assert ASK in _kinds(finding), "the reader was left with nothing to ask, either"


# -- the paid half is named, never disguised --------------------------------

def test_every_account_action_says_what_it_would_do():
    """A workflow name with no promise behind it is a dropdown entry, not an offer.

    "Digitize parcels" means nothing to somebody who has not bought it yet.
    """
    for state in (UNPLACED, PLACED, PARCELS):
        for action in first_look.account_actions(opening_reading(state)):
            assert action.get("promise"), action
            assert action.get("workflow"), action
            assert "Mapdex" in action["label"], (
                "an account action did not name the account it needs: {}".format(action)
            )


def test_account_actions_are_derived_from_findings_only():
    """No standing menu of workflows: an offer exists because a measurement earned it."""
    empty = opening_reading({"layer_count": 0})
    assert first_look.account_actions(empty) == [], (
        "an empty project was offered paid work with nothing measured to justify it"
    )


def test_a_placed_scan_is_offered_digitizing_and_an_unplaced_one_is_not():
    """Extraction has a documented prerequisite. Offering it first would sell a
    step the product itself refuses to run."""
    assert [a["workflow"] for a in first_look.account_actions(opening_reading(PLACED))] \
        == ["digitize_parcels"]
    assert [a["workflow"] for a in first_look.account_actions(opening_reading(UNPLACED))] \
        == ["georeference"]


# -- presentation ------------------------------------------------------------

def test_the_worst_finding_is_read_first():
    mixed = {"layer": {"name": "points.shp", "kind": "vector", "crs": "", "feature_count": 8},
             "crs_mismatch": ["roads.shp"], "layer_count": 4}
    order = [f["severity"] for f in opening_reading(mixed)["findings"]]
    assert order == sorted(order, key=lambda s: {"blocking": 0, "warning": 1, "info": 2}[s]), order
    assert order[0] == BLOCKING


def test_an_empty_project_is_a_state_with_advice_rather_than_silence():
    reading = opening_reading({"layer_count": 0})
    assert reading["measured"] is False
    assert reading["findings"], "the first thing a new user sees was nothing"
    assert LOCAL in _kinds(reading["findings"][0])


def test_a_project_with_layers_but_none_selected_is_not_called_empty():
    reading = opening_reading({"layer_count": 5})
    assert [f["id"] for f in reading["findings"]] == ["no_active_layer"]


def test_nothing_here_needs_a_model_or_a_network():
    """The whole point is that this runs before any provider is configured and
    before any account exists. An import of either would defeat it."""
    source = (pathlib.Path(first_look.__file__)).read_text(encoding="utf-8")
    for forbidden in ("import urllib", "api_client", "AgentSession", "from qgis", "PyQt"):
        assert forbidden not in source, forbidden
