"""The canvas half of assisted tracing: read the raster, follow the line, keep the shape.

`livewire.py` finds a path through pixels. This turns a QGIS canvas into those
pixels, turns the path back into map coordinates, and holds the state between
one click and the next.

Three things are deliberate and are the difference from a hosted tracer.

**The raster never leaves the machine.** The window under the cursor is read
from the layer already open in QGIS and searched here. A competitor that sends
the crop around your cursor to its own servers cannot be used on an archive
that is not allowed to leave the building, and it pays a network round trip for
every mouse move. This pays none.

**One search per anchor, not one per cursor position.** That is live-wire's
whole economy and the reason the preview can follow the mouse at all. See the
measurements in `livewire.py`.

**A refusal is a result.** If the drawn line does not connect the two points,
the tool says so and offers the straight segment as an explicit choice. Filling
the gap silently would put geometry in the user's layer that they never drew
and cannot see is different from the rest.

The pure classes below carry every decision that has behaviour without a
canvas, so they can be tested. `VectorizeMapTool` is the Qt shell and is
exercised by testdata/verify_qgis_end_to_end.py against a running QGIS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .guard import log_debug
from .livewire import LiveWire, TraceOptions, path_length_px, simplify
from .qt_compat import enum_member, geometry_type

try:  # pragma: no cover - import shape differs between QGIS builds
    from qgis.PyQt.QtCore import QSettings
except Exception:  # pragma: no cover - importable outside QGIS for tests
    QSettings = None

try:  # pragma: no cover - import shape differs between QGIS builds
    from qgis.gui import QgsMapTool
except Exception:  # pragma: no cover - importable outside QGIS for tests
    QgsMapTool = object


# The largest window the tool will read and search at once. Beyond this the
# search stops feeling instant (see the timings in livewire.py), and a user who
# needs to trace across more sheet than this should zoom, which also gives the
# tracer more pixels per drawn line to work with.
MAX_WINDOW_PX = 900

# How near the first vertex a click has to be, in screen pixels, before it is
# read as closing the ring rather than as another corner. Screen pixels rather
# than map units because it is a question about the user's aim, and their aim
# is in the space they can see.
CLOSE_TOLERANCE_PX = 12.0

# Geometry kinds the tool can finish into, and how many distinct vertices each
# needs to exist. Mirrors maptools.MINIMUM_VERTICES rather than importing it:
# the tracer produces many vertices and the question it asks is different, but
# the floor is the same fact about geometry.
MINIMUM_VERTICES = {"line": 2, "polygon": 3}


@dataclass(frozen=True)
class RasterWindow:
    """A block of raster pixels, and where each one is on the map.

    Held as a value rather than re-read per event: the window is what the
    search ran against, so a path is only meaningful in the window that
    produced it. Panning the canvas invalidates it, which the tool handles by
    sampling again rather than by mapping old pixels onto a new view.
    """

    values: Any                      # 2-D numpy array, row 0 at the top
    min_x: float
    max_y: float
    pixel_w: float                   # map units per pixel, x
    pixel_h: float                   # map units per pixel, y
    width: int
    height: int

    def to_pixel(self, x: float, y: float) -> tuple[int, int]:
        """Map coordinate to (row, col). Not clamped: outside is a real answer."""
        col = int((float(x) - self.min_x) / self.pixel_w)
        row = int((self.max_y - float(y)) / self.pixel_h)
        return row, col

    def to_map(self, row: float, col: float) -> tuple[float, float]:
        """Pixel centre back to a map coordinate."""
        x = self.min_x + (float(col) + 0.5) * self.pixel_w
        y = self.max_y - (float(row) + 0.5) * self.pixel_h
        return x, y

    def contains_pixel(self, row: int, col: int) -> bool:
        return 0 <= row < self.height and 0 <= col < self.width

    def path_to_map(self, points: Sequence[tuple[float, float]]):
        return [self.to_map(row, col) for row, col in points]


@dataclass
class Segment:
    """One committed stretch of the shape, and how it was produced.

    Kept apart from the vertex list because undo is per segment, not per
    vertex: a traced stretch is one decision the user made, and unwinding it a
    pixel at a time would be unusable.
    """

    points: list[tuple[float, float]]
    traced: bool
    bridged_px: int = 0


@dataclass
class TraceSession:
    """Anchors, committed segments and undo, with no Qt in sight.

    The interesting mistakes here are about state: a segment from an abandoned
    shape surviving into the next one, an undo that leaves the anchor pointing
    at a vertex that is gone, a finish that reports success on a shape with two
    corners. None of those need a canvas to happen and none of them need one to
    be caught.
    """

    geometry: str = "line"
    segments: list[Segment] = field(default_factory=list)
    anchor_map: tuple[float, float] | None = None
    #: Stretches taken back, newest last. Undo without redo is a trap: it makes
    #: the key feel dangerous, so people stop using it and click around the
    #: mistake instead.
    undone: list[Segment] = field(default_factory=list)
    #: Where this shape began. Kept separately from the anchor because the
    #: anchor moves with every stretch, and undoing the last one has to put the
    #: shape back to its start rather than to the end of what was removed.
    origin: tuple[float, float] | None = None

    @property
    def started(self) -> bool:
        return self.anchor_map is not None

    def start(self, x: float, y: float) -> None:
        self.segments = []
        self.undone = []
        self.origin = (float(x), float(y))
        self.anchor_map = self.origin

    def commit(self, points: Sequence[tuple[float, float]], traced: bool,
               bridged_px: int = 0) -> int:
        """Add a stretch and move the anchor to its end."""
        cleaned = [(float(x), float(y)) for x, y in points]
        if len(cleaned) < 2:
            return len(self.segments)
        self.segments.append(Segment(points=cleaned, traced=traced,
                                     bridged_px=int(bridged_px)))
        # New work invalidates the redo stack, as it does in every editor.
        self.undone = []
        self.anchor_map = cleaned[-1]
        return len(self.segments)

    def undo(self) -> bool:
        """Drop the last stretch and put the anchor back where it started.

        Returns False when there is nothing left to undo, which the caller
        reports rather than treating as success: an undo that silently does
        nothing reads as a broken key.
        """
        if not self.segments:
            return False
        self.undone.append(self.segments.pop())
        # With nothing left, the anchor goes back to where the shape STARTED.
        # Leaving it at the end of the stretch just removed would carry on from
        # a point the user has taken back, and the shape would be drawn from
        # somewhere they did not choose.
        self.anchor_map = (self.segments[-1].points[-1] if self.segments
                           else self.origin)
        return True

    def redo(self) -> bool:
        """Put back the last stretch undo took away."""
        if not self.undone:
            return False
        segment = self.undone.pop()
        self.segments.append(segment)
        self.anchor_map = segment.points[-1]
        return True

    @property
    def first_vertex(self) -> tuple[float, float] | None:
        """Where this shape started, which is where a polygon has to come back to."""
        for segment in self.segments:
            if segment.points:
                return segment.points[0]
        return self.origin

    @property
    def can_undo(self) -> bool:
        return bool(self.segments)

    @property
    def can_redo(self) -> bool:
        return bool(self.undone)

    def vertices(self) -> list[tuple[float, float]]:
        """Every committed vertex, with the joins between segments de-duplicated.

        A segment starts where the previous one ended, so a naive concatenation
        repeats a vertex at every join. Two identical points in a ring are not
        a corner; they are a zero-length edge that some drivers reject and
        others silently keep.
        """
        out: list[tuple[float, float]] = []
        for segment in self.segments:
            for point in segment.points:
                if not out or out[-1] != point:
                    out.append(point)
        return out

    def refusal(self) -> str:
        """Why this cannot be finished yet, or "" when it can."""
        needed = MINIMUM_VERTICES.get(self.geometry)
        if needed is None:
            return "{} is not a shape this tool traces".format(self.geometry or "that")
        have = len(self.vertices())
        if have < needed:
            return "a traced {} needs at least {} points; this one has {}".format(
                self.geometry, needed, have)
        return ""

    def finish(self) -> dict[str, Any]:
        refusal = self.refusal()
        if refusal:
            # Keeps the work. The user has not finished, and discarding their
            # trace in order to say so would cost them the shape as well.
            return {"kind": "incomplete", "reason": refusal}
        points = self.vertices()
        traced = sum(1 for s in self.segments if s.traced)
        bridged = max((s.bridged_px for s in self.segments), default=0)
        self.segments = []
        self.undone = []
        self.origin = None
        self.anchor_map = None
        return {"kind": "finish", "geometry": self.geometry, "points": points,
                "traced_segments": traced, "bridged_px": bridged}

    def cancel(self) -> dict[str, Any]:
        discarded = len(self.segments)
        self.segments = []
        self.undone = []
        self.origin = None
        self.anchor_map = None
        return {"kind": "cancelled", "discarded": discarded}


def status_for_seed(seed: Any) -> str:
    """What to tell the user about where the anchor actually landed.

    A click that moved onto a line is the tool working and is worth saying
    once; a click that landed exactly is not worth a message at all.
    """
    if seed is None:
        return ("No drawn line within reach of that click. Point at the line "
                "itself, or raise the snap distance in the tracer settings.")
    if seed.moved_px >= 1.0:
        return "Anchored on the line, {:.0f} px from the click.".format(seed.moved_px)
    return "Anchored. Move the cursor along the line; click to keep it."


def finish_hint(geometry: str, stretches: int) -> str:
    """How this shape ends, in the words of the shape being drawn.

    Said on every message rather than once at the start. "When does the drawing
    end" is the question a person asks in the middle of drawing, which is
    exactly when a hint shown at the beginning is no longer on screen.
    """
    if stretches < 1:
        return ""
    if geometry == "polygon":
        return ("click back on the first corner to close the area, or press "
                "Enter" if stretches >= 2 else "press Enter when the area is closed")
    return "right-click or press Enter to finish the line"


def status_for_trace(result: Any, geometry: str, stretches: int = 0) -> str:
    if result is None or not result.ok:
        reason = getattr(result, "reason", "") or "no path"
        return ("Cannot follow the line here: {}. Hold Shift and click for a "
                "straight segment instead.".format(reason))
    length = path_length_px(result.points)
    hint = finish_hint(geometry, stretches)
    return "Following {:.0f} px of line. Click to keep it{}.".format(
        length, "; " + hint if hint else "")


# QGIS raster data types by their enum value, as numpy dtypes. Read from the
# block's own buffer rather than pixel by pixel: a 900x900 window is 810,000
# calls across the Python/C++ boundary, which costs seconds per anchor and
# would make the tool feel broken for a reason that has nothing to do with
# tracing.
_BLOCK_DTYPES = {
    1: "uint8", 2: "uint16", 3: "int16", 4: "uint32", 5: "int32",
    6: "float32", 7: "float64",
}


# How strongly a coloured pixel is treated as ink. A modern cadastral sheet
# draws its parcel boundaries in colour, and brightness alone does not see
# them: measured on IGN sheets, orange parcel ink sits at luminance 151 against
# paper at 245, while a grey road casing and a lavender building outline sit
# between the two. Tracing on luminance therefore follows the building fills
# and leaves the parcel network in fragments -- observed directly, as a graph
# reaching 0.1% of the ink on one sheet and 2.5% on another where a bitonal
# sheet reached 79.5%.
#
# This repository already knows the fact: the alignment oracle was found
# invalid on exactly these sheets for using an absolute dark threshold of 110
# against orange ink, and the worker carries produce_colour_ink_mask for the
# same reason.
COLOUR_INK_WEIGHT = 1.0


def colour_ink_response(red, green, blue):
    """Brightness, darkened by how saturated the pixel is.

    A strongly coloured pixel reads as ink whatever its brightness, so orange
    boundary ink becomes the darkest thing on the sheet while grey and pale
    fills keep their tone. On a neutral scan chroma is zero and this is exactly
    Rec. 601 luma, so a bitonal sheet behaves as it did before.
    """
    import numpy as np  # noqa: PLC0415 - numpy is a QGIS runtime dependency

    luma = 0.299 * red + 0.587 * green + 0.114 * blue
    chroma = np.maximum(np.maximum(red, green), blue) - np.minimum(
        np.minimum(red, green), blue)
    return np.clip(luma - COLOUR_INK_WEIGHT * chroma, 0.0, 255.0)


def block_to_array(block: Any, rows: int, cols: int):
    """A QgsRasterBlock as a 2-D float32 array, or None.

    Falls back to the per-pixel read when the buffer cannot be interpreted,
    because a slow window beats no window, and reports None only when neither
    route works.
    """
    import numpy as np  # noqa: PLC0415

    try:
        dtype = _BLOCK_DTYPES.get(int(block.dataType()))
        if dtype is not None:
            raw = block.data()
            buffer = bytes(raw) if raw is not None else b""
            expected = rows * cols * np.dtype(dtype).itemsize
            if len(buffer) >= expected:
                flat = np.frombuffer(buffer[:expected], dtype=dtype)
                return flat.reshape(rows, cols).astype(np.float32)
    except Exception as exc:  # noqa: BLE001 - fall through to the slow read
        log_debug("reading a raster block buffer", exc)

    try:
        values = np.empty((rows, cols), dtype=np.float32)
        for row in range(rows):
            for col in range(cols):
                values[row, col] = float(block.value(row, col))
        return values
    except Exception as exc:  # noqa: BLE001
        log_debug("reading a raster block pixel by pixel", exc)
        return None


def sample_window(layer: Any, extent: Any, max_px: int = MAX_WINDOW_PX):
    """Read the visible part of a raster layer into a numpy window.

    Bounded on purpose. The whole search cost is the number of pixels, and a
    4K canvas over a 1:500 sheet is four million of them; capping the window
    keeps every anchor inside the interactive budget and costs only resolution
    the user can get back by zooming.
    """
    provider = layer.dataProvider()
    width_units = float(extent.width())
    height_units = float(extent.height())
    if width_units <= 0 or height_units <= 0:
        return None

    native_x = abs(float(layer.rasterUnitsPerPixelX()) or 0.0)
    native_y = abs(float(layer.rasterUnitsPerPixelY()) or 0.0)
    cols = int(width_units / native_x) if native_x > 0 else max_px
    rows = int(height_units / native_y) if native_y > 0 else max_px
    cols = max(2, min(int(max_px), cols))
    rows = max(2, min(int(max_px), rows))

    bands = int(layer.bandCount())
    stack = []
    for band in range(1, min(bands, 3) + 1):
        block = provider.block(band, extent, cols, rows)
        if block is None or not block.isValid():
            return None
        values = block_to_array(block, rows, cols)
        if values is None:
            return None
        stack.append(values)
    if not stack:
        return None
    if len(stack) >= 3:
        grey = colour_ink_response(stack[0], stack[1], stack[2])
    else:
        grey = stack[0]

    return RasterWindow(
        values=grey,
        min_x=float(extent.xMinimum()),
        max_y=float(extent.yMaximum()),
        pixel_w=width_units / cols,
        pixel_h=height_units / rows,
        width=cols,
        height=rows,
    )


class VectorizeMapTool(QgsMapTool):  # pragma: no cover - requires a live canvas
    """Click a line, follow it, keep the shape.

    Interaction, in the order a user meets it:

      click            anchor on the line under the cursor
      move             the preview follows the line ahead of the cursor
      click            keep that stretch, anchor at its end
      Shift + click    a straight segment instead, when the tracer is wrong
      Backspace        undo the last stretch
      Enter / right    finish and write the feature
      Escape           abandon the shape

    Shift is the manual override, and it is not an afterthought. Every tracer
    is wrong somewhere, and a tool whose only response to being wrong is to be
    wrong more is one people stop using. The override is one modifier away and
    the status line says so at the moment tracing fails.
    """

    def __init__(self, canvas: Any, raster_for: Callable[[], Any],
                 geometry: str,
                 on_finish: Callable[[str, list, str], None],
                 options: TraceOptions | None = None,
                 on_status: Callable[[str], None] | None = None,
                 on_cancel: Callable[[int], None] | None = None,
                 use_project_snapping: bool = True,
                 trace_existing: Callable[[], list] | None = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.raster_for = raster_for
        self.options = options or TraceOptions()
        self.session = TraceSession(geometry=geometry)
        self.on_finish = on_finish
        self.on_status = on_status or (lambda _message: None)
        self.on_cancel = on_cancel or (lambda _discarded: None)
        self._window: RasterWindow | None = None
        self._window_extent = None
        self._shortcuts: list = []
        self.use_project_snapping = bool(use_project_snapping)
        #: The layers whose already-drawn boundaries may be reused. Supplied by
        #: the plugin rather than discovered here, because which layers count is
        #: a project question and this class is about one canvas gesture.
        self.trace_existing = trace_existing or (lambda: [])
        self._tracer = None
        self._tracer_extent = None
        #: Set when the last click landed on an existing feature through the
        #: project's own snapping. It stops the ink search from moving an
        #: anchor the user placed exactly on a neighbour's vertex.
        self._anchor_is_snapped = False
        self._wire: LiveWire | None = None
        self._band = None
        self._preview = None

    # -- Qt entry points ---------------------------------------------------

    def activate(self) -> None:
        try:
            super().activate()
        except Exception as exc:  # noqa: BLE001
            log_debug("activating the vectorize tool", exc)
        self._ensure_bands()
        self._install_shortcuts()
        self.on_status(
            "Click a drawn line to start. Shift-click for a straight segment, "
            "Ctrl+Z to take one back, Enter to finish, Esc to cancel.")

    def deactivate(self) -> None:
        self._remove_shortcuts()
        self._discard_bands()
        self.session.cancel()
        self._wire = None
        self._window = None
        try:
            super().deactivate()
        except Exception as exc:  # noqa: BLE001
            log_debug("deactivating the vectorize tool", exc)

    def canvasReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        from qgis.PyQt.QtCore import Qt

        if self._is_right_button(event):
            self._settle(self.session.finish())
            return
        point, snapped = self._snapped_point(event.pos())
        straight = bool(event.modifiers() & enum_member(Qt, "KeyboardModifier",
                                                        "ShiftModifier"))
        self._click(float(point.x()), float(point.y()), straight=straight,
                    snapped=snapped)

    def canvasMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        if not self.session.started or self._wire is None or self._window is None:
            return
        # The window was read from one canvas extent, so every pixel in it is
        # tied to that view. Zooming or panning mid-trace makes the mapping
        # wrong, and the preview would follow a line that is no longer under the
        # cursor. Re-read rather than draw a confident wrong path.
        if self._canvas_has_moved():
            if not self._reseed():
                return
        point = self.toMapCoordinates(event.pos())
        row, col = self._window.to_pixel(point.x(), point.y())
        if not self._window.contains_pixel(row, col):
            return
        if self.close_target(point.x(), point.y(), CLOSE_TOLERANCE_PX) is not None:
            # Told while the cursor is there, not after the click. A shape that
            # finished itself without warning reads as a bug the first time.
            self.on_status("Click here to close the area and finish it.")
            self._draw_preview([])
            return
        result = self._wire.path_to(row, col)
        if result.ok:
            self._draw_preview(self._window.path_to_map(result.points))
            self.on_status(status_for_trace(
                result, self.session.geometry, len(self.session.segments)))
        else:
            self._draw_preview([])

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        from qgis.PyQt.QtCore import Qt

        key = event.key()
        if key == enum_member(Qt, "Key", "Key_Escape"):
            # Two stages, because one key doing two jobs at once is how people
            # lose work: the first Escape abandons the shape in progress and
            # leaves the tool armed for the next one, and only an Escape with
            # nothing in progress puts the tool away.
            if self.session.started:
                outcome = self.session.cancel()
                self._clear_bands()
                self.on_status(
                    "Shape abandoned. Click a line to start another, or press "
                    "Esc again to put the tracer away.")
                self._reseed_cleared()
                _ = outcome
                return
            self.on_status("Tracer closed.")
            self.on_cancel(0)
            return
        if key in (enum_member(Qt, "Key", "Key_Backspace"),
                   enum_member(Qt, "Key", "Key_Delete")):
            self._undo_stretch()
            return
        if key in (enum_member(Qt, "Key", "Key_Return"),
                   enum_member(Qt, "Key", "Key_Enter")):
            self._settle(self.session.finish())

    # -- behaviour ---------------------------------------------------------

    def _existing_path(self, start, end):
        """The already-digitized boundary between two points, or None.

        This is the half that makes the tool usable on a real sheet. Parcels
        share edges: the second parcel in a row shares its whole left side with
        the first, and most interior boundaries on a cadastral sheet are shared
        by two holdings. Re-tracing that edge from the pixels is double the work
        AND produces a second line a few pixels off the first, which is where
        slivers and overlaps come from -- every topology check afterwards
        reports them, and the reviewer pays for it.

        Reusing the neighbour's geometry is the only way two parcels get an
        edge that is the SAME edge rather than two nearly-identical ones. So
        this is asked before the raster: an exact shared boundary is worth more
        than one re-derived from ink.

        QgsTracer is QGIS's own machinery for it, the same one the standard
        digitizing tools use when tracing is switched on.
        """
        layers = [layer for layer in self.trace_existing() if layer is not None]
        if not layers:
            return None
        try:
            from qgis.core import QgsTracer  # noqa: PLC0415 - Qt-only import

            extent = self.canvas.extent()
            if self._tracer is None or self._tracer_extent != extent:
                tracer = QgsTracer()
                tracer.setLayers(layers)
                tracer.setExtent(extent)
                tracer.init()
                self._tracer, self._tracer_extent = tracer, extent
            found = self._tracer.findShortestPath(start, end)
            path = found[0] if isinstance(found, tuple) else found
            if path and len(path) >= 2:
                return [(float(p.x()), float(p.y())) for p in path]
        except Exception as exc:  # noqa: BLE001 - the raster path still works
            log_debug("following an existing boundary", exc)
        return None

    def _snapped_point(self, position: Any):
        """The click, moved onto an existing feature when the project says so.

        This is QGIS's own snapping, configured in the snapping toolbar, and it
        is asked FIRST for a reason that matters most in cadastre: two parcels
        that share a boundary have to share its vertices exactly. A boundary
        traced a pixel off its neighbour looks right and leaves a sliver that
        every later topology check reports.

        Returns (map point, snapped) so the caller can tell the difference. A
        snapped anchor is never moved again by the ink search: an exact shared
        vertex is worth more than being one pixel closer to the drawn line.
        """
        if self.use_project_snapping:
            try:
                utils = self.canvas.snappingUtils()
                match = utils.snapToMap(position) if utils is not None else None
                if match is not None and match.isValid():
                    return match.point(), True
            except (AttributeError, RuntimeError, TypeError) as exc:
                log_debug("asking the project for a snap", exc)
        return self.toMapCoordinates(position), False

    def _click(self, x: float, y: float, straight: bool,
               snapped: bool = False) -> None:
        if not self.session.started:
            self.session.start(x, y)
            self._anchor_is_snapped = snapped
            if not self._reseed():
                self.session.cancel()
            elif snapped:
                self.on_status(
                    "Anchored on an existing feature. Move along the line; "
                    "click to keep it.")
            return

        anchor = self.session.anchor_map
        closing = self.close_target(x, y, CLOSE_TOLERANCE_PX)
        if closing is not None:
            x, y = closing
            snapped = True
        if not straight and anchor is not None:
            # Ask the neighbours first. See _existing_path for why.
            reused = self._existing_reuse(anchor, x, y)
            if reused is not None:
                self.session.commit(reused, traced=True)
                self._anchor_is_snapped = True
                self._redraw_committed()
                self._reseed()
                if closing is not None:
                    self._settle(self.session.finish())
                    return
                self.on_status(
                    "Followed the boundary already drawn here, so the two "
                    "shapes share it exactly. Carry on, or {}.".format(
                        finish_hint(self.session.geometry,
                                    len(self.session.segments))))
                return
        if straight or self._wire is None or self._window is None:
            self.session.commit([anchor, (x, y)], traced=False)
            self._anchor_is_snapped = snapped
            self._redraw_committed()
            self._reseed()
            self.on_status("Straight segment kept. Carry on, or {}.".format(
                finish_hint(self.session.geometry, len(self.session.segments))))
            return

        row, col = self._window.to_pixel(x, y)
        result = self._wire.path_to(row, col)
        if not result.ok:
            self.on_status(status_for_trace(result, self.session.geometry))
            return
        points = self._window.path_to_map(
            simplify(result.points, self.options.clamped().simplify_px))
        if snapped and points:
            # End the stretch exactly on the snapped feature. The traced path
            # stops at the nearest INK pixel to it, which is close but not the
            # same point, and "close" is what leaves slivers between parcels.
            points[-1] = (x, y)
        self.session.commit(points, traced=True, bridged_px=result.bridged_px)
        self._anchor_is_snapped = snapped
        self._redraw_committed()
        if closing is not None:
            # The ring came back to its own first vertex, which is the whole
            # shape. Finishing here saves the user pressing Enter to say what
            # they have just plainly done.
            self._settle(self.session.finish())
            return
        self._reseed()

    def close_target(self, x: float, y: float, tolerance_px: float):
        """The shape's first vertex when the cursor is within reach of it.

        A traced parcel has to come back to where it started, exactly. Landing
        "near enough" leaves a gap at the corner where three holdings meet,
        which is the same sliver problem as a re-traced shared edge and shows
        up in the same topology report.

        Only polygons: a line that happens to end near its own start is a line
        the user drew that way.
        """
        if self.session.geometry != "polygon":
            return None
        start = self.session.first_vertex
        if start is None or len(self.session.segments) < 2:
            # Two stretches at least, or the first click after the anchor would
            # count as closing the shape onto itself.
            return None
        units = self._map_units_per_pixel() * float(tolerance_px)
        import math

        if math.hypot(x - start[0], y - start[1]) <= units:
            return start
        return None

    def _map_units_per_pixel(self) -> float:
        try:
            return float(self.canvas.mapUnitsPerPixel())
        except (AttributeError, RuntimeError, TypeError):
            return 1.0

    def _existing_reuse(self, anchor, x: float, y: float):
        """The reusable boundary between the anchor and this click, if any."""
        try:
            from qgis.core import QgsPointXY  # noqa: PLC0415 - Qt-only import

            return self._existing_path(QgsPointXY(anchor[0], anchor[1]),
                                       QgsPointXY(x, y))
        except Exception as exc:  # noqa: BLE001
            log_debug("reusing an existing boundary", exc)
            return None

    def _reseed_cleared(self) -> None:
        """Forget the search after a shape is abandoned.

        The wire is anchored to a point that no longer belongs to anything, and
        leaving it would let the next cursor move draw a preview from the shape
        the user just gave up on.
        """
        self._wire = None
        self._window = None
        self._window_extent = None
        self._tracer = None
        self._tracer_extent = None

    def _canvas_has_moved(self) -> bool:
        """Has the view changed since the window was read?"""
        try:
            extent = self.canvas.extent()
        except (AttributeError, RuntimeError):
            return False
        if self._window_extent is None:
            return True
        try:
            return not self._window_extent == extent
        except (AttributeError, TypeError):
            return True

    def _reseed(self) -> bool:
        """Read the window under the anchor and run the search from it."""
        anchor = self.session.anchor_map
        if anchor is None:
            return False
        layer = self.raster_for()
        if layer is None:
            self.on_status("Tracing needs a raster layer. Select the scanned "
                           "map in the Layers panel.")
            return False
        extent = self.canvas.extent()
        window = sample_window(layer, extent)
        if window is None:
            self.on_status("Could not read pixels from that raster here.")
            return False
        wire = LiveWire(window.values, self.options)
        row, col = window.to_pixel(anchor[0], anchor[1])
        seed = wire.seed(row, col) if window.contains_pixel(row, col) else None
        self._window, self._wire = window, wire
        self._window_extent = extent
        self.on_status(status_for_seed(seed))
        if seed is None:
            self._wire = None
            return False
        if not self._anchor_is_snapped:
            # The ink search moved the anchor onto the drawn line; the shape
            # follows it, or the next stretch starts a pixel off the boundary.
            # An anchor the PROJECT snapped is left exactly where it is: the
            # user put it on a neighbour's vertex and that is the whole point.
            self.session.anchor_map = window.to_map(seed.y, seed.x)
        return True

    def _settle(self, outcome: dict[str, Any]) -> None:
        kind = outcome.get("kind")
        if kind == "finish":
            self._clear_bands()
            self.on_finish(outcome["geometry"], outcome["points"], self.crs_authid())
            # Deliberately no status line here: the handler that stored the
            # shape knows where it went and says so, and two messages about one
            # shape means the second overwrites the useful one.
            return
        if kind == "incomplete":
            self.on_status("{}. Keep tracing, or press Esc to abandon it.".format(
                outcome["reason"]))

    def crs_authid(self) -> str:
        """The frame the traced coordinates are in, or "".

        Empty is an answer. A canvas on a custom projection has coordinates
        this build cannot name, and a shape stamped EPSG:4326 because nothing
        better was known claims a place it was not drawn in.
        """
        crs = self.canvas.mapSettings().destinationCrs()
        return str(crs.authid()) if crs.isValid() else ""

    # -- keys --------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        """Own Ctrl+Z and Ctrl+Shift+Z while the canvas is armed.

        A map tool cannot get these through keyPressEvent. Undo is a QAction on
        the QGIS main window with a window-level shortcut context, and Qt gives
        that action the key before the focus widget ever sees it -- so a tool
        that handles Ctrl+Z in keyPressEvent silently never runs, and the user
        gets the LAYER undo in the middle of a shape they have not finished.

        A shortcut owned by the canvas with WidgetWithChildrenShortcut context
        wins while the canvas has focus, and is deleted the moment the tool is
        put away so QGIS gets its own undo back.
        """
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QKeySequence
        from qgis.PyQt.QtWidgets import QShortcut

        self._remove_shortcuts()
        context = enum_member(Qt, "ShortcutContext", "WidgetWithChildrenShortcut")
        for sequence, handler in (
            (enum_member(QKeySequence, "StandardKey", "Undo"), self._undo_stretch),
            (enum_member(QKeySequence, "StandardKey", "Redo"), self._redo_stretch),
        ):
            try:
                shortcut = QShortcut(QKeySequence(sequence), self.canvas)
                shortcut.setContext(context)
                shortcut.activated.connect(handler)
                self._shortcuts.append(shortcut)
            except Exception as exc:  # noqa: BLE001 - Backspace still works
                log_debug("installing a tracer shortcut", exc)

    def _remove_shortcuts(self) -> None:
        for shortcut in getattr(self, "_shortcuts", []):
            try:
                shortcut.setEnabled(False)
                shortcut.setParent(None)
            except Exception as exc:  # noqa: BLE001
                log_debug("removing a tracer shortcut", exc)
        self._shortcuts = []

    def _undo_stretch(self) -> None:
        """Ctrl+Z and Backspace, and what to say when there is nothing left.

        With no stretch in the current shape this deliberately does NOT fall
        through to the layer undo. A user mid-shape pressing undo means the
        stretch they just traced; removing a feature they finished ten minutes
        ago instead would be the worst possible reading of the key.
        """
        if not self.session.started:
            self.on_status("Nothing is being traced. Click a line to start.")
            return
        if self.session.undo():
            self._redraw_committed()
            self._reseed()
            self.on_status("Took back one stretch. Ctrl+Shift+Z puts it back.")
        else:
            self.on_status(
                "Nothing left to take back in this shape. Esc abandons it.")

    def _redo_stretch(self) -> None:
        if self.session.redo():
            self._redraw_committed()
            self._reseed()
            self.on_status("Stretch restored.")
        else:
            self.on_status("Nothing to put back.")

    # -- rubber bands ------------------------------------------------------

    def _digitizing_style(self):
        """The colours and width QGIS uses for digitizing, from its own settings.

        Hardcoding a blue was wrong twice over. It ignored the rubber band
        colour the user set in Settings > Digitizing, which every other tool in
        QGIS obeys, so tracing looked like nothing else they do; and on a map
        whose own rendering is blue the line being drawn was invisible against
        the thing it was being drawn over. The user is entitled to have set
        that colour for a reason.
        """
        from qgis.PyQt.QtGui import QColor  # noqa: PLC0415 - Qt-only import

        settings = QSettings()

        def channel(name, fallback):
            try:
                return int(settings.value("qgis/digitizing/" + name, fallback))
            except (TypeError, ValueError):
                return fallback

        line = QColor(channel("line_color_red", 255), channel("line_color_green", 0),
                      channel("line_color_blue", 0), channel("line_color_alpha", 200))
        fill = QColor(channel("fill_color_red", 255), channel("fill_color_green", 0),
                      channel("fill_color_blue", 0), channel("fill_color_alpha", 30))
        try:
            width = float(settings.value("qgis/digitizing/line_width", 1))
        except (TypeError, ValueError):
            width = 1.0
        return line, fill, max(1.0, width)

    def _ensure_bands(self) -> None:
        from qgis.gui import QgsRubberBand
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QColor

        line, fill, width = self._digitizing_style()
        # An area being traced is drawn AS an area. With a line band the user
        # watches a boundary and only finds out what it encloses once the
        # feature exists, which is the moment they can no longer change it.
        kind = (geometry_type("PolygonGeometry")
                if self.session.geometry == "polygon"
                else geometry_type("LineGeometry"))

        if self._band is None:
            self._band = QgsRubberBand(self.canvas, kind)
            self._band.setColor(line)
            self._band.setFillColor(fill)
            self._band.setWidth(width + 1.0)
            # The vertices are what this tool produces, so they are shown. A
            # bare line hides exactly the thing the user is deciding about.
            try:
                self._band.setIcon(enum_member(QgsRubberBand, "IconType", "ICON_BOX"))
                self._band.setIconSize(7)
            except (AttributeError, TypeError) as exc:
                log_debug("marking the traced vertices", exc)
        if self._preview is None:
            faded = QColor(line)
            faded.setAlpha(max(60, line.alpha() // 2))
            # A line, always. The preview is ONE stretch from the anchor to
            # the cursor; as a polygon band QGIS would close it and draw a
            # shape between those two points that the user is not making.
            self._preview = QgsRubberBand(self.canvas, geometry_type("LineGeometry"))
            self._preview.setColor(faded)
            self._preview.setFillColor(QColor(0, 0, 0, 0))
            self._preview.setWidth(width)
            self._preview.setLineStyle(enum_member(Qt, "PenStyle", "DashLine"))

    def _clear_bands(self) -> None:
        for band in (self._band, self._preview):
            if band is not None:
                try:
                    band.reset()
                except Exception as exc:  # noqa: BLE001
                    log_debug("clearing a rubber band", exc)

    def _discard_bands(self) -> None:
        """Take the bands off the canvas, not just empty them.

        reset() clears a rubber band's points and leaves the item in the map
        scene. The tool is rebuilt every time it is armed, so emptying alone
        left one more invisible item on the canvas per arm and disarm, for as
        long as the QGIS session lasted.
        """
        for attribute in ("_band", "_preview"):
            band = getattr(self, attribute, None)
            if band is None:
                continue
            try:
                band.reset()
                scene = self.canvas.scene()
                if scene is not None:
                    scene.removeItem(band)
            except Exception as exc:  # noqa: BLE001
                log_debug("removing a rubber band from the canvas", exc)
            setattr(self, attribute, None)

    def _redraw_committed(self) -> None:
        from qgis.core import QgsPointXY

        self._ensure_bands()
        self._band.reset()
        for x, y in self.session.vertices():
            self._band.addPoint(QgsPointXY(x, y), False)
        self._band.updatePosition()
        self._band.show()
        if self._preview is not None:
            self._preview.reset()

    def _draw_preview(self, points: Sequence[tuple[float, float]]) -> None:
        from qgis.core import QgsPointXY

        self._ensure_bands()
        self._preview.reset()
        for x, y in points:
            self._preview.addPoint(QgsPointXY(x, y), False)
        self._preview.updatePosition()
        self._preview.show()

    @staticmethod
    def _is_right_button(event: Any) -> bool:
        from qgis.PyQt.QtCore import Qt

        try:
            return event.button() == enum_member(Qt, "MouseButton", "RightButton")
        except (AttributeError, TypeError):
            return False
