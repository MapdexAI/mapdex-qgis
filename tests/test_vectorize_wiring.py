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

import pytest

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


class TestTheDestinationIsTheActiveLayer:
    """One destination, and no questions in front of it.

    An earlier version asked which geometry to trace when no layer could
    answer, and put the result in a layer it invented. Founder verdict: the
    layer creation was silly, use whatever the active layer is. That is also
    how QGIS's own Add Feature tool behaves, so nobody has to be told.
    """

    def test_the_geometry_comes_from_the_active_layer(self):
        source = _source("_resolve_trace_target")
        assert "activeLayer()" in source
        assert "PolygonGeometry" in source and "LineGeometry" in source

    def test_nothing_invents_a_layer(self):
        """The traced shape goes where the user is working or nowhere."""
        traced = _source("_traced_geometry")
        assert "draw.geometry@1" not in traced, (
            "a traced shape still falls back to a layer the user did not "
            "choose")
        assert "_append_traced_feature" in traced

    def test_editing_is_started_rather_than_demanded(self):
        """Picking this tool over a layer you selected is not ambiguous.

        A dialog asking permission to do the obvious thing is a step in front
        of the answer, and QGIS still asks before anything reaches disk.
        """
        source = _source("_resolve_trace_target")
        assert "startEditing()" in source
        assert "_ask_yes_no" not in source

    def test_a_layer_that_cannot_be_edited_says_so(self):
        source = _source("_resolve_trace_target")
        assert "cannot be edited" in source

    def test_no_layer_selected_names_the_remedy(self):
        source = _source("_resolve_trace_target")
        assert "Select the layer" in source

    def test_a_raster_active_layer_is_refused_by_name(self):
        """The scan is what you trace, not what you trace INTO."""
        source = _source("_resolve_trace_target")
        assert "is not a vector layer" in source

    def test_a_point_layer_is_refused_rather_than_traced_as_a_line(self):
        source = _source("_resolve_trace_target")
        assert "holds points" in source
        assert "Draw with Mapdex" in source

    def test_arming_says_which_layer_the_shapes_will_go_into(self):
        source = _source("_resolve_trace_target")
        assert "Tracing into {}" in source

    def test_the_vertices_are_transformed_into_the_layer_crs(self):
        """On screen in the right place and in the file in the wrong one."""
        source = _source("_append_traced_feature")
        assert "QgsCoordinateTransform" in source
        assert "layer.crs()" in source


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


