"""What the server sends must be what this plugin runs.

Every other test in this directory checks one side of the companion boundary in
its own vocabulary. The capability tests build their requests from this
plugin's own registry, so they send `{"layer_id": ..., "distance": 100}` to
`geoprocessing.buffer@1` and pass. The server sends that capability
`{"operation": "buffer", "input_layer": ..., "distance": 100}` behind a
confirmation gate, and nothing ever put the two in front of each other. Both
halves were correct and "buffer yap" did nothing.

The fixtures under `packages/contracts/testdata/companion/` close that gap:

- `qgis_supported_action_kinds.json` is exactly what this build advertises. The
  Go test reads it, so the server is exercised against the real negotiation
  list rather than a hand-typed one.
- `qgis_compose_responses.json` is what the real Go compose code answers, on
  the wire, for every companion capability it can select. The Go test fails
  when the server's answer drifts from it, and this file fails when this
  plugin would not carry an answer out.

Regenerate the first with `python tests/test_server_action_contract.py`, and the
second with `UPDATE_COMPANION_WIRE=1 go test ./apps/api/internal/modules/ai/service
-run TestCompanionWireFixture`.
"""
import ast
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo.capabilities import CapabilityError, validate_request  # noqa: E402
from mapdex_qgis.nivo import (  # noqa: E402
    IMPLEMENTED_ACTIONS,
    SAFE_PARAM_KEYS,
    allowed_actions,
    capability_request,
    confirmation_action_problem,
    confirmation_actions,
)

# The plugin is mirrored to a public repository that does not carry the
# monorepo's contracts. There the cross-boundary check has nothing to read, and
# a missing fixture is not a failure of the plugin.
FIXTURES = ROOT.parents[1] / "packages" / "contracts" / "testdata" / "companion"
ADVERTISED = FIXTURES / "qgis_supported_action_kinds.json"
RESPONSES = FIXTURES / "qgis_compose_responses.json"

pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir(), reason="standalone checkout: no monorepo contracts fixtures")


def advertised_now():
    return sorted(IMPLEMENTED_ACTIONS)


def test_the_advertised_fixture_is_what_this_build_sends():
    # The server side is only as honest as this list. A stale copy would test
    # the server against a plugin that no longer exists.
    recorded = json.loads(ADVERTISED.read_text(encoding="utf-8"))
    assert recorded == advertised_now(), (
        "qgis_supported_action_kinds.json is stale; regenerate it with "
        "`python tests/test_server_action_contract.py`"
    )


def _plugin_table(name):
    """A module-level dict literal from plugin.py, which imports Qt and cannot
    be imported here. Read the way tests/test_action_parity.py reads it."""
    tree = ast.parse((ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("plugin.py has no {} table".format(name))


LEGACY_CAPABILITY_IDS = _plugin_table("LEGACY_CAPABILITY_IDS")
PLUGIN_NATIVE_ACTIONS = _plugin_table("PLUGIN_NATIVE_ACTIONS")
ACTIVE_LAYER = "qgis_layer_under_test"


def _recorded_cases():
    return json.loads(RESPONSES.read_text(encoding="utf-8"))


def _response(case):
    return json.loads(json.dumps(case["response"]).replace("__ACTIVE_LAYER__", ACTIVE_LAYER))


def carry_out(response):
    """What this plugin does with one compose response, in its own order.

    Returns (outcomes, problems). The order is `_nivo_composed`'s: every direct
    action through the registry, then the confirmation path, then the problem
    sentence when nothing could be offered. Each step is the plugin's own
    function; only the loop around them is written here.
    """
    outcomes, problems = [], []
    for action in allowed_actions(response):
        tool = action["tool"]
        try:
            resolved = capability_request(tool, action, ACTIVE_LAYER, LEGACY_CAPABILITY_IDS)
        except CapabilityError as error:
            problems.append("{}: {}".format(tool, error))
            continue
        if resolved is None:
            if tool in PLUGIN_NATIVE_ACTIONS:
                outcomes.append(("native", tool))
            else:
                problems.append("{}: no capability and no native handler".format(tool))
            continue
        capability_id, params = resolved
        try:
            validate_request(capability_id, params)
        except CapabilityError as error:
            problems.append("{}: refused by the registry: {}".format(capability_id, error))
            continue
        outcomes.append(("run", capability_id))
    for action in confirmation_actions(response):
        outcomes.append(("confirm", action["params"].get("operation")))
    if not outcomes:
        problem = confirmation_action_problem(response)
        if problem:
            problems.append(problem)
    return outcomes, problems


def test_the_recorded_responses_exist_and_are_many():
    # A fixture with nothing in it would pass every check below.
    cases = _recorded_cases()
    with_actions = [case for case in cases if case["response"].get("companion_actions")]
    assert len(cases) >= 40 and len(with_actions) >= 30, (len(cases), len(with_actions))


@pytest.mark.parametrize("case", _recorded_cases() if RESPONSES.is_file() else [],
                         ids=lambda case: case["name"])
def test_every_action_the_server_sends_is_carried_out(case):
    response = _response(case)
    sent = response.get("companion_actions") or []
    if not sent:
        pytest.skip("the server answered in words: {}".format((response.get("text") or "")[:80]))
    outcomes, problems = carry_out(response)
    assert not problems, "the server sent it and this plugin would not run it: {}".format(problems)
    assert outcomes, "the server sent {} and nothing would happen".format(
        [action.get("kind") for action in sent])


def test_every_direct_action_uses_only_parameters_its_kind_admits():
    # allowed_actions drops an action whose params fall outside SAFE_PARAM_KEYS
    # without a word. Checked separately so a drop is named rather than folded
    # into "nothing would happen".
    for case in _recorded_cases():
        for action in _response(case).get("companion_actions") or []:
            admitted = SAFE_PARAM_KEYS.get(action["kind"])
            if admitted is None or action.get("requires_confirmation"):
                continue
            extra = set(action.get("params") or {}) - admitted
            assert not extra, "{}: {} carries {} which the protocol drops".format(
                case["name"], action["kind"], sorted(extra))


if __name__ == "__main__":
    ADVERTISED.parent.mkdir(parents=True, exist_ok=True)
    ADVERTISED.write_text(json.dumps(advertised_now(), indent=2) + "\n", encoding="utf-8")
    print("wrote", ADVERTISED, len(advertised_now()), "kinds")
