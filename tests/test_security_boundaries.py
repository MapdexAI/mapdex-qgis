"""Plugin-side boundaries found by the pre-release security review.

The vendored Nivo package holds its own half (tests/test_security_boundaries.py
in mapdex/packages/nivo). These are the plugin's half: where a request's text
could reach the filesystem, or a processing parameter, as something other than
what the user loaded.
"""
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_qgis_stubs():
    # Only what importing qgis_runtime needs; every QGIS call it makes is
    # imported inside the function that makes it.
    core = sys.modules.get("qgis.core") or types.ModuleType("qgis.core")
    qgis = sys.modules.get("qgis") or types.ModuleType("qgis")
    qgis.core = core
    sys.modules.setdefault("qgis", qgis)
    sys.modules.setdefault("qgis.core", core)


_install_qgis_stubs()

from mapdex_qgis.qgis_runtime import confined_export_path, private_export_directory  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
RUNTIME = (ROOT / "mapdex_qgis" / "qgis_runtime.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("requested, expected", [
    ("plan.pdf", "plan.pdf"),
    ("\\\\attacker.example\\share\\plan.pdf", "plan.pdf"),
    ("C:\\Users\\victim\\Documents\\contract.pdf", "contract.pdf"),
    ("/home/user/.config/autostart/evil.png", "evil.png"),
    ("../../../etc/cron.d/job.tif", "job.tif"),
    ("site plan (final).PDF", "site_plan__final_.pdf"),
    ("..", "__"),
])
def test_an_export_is_written_by_name_inside_the_export_directory(tmp_path, requested, expected):
    destination = confined_export_path(str(tmp_path), requested)
    assert os.path.dirname(destination) == str(tmp_path)
    assert os.path.basename(destination) == expected


def test_the_export_directory_belongs_to_this_user_only():
    directory = private_export_directory()
    assert os.path.isdir(directory)
    assert private_export_directory() == directory
    if os.name == "posix":
        assert oct(os.stat(directory).st_mode & 0o777) == oct(0o700)


def _method(source, name):
    start = source.index("    def {}(".format(name))
    following = source.find("\n    def ", start + 1)
    return source[start:following if following != -1 else len(source)]


def test_a_second_layer_is_resolved_against_the_project_before_processing_sees_it():
    body = _method(PLUGIN, "_run_processing_operation")
    assert "resolve_target_layer(" in body
    assert body.index("resolve_target_layer(") < body.index("build_algorithm_parameters(")


def test_a_sheet_is_exported_only_inside_the_private_directory():
    body = _method(RUNTIME, "compose_sheet")
    assert "confined_export_path(private_export_directory(), export_path)" in body
    assert "exportToPdf(export_path" not in body and "exportToImage(export_path" not in body


def test_a_layer_export_uses_the_private_directory():
    body = _method(RUNTIME, "export_layer")
    assert "private_export_directory()" in body
    assert "mapdex-exports\")" not in body


def test_a_confirmation_names_the_file_a_request_touches():
    body = _method(PLUGIN, "_confirm_capability")
    assert 'endswith("path")' in body
    assert "self._confirm_capability(\n                capability_id, summary, request.get(\"params\"))" in PLUGIN
