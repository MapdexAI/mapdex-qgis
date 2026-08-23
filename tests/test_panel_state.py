"""The panel must say which engine is answering, in every state.

A user pasted an OpenAI key, pressed Disconnect, and kept getting answers. They
reported it as a leak. It was not one: a BYOK turn never reaches Mapdex, so
disconnecting from Mapdex could not have stopped it - `byok.needs_mapdex_account`
says exactly that and is correct. What was wrong is that no surface said so.
The header read "Not connected", the Nivo tab said nothing at all, and the one
sentence naming the engine lived in a settings panel that is collapsed by
default, next to a stored key shown only as grey placeholder text.

So these tests hold two things shut: the state is stated wherever the user is,
and the words for it are decided once rather than in three widget callbacks.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mapdex_qgis import panel_state  # noqa: E402

BYOK = {"runtime": "byok", "provider": "openai"}
LOCAL = {"runtime": "byok", "provider": "ollama"}
HOSTED = {"runtime": "hosted", "provider": "mapdex"}


# -- the reported case -------------------------------------------------------

def test_a_disconnected_byok_session_still_answers_and_says_so():
    """The exact state in the bug report, now stated instead of contradicted."""
    engine = panel_state.assistant_engine(BYOK, connected=False)
    assert engine["can_ask"] is True, (
        "BYOK does not need a Mapdex account and the turn will run; a panel that "
        "disables Ask here would be lying in the other direction."
    )
    assert engine["engine"] == "byok"
    assert "your own OpenAI key" in engine["line"], engine["line"]


def test_the_disconnect_notice_says_what_it_did_not_stop():
    """"Disconnected." full stop, above a working assistant, is what read as a defect."""
    notice = panel_state.disconnect_notice(BYOK)
    assert "keeps answering" in notice, notice
    assert "your own OpenAI key" in notice, notice
    # And the way out, since the user's complaint was that they could not stop it.
    assert "remove that key" in notice, notice


def test_the_hosted_disconnect_notice_does_not_promise_a_working_assistant():
    notice = panel_state.disconnect_notice(HOSTED)
    assert "keeps answering" not in notice, notice


# -- what a blocked state must offer ----------------------------------------

def test_a_blocked_hosted_turn_names_both_routes_out():
    """Naming only the route we would prefer is how a funnel becomes a wall.

    A user who has their own key and no interest in an account is entitled to
    know that Settings is a way out of this state.
    """
    engine = panel_state.assistant_engine(HOSTED, connected=False)
    assert engine["can_ask"] is False
    reason = engine["blocked_reason"]
    assert "Connect Mapdex" in reason, reason
    assert "provider" in reason and "Settings" in reason, reason


def test_a_connected_session_with_no_project_is_blocked_for_its_own_reason():
    """Two different blocks had one message, so the fix for one never worked."""
    engine = panel_state.assistant_engine(HOSTED, connected=True, project_id="")
    assert engine["can_ask"] is False
    assert "project" in engine["blocked_reason"].lower(), engine["blocked_reason"]
    assert "Connect Mapdex" not in engine["blocked_reason"], (
        "a connected user was told to connect"
    )
    assert panel_state.assistant_engine(HOSTED, True, "proj_1")["can_ask"] is True


def test_a_blocked_line_states_its_own_reason():
    """The header line has to be sufficient on its own.

    The panel's status line is a transient reply to the user's last action.
    Writing a standing reason into it overwrote what that action reported -
    measured in the QGIS harness as "New chat" losing its own "the old
    conversation is in History" message. So the reason goes in the line.
    """
    for connected, project in ((False, ""), (True, "")):
        engine = panel_state.assistant_engine(HOSTED, connected, project)
        assert engine["can_ask"] is False
        assert engine["blocked_reason"] in engine["line"], engine


# -- the capability boundary is stated, not discovered as a failure ----------

def test_byok_states_the_work_it_cannot_do():
    """`byok.session_allowance` is offline-only whether or not an account exists.

    A user asking a BYOK conversation to georeference a sheet otherwise finds
    the boundary as a refusal, after spending a turn on their own key.
    """
    for connected in (True, False):
        line = panel_state.assistant_engine(BYOK, connected=connected)["line"]
        assert panel_state.MAPDEX_WORK in line, (connected, line)


def test_a_connected_byok_user_is_told_how_to_switch_rather_than_to_connect():
    line = panel_state.assistant_engine(BYOK, connected=True)["line"]
    assert "Mapdex (hosted)" in line, line
    assert "need a Mapdex connection" not in line, (
        "a connected user was told to connect: {!r}".format(line)
    )


# -- the stored key must read as stored -------------------------------------

def test_a_stored_key_is_stated_as_text():
    """It showed only in the field's placeholder, which is grey and reads as empty.

    That is why a user with a working stored key reported having configured no
    provider at all.
    """
    line = panel_state.key_state_line("openai", has_key=True)
    assert "stored" in line
    assert "OpenAI" in line, line
    assert "instead of your Mapdex plan" in line, line


def test_no_key_and_no_provider_read_differently():
    assert panel_state.key_state_line("", False) != panel_state.key_state_line("openai", False)
    assert "Mapdex plan" in panel_state.key_state_line("", False)


def test_a_local_endpoint_is_not_asked_for_a_key_it_does_not_need():
    line = panel_state.key_state_line("ollama", has_key=False)
    assert "no key" in line.lower(), line
    assert "Paste" not in line, line


# -- drift guards ------------------------------------------------------------

def test_every_provider_in_the_menu_has_a_sentence_name():
    """A provider added to the dropdown and not here renders as "your own model
    provider", which is the generic sentence this module exists to avoid."""
    for _label, value in panel_state.PROVIDER_CHOICES:
        if not value:
            continue
        assert panel_state.engine_name(value) != "your own model provider", (
            "{} is offered in the menu but has no name for prose".format(value)
        )


def test_the_provider_key_is_not_session_scoped():
    """Disconnect clears the session. The user's own credential is not part of it.

    Clearing it would be the same overreach as leaving the recent-task list
    behind, in the opposite direction: it belongs to their account somewhere
    else, and throwing it away silently on a Mapdex action is not ours to do.
    """
    for key in panel_state.SESSION_SCOPED_SETTINGS:
        assert "nivo" not in key, (
            "{} is cleared on disconnect and is an assistant setting".format(key)
        )
    assert "mapdex/recent_tasks" in panel_state.SESSION_SCOPED_SETTINGS, (
        "the recent-task list survived disconnect, offering Resume on batches the "
        "next token cannot read - and on a different account, another workspace's tasks"
    )
