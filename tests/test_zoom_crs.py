"""Zooming to an imported result must respect the canvas projection.

`QgsMapCanvas.setExtent` takes the canvas CRS. Mapdex hands back EPSG:4326
GeoJSON, so passing `layer.extent()` straight through parked a Web Mercator
canvas near 0°/0° instead of the data.
"""
import ast
import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py"
TREE = ast.parse(PLUGIN.read_text(encoding="utf-8"))


def _function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} not found".format(name))


def _set_extent_arguments():
    for node in ast.walk(TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setExtent"
            and node.args
        ):
            yield node.args[0]


def test_canvas_extent_is_never_taken_straight_from_a_layer():
    for argument in _set_extent_arguments():
        raw_layer_extent = (
            isinstance(argument, ast.Call)
            and isinstance(argument.func, ast.Attribute)
            and argument.func.attr == "extent"
        )
        assert not raw_layer_extent, (
            "setExtent(layer.extent()) mixes CRSs; transform to the canvas CRS first"
        )


def test_zoom_helper_transforms_into_the_canvas_crs():
    body = ast.dump(_function("_zoom_to_layers"))
    assert "destinationCrs" in body, "the canvas CRS must be read"
    assert "QgsCoordinateTransform" in body, "the extent must be reprojected"
    assert "transformBoundingBox" in body
    assert "QgsCsException" in body, "an unprojectable extent must not move the view"


def test_results_import_zooms_once_over_every_layer():
    body = ast.dump(_function("_results_imported"))
    assert "_zoom_to_layers" in body
