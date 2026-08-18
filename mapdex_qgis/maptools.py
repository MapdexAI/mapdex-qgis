"""Canvas tools that let the user point at the map.

`measure.distance@1` takes two positions, and until now the only way to supply
them was to already know them. That makes the capability answerable by an agent
holding coordinates and not by a person pointing at two places, which is how
anybody actually measures. This module closes that gap.

`draw.geometry@1` is the same gap one shape wider. `features.py` refuses to
invent a polygon's corners, correctly, so before the draw tool the product could
place points it had made up and could not accept a boundary a surveyor traced.

Everything here is Qt and QGIS runtime behaviour that unit tests cannot prove.
The tests below the fold exercise the coordinate handling, the CRS transform
decision and the state machines by driving the classes with doubles; whether a
tool actually binds to the canvas is a question only a running QGIS answers.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from .guard import log_debug
from .qt_compat import enum_member

try:  # pragma: no cover - import shape differs between QGIS builds
    from qgis.gui import QgsMapTool
except Exception:  # pragma: no cover - importable outside QGIS for tests
    QgsMapTool = object


# The number of DISTINCT vertices each geometry needs in order to exist. A
# polygon of two points is not a thin polygon; it is not a polygon. QGIS accepts
# the feature anyway, and the layer then holds a shape whose area every later
# measurement reports as zero - a wrong answer with no error in front of it.
MINIMUM_VERTICES = {"point": 1, "linestring": 2, "polygon": 3}


def distinct_vertices(vertices: Sequence[Any]) -> list[tuple[float, float]]:
    """The drawn positions with repeats removed, in the order first clicked.

    Repeats are ordinary rather than exceptional: a double-click to finish
    delivers the same position twice, and a careful user clicking a polygon shut
    puts the last vertex on the first. Counting those as separate corners is how
    three clicks become a "valid" triangle with no area.
    """
    seen: list[tuple[float, float]] = []
    for vertex in vertices:
        pair = (float(vertex[0]), float(vertex[1]))
        if pair not in seen:
            seen.append(pair)
    return seen


def refusal_for(geometry: Any, vertices: Sequence[Any]) -> str:
    """Why these vertices cannot be that geometry, or "" when they can.

    The registry validates the SHAPE of the vertex list - pairs of finite
    numbers, bounded in count - because that is a fact about the parameter. How
    many of them a polygon needs is a fact about the geometry, and it depends on
    the value of a second parameter, which a per-parameter schema cannot see.

    It lives here rather than in the executor so the canvas tool and the
    executor answer the question identically: the tool uses it to know when the
    user is finished, and the executor uses it to refuse a request that arrived
    from the capability channel with a shape the canvas would never have sent.
    """
    kind = str(geometry or "").strip().lower()
    needed = MINIMUM_VERTICES.get(kind)
    if needed is None:
        return "{} is not a shape this tool draws".format(geometry or "that")
    unique = len(distinct_vertices(vertices))
    if unique < needed:
        return "a {} needs at least {} distinct point(s); this one has {}".format(
            kind, needed, unique)
    return ""


class MeasureState:
    """The two-click state machine, separated from Qt so it can be tested.

    Kept apart deliberately. The interesting mistakes here are about state (a
    stale first point surviving into the next measurement) and about coordinates
    (measuring in canvas units and reporting metres), and neither needs a canvas
    to get wrong.
    """

    def __init__(self):
        self.first: tuple[float, float] | None = None

    @property
    def waiting_for_second(self) -> bool:
        return self.first is not None

    def reset(self) -> None:
        self.first = None

    def click(self, lon: float, lat: float) -> dict[str, Any]:
        """Record a click and say what the caller should do about it."""
        point = (float(lon), float(lat))
        if self.first is None:
            self.first = point
            return {"kind": "awaiting_second", "from": point}
        start = self.first
        # Cleared before returning, not after the measurement is used. If the
        # caller raises while handling the result, the next click must start a
        # fresh measurement rather than silently pair with a point from the
        # abandoned one.
        self.first = None
        return {"kind": "measure", "from": start, "to": point}


class MeasureMapTool(QgsMapTool):  # pragma: no cover - requires a live canvas
    """Collect two clicked positions and hand them to measure.distance@1.

    The tool converts to WGS84 before reporting, because the capability declares
    longitude and latitude and a canvas in a projected CRS would otherwise feed
    it metres in a field bounded to +/-180. That is the kind of mistake that
    produces a plausible number rather than an error.
    """

    def __init__(self, canvas: Any, on_measure: Callable[[float, float, float, float], None],
                 on_status: Callable[[str], None] | None = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_measure = on_measure
        self.on_status = on_status or (lambda _message: None)
        self.state = MeasureState()

    def canvasReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        point = self.toMapCoordinates(event.pos())
        lon, lat = self._to_wgs84(point)
        outcome = self.state.click(lon, lat)
        if outcome["kind"] == "awaiting_second":
            self.on_status("First point set. Click the second point.")
            return
        start, end = outcome["from"], outcome["to"]
        self.on_measure(start[0], start[1], end[0], end[1])

    def deactivate(self) -> None:
        # A half-finished measurement must not survive the tool being put away,
        # or the first click of the next session pairs with a point the user
        # placed minutes ago somewhere else entirely.
        self.state.reset()
        try:
            super().deactivate()
        except Exception as exc:  # noqa: BLE001 - deactivation must not raise into Qt
            log_debug("deactivating the measure tool", exc)

    def _to_wgs84(self, point: Any) -> tuple[float, float]:
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsProject,
        )

        crs = self.canvas.mapSettings().destinationCrs()
        target = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs == target or not crs.isValid():
            return float(point.x()), float(point.y())
        transform = QgsCoordinateTransform(crs, target, QgsProject.instance())
        moved = transform.transform(point)
        return float(moved.x()), float(moved.y())


class DrawState:
    """Vertices collected so far, and whether the shape is finished.

    Separated from Qt for the same reason `MeasureState` is: the mistakes worth
    catching are about state, not about painting. A vertex from an abandoned
    shape surviving into the next one, or a shape reported finished before it
    has enough corners, are both wrong without a canvas being involved.
    """

    def __init__(self, geometry: Any):
        self.geometry = str(geometry or "").strip().lower()
        self.vertices: list[list[float]] = []

    @property
    def needed(self) -> int:
        return MINIMUM_VERTICES.get(self.geometry, 0)

    def reset(self) -> None:
        self.vertices = []

    def click(self, x: float, y: float) -> dict[str, Any]:
        """Record a clicked position and say what the caller should do about it."""
        point = [float(x), float(y)]
        if self.vertices and self.vertices[-1] == point:
            # A double-click delivers two releases at one position. The second
            # must not become a second corner sitting on top of the first.
            return {"kind": "repeat", "count": len(self.vertices)}
        self.vertices.append(point)
        if self.geometry == "point":
            # One click IS the whole shape. Waiting for a finishing gesture here
            # would make the simplest case the one with a hidden extra step.
            return self.finish()
        return {"kind": "vertex", "count": len(self.vertices),
                "still_needed": max(0, self.needed - len(self.vertices))}

    def finish(self) -> dict[str, Any]:
        """Close the shape, or say what it still needs."""
        refusal = refusal_for(self.geometry, self.vertices)
        if refusal:
            # Deliberately keeps what was clicked. The user has not finished;
            # discarding their work in order to tell them so would cost them the
            # corners as well as the shape.
            return {"kind": "incomplete", "reason": refusal, "count": len(self.vertices)}
        drawn = list(self.vertices)
        # Cleared before the caller sees the result, as MeasureState does. If
        # the caller raises while handling it, the next click has to start a
        # fresh shape rather than extend the abandoned one.
        self.vertices = []
        return {"kind": "finish", "geometry": self.geometry, "vertices": drawn}

    def cancel(self) -> dict[str, Any]:
        discarded = len(self.vertices)
        self.vertices = []
        return {"kind": "cancelled", "discarded": discarded}


class DrawMapTool(QgsMapTool):  # pragma: no cover - requires a live canvas
    """Collect clicked vertices and hand the finished shape to draw.geometry@1.

    Unlike the measure tool this does NOT convert to WGS84. A measurement is a
    distance and the capability declares longitude and latitude; a drawn shape is
    data, and reprojecting an AOI a surveyor traced on a national grid into
    degrees before it is even stored would resample the user's own geometry for
    no reason. The vertices travel in the canvas CRS with that CRS named beside
    them, and `crs_authid` returns "" rather than a guess when the canvas is on a
    custom projection with no authority code.
    """

    def __init__(self, canvas: Any, geometry: str,
                 on_finish: Callable[[str, list, str], None],
                 on_cancel: Callable[[int], None] | None = None,
                 on_status: Callable[[str], None] | None = None):
        super().__init__(canvas)
        self.canvas = canvas
        self.state = DrawState(geometry)
        self.on_finish = on_finish
        self.on_cancel = on_cancel or (lambda _discarded: None)
        self.on_status = on_status or (lambda _message: None)

    def canvasReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        if self._is_right_button(event):
            self._settle(self.state.finish())
            return
        point = self.toMapCoordinates(event.pos())
        self._settle(self.state.click(float(point.x()), float(point.y())))

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        from qgis.PyQt.QtCore import Qt

        key = event.key()
        if key == enum_member(Qt, "Key", "Key_Escape"):
            outcome = self.state.cancel()
            self.on_status("Drawing cancelled.")
            self.on_cancel(int(outcome["discarded"]))
            return
        if key in (enum_member(Qt, "Key", "Key_Return"), enum_member(Qt, "Key", "Key_Enter")):
            self._settle(self.state.finish())

    def _settle(self, outcome: dict[str, Any]) -> None:
        kind = outcome.get("kind")
        if kind == "finish":
            self.on_finish(outcome["geometry"], outcome["vertices"], self.crs_authid())
            return
        if kind == "incomplete":
            self.on_status(outcome["reason"])
            return
        if kind == "vertex":
            still = int(outcome["still_needed"])
            self.on_status(
                "{} point(s) placed. {}".format(
                    outcome["count"],
                    "{} more needed.".format(still) if still
                    else "Right-click or press Enter to finish."))
        # A repeat says nothing. Telling the user their double-click was a
        # double-click is noise in the one place they are concentrating.

    def crs_authid(self) -> str:
        """The authority code the clicked coordinates are in, or "".

        Empty is an answer, not a failure: a canvas on a custom CRS has
        coordinates whose frame this build cannot name, and a shape stamped
        EPSG:4326 because nothing better was known claims a place it was not
        drawn in. The caller refuses on the empty string.
        """
        crs = self.canvas.mapSettings().destinationCrs()
        return str(crs.authid()) if crs.isValid() else ""

    def deactivate(self) -> None:
        # A half-drawn shape must not survive the tool being put away, or the
        # first click of the next shape continues a boundary the user abandoned.
        self.state.reset()
        try:
            super().deactivate()
        except Exception as exc:  # noqa: BLE001 - deactivation must not raise into Qt
            log_debug("deactivating the draw tool", exc)

    @staticmethod
    def _is_right_button(event: Any) -> bool:
        from qgis.PyQt.QtCore import Qt

        try:
            return event.button() == enum_member(Qt, "MouseButton", "RightButton")
        except (AttributeError, TypeError):
            return False
