"""The canvas tool's decisions, without a canvas.

What has behaviour here and can be checked: where a map coordinate lands in the
sampled window, what the anchor and undo state machine does, what the user is
told, and whether a shape is allowed to finish. Those are the mistakes that do
not need a canvas to happen.

What cannot be checked here, and is not pretended: whether a click lands where
the user pointed, whether the rubber band repaints, whether QGIS returns the
pixels on screen. Those need a running QGIS and belong in
testdata/verify_qgis_end_to_end.py.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy", reason="the tracer needs numpy, which QGIS ships")

from mapdex_qgis import vectorize  # noqa: E402
from mapdex_qgis.livewire import Seed, TraceOptions  # noqa: E402
from mapdex_qgis.livewire import TraceResult  # noqa: E402


def window(width=10, height=8, min_x=100.0, max_y=200.0, pixel=2.0):
    return vectorize.RasterWindow(
        values=np.zeros((height, width), dtype=np.float32),
        min_x=min_x, max_y=max_y, pixel_w=pixel, pixel_h=pixel,
        width=width, height=height,
    )


class TestRasterWindow:
    def test_a_map_coordinate_lands_in_the_pixel_that_covers_it(self):
        win = window()
        assert win.to_pixel(100.5, 199.5) == (0, 0)
        assert win.to_pixel(102.5, 199.5) == (0, 1)
        assert win.to_pixel(100.5, 197.5) == (1, 0)

    def test_a_pixel_maps_back_to_its_own_centre(self):
        win = window()
        x, y = win.to_map(0, 0)
        assert (x, y) == (101.0, 199.0)
        assert win.to_pixel(x, y) == (0, 0)

    def test_the_round_trip_holds_across_the_whole_window(self):
        """A half-pixel drift here puts every traced vertex off the line."""
        win = window()
        for row in range(win.height):
            for col in range(win.width):
                assert win.to_pixel(*win.to_map(row, col)) == (row, col)

    def test_outside_is_an_answer_rather_than_a_clamp(self):
        """Clamping would silently trace from the window edge.

        The cursor leaving the sampled window is ordinary (the user pans), and
        the tool has to know it happened so it can re-sample instead of
        anchoring on a pixel the user never pointed at.
        """
        win = window()
        assert not win.contains_pixel(*win.to_pixel(1.0, 199.0))
        assert not win.contains_pixel(*win.to_pixel(101.0, 1.0))
        assert win.contains_pixel(*win.to_pixel(101.0, 199.0))

    def test_a_traced_path_comes_back_as_map_coordinates(self):
        win = window()
        assert win.path_to_map([(0, 0), (1, 1)]) == [(101.0, 199.0), (103.0, 197.0)]


class TestTraceSession:
    def test_a_session_starts_empty_and_anchored_nowhere(self):
        session = vectorize.TraceSession()
        assert not session.started
        assert session.vertices() == []

    def test_committing_moves_the_anchor_to_the_end_of_the_stretch(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)], traced=True)
        assert session.anchor_map == (2.0, 2.0)

    def test_the_joins_between_stretches_are_not_repeated_vertices(self):
        """Two identical points in a ring are a zero-length edge, not a corner."""
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        session.commit([(1.0, 0.0), (2.0, 0.0)], traced=True)
        assert session.vertices() == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]

    def test_a_stretch_of_one_point_is_not_a_stretch(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        assert session.commit([(0.0, 0.0)], traced=True) == 0

    def test_undo_removes_a_whole_stretch_and_moves_the_anchor_back(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        session.commit([(1.0, 0.0), (2.0, 0.0)], traced=True)
        assert session.undo() is True
        assert session.anchor_map == (1.0, 0.0)
        assert session.vertices() == [(0.0, 0.0), (1.0, 0.0)]

    def test_undo_with_nothing_to_undo_says_so(self):
        """Returning success would read as a broken key."""
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        assert session.undo() is False

    def test_a_line_needs_two_points_and_a_polygon_three(self):
        line = vectorize.TraceSession(geometry="line")
        line.start(0.0, 0.0)
        line.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        assert line.refusal() == ""

        polygon = vectorize.TraceSession(geometry="polygon")
        polygon.start(0.0, 0.0)
        polygon.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        assert "at least 3" in polygon.refusal()

    def test_an_unfinishable_shape_keeps_the_work(self):
        """Discarding the trace in order to report it would cost the shape too."""
        session = vectorize.TraceSession(geometry="polygon")
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        outcome = session.finish()
        assert outcome["kind"] == "incomplete"
        assert session.vertices() == [(0.0, 0.0), (1.0, 0.0)]

    def test_finishing_hands_over_the_shape_and_clears_the_session(self):
        """A finished shape must not bleed into the next one."""
        session = vectorize.TraceSession(geometry="line")
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        outcome = session.finish()
        assert outcome["kind"] == "finish"
        assert outcome["points"] == [(0.0, 0.0), (1.0, 0.0)]
        assert outcome["traced_segments"] == 1
        assert session.vertices() == []
        assert not session.started

    def test_cancelling_reports_what_was_thrown_away(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        assert session.cancel() == {"kind": "cancelled", "discarded": 1}
        assert not session.started

    def test_a_geometry_the_tool_does_not_trace_is_refused_by_name(self):
        session = vectorize.TraceSession(geometry="point")
        session.start(0.0, 0.0)
        assert "not a shape this tool traces" in session.refusal()

    def test_starting_again_abandons_the_previous_shape(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        session.start(9.0, 9.0)
        assert session.vertices() == []
        assert session.anchor_map == (9.0, 9.0)

    def test_a_straight_override_is_recorded_as_not_traced(self):
        """The provenance of every stretch survives to the finished shape."""
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        session.commit([(1.0, 0.0), (5.0, 0.0)], traced=False)
        assert session.finish()["traced_segments"] == 1


class TestWhatTheUserIsTold:
    def test_a_click_that_found_no_line_explains_the_remedy(self):
        message = vectorize.status_for_seed(None)
        assert "No drawn line" in message
        assert "snap" in message

    def test_a_click_that_snapped_says_how_far_it_moved(self):
        seed = Seed(y=10, x=10, clicked_y=10, clicked_x=6, reachable=99, bridge_px=1)
        assert "4 px" in vectorize.status_for_seed(seed)

    def test_a_click_that_landed_exactly_is_not_reported_as_moved(self):
        """Telling a user their accurate click was accurate is noise."""
        seed = Seed(y=10, x=10, clicked_y=10, clicked_x=10, reachable=99, bridge_px=1)
        assert "px from the click" not in vectorize.status_for_seed(seed)

    def test_a_failed_trace_names_the_manual_override(self):
        """The moment the tracer is wrong is the moment to say how to override it."""
        failed = TraceResult(reason="the drawn line does not connect these two points")
        message = vectorize.status_for_trace(failed, "line")
        assert "does not connect" in message
        assert "Shift" in message

    def test_a_successful_trace_reports_what_it_followed(self):
        result = TraceResult(points=[(0, 0), (0, 10)])
        assert "10 px of line" in vectorize.status_for_trace(result, "line")

    def test_a_polygon_is_told_it_closes_rather_than_ends(self):
        result = TraceResult(points=[(0, 0), (0, 10)])
        assert "close" in vectorize.status_for_trace(result, "polygon")


class TestBlockReading:
    class _Block:
        """A stand-in for QgsRasterBlock, buffer route and slow route."""

        def __init__(self, values, dtype_code, buffer_ok=True):
            self.values = values
            self._dtype_code = dtype_code
            self._buffer_ok = buffer_ok

        def dataType(self):  # noqa: N802 - Qt naming
            return self._dtype_code

        def data(self):
            if not self._buffer_ok:
                raise RuntimeError("no buffer here")
            return self.values.tobytes()

        def value(self, row, col):
            return float(self.values[row, col])

    def test_the_buffer_route_returns_the_pixels(self):
        values = np.arange(12, dtype=np.uint8).reshape(3, 4)
        block = self._Block(values, 1)
        read = vectorize.block_to_array(block, 3, 4)
        assert read.shape == (3, 4)
        assert read[2, 3] == pytest.approx(11.0)

    def test_an_unreadable_buffer_falls_back_rather_than_failing(self):
        """A slow window beats no window."""
        values = np.arange(12, dtype=np.uint8).reshape(3, 4)
        block = self._Block(values, 1, buffer_ok=False)
        read = vectorize.block_to_array(block, 3, 4)
        assert read is not None
        assert read[1, 1] == pytest.approx(5.0)

    def test_an_unknown_data_type_still_reads(self):
        values = np.arange(12, dtype=np.uint8).reshape(3, 4)
        block = self._Block(values, 999)
        assert vectorize.block_to_array(block, 3, 4) is not None

    def test_a_truncated_buffer_is_not_reshaped_into_nonsense(self):
        """Reshaping a short buffer raises; guessing at the missing rows lies."""
        class Short(self._Block):
            def data(self):
                return b"\\x01\\x02"

        block = Short(np.arange(12, dtype=np.uint8).reshape(3, 4), 1)
        read = vectorize.block_to_array(block, 3, 4)
        assert read is not None          # fell back to the per-pixel route
        assert read[0, 0] == pytest.approx(0.0)


class TestOptionsAreRespected:
    def test_the_tool_clamps_whatever_settings_hand_it(self):
        wild = TraceOptions(simplify_px=-3, snap_px=999)
        safe = wild.clamped()
        assert safe.simplify_px >= 0
        assert safe.snap_px <= 64


class TestColourSheets:
    """A modern cadastre draws its boundaries in colour, and luma cannot see them.

    Measured on real IGN sheets before this existed: tracing on brightness alone
    left the parcel network in fragments, with the graph reaching 0.1% of the
    ink on paris_marais and 2.5% on lyon_presquile where a bitonal sheet reached
    79.5%. With chroma folded in, lyon reaches 9,038 pixels and 99.2% of the
    traced path lands on ink.
    """

    def test_orange_boundary_ink_reads_darker_than_the_paper_it_is_on(self):
        """The case that motivates the whole function.

        IGN parcel ink is (255, 128, 0) at luminance 151; the paper it sits on
        is brighter, so on luminance the ink is not the darkest thing present
        and the tracer follows something else.
        """
        orange = vectorize.colour_ink_response(
            np.array([[255.0]]), np.array([[128.0]]), np.array([[0.0]]))
        paper = vectorize.colour_ink_response(
            np.array([[245.0]]), np.array([[245.0]]), np.array([[245.0]]))
        assert float(orange[0, 0]) < float(paper[0, 0])
        assert float(orange[0, 0]) == pytest.approx(0.0)

    def test_a_pale_building_fill_stays_lighter_than_boundary_ink(self):
        """It has to separate the two, not merely darken everything coloured."""
        fill = vectorize.colour_ink_response(
            np.array([[230.0]]), np.array([[203.0]]), np.array([[199.0]]))
        orange = vectorize.colour_ink_response(
            np.array([[255.0]]), np.array([[128.0]]), np.array([[0.0]]))
        assert float(fill[0, 0]) > float(orange[0, 0])

    def test_a_neutral_scan_is_left_exactly_as_luma(self):
        """A bitonal sheet must behave as it did before colour was considered."""
        grey = np.array([[40.0, 236.0]])
        response = vectorize.colour_ink_response(grey, grey, grey)
        assert response[0, 0] == pytest.approx(40.0)
        assert response[0, 1] == pytest.approx(236.0)

    def test_the_response_never_leaves_the_tone_range(self):
        response = vectorize.colour_ink_response(
            np.array([[255.0, 0.0]]), np.array([[0.0, 0.0]]), np.array([[0.0, 255.0]]))
        assert float(response.min()) >= 0.0
        assert float(response.max()) <= 255.0


class TestUndoAndRedo:
    """Undo has to be safe to press, or people stop pressing it.

    The founder feedback that produced this was one line: the tool works and
    the undo UX is painful. Undo without redo makes the key feel dangerous, so
    a user clicks around a mistake instead of taking it back, and the shape
    they end up with is worse than the one they were trying to fix.
    """

    def _two_stretch_session(self):
        session = vectorize.TraceSession()
        session.start(0.0, 0.0)
        session.commit([(0.0, 0.0), (1.0, 0.0)], traced=True)
        session.commit([(1.0, 0.0), (2.0, 0.0)], traced=True)
        return session

    def test_a_stretch_taken_back_can_be_put_back(self):
        session = self._two_stretch_session()
        assert session.undo() is True
        assert session.can_redo is True
        assert session.redo() is True
        assert session.vertices() == [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        assert session.anchor_map == (2.0, 0.0)

    def test_redo_with_nothing_undone_says_so(self):
        assert self._two_stretch_session().redo() is False

    def test_tracing_after_an_undo_clears_the_redo_stack(self):
        """As in every editor: new work makes the old redo meaningless."""
        session = self._two_stretch_session()
        session.undo()
        assert session.can_redo is True
        session.commit([(1.0, 0.0), (9.0, 9.0)], traced=True)
        assert session.can_redo is False

    def test_undo_all_the_way_back_leaves_the_shape_empty_not_broken(self):
        session = self._two_stretch_session()
        assert session.undo() and session.undo()
        assert session.can_undo is False
        assert session.vertices() == []
        assert session.started            # the anchor survives; the shape does not

    def test_finishing_clears_what_could_be_redone(self):
        """A stored shape must not be reachable through the next one's redo."""
        session = self._two_stretch_session()
        session.undo()
        session.finish()
        assert session.can_redo is False

    def test_cancelling_clears_it_too(self):
        session = self._two_stretch_session()
        session.undo()
        session.cancel()
        assert session.can_redo is False

    def test_starting_a_shape_clears_the_previous_redo(self):
        session = self._two_stretch_session()
        session.undo()
        session.start(5.0, 5.0)
        assert session.can_redo is False


