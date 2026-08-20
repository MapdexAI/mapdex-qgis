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


class TestTheToolStaysUsable:
    """The four things that decide whether a person can work with it for an hour.

    Every one of these was wrong in the first version, and all four came from
    modelling the tracer on the draw tool, which makes one layer and is
    finished. A digitizer is a hundred shapes in a row.
    """

    def test_finishing_a_shape_does_not_put_the_tracer_away(self):
        """The bug that made it unusable: re-arming between every parcel."""
        source = _source("_traced_geometry")
        assert "_restore_map_tool" not in source, (
            "finishing a traced shape disarms the tool, so the user has to "
            "click the toolbar again for every single feature")

    def test_cancelling_does_put_it_away(self):
        """Escape means stop, and it has to actually stop."""
        assert "_restore_map_tool" in _source("_trace_cancelled")

    def test_putting_a_mapdex_tool_away_also_unchecks_the_tracer(self):
        """Otherwise the button stays lit over a canvas it no longer receives."""
        source = _source("_restore_map_tool")
        assert "_vectorize_tool" in source
        assert "_vectorize_action" in source

    def test_the_traced_feature_lands_in_the_layer_undo_stack(self):
        """Ctrl+Z has to remove the shape the user just traced."""
        source = _source("_append_traced_feature")
        assert "beginEditCommand" in source
        assert "endEditCommand" in source
        assert "destroyEditCommand" in source, (
            "a failed add must not leave an open edit command behind")

    def test_the_attribute_form_is_offered_and_can_be_suppressed(self):
        """Cadastre is digitized with the parcel number typed as you go.

        And a user who turned the form off did it to trace faster, which is
        this tool whole point, so the setting is respected rather than
        overridden.
        """
        source = _source("_confirm_traced_attributes")
        assert "openFeatureForm" in source
        assert "suppress" in source

    def test_the_layer_defaults_are_applied(self):
        assert "QgsVectorLayerUtils" in _source("_traced_feature")

    def test_a_traced_ring_is_closed(self):
        source = _source("_append_traced_feature")
        assert "vertices[0] != vertices[-1]" in source


class TestTheWindowFollowsTheCanvas:
    def test_the_preview_re_reads_after_the_view_moves(self):
        """A window read at the last anchor maps the wrong pixels after a zoom.

        Without this the preview follows a line that is no longer under the
        cursor, which looks like the tracer being wrong rather than the view
        having moved.
        """
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert "_canvas_has_moved" in source
        move_event = source[source.index("def canvasMoveEvent"):]
        move_event = move_event[:move_event.index("def keyPressEvent")]
        assert "_canvas_has_moved" in move_event
        assert "_reseed" in move_event

    def test_the_extent_the_window_came_from_is_remembered(self):
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert "_window_extent" in source
