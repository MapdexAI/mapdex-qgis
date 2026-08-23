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
# So the reading goes first. It needs neither - it is local, measured, and
# costs no model call - which means the screen has already said something true
# about the file in front of them before it asks for anything.
FIRST_OPEN_TITLE = "Nivo reads the map you already have open."
FIRST_OPEN_PROMPT = "To ask Nivo something, choose where it thinks:"
FIRST_OPEN_FREE = "That reading needed no account."

# The two are not equals and are not drawn as equals. "Use your key" would be
# the wrong words for a GIS technician - hard rule 22 keeps developer jargon
# off the end-user screen - so it says what it is, and names the providers so
# it is recognisable.
CONNECT_PROMISE = "Free account. Georeference, digitize, validate and batch run here."
OWN_MODEL_LABEL = "Use my own AI model"
OWN_MODEL_PROMISE = (
    "OpenAI, Anthropic, Gemini, or a model running on this machine. "
    "Mapdex work stays unavailable."
)


def is_first_open(connected: bool, has_provider: bool) -> bool:
    """Has this install chosen where Nivo thinks yet?

    DERIVED, never a stored "has seen onboarding" flag. A flag goes stale, and
    once spent it cannot come back - a defect class this repository has already
    paid for. Disconnect and remove the key and you genuinely are a new user
    again, which is the honest answer rather than a convenient one.
    """
    return not connected and not has_provider


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
    if not connected:
        blocked = "Connect Mapdex to ask Nivo, or set your own model provider under Settings."
        return {
            "engine": "none",
            "line": "Not connected. " + blocked,
            "can_ask": False,
            "blocked_reason": blocked,
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


def disconnect_notice(runtime: Mapping[str, Any] | None) -> str:
    """What Disconnect reports, including what it did NOT stop.

    "Disconnected." full stop, above an assistant that keeps answering, is the
    sentence that made a working design look like a leak.
    """
    if is_byok(runtime):
        return (
            "Disconnected from Mapdex. Nivo keeps answering with {}; remove that key "
            "under Settings to stop it too.".format(
                engine_name(str((runtime or {}).get("provider") or ""))
            )
        )
    return "Disconnected. Connect again to ask Nivo and to start tasks."
