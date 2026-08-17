"""Conversation continuity: the thing that makes a follow-up question work.

`POST /v1/compose` has always accepted a `thread_id`, and with one it replays
the conversation and carries the earlier source reference forward. The plugin
sent none, so every QGIS turn arrived with no memory of the last one, nothing
was ever stored, and there was no history to open because none was kept. A user
could not say "and the largest one?" after asking about a layer.

These tests cover the pure half (`nivo.py`) and the client (`api_client.py`)
directly, and the wiring in `plugin.py` at source level - plugin.py imports
`qgis` at module scope and cannot be imported in CI.
"""
import ast
import pathlib
import re
import sys
from urllib import error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.api_client import MapdexAPI, MapdexAPIError  # noqa: E402
from mapdex_qgis.nivo import (  # noqa: E402
    DEFAULT_THREAD_TITLE,
    THREAD_ID_SETTING,
    THREAD_PROJECT_SETTING,
    describe_thread,
    format_timestamp,
    remembered_thread,
    thread_is_gone,
    thread_list_items,
    thread_title,
    thread_turns,
)

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
PANEL = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")


def _method(source, name, until):
    start = source.index("def {}(".format(name))
    return source[start:source.index(until, start + 1)]


# `plugin.py` imports `qgis` at module scope and cannot be imported in CI, so
# the two conversation helpers are lifted out of its AST and executed on their
# own. They are pure network orchestration - no widgets, no QGIS - which is
# exactly why the turn logic can be tested for real rather than by grep.
def _lifted(*names):
    tree = ast.parse(PLUGIN)
    wanted = [node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(wanted) == len(names), "missing from plugin.py: {}".format(names)
    namespace = {"MapdexAPIError": MapdexAPIError, "thread_is_gone": thread_is_gone}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "plugin-threads", "exec"), namespace)
    return namespace


class _Turn:
    """A plugin stand-in carrying only what the lifted helpers touch."""

    def __init__(self, api):
        self.api = api
        functions = _lifted("_compose_in_thread", "_open_conversation")
        self._compose_in_thread = functions["_compose_in_thread"].__get__(self)
        self._open_conversation = functions["_open_conversation"].__get__(self)

    def ask(self, thread_id="", message="and the largest one?"):
        return self._compose_in_thread("proj_1", message, {"client": "qgis"}, thread_id, "T")


class _FakeAPI:
    """Records calls; raises whatever the test queues for compose."""

    def __init__(self, compose_results, thread_ids=("th_new",)):
        self.compose_results = list(compose_results)
        self.thread_ids = list(thread_ids)
        self.compose_calls = []
        self.created = []

    def create_thread(self, project_id, title=""):
        self.created.append((project_id, title))
        if not self.thread_ids:
            raise MapdexAPIError("threads unavailable", 503)
        value = self.thread_ids.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"id": value}

    def compose(self, project_id, message, context, thread_id=""):
        self.compose_calls.append(thread_id)
        outcome = self.compose_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


def test_compose_carries_the_thread_id_so_a_follow_up_has_the_earlier_turn():
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    def fake(method, path, payload=None, project_id=""):
        captured.update(method=method, path=path, payload=payload, project_id=project_id)
        return {"kind": "message"}

    api._request = fake  # type: ignore[method-assign]
    api.compose("proj_1", "and the largest one?", {"client": "qgis"}, thread_id="th_42")
    assert captured["payload"]["thread_id"] == "th_42"
    assert captured["path"] == "/v1/compose"
    assert captured["project_id"] == "proj_1"


def test_a_first_turn_omits_thread_id_rather_than_sending_an_empty_one():
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}

    def fake(method, path, payload=None, project_id=""):
        captured.update(payload=payload)
        return {}

    api._request = fake  # type: ignore[method-assign]
    api.compose("proj_1", "how many parcels?", {"client": "qgis"})
    assert "thread_id" not in captured["payload"]


