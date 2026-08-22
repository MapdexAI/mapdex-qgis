"""Guard against Qt5-only enum access.

PyQt6 (QGIS 4) requires scoped enums: ``QMessageBox.StandardButton.Yes``, not
``QMessageBox.Yes``. The flat form raises AttributeError at runtime, in a
dialog the unit tests never open, so the source is scanned instead. Resolve
every enum through ``qt_compat.enum_member``, or through the named helpers
``geometry_type`` and ``layer_type`` for the two read all over the plugin.

This guard used to name six classes, taken from the findings of one
plugins.qgis.org scan. A guard built from last time's list holds for last
time's list: the next scan returned nineteen flat accesses on five classes
nobody had thought to add - QgsMapLayer, QgsWkbTypes, QgsRubberBand,
QgsEditFormConfig, QgsLayoutExporter - every one of them an AttributeError
waiting on QGIS 4. So the rule is inverted. ANY capitalised attribute on a
Qt or QGIS class is treated as an enum member, and the exceptions are listed
by name with the reason each one is not.
"""
import pathlib
import re

from mapdex_qgis.qt_compat import qgis_version

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis"

# Capitalised attributes that are NOT enum members. Each is a nested class or
# the scope name itself, which is what a correctly scoped access looks like.
NOT_AN_ENUM = {
    # Nested option/settings classes.
    "QgsVectorFileWriter.SaveVectorOptions",
    "QgsLayoutExporter.ImageExportSettings",
    "QgsLayoutExporter.PdfExportSettings",
    "QgsColorRampShader.ColorRampItem",
    # Scope names: reaching one of these IS the scoped form.
    "QMessageBox.Icon", "QMessageBox.StandardButton", "QMessageBox.ButtonRole",
    "QgsVectorFileWriter.WriterError", "QgsVectorFileWriter.ActionOnExistingFile",
    "QgsVectorFileWriter.VectorFormatOption",
    "QFileDialog.Option", "QFileDialog.FileMode", "QFileDialog.AcceptMode",
    "QgsTask.Flag", "QgsTask.TaskStatus", "QgsTask.CanCancel",
    "QDockWidget.DockWidgetFeature",
    "Qgis.MessageLevel",
    "QgsWkbTypes.GeometryType", "QgsMapLayer.LayerType",
    "QgsRubberBand.IconType", "QgsEditFormConfig.FeatureFormSuppress",
    "QgsLayoutExporter.ExportResult", "QgsFeatureRequest.Flag",
    # QVariant::Type was REMOVED in Qt6, so QVariant.Double is not merely
    # unscoped, it does not exist. Resolve a field's storage type through
    # qt_compat.field_type, which tries QMetaType.Type first.
    "QVariant.Type",
}

# Any Qt or QGIS class, and any attribute on it that starts with a capital.
# A method is lower-case by Qt convention and is skipped below, so what is
# left is an enum member or a nested type, and the nested types are named
# above rather than inferred.
PATTERN = re.compile(r"\b(Q[A-Za-z0-9_]+)\.([A-Z][A-Za-z0-9_]*)")
BARE_QT = re.compile(r"\bQt\.([A-Z][A-Za-z0-9_]*)")

# PyQt5.15 and PyQt6 both expose exec(); exec_() is the Python-2-era spelling
# that the repository's QGIS 4 check asks to be renamed.
EXEC_UNDERSCORE = re.compile(r"\.exec_\s*\(")

# generated_contracts.py is generated, and qt_compat.py is the helper that
# documents the scoped/flat forms in its own docstring.
SKIP = {"generated_contracts.py", "qt_compat.py"}


def _sources():
    return sorted(path for path in SOURCE.glob("*.py") if path.name not in SKIP)


def test_no_flat_qt5_enum_access():
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "enum_member(" in stripped:
                continue
            for owner, attribute in PATTERN.findall(line):
                if "{}.{}".format(owner, attribute) in NOT_AN_ENUM:
                    continue
                if attribute[:1].islower():
                    continue  # a method call, not an enum member
                offenders.append("{}:{}: {}.{}".format(path.name, number, owner, attribute))
    assert not offenders, "Use enum_member for Qt6-safe enums:\n" + "\n".join(offenders)


def test_no_bare_qt_namespace_enum():
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", "from ", "import ")) or "enum_member(" in stripped:
                continue
            for attribute in BARE_QT.findall(line):
                offenders.append("{}:{}: Qt.{}".format(path.name, number, attribute))
    assert not offenders, "Use enum_member(Qt, ...) instead:\n" + "\n".join(offenders)


def test_qgis_version_supports_qgis3_application_accessor():
    class Application:
        @staticmethod
        def qgisVersion():
            return "3.34.0"

    assert qgis_version(Application) == "3.34.0"


def test_qgis_version_supports_qgis4_qgis_constant():
    class Application:
        pass

    class QgisApi:
        QGIS_VERSION = "4.0.2-Norrköping"

    assert qgis_version(Application, QgisApi) == "4.0.2-Norrköping"


def test_qaction_is_not_imported_directly_from_qtwidgets():
    """In Qt6/PyQt6 (QGIS 4), QAction moved from QtWidgets to QtGui.

    All source files must import QAction from .qt_compat, never from QtWidgets.
    """
    offenders = []
    for path in _sources():
        content = path.read_text(encoding="utf-8")
        if "from qgis.PyQt.QtWidgets import" in content and "QAction" in content:
            # Check if QAction is inside the QtWidgets import block
            match = re.search(r"from qgis\.PyQt\.QtWidgets import \([^)]*QAction[^)]*\)", content)
            if match:
                offenders.append(f"{path.name}: imports QAction from qgis.PyQt.QtWidgets")
    assert not offenders, "Import QAction from .qt_compat instead:\n" + "\n".join(offenders)


def test_no_qt5_exec_underscore():
    """``exec_()`` is the Qt5 spelling the QGIS 4 check asks to be renamed.

    PyQt5.15 (QGIS 3.34) and PyQt6 both expose ``exec()``, so there is nothing
    to keep the old name for; a hasattr fallback only kept it in the source for
    the check to find.
    """
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if EXEC_UNDERSCORE.search(line):
                offenders.append("{}:{}: {}".format(path.name, number, line.strip()))
    assert not offenders, "Call exec() instead:\n" + "\n".join(offenders)


def test_the_scan_is_not_blind():
    """A guard that matches nothing passes for the wrong reason.

    The previous version of this file matched six class names, so it passed
    while nineteen flat accesses on five other classes shipped. This asserts
    the pattern still sees a known-bad line.
    """
    assert PATTERN.findall("if layer.type() == QgsMapLayer.VectorLayer:")
    assert PATTERN.findall("kind = QgsWkbTypes.PolygonGeometry")
    assert EXEC_UNDERSCORE.search("dialog.exec_()")
    assert not PATTERN.findall("QgsProject.instance().layerTreeRoot()")
