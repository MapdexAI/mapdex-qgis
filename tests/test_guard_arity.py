"""Qt argument arity through the error boundary.

QAction.triggered and QPushButton.clicked both pass a boolean `checked`. PyQt
normally inspects the slot and passes only as many arguments as it declares -
but it inspects the DECORATOR's wrapper, and functools.wraps does not copy a
signature. A `*args` wrapper therefore reads as "accepts anything", so Qt began
passing `checked` into every parameterless handler and every button in the
plugin raised TypeError. The panel opened onto nothing and no control worked.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.guard import guarded  # noqa: E402


class Slots:
    """Handlers shaped like the real ones connected to Qt signals."""

    def __init__(self):
        self.calls = []
        self.reported = []

    def _report_unexpected(self, action, exc):
        self.reported.append((action, exc))

    @guarded
    def no_args(self):
        self.calls.append("no_args")
        return "ok"

    @guarded
    def one_optional(self, index=None):
        self.calls.append(index)
        return index

    @guarded
    def takes_varargs(self, *values):
        self.calls.append(values)
        return values

    @guarded
    def raises_type_error_inside(self):
        self.calls.append("attempt")
        raise TypeError("a genuine bug inside the handler")


def test_a_signal_may_pass_checked_to_a_parameterless_slot():
    host = Slots()
    assert host.no_args(False) == "ok"
    assert host.calls == ["no_args"]
    assert host.reported == []


def test_extra_arguments_are_dropped_not_forwarded():
    host = Slots()
    assert host.one_optional(3, "unexpected", "extra") == 3
    assert host.calls == [3]


def test_a_slot_that_wants_the_argument_still_receives_it():
    host = Slots()
    host.one_optional(7)
    assert host.calls == [7]


def test_a_varargs_slot_keeps_everything():
    host = Slots()
    assert host.takes_varargs(1, 2, 3) == (1, 2, 3)


def test_a_genuine_type_error_runs_the_handler_once_and_is_reported():
    # Catching TypeError and retrying with fewer arguments cannot tell a Qt
    # arity mismatch from a real TypeError inside the method, so it would run
    # the handler twice and repeat side effects it had already performed.
    # Truncation happens before the call, so this runs exactly once.
    host = Slots()
    assert host.raises_type_error_inside(False) is None
    assert host.calls == ["attempt"]
    assert len(host.reported) == 1
    assert isinstance(host.reported[0][1], TypeError)


def test_keyword_arguments_are_preserved():
    host = Slots()
    assert host.one_optional(index=5) == 5
