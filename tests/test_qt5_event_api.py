"""Guard against Qt6-only event API breaking QGIS 3.

``test_qt6_enums`` guards one direction: Qt5 spellings that fail on QGIS 4.
This guards the other. ``QMouseEvent.position()`` and ``globalPosition()``
exist only in Qt6, so calling them directly raised AttributeError on every
click in QGIS 3.44 (reported from a user's Windows install). Resolve event
positions through ``qt_compat.event_point`` instead.
"""
import pathlib
import re

from mapdex_qgis.qt_compat import event_point

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis"
QT6_ONLY = re.compile(r"\.(position|globalPosition|scenePosition)\(\)")


class _Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _PointF:
    def __init__(self, x, y):
        self._point = _Point(int(x), int(y))

    def toPoint(self):  # noqa: N802 - mirrors Qt
        return self._point


class Qt5MouseEvent:
    def pos(self):
        return _Point(3, 4)


class Qt6MouseEvent:
    def position(self):
        return _PointF(5.6, 7.2)

    def pos(self):  # deprecated in Qt6; must not be preferred
        raise AssertionError("pos() used where position() exists")


def test_event_point_uses_pos_on_qt5():
    point = event_point(Qt5MouseEvent())
    assert (point.x, point.y) == (3, 4)


def test_event_point_prefers_position_on_qt6():
    point = event_point(Qt6MouseEvent())
    assert (point.x, point.y) == (5, 7)


def test_no_direct_qt6_only_event_position_calls():
    offenders = []
    for path in SOURCE.rglob("*.py"):
        if "_vendor" in path.parts or path.name == "qt_compat.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if QT6_ONLY.search(line):
                offenders.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
    assert not offenders, (
        "Qt6-only event position API called directly; use qt_compat.event_point:\n"
        + "\n".join(offenders)
    )
