"""What the panel says about the session it is in, decided in one place.

Three surfaces had to agree about one fact and none of them talked to each
other. The header said "Not connected". The Nivo tab said nothing at all. The
settings panel, collapsed by default, held the only sentence that named the
engine actually answering.

So a user who had pasted a provider key, pressed Disconnect, and kept getting
answers had every reason to read that as a leak. It was not one: a BYOK turn
never reaches Mapdex in the first place, which is exactly why disconnecting
from Mapdex could not stop it. The panel simply never said which of the two
engines was running, and it showed the stored provider settings as GREY
PLACEHOLDER TEXT, which reads as an empty field rather than as a stored value.

This module owns those sentences. It imports no Qt and no QGIS, so every line
the user reads is decided somewhere a test can reach, instead of being
assembled across three widget callbacks that can drift apart.
"""
from __future__ import annotations

from typing import Any, Mapping

from ._vendor.nivo.providers import RUNTIME_BYOK

# The provider choices, owned here rather than in the combo box, because the
# prose below has to name the same thing the dropdown does. Two lists is how a
# panel comes to call something "OpenAI" in one place and "openai" in another.
PROVIDER_CHOICES = (
    ("Mapdex (hosted, uses your plan)", ""),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gemini", "gemini"),
    ("OpenAI-compatible endpoint", "openai_compatible"),
    ("Ollama (local)", "ollama"),
)

_MENU_LABELS = {value: label for label, value in PROVIDER_CHOICES}

# How a provider is named INSIDE a sentence, which is not always its menu
# label: "Answering with your own Ollama (local)" is not English.
#
# Naming the vendor here is deliberate and is not the leak `byok.mask_vendor`
# guards against. That rule takes the vendor out of FAILURE text, where the
# name is our routing showing through. Here the user picked this entry from a
# dropdown themselves, and telling them which of their own keys is being spent
# is the whole point of the line.
_ENGINE_NAMES = {
    "openai": "your own OpenAI key",
    "anthropic": "your own Anthropic key",
    "gemini": "your own Google Gemini key",
    "openai_compatible": "your own endpoint",
    "ollama": "your local model",
}

# What a turn that never contacts Mapdex cannot do. `byok.session_allowance`
# is the registry fact; this is the same fact in the user's vocabulary, and it
# holds whether or not a Mapdex session exists, because a BYOK turn is scoped
# to the offline capabilities either way.
MAPDEX_WORK = "Georeference, digitize, validate, batch and review"

# QSettings keys that belong to the Mapdex session rather than to this install.
# Disconnecting ends the session, so these end with it: a recent-task list left
# behind offers Resume on batches the next token cannot read, and if the next
# connection is a DIFFERENT account it is another workspace's task list sitting
# in this panel.
#
# The provider key is deliberately absent. It is the user's own credential for
# their own account somewhere else; ending a Mapdex session is not a reason to
# throw it away, and doing so silently would be the same overreach in the
# opposite direction.
SESSION_SCOPED_SETTINGS = ("mapdex/project_id", "mapdex/recent_tasks")

# The two pages that hold nothing but Mapdex session state. Shown while
# disconnected they still listed the previous session's project, its selected
# source and its recent tasks, with a live Resume button whose batch the next
# token cannot read.
TASK_LOCKED_NOTICE = (
    "Connect Mapdex to start a georeference, digitize or validate task on a layer or file."
)
JOBS_LOCKED_NOTICE = "Connect Mapdex to see your tasks, their progress and their results."


# ---------------------------------------------------------------------------
# First open
# ---------------------------------------------------------------------------
#
# Showing the choice and nothing else would make the panel a wall in front of a
# product nobody has seen, and it asks a question that cannot be answered yet:
# Mapdex or your own model is a billing and privacy decision, and the person
# reading it does not know what either would do for them.
#
# First open is a focused connection gate. A normal Nivo reading, runtime
# notice and prepared turns belong to the conversation after a provider can
# answer; showing them here made onboarding and chat look like one broken
# transcript. Keep the value proposition short enough to survive a 420 px dock.
FIRST_OPEN_TITLE = "Ask your map in plain language"
FIRST_OPEN_PROMPT = (
    "Nivo measures answers from your layers, styles them and runs QGIS tools "
    "after you say yes. Send scanned maps to Mapdex and get them back placed."
)
# Three things that are true of the product, each checkable on first use. A
# list of features would be a brochure; these are the promises the person can
# hold the panel to.
CONNECT_POINTS = (
    "Numbers come from QGIS measurements, not guesses",
    "It asks before it changes or creates data",
    "Use Mapdex, your own API key or a local model",
)

