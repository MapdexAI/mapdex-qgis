"""Nivo context should not be empty when QGIS has visible map layers.

QGIS 4 can leave iface.activeLayer()/layerTreeView().currentLayer() unset while
the canvas still has valid visible layers. The companion context is bounded and
safe, so falling back to a visible project layer is preferable to telling Nivo
there is no active QGIS layer at all.
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


def test_active_layer_resolver_falls_back_to_visible_project_layers():
    body = ast.dump(_function("_active_qgis_layer"))
    assert "layerTreeRoot" in body
    assert "findLayers" in body
    assert "isVisible" in body
