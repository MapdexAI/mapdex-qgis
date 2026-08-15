"""The new-layer geometry choice must be a picker, not a chat question.

Nivo asked "point, line or polygon?" in the transcript. The user's answer starts
a FRESH compose turn, where "polygon" is a bare word carrying no intent, so it
fell through to the generic capability offer and the layer was never created.
A question the assistant cannot receive an answer to is worse than no question.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import GEOMETRY_CHOICES, geometry_from_choice  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")


def test_geometry_choices_map_to_ogc_types():
    assert [geometry for _label, geometry in GEOMETRY_CHOICES] == ["point", "linestring", "polygon"]
    assert geometry_from_choice("Polygon") == "polygon"
    assert geometry_from_choice("line") == "linestring"
    assert geometry_from_choice("POINT") == "point"


def test_a_cancelled_or_unrecognised_choice_creates_nothing():
    # A cancelled dialog returns "": the caller must not fall back to a default
    # and silently create the wrong kind of layer.
    for value in ("", None, "circle", "poligon", "'; DROP TABLE x --"):
        assert geometry_from_choice(value) == ""


def test_the_choice_is_offered_as_a_picker_not_as_a_chat_message():
    assert "What kind of layer should I create" not in PLUGIN
    assert "QInputDialog.getItem" in PLUGIN


def test_cancelling_the_picker_does_not_create_a_layer():
    body = PLUGIN[PLUGIN.index("def _create_scratch_layer"):PLUGIN.index("def _ask_geometry_type")]
    # The guard must return before QgsVectorLayer is constructed.
    guard = body.index("if not geometry:")
    construction = body.index("QgsVectorLayer(")
    assert guard < construction


def test_the_picker_only_offers_the_closed_set():
    body = PLUGIN[PLUGIN.index("def _ask_geometry_type"):PLUGIN.index("def _unique_layer_name")]
    # editable=False: the user cannot type a geometry the plugin cannot build.
    assert "False,  # not editable" in body
    assert "GEOMETRY_CHOICES" in body