# The line under the one button: what pressing it does next, so a browser
# opening is not a surprise.
CONNECT_PROMISE = "Opens your browser to sign in or create a free account."
# Kept as a separate name for the plugin's existing reference; there is one
# connection gate now, so the sentence is the same.
CONNECT_PROMISE_UPGRADE = CONNECT_PROMISE

# ---------------------------------------------------------------------------
# Which model answers in Nivo
# ---------------------------------------------------------------------------
#
# Three choices, as radio cards, mapped onto the provider values the stored
# settings already use. The vendor menu appears only under "My own API key",
# because the person choosing Mapdex or a local model has no vendor to pick.
MODEL_CHOICES = (
    ("mapdex", "Mapdex",
     "Included in your plan. What you ask about the map is sent to Mapdex."),
    ("own", "My own API key",
     "Questions go straight from QGIS to your provider. The key stays in QGIS's encrypted store."),
    ("local", "A local model",
     "Runs on your machine, for example with Ollama."),
)
MODEL_FIELDS_HINT = (
    "The key and model fields appear only when you choose your own key or a local model."
)
LOCAL_PROVIDER = "ollama"
KEY_PROVIDER_CHOICES = tuple(
    (label, value) for label, value in (
        ("OpenAI", "openai"),
        ("Anthropic", "anthropic"),
        ("Google Gemini", "gemini"),
        ("OpenAI-compatible endpoint", "openai_compatible"),
    )
)


def model_choice_for(provider: str) -> str:
    """Which of the three cards a stored provider value belongs to."""
    provider = str(provider or "")
    if not provider:
        return "mapdex"
    if provider == LOCAL_PROVIDER:
        return "local"
    return "own"


def model_fields_for(provider: str) -> dict[str, bool]:
    """Which fields the Settings view shows for a provider value.

    Mapdex shows none. A local model needs a model and an endpoint and no key.
    A vendor key needs the vendor, a model and the key; only a compatible
    endpoint also needs its address.
    """
    choice = model_choice_for(provider)
    return {
        "vendor": choice == "own",
        "model": choice != "mapdex",
        "endpoint": provider in ("openai_compatible", LOCAL_PROVIDER),
        "key": choice == "own",
    }


def is_first_open(connected: bool, has_provider: bool = False) -> bool:
    """Whether the connection gate owns the panel.

    DERIVED, never a stored "has seen onboarding" flag. A flag goes stale, and
    once spent it cannot come back - a defect class this repository has already
    paid for. Disconnect and remove the key and you genuinely are a new user
    again, which is the honest answer rather than a convenient one.
    """
    return not connected


def engine_name(provider: str) -> str:
    """How this provider is referred to inside a sentence."""
    return _ENGINE_NAMES.get(str(provider or ""), "your own model provider")


def is_byok(runtime: Mapping[str, Any] | None) -> bool:
    """True when the next turn goes to the user's own provider."""
    return str((runtime or {}).get("runtime") or "") == RUNTIME_BYOK


