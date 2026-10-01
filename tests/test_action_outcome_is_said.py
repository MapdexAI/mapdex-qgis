"""A failed action has to contradict the claim, where the claim is.

`_nivo_composed` appends the assistant's reply to the transcript FIRST and
dispatches the companion actions afterwards. The reply is a claim - "Showing
Istanbul on the map" - so an action that refuses, is unimplemented, or raises
used to leave that claim standing beside a canvas that never moved. Its only
report was `_set_status`, and this session already established that the status
line is written on every layer click, so the reason was gone by the next click.

Nothing here is about one place or one language: the claim is about our own
plumbing, and every failure of it reads the same way in every locale.

plugin.py cannot be imported without QGIS, so these read the source. That is
the right instrument for the defect anyway - it is an OMISSION at a call site,
and no behavioural test of one branch fails when a second branch forgets.
"""
import ast
import pathlib

PLUGIN = (pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py")
SOURCE = PLUGIN.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("plugin.py has no {!r}".format(name))


def _calls(name):
    return {
        node.func.attr
        for node in ast.walk(_function(name))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_failure_helper_reaches_the_transcript():
    calls = _calls("_action_failed")
    assert "_say" in calls, (
        "a failed action reports only to the status line, which the next layer "
        "click overwrites"
    )
    assert "_set_status" in calls, "the status line still states the current outcome"


def test_the_failure_is_marked_as_one():
    """Severity is not decoration: DESIGN.md section 8 forbids conveying status
    by colour alone, and an unmarked line reads as another remark from the
    assistant rather than as the previous line being wrong."""
    body = ast.get_source_segment(SOURCE, _function("_action_failed"))
    assert 'severity="warning"' in body, body


def test_every_way_an_action_can_fail_says_so():
    """The three failure branches in the dispatcher, plus the refusal helper.

    A branch that returns after `_set_status` alone is the defect; each of
    these has to route through the helper.
    """
    for name in ("_capability_refused",):
        assert "_action_failed" in _calls(name), name
    dispatch = ast.get_source_segment(SOURCE, _function("_apply_nivo_action"))
    assert dispatch.count("_action_failed") >= 3, (
        "a dispatcher branch still fails silently:\n" + dispatch
    )


def test_an_executor_that_raised_is_blocking_not_a_remark():
    body = ast.get_source_segment(SOURCE, _function("_run_capability"))
    assert 'severity="blocking"' in body, (
        "an action whose executor raised is reported as an ordinary line"
    )


def test_success_still_says_what_it_did():
    """The other half. A guard that only proved failures are reported would
    pass against a build that reported nothing else."""
    assert "_say" in _calls("_run_capability")


def test_a_duplicate_action_stays_a_status_line():
    """Deliberately NOT said. Ignoring a repeated action id is bookkeeping, not
    a contradiction of anything the user was told, and putting it in the
    transcript would train people to skip the lines that matter."""
    dispatch = ast.get_source_segment(SOURCE, _function("_apply_nivo_action"))
    start = dispatch.index("duplicate or malformed")
    branch = dispatch[start:start + dispatch[start:].index("return")]
    assert "_action_failed" not in branch, branch
