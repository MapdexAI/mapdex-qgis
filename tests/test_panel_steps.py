"""The QGIS transcript must show the steps, not only the reply.

`plugin.py` imports `qgis` at module scope and cannot be imported in CI, so
these are source-level guards in the same style as `test_conversations.py`. The
presentation rule itself is real code with real tests in `test_trace_view.py`;
what is checked here is that the panel is wired to it, because a rule nothing
calls is the defect this plugin has already shipped more than once.
"""

import ast
import pathlib

PLUGIN = (pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py").read_text(
    encoding="utf-8"
)


def _method(name, until="\n    @guarded"):
    start = PLUGIN.index("def {}(".format(name))
    return PLUGIN[start : PLUGIN.index(until, start + 1)]


def test_the_panel_imports_the_presentation_rule():
    # A module reachable only from its own test is not in the product. The
    # plugin has shipped 3,570 lines of that before; this is the cheap guard.
    assert "from .trace_view import budget_notice, step_rows" in PLUGIN


def test_a_composed_turn_carries_its_steps_into_the_transcript():
    body = _method("_nivo_composed")
    assert "steps = step_rows(response)" in body
    assert "_replace_last_assistant_turn(reply, steps)" in body, (
        "the reply is stored without its steps"
    )


def test_the_status_line_reports_only_a_turn_that_was_cut_short():
    # It used to write the step counter unconditionally and leave it there, so
    # a finished turn read "Step 2 of 32" with nothing running.
    body = _method("_nivo_composed")
    assert "budget_notice(response)" in body
    assert "budget_label" not in body


def test_the_transcript_draws_a_widget_per_step():
    # The per-entry drawing moved out of the loop into `_turn_widget` when an
    # entry gained a severity glyph and action chips. The rule is unchanged:
    # every step of a composed turn gets its own widget in the transcript.
    render = _method("_turn_widget", "\n    def _adopt_conversation")
    assert 'turn.get("steps") or []' in render
    assert "self._step_widget(row_data)" in render


def test_a_step_states_its_status_in_words():
    # Status is never colour alone: the panel is themed by QGIS and a reader on
    # a monochrome theme must get the same answer as everyone else.
    body = _method("_step_widget", "\n    @guarded")
    assert 'row_data.get("status_label")' in body
    assert 'row_data.get("marker"' not in body


def test_successful_steps_do_not_dump_raw_arguments_into_chat():
    body = _method("_step_widget", "\n    @guarded")
    assert 'row_data.get("params")' not in body


def test_capability_result_updates_the_current_answer_instead_of_adding_one():
    body = _method("_run_capability", "\n    def _report_action_result")
    assert "self._replace_last_assistant_turn(described)" in body
    assert "self._say(described)" not in body


def test_step_text_never_takes_a_rich_text_path():
    # QLabel sniffs its string and renders anything HTML-shaped as HTML. A layer
    # name containing `<b>` is data, and a server-supplied message even more so.
    body = _method("_step_widget", "\n    @guarded")
    assert "self._plain(QLabel()" in body
    assert "QLabel(line)" not in body
    assert "setText(" not in body, "text must go through _plain, which pins PlainText"


def test_the_step_widget_touches_no_network_or_project_state():
    tree = ast.parse(PLUGIN)
    node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_step_widget"
    )
    dumped = ast.dump(node)
    for banned in ("self.api", "iface", "QgsProject", "_task"):
        assert banned not in dumped, "_step_widget is a painter, not an actor"


def test_every_assistant_turn_is_built_by_one_constructor():
    """A transcript entry has a shape, and seven call sites once ignored it.

    The shape changed when steps were added and those sites kept writing a
    two-element tuple. Nothing caught it: plugin.py cannot be imported by this
    suite, so the mismatch was invisible until a user hit it inside QGIS. The
    guard is structural rather than behavioural, because the defect is an
    OMISSION and no behavioural test of one site fails when a new site forgets.

    The entry is a dict now, built by `transcript_turn`, because it went on
    growing - actions, a severity, a measured fact line - and a tuple that has
    already caused this once would cause it again.

    Three ways to write one, and the first version of this guard covered ONE.
    `.append(...)` was checked; a comprehension rebuilding the whole list and a
    `[-1] = ...` replacing the last entry were not, and both were live. So the
    check is now on the VALUE written into `_nivo_turns` by any route.
    """
    tree = ast.parse(PLUGIN)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "append":
            continue
        target = func.value
        if not (isinstance(target, ast.Attribute) and target.attr == "_nivo_turns"):
            continue
        built = node.args and isinstance(node.args[0], ast.Call)
        name = getattr(node.args[0].func, "id", "") if built else ""
        if name != "transcript_turn":
            offenders.append(node.lineno)
    assert not offenders, "hand-built transcript entries at lines {}".format(offenders)
    # Two sites: the user's own turn, and _say for every assistant turn.
    assert PLUGIN.count("self._nivo_turns.append(") == 2
    assert "def transcript_turn(sender, text" in PLUGIN, "the constructor was renamed away"

    # And ASSIGNMENT, which this guard did not cover and which is how the
    # fourth site survived: replaying a saved conversation rebuilt the whole
    # list as tuples in a comprehension, so nothing appended and nothing
    # failed until the transcript tried to read a key out of a tuple. An
    # omission is not always a call.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Any route to the list: `self._nivo_turns = ...` rebuilding it, and
        # `self._nivo_turns[-1] = ...` replacing one entry.
        writes = any("_nivo_turns" in ast.dump(target) for target in node.targets)
        if not writes:
            continue
        for inner in ast.walk(node.value):
            # Load context only: `for sender, text in turns` unpacks into a
            # tuple too, and that one is the comprehension reading its input
            # rather than an entry being built.
            if isinstance(inner, ast.Tuple) and isinstance(inner.ctx, ast.Load):
                raise AssertionError(
                    "a transcript entry is assembled as a tuple at line {}".format(node.lineno))


def test_the_turn_constructor_is_not_a_qt_entry_point():
    # `@guarded` is the error boundary for a slot. Putting it on a helper is
    # harmless until it is inserted ABOVE an existing decorator and silently
    # steals it from the slot below, which is exactly what happened once here.
    index = PLUGIN.index("def _say(self")
    preceding = PLUGIN[:index].rstrip().splitlines()[-1].strip()
    assert preceding != "@guarded", "_say took the decorator belonging to the method below it"
    guard_line = PLUGIN[:PLUGIN.index("def _apply_nivo_action(")].rstrip().splitlines()[-1].strip()
    assert guard_line == "@guarded", "the action dispatcher lost its error boundary"


def test_a_replayed_conversation_says_its_steps_were_not_recorded():
    # The server stores messages, not traces. Restoring history with an empty
    # step list says "not recorded"; inventing steps would say "this is what it
    # did", which nobody measured.
    body = _method("_thread_opened")
    assert "transcript_turn(sender, text) for sender, text in turns" in body, (
        "the replay builds entries by hand again; it is the site that survived "
        "the append-only version of the constructor guard"
    )
