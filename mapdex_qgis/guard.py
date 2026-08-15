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


def guarded(method: Callable[..., Any]) -> Callable[..., Any]:
    """Stop an exception in a Qt slot from terminating QGIS.

    The wrapped method returns None when it fails. Callers inside the plugin
    must therefore not depend on a return value for correctness of anything
    destructive; every guarded entry point is a UI action whose failure mode is
    "nothing happened, and the user was told".
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the boundary is the point
            report = getattr(self, "_report_unexpected", None)
            if callable(report):
                try:
                    report(getattr(method, "__name__", "action"), exc)
                except Exception:
                    # The reporter itself failed (a destroyed widget, say).
                    # Swallowing here is still better than aborting QGIS.
                    pass
            return None

    wrapper.__wrapped__ = method
    wrapper.__mapdex_guarded__ = True
    return wrapper


def is_guarded(function: Any) -> bool:
    return bool(getattr(function, "__mapdex_guarded__", False))