def test_thread_endpoints_use_the_documented_paths_and_tenant_header():
    api = MapdexAPI("https://api.mapdex.ai", "token")
    calls = []

    def fake(method, path, payload=None, project_id=""):
        calls.append((method, path, payload, project_id))
        if path == "/v1/threads" and method == "GET":
            return [{"id": "th_1", "title": "Parcels"}]
        if path == "/v1/threads" and method == "POST":
            return {"id": "th_new", "title": "Parcels"}
        if path.endswith("/messages"):
            return [{"role": "user", "content": "hi"}]
        return None

    api._request = fake  # type: ignore[method-assign]
    assert api.list_threads("proj_1") == [{"id": "th_1", "title": "Parcels"}]
    assert api.create_thread("proj_1", "Parcels")["id"] == "th_new"
    assert api.thread_messages("th_1", "proj_1") == [{"role": "user", "content": "hi"}]
    assert api.delete_thread("th_1", "proj_1") is None
    assert calls == [
        ("GET", "/v1/threads", None, "proj_1"),
        ("POST", "/v1/threads", {"title": "Parcels", "project_id": "proj_1"}, "proj_1"),
        ("GET", "/v1/threads/th_1/messages", None, "proj_1"),
        ("DELETE", "/v1/threads/th_1", None, "proj_1"),
    ]


def test_create_thread_sends_project_id_in_the_body_because_the_handler_requires_it():
    # CreateThread reads project_id from the JSON body, not the header, and
    # answers 400 without it.
    api = MapdexAPI("https://api.mapdex.ai", "token")
    captured = {}
    api._request = lambda method, path, payload=None, project_id="": captured.update(  # type: ignore[method-assign]
        payload=payload
    ) or {"id": "th_1"}
    api.create_thread("proj_9")
    assert captured["payload"]["project_id"] == "proj_9"


def test_a_thread_id_is_encoded_before_it_becomes_part_of_a_path():
    # The id round-trips through QSettings, a local file another program can
    # edit, so it is not trusted to be a bare `th_…`.
    api = MapdexAPI("https://api.mapdex.ai", "token")
    seen = []
    api._request = lambda method, path, payload=None, project_id="": seen.append(path)  # type: ignore[method-assign]
    api.delete_thread("../../v1/projects", "proj_1")
    api.thread_messages("th_1/../../runs", "proj_1")
    assert seen == [
        "/v1/threads/..%2F..%2Fv1%2Fprojects",
        "/v1/threads/th_1%2F..%2F..%2Fruns/messages",
    ]


def test_a_thread_list_envelope_is_accepted_as_well_as_the_bare_array():
    api = MapdexAPI("https://api.mapdex.ai", "token")
    api._request = lambda *a, **k: {"threads": [{"id": "th_1"}, "junk"]}  # type: ignore[method-assign]
    assert api.list_threads("proj_1") == [{"id": "th_1"}]


def test_the_error_envelope_code_is_kept_so_callers_never_match_message_text():
    api = MapdexAPI("https://api.mapdex.ai", "token")

    class FakeResponse:
        def read(self):
            return b'{"code":"NOT_FOUND","message":"Thread not found"}'

        def close(self):
            return None

    def boom(*_args, **_kwargs):
        raise error.HTTPError(
            "https://api.mapdex.ai/v1/compose", 404, "Not Found", hdrs={}, fp=FakeResponse()
        )

    api._urlopen = boom
    import mapdex_qgis.api_client as client

    original = client._urlopen
    client._urlopen = boom
    try:
        api.compose("proj_1", "hi", {}, thread_id="th_gone")
        raise AssertionError("expected MapdexAPIError")
    except MapdexAPIError as exc:
        assert exc.status == 404
        assert exc.code == "NOT_FOUND"
    finally:
        client._urlopen = original


# --------------------------------------------------------------------------
# Which remembered conversation is still usable
# --------------------------------------------------------------------------


def test_a_remembered_thread_is_only_restored_for_its_own_project():
    assert remembered_thread("th_1", "proj_a", "proj_a") == "th_1"
    # Threads are project-scoped on the server; continuing one from elsewhere
    # asks the API for a conversation this project cannot see.
    assert remembered_thread("th_1", "proj_a", "proj_b") == ""
    assert remembered_thread("th_1", "", "proj_a") == ""
    assert remembered_thread("", "proj_a", "proj_a") == ""
    assert remembered_thread("th_1", "proj_a", "") == ""
    assert remembered_thread(None, None, None) == ""


def test_a_missing_thread_is_recognised_by_status_not_by_message_text():
    assert thread_is_gone(404) is True
    assert thread_is_gone("404") is True
    assert thread_is_gone(500) is False
    assert thread_is_gone(None) is False
    assert thread_is_gone("Thread not found") is False


