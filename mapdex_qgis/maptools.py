"""Canvas tools that let the user point at the map.

`measure.distance@1` takes two positions, and until now the only way to supply
them was to already know them. That makes the capability answerable by an agent
holding coordinates and not by a person pointing at two places, which is how
anybody actually measures. This module closes that gap.

Everything here is Qt and QGIS runtime behaviour that unit tests cannot prove.
The tests below the fold exercise the coordinate handling, the CRS transform
decision and the state machine by driving the class with doubles; whether the
tool actually binds to the canvas is a question only a running QGIS answers.
"""
from __future__ import annotations

from typing import Any, Callable

try:  # pragma: no cover - import shape differs between QGIS builds
    from qgis.gui import QgsMapTool
except Exception:  # pragma: no cover - importable outside QGIS for tests
    QgsMapTool = object


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
        except Exception:  # noqa: BLE001 - deactivation must not raise into Qt
            pass

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
