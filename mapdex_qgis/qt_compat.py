"""Qt5 (QGIS 3) / Qt6 (QGIS 4) enum helpers.

PyQt6 requires scoped enums (``Qt.DockWidgetArea.LeftDockWidgetArea``).
PyQt5 accepts both scoped and flat forms. Always resolve through these
helpers so the plugin loads on QGIS 3.34+ and QGIS 4.x.
"""
from __future__ import annotations


def enum_member(owner, *names):
    """Resolve ``owner.A.B`` or fall back through ``owner.B``, ``owner.A``, …"""
    # Prefer fully scoped path (Qt6 / modern PyQt5).
    current = owner
    try:
        for name in names:
            current = getattr(current, name)
        return current
    except AttributeError:
        pass
    # Flat Qt5 fallback: last name on owner (e.g. Qt.LeftDockWidgetArea).
    last = names[-1]
    if hasattr(owner, last):
        return getattr(owner, last)
    raise AttributeError(
        "Qt enum not found: {owner}.{path}".format(
            owner=getattr(owner, "__name__", owner),
            path=".".join(names),
        )
    )