def test_the_settings_keys_are_namespaced_under_the_assistant():
    assert THREAD_ID_SETTING == "mapdex/nivo/thread_id"
    assert THREAD_PROJECT_SETTING == "mapdex/nivo/thread_project_id"


# --------------------------------------------------------------------------
# What History shows
# --------------------------------------------------------------------------


def test_thread_rows_keep_the_servers_newest_first_order():
    payload = [
        {"id": "th_new", "title": "B", "updated_at": "2026-08-17T10:00:00Z"},
        {"id": "th_old", "title": "A", "updated_at": "2026-08-01T10:00:00Z"},
    ]
    assert [row["id"] for row in thread_list_items(payload)] == ["th_new", "th_old"]


def test_a_row_without_an_id_cannot_be_opened_so_it_is_dropped():
    assert thread_list_items([{"title": "orphan"}, "junk", {"id": "th_1"}]) == [
        {
            "id": "th_1",
            "title": DEFAULT_THREAD_TITLE,
            "updated_at": "",
            "created_at": "",
            "message_count": None,
        }
    ]


def test_a_missing_message_count_is_none_not_zero():
    # None and 0 are different claims: "the server does not report this" must
    # not render as "this conversation has no messages". /v1/threads sends no
    # count today, so every row would otherwise read "0 messages".
    assert thread_list_items([{"id": "th_1"}])[0]["message_count"] is None
    assert thread_list_items([{"id": "th_1", "message_count": 4}])[0]["message_count"] == 4
    assert thread_list_items([{"id": "th_1", "message_count": "x"}])[0]["message_count"] is None
    assert thread_list_items([{"id": "th_1", "message_count": -2}])[0]["message_count"] is None


def test_a_row_states_the_title_and_when_and_the_count_only_when_it_is_known():
    row = {"id": "th_1", "title": "Parcel areas", "updated_at": "", "message_count": None}
    assert describe_thread(row) == "Parcel areas"
    assert describe_thread({"id": "th_1", "title": "P", "message_count": 1}) == "P · 1 message"
    assert describe_thread({"id": "th_1", "title": "P", "message_count": 6}) == "P · 6 messages"
    assert describe_thread({}) == DEFAULT_THREAD_TITLE


def test_an_unreadable_timestamp_says_nothing_rather_than_showing_raw_iso():
    assert format_timestamp("2026-08-17T11:32:04Z").endswith(tuple("0123456789"))
    assert len(format_timestamp("2026-08-17T11:32:04Z")) == len("2026-08-17 11:32")
    assert format_timestamp("2026-08-17T11:32:04+03:00") != ""
    assert format_timestamp("not a date") == ""
    assert format_timestamp("") == ""
    assert format_timestamp(None) == ""


def test_a_title_comes_from_the_users_own_words_and_is_only_truncated():
    # Mechanical: no inspection of what the words mean, in any language. The
    # server's own fallback is a timestamp, which names no conversation.
    assert thread_title("  En büyük parseli göster  ") == "En büyük parseli göster"
    assert thread_title("a\n b\tc") == "a b c"
    assert thread_title("") == DEFAULT_THREAD_TITLE
    assert thread_title(None) == DEFAULT_THREAD_TITLE
    long = thread_title("x" * 200)
    assert len(long) == 60 and long.endswith("…")


# --------------------------------------------------------------------------
# Loading a conversation back into the transcript
# --------------------------------------------------------------------------


def test_stored_messages_become_the_transcripts_own_sender_text_pairs():
    payload = [
        {"role": "user", "content": "how many parcels?"},
        {"role": "assistant", "content": "24."},
        {"role": "user", "content": "and the largest one?"},
        {"role": "assistant", "content": "P-013, 20,000 m²."},
    ]
    assert thread_turns(payload) == [
        ("user", "how many parcels?"),
        ("assistant", "24."),
        ("user", "and the largest one?"),
        ("assistant", "P-013, 20,000 m²."),
    ]


def test_non_conversation_rows_and_empty_content_are_not_rendered_as_turns():
    payload = [
        {"role": "system", "content": "prompt"},
        {"role": "tool", "content": "{}"},
        {"role": "assistant", "content": "   "},
        {"role": "assistant", "content": None},
        "junk",
        {"role": "USER", "content": "kept"},
    ]
    assert thread_turns(payload) == [("user", "kept")]


