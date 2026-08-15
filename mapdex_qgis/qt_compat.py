"""Qt5 (QGIS 3) / Qt6 (QGIS 4) enum helpers.

PyQt6 requires scoped enums (``Qt.DockWidgetArea.LeftDockWidgetArea``).
PyQt5 accepts both scoped and flat forms. Always resolve through these
helpers so the plugin loads on QGIS 3.34+ and QGIS 4.x.
"""
from __future__ import annotations

try:
    from qgis.PyQt.QtGui import QAction  # Qt6 (QGIS 4)
except (ImportError, ModuleNotFoundError):
    try:
        from qgis.PyQt.QtWidgets import QAction  # Qt5 (QGIS 3)
    except (ImportError, ModuleNotFoundError):
        try:
            from PyQt6.QtGui import QAction
        except (ImportError, ModuleNotFoundError):
            try:
                from PyQt5.QtWidgets import QAction
            except (ImportError, ModuleNotFoundError):
                QAction = None


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


def qgis_version(application_cls, qgis_cls=None):
    """Return the QGIS version across QGIS 3 and QGIS 4 Python APIs.

    QGIS 3 exposed ``QgsApplication.qgisVersion()`` in common builds. QGIS 4
    removed that accessor; the public version constant lives on ``Qgis``.
    Keep the lookup centralized so context collection does not crash when a
    binding drops or moves a version API.
    """
    version_fn = getattr(application_cls, "qgisVersion", None)
    if callable(version_fn):
        value = version_fn()
        if value:
            return str(value)
    if qgis_cls is not None:
        for name in ("QGIS_VERSION", "QGIS_RELEASE_NAME"):
            value = getattr(qgis_cls, name, "")
            if value:
                return str(value)
    for name in ("applicationVersion", "version"):
        version_fn = getattr(application_cls, name, None)
        if callable(version_fn):
            value = version_fn()
            if value:
                return str(value)
    return ""
