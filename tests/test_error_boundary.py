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

from mapdex_qgis.guard import (  # noqa: E402
    describe_exception,
    format_traceback,
    guarded,
    is_guarded,
    log_debug,
)

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
# Recording what the plugin deliberately continued past
# --------------------------------------------------------------------------

def test_logging_a_swallowed_failure_never_raises_without_qgis():
    # log_debug is called from inside except blocks, including during teardown.
    # Outside QGIS there is no log to write to, and that must not become the
    # exception that escapes the handler it was called from.
    assert log_debug("doing something", Boom("gone")) is None


def test_logging_a_swallowed_failure_survives_a_broken_log(monkeypatch):
    import mapdex_qgis.guard as guard_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("the message log is gone")

    monkeypatch.setattr(guard_module, "describe_exception", explode)
    assert log_debug("doing something", Boom("gone")) is None


def test_the_active_layer_search_records_why_a_route_failed():
    """Four routes to "the layer the user means"; a silent miss is unanswerable.

    "Nivo says no layer is active" with a layer plainly selected used to leave
    no trace at all. Bandit refuses a bare `pass` here; this states the positive
    requirement, which is that the reason reaches the log.
    """
    body = PLUGIN[PLUGIN.index("def _active_qgis_layer"):PLUGIN.index("def _nivo_layer_for_action")]
    assert body.count("log_debug(") == 4


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
        # Small QObject bridges own their Qt slots and do not have the plugin's
        # error reporter. Their purpose is thread delivery; the plugin callback
        # they invoke remains guarded at the actual UI boundary.
        if "@pyqtSlot(str)\n    def {}(".format(name) in PLUGIN:
            continue
        assert "@guarded\n    def {}(".format(name) in PLUGIN, (
            "signal connected to unguarded handler: {}".format(name)
        )


def test_the_reporter_does_not_depend_on_the_plugins_own_panel():
    """An error boundary must not report through the thing that broke.

    When the panel fails to build, _set_status and _announce fail too, so a
    reporter that only used them delivered nothing at all - the user clicked the
    menu entry and QGIS did nothing, with the real exception swallowed.
    """
    body = PLUGIN[PLUGIN.index("def _report_unexpected"):PLUGIN.index("def _ensure_dock")]
    assert "QgsMessageLog.logMessage" in body
    assert "messageBar()" in body, "no channel independent of our dock"
    assert "QMessageBox.critical" in body, "no last resort when the panel is gone"


def test_each_reporting_channel_is_independently_protected():
    body = PLUGIN[PLUGIN.index("def _report_unexpected"):PLUGIN.index("def _ensure_dock")]
    # One failing channel must not stop the next from being tried.
    assert body.count("except Exception:") >= 3
    assert body.count("if not delivered:") == 2