def test_history_text_is_not_truncated_the_way_bounded_context_fields_are():
    # companion_context bounds every field it sends to the model. A message
    # coming BACK is a record of what was said; clipping it at 256 characters
    # would rewrite the user's own history.
    long = "y" * 900
    assert thread_turns([{"role": "assistant", "content": long}]) == [("assistant", long)]


def test_a_message_envelope_is_accepted_as_well_as_the_bare_array():
    assert thread_turns({"messages": [{"role": "user", "content": "hi"}]}) == [("user", "hi")]


# --------------------------------------------------------------------------
# The wiring in plugin.py and panel.py
# --------------------------------------------------------------------------


def test_the_panel_offers_both_controls_in_the_nivo_header():
    header = PANEL[PANEL.index("nivo_header = QWidget()"):PANEL.index("nivo_reply = QScrollArea()")]
    assert "nivo_header_layout.addWidget(\n        nivo_new_button" in header
    assert "nivo_header_layout.addWidget(\n        nivo_history_button" in header
    for handle in ("nivo_new_button", "nivo_history_button"):
        assert '"{}": {}'.format(handle, handle) in PANEL, "not handed back to plugin.py"


def test_new_task_is_described_as_local_so_it_never_reads_as_a_delete():
    body = PANEL[PANEL.index("nivo_new_button = QToolButton()"):PANEL.index("nivo_history_button =")]
    tooltip = body[body.index("setToolTip("):]
    assert "History" in tooltip and "deleted" in tooltip


def test_new_task_deletes_nothing_on_the_server():
    body = _method(PLUGIN, "new_nivo_task", "\n    @guarded")
    assert "delete_thread" not in body, "New chat must not remove the conversation from Mapdex"
    assert '_adopt_conversation("")' in body, "it must drop the current thread"
    assert "self._nivo_turns = []" in body


def test_the_history_dialog_has_a_widget_for_every_state_it_can_be_in():
    body = PANEL[PANEL.index("def build_thread_history_dialog"):]
    # Loading and empty and failed are three different things to say; a dialog
    # with only a list says the same blank box for all three.
    assert "state_label" in body and "retry_button" in body
    for key in ("dialog", "state_label", "list", "retry_button", "open_button",
                "delete_button", "close_button"):
        assert '"{}"'.format(key) in body


def test_the_history_states_are_each_reachable_from_the_plugin():
    loading = _method(PLUGIN, "_load_thread_history", "\n    @guarded")
    assert "Loading conversations" in loading
    assert 'refs["retry_button"].setVisible(False)' in loading
    loaded = _method(PLUGIN, "_thread_history_loaded", "\n    def _refresh_history_buttons")
    assert "Could not load conversations" in loaded
    assert 'refs["retry_button"].setVisible(True)' in loaded, "a failure must offer a retry"
    assert "No earlier conversations in this project yet" in loaded
    assert "describe_thread(row)" in loaded


def test_retry_reloads_the_same_request_rather_than_closing_the_dialog():
    body = _method(PLUGIN, "open_nivo_history", "\n    @guarded")
    assert 'refs["retry_button"].clicked.connect(self._load_thread_history)' in body


def test_deleting_a_conversation_asks_first():
    body = _method(PLUGIN, "_delete_selected_thread", "\n    @guarded")
    assert "QMessageBox.question" in body
    assert "cannot be undone" in body
    assert "self.api.delete_thread" in body
    # Scoped enums: PyQt6 removed the flat forms and raises AttributeError the
    # first time the branch runs, which no stubbed test would ever reach.
    assert 'enum_member(QMessageBox, "StandardButton", "Yes")' in body


def test_a_thread_the_server_no_longer_has_starts_a_new_one_instead_of_erroring():
    body = _method(PLUGIN, "_compose_in_thread", "\n    def _open_conversation")
    assert "thread_is_gone(exc.status)" in body
    assert body.count("self._open_conversation(project_id, title)") == 2, (
        "the 404 path must open a replacement and ask again"
    )
    assert body.count("self.api.compose(") == 2


def test_the_first_turn_opens_a_conversation_and_sends_it():
    api = _FakeAPI([{"text": "24 parcels."}], thread_ids=("th_1",))
    outcome = _Turn(api).ask(thread_id="")
    assert api.created == [("proj_1", "T")]
    assert api.compose_calls == ["th_1"]
    assert outcome == {"thread_id": "th_1", "response": {"text": "24 parcels."}, "notice": ""}


