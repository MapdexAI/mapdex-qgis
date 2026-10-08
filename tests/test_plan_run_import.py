"""A priced plan-run result can be imported, by a control that names the price.

A plan run started from the Nivo panel belongs to no batch. When its result
costs money (a placed sheet's first import), the import does not start on its
own - and the panel then told the person to "Press Get result from Mapdex", a
button that exists only for a batch. The result could never be imported.

Now the plan run's turn carries its own control, with the price in its label,
and pressing it imports exactly that run. `plugin.py` imports QGIS at module
scope, so the two handlers are lifted out of its AST and run against a fake
panel, the way test_placement_prerequisite.py does.
"""
import ast
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import first_look, leave_price  # noqa: E402

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _lifted(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.decorator_list = []
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {
                "leave_price": leave_price,
                "first_look": first_look,
                "get_capability": lambda capability: None,
            }
            exec(compile(module, "plugin.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError("{} not found in plugin.py".format(name))


NEEDS_CONSENT = _lifted("_result_needs_consent")
FIRST_LOOK_ACTION = _lifted("_first_look_action")

QUOTE = {"sheets": 1, "unpaid_sheets": 1, "charge_cents": 200, "available_cents": 1100}


def panel():
    said, statuses, tasks, fetched = [], [], [], []
    fake = types.SimpleNamespace(
        _result_price_line="",
        _refresh_ui=lambda: None,
        _set_status=statuses.append,
        _announce=lambda *a, **k: None,
        _say=lambda text, **kwargs: said.append((text, kwargs)),
        _task=lambda title, work, done: tasks.append((title, work, done)),
        _results_imported=lambda *a: None,
    )
    fake._fetch_result_files = lambda detail, **kwargs: fetched.append((detail, kwargs)) or {"files": []}
    return fake, said, statuses, tasks, fetched


def test_a_priced_plan_run_result_gets_its_own_control_with_the_price():
    fake, said, statuses, tasks, _ = panel()
    NEEDS_CONSENT(fake, {
        "needs_consent": True, "quote": QUOTE, "batch": {}, "only_runs": ("run_9",), "interactive": False,
    })
    assert tasks == [], "a priced result was imported without anybody pressing anything"
    assert len(said) == 1
    text, kwargs = said[0]
    action = kwargs["actions"][0]
    assert action["kind"] == "import_run" and action["run_ids"] == ["run_9"]
    assert "$2" in action["label"]
    # The text points at the control that is there, never at the batch button.
    assert "Get result from Mapdex" not in text and "Get result from Mapdex" not in statuses[-1]
    assert "Import into QGIS" in text


def test_a_batch_result_still_points_at_the_get_result_button():
    fake, said, statuses, tasks, _ = panel()
    NEEDS_CONSENT(fake, {
        "needs_consent": True, "quote": QUOTE, "batch": {"items": [{"run_id": "run_1"}]},
        "only_runs": (), "interactive": False,
    })
    assert said == [] and tasks == []
    assert "Get result from Mapdex" in statuses[-1]


def test_pressing_the_control_imports_exactly_that_run_with_consent():
    fake, _, _, tasks, fetched = panel()
    FIRST_LOOK_ACTION(fake, {"kind": "import_run", "run_ids": ["run_9"], "label": "Import into QGIS ($2)"}, "")
    assert len(tasks) == 1
    _title, work, _done = tasks[0]
    work()
    assert fetched == [({}, {"only_runs": ("run_9",), "interactive": True, "consented": True})]


def test_the_label_names_the_price_free_or_unknown():
    assert leave_price.import_label(QUOTE) == "Import into QGIS ($2)"
    assert leave_price.import_label({"charge_cents": 0, "sheets": 1, "unpaid_sheets": 0}) == "Import into QGIS (free)"
    assert leave_price.import_label(None) == "Import into QGIS (price not checked)"
