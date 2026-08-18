"""An error boundary for every Qt entry point.

A QGIS plugin must never be able to close QGIS. Under PyQt5.5+ and PyQt6 an
unhandled Python exception inside a slot does not merely print a traceback: the
default excepthook for slots calls ``abort()``, which terminates the host
process. So one unguarded ``AttributeError`` in a button handler is a crash of
the user's whole session, with unsaved project work in it.

:func:`guarded` wraps a method so that any exception is reported and swallowed.
It is deliberately broad - catching ``Exception`` is normally a smell, but here
the alternative is killing the application, and the boundary reports what it
caught rather than hiding it.

``BaseException`` is NOT caught: ``KeyboardInterrupt`` and ``SystemExit`` must
still travel, or QGIS could not shut down while a handler is running.

The decorator preserves the bound method, so a handler connected with
``signal.connect(self.method)`` can still be disconnected in ``unload()``. That
matters: a lambda cannot be disconnected reliably, and a stale connection into a
destroyed panel is itself a crash.
"""
from __future__ import annotations

import functools
import inspect
import traceback
from typing import Any, Callable

MAX_REPORTED_CHARS = 2000


def describe_exception(exc: BaseException) -> str:
    """A short, user-facing description that never leaks a stack trace."""
    name = type(exc).__name__
    detail = str(exc).strip()
    if not detail:
        return name
    return "{}: {}".format(name, detail[:200])


def format_traceback(exc: BaseException) -> str:
    """The full trace, for the QGIS log only."""
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )[:MAX_REPORTED_CHARS]


def log_debug(context: str, exc: BaseException) -> None:
    """Record a failure the plugin deliberately continued past.

    Several paths here keep working when a QGIS or Qt call fails: a provider
    whose registry is broken must not cost the user every other connection, one
    of several routes to the active layer may be missing on a given build, a
    widget may already have been destroyed. Continuing is right; going silent is
    not, because a swallowed exception is a failure nobody can find. These sites
    write the reason to the Mapdex log panel instead of dropping it.

    This is diagnostic, not the error boundary: :func:`guarded` still owns
    anything that reaches the user. Nothing here raises - it is called from
    inside ``except`` blocks, including during teardown, so a logging failure
    must never become the exception that escapes.
    """
    try:
        from qgis.core import Qgis, QgsMessageLog  # noqa: PLC0415 - QGIS-only import

        from .qt_compat import enum_member  # noqa: PLC0415 - avoids an import cycle at load

        QgsMessageLog.logMessage(
            "{}: {}".format(context, describe_exception(exc)),
            "Mapdex",
            enum_member(Qgis, "MessageLevel", "Info"),
        )
    except Exception:  # nosec B110
        # No QGIS (unit tests), or the log itself is gone (teardown). There is
        # nowhere left to write, and reporting a failure must not create one.
        pass


def _report_safely(report: Callable[..., Any], action: str, exc: BaseException) -> None:
    """Hand a caught failure to the plugin's reporter, whatever happens next."""
    try:
        report(action, exc)
    except Exception:  # nosec B110
        # The reporter itself failed (a destroyed widget, say), so there is
        # nothing left to report it to - log_debug would only add a second way
        # to fail. Swallowing here is still better than aborting QGIS.
        pass


def _max_positional_args(method: Callable[..., Any]) -> int | None:
    """How many positional arguments the method accepts, excluding ``self``.

    None means "no limit" - the method takes ``*args`` and can be handed
    whatever Qt sends.
    """
    try:
        spec = inspect.getfullargspec(method)
    except TypeError:
        return None
    if spec.varargs is not None:
        return None
    return max(0, len(spec.args) - 1)


def guarded(method: Callable[..., Any]) -> Callable[..., Any]:
    """Stop an exception in a Qt slot from terminating QGIS.

    The wrapped method returns None when it fails. Callers inside the plugin
    must therefore not depend on a return value for correctness of anything
    destructive; every guarded entry point is a UI action whose failure mode is
    "nothing happened, and the user was told".
    """

    limit = _max_positional_args(method)

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        # Qt signals pass extra arguments - QAction.triggered and
        # QPushButton.clicked both send a boolean `checked`. PyQt normally
        # inspects the slot and passes only as many arguments as it declares,
        # but it inspects THIS wrapper, and functools.wraps does not copy a
        # signature. A `*args` wrapper therefore reads as "accepts anything",
        # so Qt started passing `checked` into every parameterless handler and
        # every button in the plugin raised TypeError.
        #
        # Truncating here reproduces PyQt's own rule deterministically. The
        # alternative - catching TypeError and retrying with fewer arguments -
        # cannot tell a Qt arity mismatch from a TypeError raised inside the
        # method, so it would silently run a handler twice and repeat whatever
        # side effects it had already performed.
        if limit is not None and len(args) > limit:
            args = args[:limit]
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the boundary is the point
            report = getattr(self, "_report_unexpected", None)
            if callable(report):
                _report_safely(report, getattr(method, "__name__", "action"), exc)
            return None

    wrapper.__wrapped__ = method
    wrapper.__mapdex_guarded__ = True
    return wrapper


def is_guarded(function: Any) -> bool:
    return bool(getattr(function, "__mapdex_guarded__", False))