def test_a_later_turn_reuses_the_conversation_without_opening_another():
    # This is the whole point: with the id, compose replays the earlier turn,
    # so "and the largest one?" resolves against the layer named before it.
    api = _FakeAPI([{"text": "P-013."}])
    outcome = _Turn(api).ask(thread_id="th_1")
    assert api.created == []
    assert api.compose_calls == ["th_1"]
    assert outcome["thread_id"] == "th_1"


def test_a_remembered_conversation_the_server_lost_is_replaced_not_reported():
    # The user never knew the conversation had an id; "Thread not found" is not
    # an error they can act on. The question is asked again in a fresh one.
    gone = MapdexAPIError("Thread not found", 404, code="NOT_FOUND")
    api = _FakeAPI([gone, {"text": "24 parcels."}], thread_ids=("th_fresh",))
    outcome = _Turn(api).ask(thread_id="th_stale")
    assert api.compose_calls == ["th_stale", "th_fresh"]
    assert outcome["thread_id"] == "th_fresh"
    assert outcome["response"] == {"text": "24 parcels."}
    assert outcome["notice"] == ""


def test_a_real_failure_is_not_retried_as_a_missing_conversation():
    boom = MapdexAPIError("Upstream model timed out", 502)
    api = _FakeAPI([boom])
    try:
        _Turn(api).ask(thread_id="th_1")
        raise AssertionError("expected the error to reach the user")
    except MapdexAPIError as exc:
        assert exc.status == 502
    assert api.created == [], "a 502 must not silently abandon the conversation"
    assert api.compose_calls == ["th_1"]


def test_a_deployment_that_cannot_open_a_conversation_still_answers():
    # Continuity degrades; the turn does not fail. The panel says so rather
    # than leaving the user to discover that follow-ups stopped working.
    api = _FakeAPI([{"text": "24 parcels."}], thread_ids=())
    outcome = _Turn(api).ask(thread_id="")
    assert api.compose_calls == [""], "no thread id is sent when none could be opened"
    assert outcome["thread_id"] == ""
    assert outcome["response"] == {"text": "24 parcels."}
    assert "without conversation history" in outcome["notice"]


def test_a_create_that_returns_no_id_is_treated_as_no_conversation():
    class Blank(_FakeAPI):
        def create_thread(self, project_id, title=""):
            self.created.append((project_id, title))
            return {}

    api = Blank([{"text": "ok"}])
    outcome = _Turn(api).ask(thread_id="")
    assert outcome["thread_id"] == ""
    assert "without conversation history" in outcome["notice"]


def test_the_conversation_opened_for_a_failed_turn_travels_back_on_the_error():
    gone = MapdexAPIError("Thread not found", 404)
    boom = MapdexAPIError("Upstream model timed out", 502)
    api = _FakeAPI([gone, boom], thread_ids=("th_fresh",))
    try:
        _Turn(api).ask(thread_id="th_stale")
        raise AssertionError("expected the retry failure to surface")
    except MapdexAPIError as exc:
        # Without this the fresh thread is orphaned and the next attempt opens
        # yet another one, losing the turns in between.
        assert exc.mapdex_thread_id == "th_fresh"


def test_a_failed_turn_keeps_the_conversation_it_had_just_opened():
    # Otherwise every failure abandons a fresh thread on the server and opens
    # another one on the next attempt, and the retry loses the earlier turns.
    helper = _method(PLUGIN, "_compose_in_thread", "\n    def _open_conversation")
    assert helper.count("mapdex_thread_id = thread_id") == 2
    composed = _method(PLUGIN, "_nivo_composed", "\n    @guarded")
    assert 'carried = getattr(exception, "mapdex_thread_id", "")' in composed
    # Only a conversation THIS turn opened. Re-stamping the remembered one
    # would bind it to whatever project happens to be active now.
    assert "if carried:\n                self._adopt_conversation(carried)" in composed


def test_deleting_the_open_conversation_drops_it_rather_than_leaving_a_404():
    body = _method(PLUGIN, "_thread_deleted", "\n    @guarded")
    assert "thread_id == self._nivo_thread_id" in body
    assert '_adopt_conversation("")' in body
    assert "self._load_thread_history()" in body, "the list must reflect the deletion"