class TestWhereAShapeStarted:
    """A polygon has to come back to its own first vertex, exactly.

    Landing near enough leaves a gap at the corner where three holdings meet,
    which is the same defect as a re-traced shared edge and lands in the same
    topology report.
    """

    def _polygon(self):
        session = vectorize.TraceSession(geometry="polygon")
        session.start(0.0, 0.0)
        return session

    def test_the_first_vertex_is_where_the_first_stretch_began(self):
        session = self._polygon()
        session.commit([(0.0, 0.0), (10.0, 0.0)], traced=True)
        session.commit([(10.0, 0.0), (10.0, 10.0)], traced=True)
        assert session.first_vertex == (0.0, 0.0)

    def test_before_any_stretch_it_is_the_anchor(self):
        assert self._polygon().first_vertex == (0.0, 0.0)

    def test_undoing_back_to_nothing_leaves_the_anchor_as_the_start(self):
        session = self._polygon()
        session.commit([(0.0, 0.0), (10.0, 0.0)], traced=True)
        session.undo()
        assert session.first_vertex == (0.0, 0.0)

    def test_a_new_shape_starts_somewhere_new(self):
        session = self._polygon()
        session.commit([(0.0, 0.0), (10.0, 0.0)], traced=True)
        session.start(5.0, 5.0)
        assert session.first_vertex == (5.0, 5.0)
