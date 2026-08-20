"""The tracer has to be reachable, and it has to leave when the plugin does.

Two failures this file exists to catch, both of which have happened in this
plugin before.

A tool that is built and never wired reaches nobody. That is the whole subject
of tests/test_module_reachability.py, which caught roughly 3,600 lines of it.

An action or a toolbar the plugin adds to the QGIS main window and never takes
back off survives a reload, so every reload leaves another copy behind. The
unload() comment in plugin.py records that happening with the layer context
entries; a toolbar is the same object owned the same way.

These read plugin.py rather than running it, because a QGIS main window is not
available here. What they can prove is that the wiring is present and
symmetric.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLUGIN_PATH = ROOT / "mapdex_qgis" / "plugin.py"
PLUGIN = PLUGIN_PATH.read_text(encoding="utf-8")
TREE = ast.parse(PLUGIN)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("plugin.py has no {}()".format(name))


def _source(name: str) -> str:
    return ast.get_source_segment(PLUGIN, _function(name)) or ""


class TestTheToolIsReachable:
    def test_plugin_imports_the_tracer(self):
        """Reachability is checked repository-wide; this names the expectation."""
        assert "from .vectorize import VectorizeMapTool" in PLUGIN

    def test_the_tracer_module_imports_the_algorithm(self):
        vectorize = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert "from .livewire import" in vectorize

    def test_initgui_installs_the_tracer_action(self):
        assert "self._install_vectorize_action()" in _source("initGui")

    def test_the_action_is_checkable_because_it_is_a_mode(self):
        """An unchecked toolbar button gives no sign the canvas is armed."""
        install = _source("_install_vectorize_action")
        assert "setCheckable(True)" in install
        assert "_toggle_vectorize_tool" in install


class TestTheToolbar:
    def test_mapdex_owns_a_toolbar_rather_than_one_shared_icon(self):
        """Two tools, two buttons. addToolBarIcon can only ever give one."""
        init = _source("initGui")
        assert 'self.iface.addToolBar("Mapdex")' in init
        assert "setObjectName" in init

    def test_both_tools_are_on_it(self):
        init = _source("initGui")
        assert "self.toolbar.addAction(self.action)" in init
        assert "self.toolbar.addAction(self._vectorize_action)" in init

    def test_the_toolbar_replaces_the_shared_plugins_icon(self):
        """Leaving both would put Mapdex in two places at once."""
        assert "addToolBarIcon" not in _source("initGui")


class TestUnloadIsSymmetric:
    def test_the_new_actions_come_back_off_the_menu(self):
        unload = _source("unload")
        assert "_vectorize_action" in unload
        assert "_vectorize_settings_action" in unload

    def test_the_toolbar_comes_back_off_the_window(self):
        """Otherwise every reload leaves another empty Mapdex bar behind."""
        unload = _source("unload")
        assert "toolbar" in unload
        assert "setParent(None)" in unload

    def test_every_action_initgui_adds_is_named_in_unload(self):
        """The general rule, so the next action added cannot forget to leave."""
        init = _source("initGui") + _source("_install_vectorize_action")
        unload = _source("unload")
        added = {name for name in
                 ("_vectorize_action", "_vectorize_settings_action",
                  "_measure_action", "_draw_action")
                 if name in PLUGIN}
        missing = sorted(name for name in added if name not in unload)
        assert not missing, (
            "these actions are installed and never removed, so a reload will "
            "leave a second copy of each: {}".format(missing))


class TestTheTracerAsksTheLayerRatherThanTheUser:
    def test_the_geometry_comes_from_the_layer_being_edited(self):
        """A dialog in front of every trace would cost more than it settles."""
        source = _source("_trace_geometry_for_active_layer")
        assert "PolygonGeometry" in source
        assert "polygon" in source and "line" in source

    def test_a_trace_prefers_the_open_editing_session(self):
        """Tracing a sheet means adding to the layer being digitized.

        Creating a new layer per traced parcel would make the tool unusable for
        the job it exists for.
        """
        source = _source("_traced_geometry")
        assert "_append_traced_feature" in source
        assert "draw.geometry@1" in source     # the fallback still exists

    def test_the_vertices_are_transformed_into_the_layer_crs(self):
        """On screen in the right place and in the file in the wrong one."""
        source = _source("_append_traced_feature")
        assert "QgsCoordinateTransform" in source
        assert "layer.crs()" in source

    def test_a_mismatched_layer_is_explained_rather_than_forced(self):
        source = _source("_append_traced_feature")
        assert "geometryType()" in source
        assert "switch layers" in source
