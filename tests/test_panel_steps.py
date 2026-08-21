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
    assert "from .trace_view import budget_label, step_rows" in PLUGIN


def test_a_composed_turn_carries_its_steps_into_the_transcript():
    body = _method("_nivo_composed")
    assert "steps = step_rows(response)" in body
    assert '("assistant", reply, steps)' in body, "the reply is stored without its steps"


def test_the_status_line_reports_the_step_budget():
    body = _method("_nivo_composed")
    assert "budget_label(response)" in body


def test_the_transcript_draws_a_widget_per_step():
    render = _method("_render_nivo_turns", "\n    # ---")
    assert "for row_data in steps or []" in render
    assert "self._step_widget(row_data)" in render


def test_a_step_states_its_status_in_words():
    # Status is never colour alone: the panel is themed by QGIS and a reader on
    # a monochrome theme must get the same answer as everyone else.
    body = _method("_step_widget", "\n    @guarded")
    assert 'row_data.get("status_label")' in body
    assert 'row_data.get("marker"' in body


def test_a_step_shows_what_it_was_called_with():
    body = _method("_step_widget", "\n    @guarded")
    assert 'row_data.get("params")' in body


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
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Tuple):
            offenders.append(node.lineno)
            continue
        if len(node.args[0].elts) != 3:
            offenders.append(node.lineno)
    # Two sites remain: the user's own turn, which is not an assistant turn,
    # and _say itself. Everything else goes through the constructor.
    assert not offenders, "two-element transcript entries at lines {}".format(offenders)
    assert PLUGIN.count("self._nivo_turns.append(") == 2


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
    assert "[(sender, text, []) for sender, text in turns]" in body
