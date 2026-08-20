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
from .qt_compat import enum_member

try:  # pragma: no cover - import shape differs between QGIS builds
    from qgis.gui import QgsMapTool
except Exception:  # pragma: no cover - importable outside QGIS for tests
    QgsMapTool = object


# The largest window the tool will read and search at once. Beyond this the
# search stops feeling instant (see the timings in livewire.py), and a user who
# needs to trace across more sheet than this should zoom, which also gives the
# tracer more pixels per drawn line to work with.
MAX_WINDOW_PX = 900

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

    @property
    def started(self) -> bool:
        return self.anchor_map is not None

    def start(self, x: float, y: float) -> None:
        self.segments = []
        self.anchor_map = (float(x), float(y))

    def commit(self, points: Sequence[tuple[float, float]], traced: bool,
               bridged_px: int = 0) -> int:
        """Add a stretch and move the anchor to its end."""
        cleaned = [(float(x), float(y)) for x, y in points]
        if len(cleaned) < 2:
            return len(self.segments)
        self.segments.append(Segment(points=cleaned, traced=traced,
                                     bridged_px=int(bridged_px)))
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
        self.segments.pop()
        self.anchor_map = self.segments[-1].points[-1] if self.segments else self.anchor_map
        return True

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
        self.anchor_map = None
        return {"kind": "finish", "geometry": self.geometry, "points": points,
                "traced_segments": traced, "bridged_px": bridged}

    def cancel(self) -> dict[str, Any]:
        discarded = len(self.segments)
        self.segments = []
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


def status_for_trace(result: Any, geometry: str) -> str:
    if result is None or not result.ok:
        reason = getattr(result, "reason", "") or "no path"
        return ("Cannot follow the line here: {}. Hold Shift and click for a "
                "straight segment instead.".format(reason))
    length = path_length_px(result.points)
    hint = "Enter to finish" if geometry != "polygon" else "Enter to close the shape"
    return "Following {:.0f} px of line. Click to keep it, {}.".format(length, hint)


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
                 on_cancel: Callable[[int], None] | None = None):
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
        self.on_status("Click on a drawn line to start tracing.")

    def deactivate(self) -> None:
        self._clear_bands()
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
        point = self.toMapCoordinates(event.pos())
        straight = bool(event.modifiers() & enum_member(Qt, "KeyboardModifier",
                                                        "ShiftModifier"))
        self._click(float(point.x()), float(point.y()), straight=straight)

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
        result = self._wire.path_to(row, col)
        if result.ok:
            self._draw_preview(self._window.path_to_map(result.points))
        else:
            self._draw_preview([])

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        from qgis.PyQt.QtCore import Qt

        key = event.key()
        if key == enum_member(Qt, "Key", "Key_Escape"):
            outcome = self.session.cancel()
            self._clear_bands()
            self.on_status("Tracing cancelled.")
            self.on_cancel(int(outcome["discarded"]))
            return
        if key == enum_member(Qt, "Key", "Key_Backspace"):
            if self.session.undo():
                self._redraw_committed()
                self._reseed()
                self.on_status("Last stretch removed.")
            else:
                self.on_status("Nothing to undo yet.")
            return
        if key in (enum_member(Qt, "Key", "Key_Return"),
                   enum_member(Qt, "Key", "Key_Enter")):
            self._settle(self.session.finish())

    # -- behaviour ---------------------------------------------------------

    def _click(self, x: float, y: float, straight: bool) -> None:
        if not self.session.started:
            self.session.start(x, y)
            if not self._reseed():
                self.session.cancel()
            return

        anchor = self.session.anchor_map
        if straight or self._wire is None or self._window is None:
            self.session.commit([anchor, (x, y)], traced=False)
            self._redraw_committed()
            self._reseed()
            self.on_status("Straight segment kept.")
            return

        row, col = self._window.to_pixel(x, y)
        result = self._wire.path_to(row, col)
        if not result.ok:
            self.on_status(status_for_trace(result, self.session.geometry))
            return
        points = self._window.path_to_map(
            simplify(result.points, self.options.clamped().simplify_px))
        self.session.commit(points, traced=True, bridged_px=result.bridged_px)
        self._redraw_committed()
        self._reseed()

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
        # Snapping moved the anchor onto the line; the shape must follow it,
        # or the next segment starts a pixel off the boundary it is tracing.
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
            self.on_status(outcome["reason"])

    def crs_authid(self) -> str:
        """The frame the traced coordinates are in, or "".

        Empty is an answer. A canvas on a custom projection has coordinates
        this build cannot name, and a shape stamped EPSG:4326 because nothing
        better was known claims a place it was not drawn in.
        """
        crs = self.canvas.mapSettings().destinationCrs()
        return str(crs.authid()) if crs.isValid() else ""

    # -- rubber bands ------------------------------------------------------

    def _ensure_bands(self) -> None:
        from qgis.core import QgsWkbTypes
        from qgis.gui import QgsRubberBand
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QColor

        if self._band is None:
            self._band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._band.setColor(QColor(0, 120, 215))
            self._band.setWidth(3)
        if self._preview is None:
            self._preview = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._preview.setColor(QColor(0, 120, 215, 140))
            self._preview.setWidth(2)
            self._preview.setLineStyle(enum_member(Qt, "PenStyle", "DashLine"))

    def _clear_bands(self) -> None:
        for band in (self._band, self._preview):
            if band is not None:
                try:
                    band.reset()
                except Exception as exc:  # noqa: BLE001
                    log_debug("clearing a rubber band", exc)

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
