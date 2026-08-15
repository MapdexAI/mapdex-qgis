"""The plugin must never be able to close QGIS.

Under PyQt5.5+ and PyQt6, an unhandled Python exception inside a slot does not
just print a traceback - the slot excepthook calls abort() and the host process
dies. One unguarded AttributeError in a button handler therefore takes down the
user's whole QGIS session, unsaved project included.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.guard import describe_exception, format_traceback, guarded, is_guarded  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")


class Boom(Exception):
    pass


class Host:
    def __init__(self):
        self.reported = []

    def _report_unexpected(self, action, exc):
        self.reported.append((action, exc))

    @guarded
    def explode(self):
        raise Boom("layer went away")

    @guarded
    def works(self, value):
        return value * 2

    @guarded
    def interrupted(self):
        raise KeyboardInterrupt


def test_an_exception_in_a_slot_is_contained_not_propagated():
    host = Host()
    assert host.explode() is None
    assert host.reported and isinstance(host.reported[0][1], Boom)
    assert host.reported[0][0] == "explode"


def test_a_working_handler_is_unaffected():
    assert Host().works(21) == 42


def test_shutdown_signals_still_travel():
    # Catching BaseException would stop QGIS closing while a handler runs.
    try:
        Host().interrupted()
    except KeyboardInterrupt:
        return
    raise AssertionError("KeyboardInterrupt was swallowed")


def test_a_failing_reporter_still_cannot_abort_qgis():
    class BrokenHost(Host):
        def _report_unexpected(self, action, exc):
            raise RuntimeError("the panel is already destroyed")

    assert BrokenHost().explode() is None


def test_a_host_without_a_reporter_is_still_safe():
    class Bare:
        @guarded
        def explode(self):
            raise Boom("no reporter here")

    assert Bare().explode() is None


def test_the_guard_preserves_the_bound_method_for_disconnect():
    # unload() disconnects handlers by identity; a lambda cannot be
    # disconnected, and a stale connection into a destroyed panel is itself a
    # crash. functools.wraps keeps the method usable as a slot.
    host = Host()
    assert host.explode.__name__ == "explode"
    assert is_guarded(Host.explode)


def test_reported_text_is_short_and_carries_no_stack_trace():
    exc = Boom("layer went away")
    assert describe_exception(exc) == "Boom: layer went away"
    assert "Traceback" not in describe_exception(exc)


def test_the_full_trace_is_available_for_the_log_and_is_bounded():
    try:
        raise Boom("x" * 5000)
    except Boom as exc:
        trace = format_traceback(exc)
    assert "Boom" in trace
    assert len(trace) <= 2000


# --------------------------------------------------------------------------
# Every Qt entry point in the plugin must carry the boundary
# --------------------------------------------------------------------------

# Handlers connected to a Qt signal, run from a QgsTask callback, or called by
# the QGIS plugin lifecycle.
REQUIRED = (
    "initGui", "unload", "connect", "disconnect",
    "ask_nivo", "stop_nivo", "_nivo_composed",
    "_apply_nivo_action", "_confirm_nivo_action", "_run_processing_operation",
    "_create_scratch_layer", "_zoom_to_server_extent",
    "run_input", "import_results", "cancel_batch", "retry_failed",
    "open_review", "open_project", "resume_recent",
    "save_connection_settings", "clear_assistant_key",
    "_on_current_layer_changed", "_batch_updated", "_results_imported",
)


def test_every_qt_entry_point_is_guarded():
    missing = [name for name in REQUIRED if "@guarded\n    def {}(".format(name) not in PLUGIN]
    assert not missing, "unguarded Qt entry points: {}".format(missing)


def test_the_reporter_never_re_raises():
    body = PLUGIN[PLUGIN.index("def _report_unexpected"):PLUGIN.index("def _ensure_dock")]
    # Match a raise STATEMENT, not the word in the docstring that explains the
    # rule. A prose match here would make the guard pass or fail by accident.
    statements = [line.strip() for line in body.splitlines() if line.strip().startswith("raise ")]
    assert not statements, "the reporter re-raises: {}".format(statements)
    # Both the log and the user-facing message are themselves protected: a
    # destroyed widget must not turn error reporting into a second crash.
    assert body.count("except Exception:") >= 2


def test_signal_connections_use_guarded_bound_methods():
    # A connection to an unguarded inner helper would bypass the boundary.
    for match in re.finditer(r"\.connect\(self\.(\w+)\)", PLUGIN):
        name = match.group(1)
        if name.startswith("_") and "def {}(".format(name) not in PLUGIN:
            continue
        assert "@guarded\n    def {}(".format(name) in PLUGIN, (
            "signal connected to unguarded handler: {}".format(name)
        )
