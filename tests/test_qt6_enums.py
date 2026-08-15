"""Guard against Qt5-only enum access.

PyQt6 (QGIS 4) requires scoped enums: ``QMessageBox.StandardButton.Yes``, not
``QMessageBox.Yes``. The flat form raises AttributeError at runtime, in a
dialog the unit tests never open, so the source is scanned instead. Resolve
every enum through ``qt_compat.enum_member``, which handles both bindings.
"""
import pathlib
import re

from mapdex_qgis.qt_compat import qgis_version

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis"

# Attributes on these classes that are NOT enum members: methods, nested enum
# type names (the scoped path itself), and helper classes.
ALLOWED = {
    "QMessageBox": {
        "about", "aboutQt", "critical", "information", "question", "warning",
        "Icon", "StandardButton", "ButtonRole",
    },
    "QgsVectorFileWriter": {
        "SaveVectorOptions", "writeAsVectorFormat", "writeAsVectorFormatV2",
        "writeAsVectorFormatV3", "supportedFiltersAndFormats", "driverForExtension",
        "WriterError", "ActionOnExistingFile", "VectorFormatOption",
    },
    "QFileDialog": {
        "getOpenFileName", "getOpenFileNames", "getSaveFileName",
        "getExistingDirectory", "Option", "FileMode", "AcceptMode",
    },
    "QgsTask": {"fromFunction", "Flag", "TaskStatus", "CanCancel"},
    "QDockWidget": {"DockWidgetFeature", "setWidget"},
}

PATTERN = re.compile(
    r"\b(QMessageBox|QgsVectorFileWriter|QFileDialog|QgsTask|QDockWidget)\.([A-Za-z_][A-Za-z0-9_]*)"
)
BARE_QT = re.compile(r"\bQt\.([A-Z][A-Za-z0-9_]*)")


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
                if attribute in ALLOWED.get(owner, set()):
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