def assistant_engine(
    runtime: Mapping[str, Any] | None,
    connected: bool,
    project_id: str = "",
) -> dict[str, Any]:
    """Which engine answers the next turn, as one resolved answer.

    ``runtime`` must be the SAME resolution the turn itself takes
    (`plugin.assistant_runtime`). Resolving it a second time from the widgets
    is how the panel came to promise a path the turn never took.

    Returns the header line, whether Ask is live, and - when it is not - a
    reason naming every route out of the state rather than only the one we
    would prefer the user took.
    """
    runtime = runtime or {}
    if not connected:
        blocked = "Connect Mapdex to ask Nivo. You can choose your model provider after connecting."
        return {
            "engine": "none",
            "line": "Not connected. " + blocked,
            "can_ask": False,
            "blocked_reason": blocked,
        }
    if (
        str(runtime.get("runtime") or "") == "unavailable"
        and str(runtime.get("reason") or "") == "provider_credential_missing"
    ):
        provider = str(runtime.get("provider") or "")
        label = _MENU_LABELS.get(provider, provider or "Your model provider")
        blocked = "Add an API key for {}, or choose Mapdex (hosted) in Settings.".format(label)
        return {
            "engine": "none",
            "line": "{} is selected, but no key is available. {}".format(label, blocked),
            "can_ask": False,
            "blocked_reason": blocked,
        }
    if is_byok(runtime):
        name = engine_name(str((runtime or {}).get("provider") or ""))
        tail = (
            "{} are not part of this conversation: choose Mapdex (hosted) under "
            "Settings for those.".format(MAPDEX_WORK)
            if connected
            else "{} need a Mapdex connection.".format(MAPDEX_WORK)
        )
        return {
            "engine": "byok",
            "line": "Answering with {}. {}".format(name, tail),
            "can_ask": True,
            "blocked_reason": "",
        }
    if not str(project_id or ""):
        # The line carries the blockage too. It has to be sufficient on its own:
        # the panel's status line is a transient reply to what the user just
        # did, and a standing reason written there overwrites whatever the last
        # action reported - which is how "New chat" stopped saying where the
        # old conversation went.
        blocked = "Choose a project before asking Nivo."
        return {
            "engine": "mapdex",
            "line": "Answering with Mapdex, on your plan. " + blocked,
            "can_ask": False,
            "blocked_reason": blocked,
        }
    return {
        "engine": "mapdex",
        "line": "Answering with Mapdex, on your plan.",
        "can_ask": True,
        "blocked_reason": "",
    }


def key_state_line(provider: str, has_key: bool) -> str:
    """What the settings panel says about the key, as text rather than a hint.

    The stored key showed only as the placeholder "A key is stored. Type a new
    one to replace it.", and a placeholder is grey and reads as an instruction
    to fill an empty field. That is why a user with a working OpenAI key
    reported having configured no provider at all.
    """
    provider = str(provider or "")
    if not provider:
        return "No provider key stored. Nivo runs on your Mapdex plan."
    if provider == "ollama":
        return "A local endpoint needs no key. Nivo runs on your local model."
    if has_key:
        return "A key is stored for {}. Nivo uses it instead of your Mapdex plan.".format(
            _MENU_LABELS.get(provider, provider)
        )
    return "No key stored yet. Paste one, or choose Mapdex (hosted) to use your plan."


def send_enabled(can_ask: bool, busy: bool, text: str | None) -> bool:
    """Send is live only when there is a question to send.

    The send arrow is a filled blue control, and a screen carries one filled
    blue action. Lit over an empty field it competed with the action that did
    matter: a "Needs your OK" card's own button showed as a second blue on the
    same screen, and pressing the arrow there sent nothing.
    """
    return bool(can_ask) and not busy and bool(str(text or "").strip())


def disconnect_notice(runtime: Mapping[str, Any] | None) -> str:
    """What Disconnect reports, including what it did NOT stop.

    "Disconnected." full stop, above an assistant that keeps answering, is the
    sentence that made a working design look like a leak.
    """
    return "Disconnected. Connect again to ask Nivo and to start tasks."


def support_details(plugin_version: str, qgis_version: str, qt_version: str, system: str) -> str:
    """What "Copy details" puts on the clipboard: versions and the system only.

    No account, email, token, project or file: a person pastes this into a
    message to us, and it must be safe to paste anywhere.
    """
    return "\n".join((
        "Mapdex for QGIS {}".format(plugin_version or "unknown"),
        "QGIS {}".format(qgis_version or "unknown"),
        "Qt {}".format(qt_version or "unknown"),
        "System {}".format(system or "unknown"),
    ))
