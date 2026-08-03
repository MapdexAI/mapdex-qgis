"""Background tasks must not outlive their owner unsafely.

A QgsTask handed to the QGIS task manager is owned by C++, but if Python drops
its reference while the task is still queued, QGIS dies with a Windows access
violation inside QgsTask::processSubTasksForCompletion — no Python traceback,
just a crash at start-up. The completion callback is equally dangerous: it runs
after the work finishes, which can be after the panel was destroyed.
"""
import ast
import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py"
SOURCE = PLUGIN.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} not found".format(name))


def test_dispatched_tasks_are_referenced_until_they_report_back():
    body = ast.dump(_function("_task"))
    assert "_tasks" in body, "_task must keep a strong reference to the QgsTask"
    assert "addTask" in body


def test_completion_callback_bails_out_when_the_panel_is_gone():
    body = ast.dump(_function("_task"))
    assert "dock" in body, "the completion callback must check the panel still exists"
    assert "RuntimeError" in body, "the completion callback must tolerate destroyed widgets"


def test_unload_cancels_in_flight_tasks():
    body = ast.dump(_function("unload"))
    assert "_tasks" in body and "cancel" in body


def test_refresh_ui_is_safe_after_unload():
    body = ast.dump(_function("_refresh_ui"))
    assert "dock" in body, "_refresh_ui must return early once the dock is gone"