class TestUndoActuallyReachesTheTool:
    """Ctrl+Z cannot arrive through keyPressEvent, and that is the whole point.

    Undo in QGIS is a QAction on the main window with a window-level shortcut
    context. Qt gives that action the key before the focus widget sees it, so a
    map tool handling Ctrl+Z in keyPressEvent silently never runs -- and the
    user gets the LAYER undo in the middle of a shape they have not finished,
    which removes a feature they stored ten minutes ago.
    """

    def test_the_tool_owns_the_shortcut_on_the_canvas(self):
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert "QShortcut" in source
        assert "WidgetWithChildrenShortcut" in source, (
            "a window-context shortcut loses to the QGIS undo action; only a "
            "canvas-owned one with widget context wins while tracing")

    def test_the_shortcut_is_given_back_when_the_tool_is_put_away(self):
        """Otherwise Ctrl+Z stops being QGIS undo for the rest of the session."""
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert "_remove_shortcuts" in source
        deactivate = source[source.index("def deactivate"):]
        deactivate = deactivate[:deactivate.index("def canvasReleaseEvent")]
        assert "_remove_shortcuts" in deactivate

    def test_redo_is_bound_as_well_as_undo(self):
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        assert '"Redo"' in source

    def test_backspace_and_delete_do_the_same_thing_as_ctrl_z(self):
        """QGIS digitizing uses Backspace, so both have to work."""
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        keys = source[source.index("def keyPressEvent"):]
        keys = keys[:keys.index("# -- behaviour")]
        assert "Key_Backspace" in keys and "Key_Delete" in keys
        assert "_undo_stretch" in keys

    def test_undo_with_nothing_traced_does_not_fall_through_to_the_layer(self):
        """The worst possible reading of the key.

        Mid-shape, undo means the stretch just traced. Removing a finished
        feature instead, because this shape has nothing left, would destroy
        work the user was not thinking about.
        """
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        handler = source[source.index("def _undo_stretch"):]
        handler = handler[:handler.index("def _redo_stretch")]
        assert "Nothing left to take back" in handler

    def test_escape_abandons_the_shape_before_it_closes_the_tool(self):
        """One key doing two jobs at once is how people lose work."""
        source = (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")
        keys = source[source.index("def keyPressEvent"):]
        keys = keys[:keys.index("# -- behaviour")]
        assert "if self.session.started:" in keys
        assert "Esc again" in keys


class TestTheTwoButtonsLookDifferent:
    """A toolbar with two identical icons is a guess, not a toolbar.

    The panel and the tracer sit next to each other, and until the tracer had
    its own glyph both were plugin_icon(): the user could tell them apart only
    by hovering, which is exactly the cost a toolbar exists to remove.
    """

    def test_the_tracer_does_not_reuse_the_mapdex_mark(self):
        install = _source("_install_vectorize_action")
        assert "asset_icon(" in install
        assert "plugin_icon()" not in install, (
            "the tracer button carries the same mark as the panel button "
            "beside it")

    def test_the_panel_still_carries_the_mark(self):
        """Still the mark, now the right variant of it.

        This check exists so the panel button stays distinguishable from the
        tracer beside it, and it is `mapdex_mark_icon()` that carries the mark
        now: `plugin_icon()` was the INDIGO symbol, which MEMORY hard rule 27
        reserves for accent placements and which was the same picture on a
        light toolbar and a dark one. See branding.py.
        """
        assert "mapdex_mark_icon()" in _source("initGui")
        assert "plugin_icon()" not in _source("initGui"), (
            "the toolbar is back on the accent-only indigo mark"
        )

    def test_a_missing_asset_falls_back_instead_of_drawing_nothing(self):
        """A QIcon with no file is invisible, and an invisible button is worse
        than the wrong picture on it."""
        source = _source("asset_icon")
        assert "plugin_icon()" in source
        assert "isfile" in source

    def test_the_icon_is_generated_rather_than_a_mystery_binary(self):
        generator = ROOT / "scripts" / "make_tracer_icon.py"
        assert generator.is_file(), (
            "the icon ships as a PNG nobody can regenerate")
        body = generator.read_text(encoding="utf-8")
        assert "GRID = 24" in body, "designed at some size other than the one drawn"

    def test_the_asset_exists_and_is_the_size_a_toolbar_wants(self):
        icon = ROOT / "mapdex_qgis" / "assets" / "icon_vectorize.png"
        assert icon.is_file()
        Image = pytest.importorskip("PIL.Image", reason="PIL not present here")
        with Image.open(icon) as handle:
            assert handle.size == (128, 128)
            assert handle.mode == "RGBA", "a toolbar icon needs transparency"


class TestTheDarkThemeVariant:
    """A near-black icon on the Night Mapping toolbar is the same as no icon."""

    def _assets(self):
        return ROOT / "mapdex_qgis" / "assets"

    def test_both_variants_ship(self):
        for name in ("icon_vectorize.png", "icon_vectorize_dark.png"):
            assert (self._assets() / name).is_file(), name

    def test_the_dark_variant_is_actually_light(self):
        """Measured, because two files with the same name pattern prove nothing.

        A copy of the light icon under the dark name would pass every other
        check here and be invisible on the bar it exists for.
        """
        Image = pytest.importorskip("PIL.Image", reason="PIL not present here")

        def mean_ink(path):
            with Image.open(path) as handle:
                pixels = handle.convert("RGBA").load()
                width, height = handle.size
                values = [
                    sum(pixels[x, y][:3]) / 3.0
                    for y in range(height) for x in range(width)
                    if pixels[x, y][3] >= 170
                ]
            return sum(values) / max(1, len(values))

        light = mean_ink(self._assets() / "icon_vectorize.png")
        dark = mean_ink(self._assets() / "icon_vectorize_dark.png")
        assert dark > light + 80, (
            "the dark variant is not lighter than the light one, so it will "
            "disappear on a dark toolbar (light {:.0f}, dark {:.0f})".format(
                light, dark))

    def test_the_theme_is_read_from_the_palette_not_from_its_name(self):
        """QGIS ships several themes, users install more, and the OS can darken
        the application without any QGIS setting changing at all. A list of
        theme names is wrong for every theme nobody thought of."""
        source = _source("interface_is_dark")
        assert "palette()" in source
        assert "lightness()" in source
        # The docstring NAMES the themes in order to explain why it does not
        # match on them, so the check has to read the code and not the prose.
        code = ast.get_source_segment(PLUGIN, _function("interface_is_dark"))
        tree = ast.parse(code.strip().replace("def interface_is_dark",
                                              "def interface_is_dark", 1))
        function = tree.body[0]
        if (function.body and isinstance(function.body[0], ast.Expr)
                and isinstance(function.body[0].value, ast.Constant)):
            function.body = function.body[1:]
        body = ast.unparse(function)
        for name in ("Night Mapping", "Blend of Gray", "UITheme"):
            assert name not in body, (
                "matching a theme by name: {}".format(name))

    def test_a_missing_dark_file_falls_back_to_the_visible_one(self):
        """Wrong-but-visible beats an empty button."""
        source = _source("themed_asset_icon")
        assert "isfile" in source
        assert "asset_icon(name)" in source

    def test_the_action_asks_for_the_themed_icon(self):
        assert "themed_asset_icon(" in _source("_install_vectorize_action")

    def test_one_generator_produces_both(self):
        body = (ROOT / "scripts" / "make_tracer_icon.py").read_text(encoding="utf-8")
        assert "PALETTES" in body
        assert '"dark"' in body and '"light"' in body


class TestTheWorkflowNotJustTheGesture:
    """Digitizing a sheet is not one shape, and these are the parts that make
    it a session rather than a demo.

    Founder feedback, twice: the lines come out well and the tool is not usable
    enough. The second time it was explicit -- think about how somebody uses
    this, not just about drawing. Everything below came from working through
    one real job: a cadastral sheet, parcel after parcel.
    """

    def _tool(self):
        return (ROOT / "mapdex_qgis" / "vectorize.py").read_text(encoding="utf-8")

    def test_an_already_drawn_boundary_is_reused_rather_than_traced_again(self):
        """The single biggest thing on a real sheet.

        Most interior boundaries belong to two parcels. Re-tracing one from the
        pixels is double the work and produces a second line a few pixels off
        the first, which is exactly where slivers and overlaps come from.
        """
        source = self._tool()
        assert "QgsTracer" in source, (
            "nothing follows the geometry already digitized, so every shared "
            "edge is drawn twice")
        assert "findShortestPath" in source

    def test_the_neighbours_are_asked_before_the_pixels(self):
        """Order matters: an exact shared edge beats one re-derived from ink."""
        source = self._tool()
        click = source[source.index("    def _click("):]
        click = click[:click.index("    def _reseed(")]
        assert click.index("_existing_reuse") < click.index("path_to("), (
            "the raster is searched before the neighbour is asked, so a shared "
            "boundary gets re-traced even when it is already there")

    def test_the_layer_being_edited_is_followed_first(self):
        """On a cadastral sheet the edge worth reusing is the parcel just drawn."""
        source = _source("_layers_to_follow")
        assert "activeLayer()" in source
        assert "isVisible()" in source, (
            "a layer the user switched off is one they decided not to work "
            "against")

    def test_the_project_snapping_is_asked_before_the_ink(self):
        source = self._tool()
        assert "snappingUtils" in source
        assert "snapToMap" in source

    def test_a_snapped_anchor_is_not_moved_again_by_the_ink_search(self):
        """The user put it on a neighbour's vertex; that is the whole point."""
        source = self._tool()
        assert "_anchor_is_snapped" in source
        reseed = source[source.index("    def _reseed("):]
        reseed = reseed[:reseed.index("    def _settle(")]
        assert "if not self._anchor_is_snapped:" in reseed

    def test_a_polygon_closes_on_its_own_first_vertex(self):
        source = self._tool()
        assert "close_target" in source
        assert "CLOSE_TOLERANCE_PX" in source

    def test_closing_needs_more_than_one_stretch(self):
        """Otherwise the click after the anchor closes the shape onto itself."""
        source = self._tool()
        target = source[source.index("    def close_target("):]
        target = target[:target.index("    def _map_units_per_pixel(")]
        assert "len(self.session.segments) < 2" in target

    def test_only_polygons_close(self):
        source = self._tool()
        target = source[source.index("    def close_target("):]
        target = target[:target.index("    def _map_units_per_pixel(")]
        assert 'self.session.geometry != "polygon"' in target
