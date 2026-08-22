"""One assistant turn driven against the user's own model provider.

The hosted turn is a single round trip: build the map context, POST it to
Mapdex /v1/compose, render the reply. A BYOK turn is a *loop* - the model names
a capability, trusted code runs it, the observation goes back to the model - and
inside QGIS that loop straddles two threads. iface, the map canvas and the layer
tree are main-thread only, so every capability execution and every confirmation
dialog has to happen there; the provider round trip must not, or QGIS freezes
for the length of a model call.

AgentSession already separates those halves: prompt() hands out the call to
make, advance() consumes the reply. This module is the small amount of state
between them, plus the one decision that must never be taken silently.

**A provider failure never falls back to Mapdex on its own.** Falling back would
post the user's map context to Mapdex moments after the settings panel promised
it would not - the same defect class as the copy this feature already shipped
once, only worse, because it would be the data moving rather than a sentence.
The hosted path is offered as a question and taken only on a yes, and the offer
states that nothing has been sent yet.

Nothing here imports QGIS, so the loop, the capability allowance and the
fallback rule are all testable without a host application. The caller owns the
threading and nothing else.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from ._vendor.nivo.agent import STATE_FAILED, AgentSession
from ._vendor.nivo.capabilities import CLIENT_QGIS, offline_capability_ids
from ._vendor.nivo.providers import PROVIDERS, RUNTIME_BYOK, redact

# Which company answers a turn is our routing, not something to announce in the
# panel. Every provider names its vendor in its own errors - "openai returned
# HTTP 401", "Could not reach gemini" - which is exactly right for a log and
# was being handed to the user verbatim.
#
# Built from the provider registry rather than a literal list, so a provider
# added to the package cannot quietly start leaking through this notice. The
# lookahead spares "OpenAI-compatible", which is the label the user picked in
# the settings dropdown themselves and is therefore not a leak.
_VENDOR_NAMES = re.compile(
    r"\b(?:{})\b(?![-_ ]?compatible)".format(
        "|".join(re.escape(name) for name in sorted(PROVIDERS, key=len, reverse=True)
                 if "compatible" not in name)),
    re.IGNORECASE,
)


def mask_vendor(text: str) -> str:
    """Take the vendor's name out of a sentence the user will read.

    User-facing copy only. The exception keeps its own text, so a support log
    still says which provider failed.
    """
    return _VENDOR_NAMES.sub("your provider", str(text))

# Said when the user declined the offer, because "your provider could not
# answer" alone leaves open whether we tried Mapdex anyway.
NOTHING_SENT = "Nothing was sent to Mapdex."
HANDED_OVER = "Asking Mapdex instead."


def is_byok(runtime: Mapping[str, Any] | None) -> bool:
    """True when this turn goes to the user's own provider."""
    return str((runtime or {}).get("runtime") or "") == RUNTIME_BYOK


def needs_mapdex_account(runtime: Mapping[str, Any] | None) -> bool:
    """Whether a Mapdex session is a precondition for this turn.

    A BYOK turn reaches the user's own provider and may run only capabilities
    that execute on this machine, so it needs no account: refusing it without
    one is what made the feature unreachable for exactly the people it is
    offered to. Every other runtime IS a Mapdex call, so the gate stays.
    """
    return not is_byok(runtime)


def session_allowance(client: str = CLIENT_QGIS) -> frozenset:
    """The capabilities a BYOK session may plan from.

    Everything this client can do locally, and nothing that executes as a
    Mapdex Run. Georeferencing, digitization, validation, batch and run review
    are the product and they run server-side; a turn that never contacts Mapdex
    cannot perform them, so the catalogue says so instead of letting the model
    choose one and discover the boundary as a failure.
    """
    return offline_capability_ids(client)


def provider_failure_notice(error: BaseException | str) -> str:
    """What to tell the user, with anything credential-shaped masked."""
    detail = mask_vendor(redact(error).strip())
    if not detail and isinstance(error, BaseException):
        detail = type(error).__name__
    return "Your model provider could not answer: {}".format(detail or "no reason given")


def hosted_fallback(
    notice: str,
    ask: Callable[[str], bool] | None,
    run_hosted: Callable[[], Any] | None,
) -> bool:
    """Offer Mapdex after a provider failure. True only when it was taken.

    The whole rule lives here so there is one place to read and one place to
    test: no offer, no hosted turn; no yes, no hosted turn. A caller that wants
    to send the question to Mapdex after the user's own provider failed has to
    come through this function, and this function always asks first.
    """
    if run_hosted is None or ask is None:
        return False
    if not ask(notice):
        return False
    run_hosted()
    return True


def provider_failed(
    error: BaseException | str,
    ask: Callable[[str], bool] | None = None,
    run_hosted: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """The terminal outcome of a turn whose provider could not answer."""
    notice = provider_failure_notice(error)
    handed = hosted_fallback(notice, ask, run_hosted)
    return {
        "kind": "provider_failed",
        "state": STATE_FAILED,
        "handed_to_mapdex": handed,
        "message": "{} {}".format(notice, HANDED_OVER if handed else NOTHING_SENT),
    }


class ByokTurn:
    """One objective pursued against the user's provider, a step at a time.

    The caller alternates: next_prompt() on the thread that may read QGIS,
    provider_reply() on a worker, deliver() back on the first one. deliver()
    returns a terminal outcome or None, and None means go round again - the
    model executed something and gets to choose what comes next.
    """

    def __init__(
        self,
        session: AgentSession,
        ask_to_use_hosted: Callable[[str], bool] | None = None,
        run_hosted: Callable[[], Any] | None = None,
    ):
        self.session = session
        self.ask_to_use_hosted = ask_to_use_hosted
        self.run_hosted = run_hosted
        self.handed_to_mapdex = False

    def start(self, objective: str, context: Mapping[str, Any] | None = None) -> "ByokTurn":
        self.session.start(objective, context)
        return self

    def next_prompt(self) -> tuple[str, Sequence[Mapping[str, str]]] | None:
        """The next model call, or None when the step budget is spent."""
        if self.session.budget_spent():
            return None
        return self.session.prompt()

    def provider_reply(self, system: str, messages: Sequence[Mapping[str, str]]) -> str:
        """The only part of a turn that belongs on a worker thread."""
        return self.session.provider.complete(system, messages)

    def deliver(self, reply: Any) -> dict[str, Any] | None:
        """Consume one model reply. This runs capabilities, so: main thread."""
        return self.session.advance(str(reply or ""))

    def exhausted(self) -> dict[str, Any]:
        return self.session.exhausted()

    def provider_failed(self, error: BaseException | str) -> dict[str, Any]:
        """End the turn on a provider failure, offering Mapdex explicitly."""
        self.session.state = STATE_FAILED
        outcome = provider_failed(error, self.ask_to_use_hosted, self.run_hosted)
        self.handed_to_mapdex = bool(outcome["handed_to_mapdex"])
        return outcome