def test_the_dialog_opens_on_both_qt_bindings():
    # PyQt6 removed `exec_`. Picking the wrong one raises AttributeError the
    # first time a user clicks History and never before, which is exactly the
    # failure a stubbed test cannot reproduce.
    body = _method(PLUGIN, "open_nivo_history", "\n    @guarded")
    assert 'getattr(dialog, "exec", None) or getattr(dialog, "exec_")' in body


def test_the_conversation_survives_a_qgis_restart_and_stays_in_its_project():
    body = _method(PLUGIN, "_adopt_conversation", "\n    @guarded")
    assert "settings.setValue(THREAD_ID_SETTING, thread_id)" in body
    assert "settings.setValue(THREAD_PROJECT_SETTING" in body
    assert "settings.remove(THREAD_ID_SETTING)" in body
    # Restored through the project check, never read straight out of settings.
    assert "remembered_thread(" in PLUGIN[:PLUGIN.index("def initGui")]


def test_disconnect_forgets_the_conversation_of_the_session_that_ended():
    body = _method(PLUGIN, "disconnect", "\n    @guarded")
    assert '_adopt_conversation("", "")' in body


def test_opening_a_conversation_makes_it_the_one_the_next_question_continues():
    body = _method(PLUGIN, "_thread_opened", "\n    @guarded")
    assert "thread_turns(payload)" in body
    assert "_adopt_conversation(thread_id, project_id)" in body
    # Vanished between listing it and opening it: say so, and adopt nothing.
    assert "thread_is_gone(exception.status)" in body
    assert body.index("thread_is_gone") < body.index("_adopt_conversation")


def test_the_transcript_renders_history_as_text_and_never_as_markup():
    # QLabel defaults to Qt::AutoText and renders anything that looks like HTML
    # as HTML. Replaying stored messages must not open a rich-text path.
    plain = _method(PLUGIN, "_plain", "\n    @guarded")
    assert 'enum_member(Qt, "TextFormat", "PlainText")' in plain
    render = _method(PLUGIN, "_render_nivo_turns", "\n    # ---")
    assert "QLabel(str(text))" not in render, "a raw QLabel(text) is an auto-rich-text path"
    assert render.count("self._plain(QLabel(), text)") == 2


def test_the_network_helpers_touch_no_qgis_state_or_widget():
    # _compose_in_thread and _open_conversation run on a QgsTask worker thread.
    # The canvas, the layer tree and every panel widget are main-thread only.
    tree = ast.parse(PLUGIN)
    for name in ("_compose_in_thread", "_open_conversation"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        body = ast.dump(node)
        for banned in ("iface", "nivo_status", "_set_status", "_nivo_snapshot",
                       "_render_nivo_turns", "QMessageBox"):
            assert banned not in body, "{} touches {} from a worker thread".format(name, banned)


def test_both_controls_are_guarded_qt_entry_points():
    for name in ("new_nivo_task", "open_nivo_history", "_load_thread_history",
                 "_thread_history_loaded", "_open_selected_thread", "_thread_opened",
                 "_delete_selected_thread", "_thread_deleted"):
        assert "@guarded\n    def {}(".format(name) in PLUGIN, (
            "{} can abort QGIS if it raises".format(name)
        )


def test_the_new_widget_handles_are_dropped_on_unload():
    refs = PLUGIN[PLUGIN.index("PANEL_WIDGET_REFS = ("):PLUGIN.index("def plugin_icon")]
    assert '"nivo_new_button"' in refs and '"nivo_history_button"' in refs


def test_nothing_here_reads_what_the_words_mean_in_any_language():
    """Routing is by HTTP status and by role; titles are pure truncation.

    A keyword list silently breaks every language not written into it. These
    are the two functions that see free text, so both are exercised in
    languages nobody wrote a rule for, and both must behave identically to the
    English case.
    """
    for sentence in (
        "show me the largest parcel",
        "en büyük parseli göster",
        "最も大きい区画を表示して",
        "أظهر أكبر قطعة أرض",
        "покажи самый большой участок",
    ):
        assert thread_title(sentence) == " ".join(sentence.split())
        assert thread_title(sentence + " " + "z" * 200).endswith("…")
        assert thread_turns([{"role": "user", "content": sentence}]) == [("user", sentence)]
        # The same words as an unknown role are still not a conversation turn.
        assert thread_turns([{"role": "system", "content": sentence}]) == []
