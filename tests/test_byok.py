"""The turn actually reaches the user's own provider, and never Mapdex by accident.

This feature shipped a false privacy claim once: the settings panel said a
stored key meant "requests go straight to that provider" while every turn was
posted to Mapdex /v1/compose. The claim has since been corrected, and these
tests are what has to be true before it may be made again.

Four properties, each of which has its own way of being quietly wrong:

* a configured key routes the turn to the provider, and a turn that never
  contacts Mapdex does not demand a Mapdex account first - refusing it there is
  what made the feature unreachable for the people it exists for;
* the session may plan only from capabilities that execute on this machine, so
  the commercial boundary is a property of the session rather than a hope about
  what the model will pick;
* a provider failure never becomes a Mapdex request on its own, because that
  would upload the context the panel had just promised to keep off Mapdex;
* the loop runs, end to end, over a real provider wire format with the socket
  replaced - a claim about where data goes must not rest on a mocked provider
  object that never encodes a request.

`plugin.py` cannot be imported here (it imports `qgis` at module scope and CI
has no QGIS), so the parts of the branch that live in the plugin are asserted
against its AST, exactly as `test_plugin_dispatch.py` does. The behaviour is
tested where the behaviour is: in `byok.py`, which the plugin drives.
"""
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo.agent import STATE_COMPLETED, AgentSession  # noqa: E402
from mapdex_qgis.byok import (  # noqa: E402
    ByokTurn,
    hosted_fallback,
    is_byok,
    needs_mapdex_account,
    provider_failed,
    provider_failure_notice,
    session_allowance,
)
from mapdex_qgis._vendor.nivo.capabilities import (  # noqa: E402
    CLIENT_QGIS,
    EXEC_REMOTE_SERVICE,
    for_client,
)
from mapdex_qgis._vendor.nivo.providers import ProviderError, build_provider, resolve_runtime  # noqa: E402

SECRET = "sk-test-do-not-log-0123456789abcdef"  # nosec B105 - a fixture, never a real key
KEYED_SETTINGS = {"provider": "openai", "api_key": SECRET, "model": "gpt-4o-mini"}

PLUGIN_SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
PLUGIN_TREE = ast.parse(PLUGIN_SOURCE)


def _method(name):
    for node in ast.walk(PLUGIN_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("plugin.py has no {}".format(name))


def _names(node):
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def _attributes(node):
    return {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}


def call(capability, **params):
    return json.dumps({"action": "call", "capability": capability, "params": params, "why": "step"})


def answer(message):
    return json.dumps({"action": "answer", "message": message})


class ScriptedProvider:
    """A model that replays fixed replies. Never opens a socket."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        return self.replies.pop(0) if self.replies else answer("done")


class BrokenProvider:
    def __init__(self, error=None):
        self.error = error or ProviderError("openai returned HTTP 401.")

    def complete(self, system, messages):
        raise self.error


# --------------------------------------------------------------------------
# Which runtime, and what it is allowed to require
# --------------------------------------------------------------------------

def test_a_configured_key_sends_the_turn_to_the_users_provider():
    assert is_byok(resolve_runtime(KEYED_SETTINGS))
    assert is_byok(resolve_runtime({"provider": "ollama", "base_url": "http://127.0.0.1:11434"}))
    assert not is_byok(resolve_runtime({}))
    assert not is_byok(resolve_runtime({"provider": "openai"}))  # a provider with no key is not BYOK


def test_a_byok_turn_does_not_require_a_mapdex_account():
    # The gate that has to move. A user asking their own model about their own
    # layers has nothing to authenticate to Mapdex for.
    assert not needs_mapdex_account(resolve_runtime(KEYED_SETTINGS))
    assert not needs_mapdex_account(resolve_runtime({"provider": "ollama", "base_url": "http://localhost:11434"}))
    # The hosted turn IS a Mapdex call, so it still does.
    assert needs_mapdex_account(resolve_runtime({}))
    assert needs_mapdex_account(resolve_runtime({"plan_allows_hosted": False}))


def test_the_plugin_gates_on_the_runtime_rather_than_on_the_token():
    """The account check in ask_nivo must be conditional, and BYOK must branch."""
    body = ast.unparse(_method("ask_nivo"))
    assert "needs_mapdex_account(runtime)" in body, (
        "ask_nivo still demands a Mapdex session unconditionally, so a BYOK user cannot reach "
        "their own provider - the exact gap this feature exists to close."
    )
    assert "is_byok(runtime)" in body and "_start_byok_turn" in body, (
        "ask_nivo resolves the runtime and does not branch on it"
    )


# --------------------------------------------------------------------------
# The commercial boundary is a property of the session
# --------------------------------------------------------------------------

def test_the_offline_allowance_excludes_every_mapdex_executed_capability():
    allowance = session_allowance(CLIENT_QGIS)
    server_side = {c.id for c in for_client(CLIENT_QGIS) if c.execution == EXEC_REMOTE_SERVICE}
    assert server_side, "no capability executes on Mapdex any more; this guard is blind"
    assert not (allowance & server_side), sorted(allowance & server_side)
    # And it is not merely empty: the local half of the product is still there.
    assert "analytics.profile@1" in allowance
    assert "inspect.fields@1" in allowance
    assert len(allowance) > 20


def test_a_byok_session_refuses_a_mapdex_capability_the_model_names():
    """The catalogue is guidance; the allowance is the boundary.

    A model that names `mapdex.workflow@1` anyway must not reach the executor,
    or the account gate would be a prompt instruction rather than a rule.
    """
    executed = []
    # A well-formed request, so it is the allowance that refuses it rather than
    # parameter validation happening to catch it first.
    provider = ScriptedProvider([
        call("mapdex.workflow@1", workflow="georeference_maps", source_id="sheet-1"),
        answer("I cannot run Mapdex work without a Mapdex account."),
    ])
    session = AgentSession(
        provider,
        lambda request: executed.append(request["capability"]),
        client=CLIENT_QGIS,
        allowed=session_allowance(CLIENT_QGIS),
    )
    outcome = session.run("georeference this sheet")
    assert executed == [], "a Mapdex Run reached the executor from a session with no Mapdex account"
    assert outcome["state"] == STATE_COMPLETED
    refusals = [item for item in outcome["evidence"] if not item["ok"]]
    assert refusals and "not available" in refusals[0]["error"]


# The allowance is the commercial split, so it is derived from the registry
# rather than from what this build happens to execute. That leaves a gap worth
# pinning rather than leaving to be rediscovered: a capability inside the split
# that nothing here runs is one the model can choose and lose a step on.
ALLOWED_BUT_UNBOUND = {
    "report.build@1": (
        "The documented gap in tests/test_module_reachability.py: it needs a ledger of what "
        "this session measured, which no module keeps. It behaves the same on the hosted path, "
        "and the bounded loop recovers from it within the same turn."
    ),
}


def _declared_ids():
    """Capability ids the registry SOURCES declare.

    Not `all_capabilities()`, for the reason `test_module_reachability.py`
    records: the registry is module-level mutable state and a test registering a
    fake `custom.hillshade@1` to prove the extension point works makes the live
    registry report a test fixture as an unbound product capability - and only
    when the suite runs in the order that puts that test first.

    Both halves are read. The generic catalogue lives in the vendored package;
    Mapdex's five are registered separately, and while the allowance below
    subtracts every remote-service capability anyway, reading only one file
    would stop being true the day a `mapdex.*` capability is declared local.
    """
    import re

    generic = (ROOT / "mapdex_qgis" / "_vendor" / "nivo" / "capabilities.py").read_text(encoding="utf-8")
    ids = set(re.findall(r'_c\("([a-z_]+\.[a-z_0-9]+@[0-9]+)"', generic))
    assert ids, "the vendored capabilities.py parsed to zero capabilities; this guard is now blind"

    host = (ROOT / "mapdex_qgis" / "mapdex_capabilities.py").read_text(encoding="utf-8")
    mapdex = set(re.findall(r'Capability\(\s*"([a-z_]+\.[a-z_0-9]+@[0-9]+)"', host))
    assert mapdex, "mapdex_capabilities.py parsed to zero capabilities; this guard is now blind"
    return ids | mapdex


def test_the_allowance_offers_nothing_new_that_cannot_be_executed():
    from mapdex_qgis.qgis_runtime import bound_capability_ids

    declared = session_allowance(CLIENT_QGIS) & _declared_ids()
    unbound = sorted(declared - set(bound_capability_ids()) - set(ALLOWED_BUT_UNBOUND))
    assert not unbound, (
        "a BYOK session may plan these and nothing here runs them, so the model spends a step "
        "on each: {}. Bind them, or record the decision in ALLOWED_BUT_UNBOUND.".format(unbound)
    )


def test_that_record_does_not_outlive_its_reason():
    from mapdex_qgis.qgis_runtime import bound_capability_ids

    bound = set(bound_capability_ids())
    settled = sorted(name for name in ALLOWED_BUT_UNBOUND if name in bound)
    assert not settled, "these are bound now and should leave ALLOWED_BUT_UNBOUND: {}".format(settled)


def test_the_plugin_binds_the_session_to_that_allowance():
    body = ast.unparse(_method("_start_byok_turn"))
    assert "AgentSession" in body and "allowed=session_allowance" in body, (
        "the BYOK session is built without the offline allowance, so a model naming a Mapdex "
        "Run would be planning against capabilities this turn cannot perform: {}".format(body)
    )


# --------------------------------------------------------------------------
# A provider failure is never a silent Mapdex request
# --------------------------------------------------------------------------

def test_a_provider_failure_does_not_reach_mapdex_on_its_own():
    hosted = []
    turn = ByokTurn(
        AgentSession(BrokenProvider(), lambda request: None, client=CLIENT_QGIS),
        ask_to_use_hosted=lambda notice: False,
        run_hosted=lambda: hosted.append("sent"),
    ).start("how many parcels?")
    system, messages = turn.next_prompt()
    try:
        turn.provider_reply(system, messages)
        raise AssertionError("the broken provider did not raise")
    except ProviderError as error:
        outcome = turn.provider_failed(error)
    assert hosted == [], "the map context went to Mapdex without the user being asked"
    assert outcome["handed_to_mapdex"] is False
    assert "Nothing was sent to Mapdex." in outcome["message"]


def test_the_hosted_path_is_taken_only_on_an_explicit_yes():
    hosted = []
    asked = []

    def ask(notice):
        asked.append(notice)
        return True

    turn = ByokTurn(
        AgentSession(BrokenProvider(), lambda request: None, client=CLIENT_QGIS),
        ask_to_use_hosted=ask,
        run_hosted=lambda: hosted.append("sent"),
    ).start("how many parcels?")
    outcome = turn.provider_failed(ProviderError("openai returned HTTP 500."))
    assert len(asked) == 1, "the user was not asked before their context went to Mapdex"
    assert hosted == ["sent"]
    assert outcome["handed_to_mapdex"] is True
    assert "Asking Mapdex instead." in outcome["message"]


def test_no_offer_is_made_when_there_is_no_hosted_path_to_offer():
    # A signed-out BYOK user must not be asked a question whose yes does nothing.
    asked = []
    assert hosted_fallback("boom", lambda notice: asked.append(notice) or True, None) is False
    assert asked == [], "asked about a fallback that cannot happen"
    assert hosted_fallback("boom", None, lambda: None) is False


def test_a_failure_notice_never_carries_the_key():
    notice = provider_failure_notice(ProviderError("openai rejected Bearer {}".format(SECRET)))
    assert SECRET not in notice
    assert "[redacted]" in notice
    # An exception with no message still says something.
    assert "TimeoutError" in provider_failure_notice(TimeoutError())


def test_the_plugin_failure_handler_cannot_reach_mapdex_directly():
    """Only the asking function may start a hosted turn after a provider error."""
    handler = _method("_byok_failed")
    assert "_start_hosted_turn" not in _attributes(handler), (
        "the provider-failure handler starts the hosted turn itself, so the user is never asked"
    )
    assert {"provider_failed"} & _names(handler) or "provider_failed" in _attributes(handler), (
        "the failure handler does not go through the function that asks first"
    )
    replied = _method("_byok_replied")
    assert "_start_hosted_turn" not in _attributes(replied)
    offer = _method("_offer_hosted_path")
    assert "question" in _attributes(offer), "the offer is not a question the user answers"


# --------------------------------------------------------------------------
# End to end, over a real wire format, with the socket replaced
# --------------------------------------------------------------------------

class FakeOpenAI:
    """The OpenAI chat-completions wire format, without a network.

    Substituted at the transport, not at the provider, so the request the
    provider actually encodes - model, headers, message roles - is exercised.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, url, headers, payload, timeout):
        self.requests.append({"url": url, "headers": dict(headers), "payload": payload})
        body = self.replies.pop(0) if self.replies else answer("done")
        return json.dumps({"choices": [{"message": {"content": body}}]}).encode("utf-8")


def _drive(turn):
    """The plugin's loop, minus the threads. Same alternation, same order."""
    for _step in range(8):
        prompt = turn.next_prompt()
        if prompt is None:
            return turn.exhausted()
        reply = turn.provider_reply(*prompt)
        outcome = turn.deliver(reply)
        if outcome is not None:
            return outcome
    return turn.exhausted()


def test_a_byok_user_with_no_mapdex_token_reaches_their_own_provider():
    transport = FakeOpenAI([
        call("inspect.fields@1", layer_id="parcels"),
        answer("The layer has one field: landuse."),
    ])
    executed = []

    def executor(request):
        executed.append(request["capability"])
        return {"fields": [{"name": "landuse", "type": "text"}]}

    # No Mapdex token anywhere in this test, and nothing that could use one.
    provider = build_provider(KEYED_SETTINGS, transport=transport)
    session = AgentSession(
        provider, executor, client=CLIENT_QGIS, allowed=session_allowance(CLIENT_QGIS)
    )
    outcome = _drive(ByokTurn(session).start("what fields does this layer have?", {"crs": "EPSG:3857"}))

    assert outcome["state"] == STATE_COMPLETED
    assert outcome["message"].startswith("The layer has one field")
    assert executed == ["inspect.fields@1"], "the capability the model chose did not run"
    # The request really went to the vendor, carrying the key and the model.
    assert [item["url"] for item in transport.requests] == ["https://api.openai.com/v1/chat/completions"] * 2
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer " + SECRET
    assert transport.requests[0]["payload"]["model"] == "gpt-4o-mini"
    # The measured fact came from the executor, not from the model.
    assert outcome["evidence"][0]["result"]["fields"][0]["name"] == "landuse"


def test_the_key_never_travels_in_the_prompt_or_the_context():
    transport = FakeOpenAI([answer("ok")])
    session = AgentSession(
        build_provider(KEYED_SETTINGS, transport=transport),
        lambda request: None,
        client=CLIENT_QGIS,
        allowed=session_allowance(CLIENT_QGIS),
    )
    _drive(ByokTurn(session).start("hello", {"active_layer": {"name": "parcels"}}))
    payload = json.dumps(transport.requests[0]["payload"])
    assert SECRET not in payload, "the provider key reached the model prompt"


def test_a_blank_model_field_resolves_a_default_rather_than_sending_an_empty_one():
    transport = FakeOpenAI([answer("ok")])
    provider = build_provider({"provider": "openai", "api_key": SECRET, "model": ""}, transport=transport)
    provider.complete("system", [{"role": "user", "content": "hi"}])
    assert transport.requests[0]["payload"]["model"] == "gpt-4o-mini"


def test_the_confirmation_gate_still_belongs_to_the_registry_inside_the_loop():
    """A consequential capability asks a person, whatever the model says."""
    executed = []
    provider = ScriptedProvider([call("field.calculate@1", layer_id="parcels", field="area", expression="$area")])
    session = AgentSession(
        provider,
        lambda request: executed.append(request["capability"]),
        client=CLIENT_QGIS,
        confirm=lambda decision: False,
        allowed=session_allowance(CLIENT_QGIS),
    )
    outcome = _drive(ByokTurn(session).start("add an area column"))
    assert executed == []
    assert outcome["kind"] == "confirmation_required"
    # And the plugin asks the registry, not the model, which capability that is.
    body = ast.unparse(_method("_confirm_byok_step"))
    assert "_confirm_capability" in body and "capability" in body


# --------------------------------------------------------------------------
# Threading: QGIS reads on the main thread, the provider call off it
# --------------------------------------------------------------------------

def test_only_the_provider_call_is_handed_to_the_worker_thread():
    """The hosted path learned this the hard way; the loop must not relearn it.

    Reading `iface.activeLayer()`, the canvas or the layer tree from a QgsTask
    returned an empty snapshot and Nivo answered "no layer is active yet" with a
    layer plainly open. So the work function handed to `_task` may call exactly
    one thing.
    """
    step = _method("_byok_step")
    work = None
    for node in ast.walk(step):
        if isinstance(node, ast.Lambda):
            work = node
            break
    assert work is not None, "_byok_step no longer hands a work function to the task"
    assert _attributes(work) == {"provider_reply"}, (
        "the worker thread does more than the model call: {}".format(sorted(_attributes(work)))
    )

    # And the context is built before the branch, on the thread that may read QGIS.
    ask = ast.unparse(_method("ask_nivo"))
    assert ask.index("companion_context") < ask.index("_start_byok_turn"), (
        "the map context is built after the turn is dispatched"
    )


def test_the_capabilities_run_where_qgis_can_be_touched():
    # `deliver` executes capabilities, so it must be called from the callback
    # the task manager runs on the main thread, never inside the work function.
    replied = ast.unparse(_method("_byok_replied"))
    assert "turn.deliver(reply)" in replied
    step = ast.unparse(_method("_byok_step"))
    assert "deliver" not in step, "a capability would execute on the worker thread"


def test_a_superseded_turn_stops_the_loop():
    # Stop bumps the request id; a late reply from the provider must not carry
    # on executing capabilities against a map the user has moved on from.
    for name in ("_byok_replied", "_byok_finished", "_byok_failed"):
        body = ast.unparse(_method(name))
        assert "request_id != self._nivo_request_id" in body, name


def test_provider_failed_reports_the_state_it_left_the_session_in():
    session = AgentSession(BrokenProvider(), lambda request: None, client=CLIENT_QGIS)
    turn = ByokTurn(session).start("anything")
    outcome = turn.provider_failed(ProviderError("nope"))
    assert outcome["kind"] == "provider_failed"
    assert session.state == outcome["state"]
    # No offer configured at all is still not a silent send.
    assert outcome["handed_to_mapdex"] is False


def test_the_bare_failure_helper_matches_the_turn_method():
    # The plugin uses the free function when the provider could not even be
    # built, and the method once a turn exists. They must not drift.
    plain = provider_failed(ProviderError("nope"))
    turn = ByokTurn(AgentSession(BrokenProvider(), lambda request: None, client=CLIENT_QGIS))
    turn.start("anything")
    assert plain["message"] == turn.provider_failed(ProviderError("nope"))["message"]


# --------------------------------------------------------------------------
# The vendor's name is ours, not the user's
# --------------------------------------------------------------------------

def _real_provider_failures():
    """Every provider's own failure text, produced by the provider itself.

    Typed literals would drift from the messages that actually ship, which is
    the whole way this leak stayed invisible: the sentences live in the
    package and were read by nobody reviewing the panel.
    """
    from mapdex_qgis._vendor.nivo.providers import PROVIDERS

    for name, cls in sorted(PROVIDERS.items()):
        provider = cls(api_key="sk-test-key-value", base_url="https://example.invalid",
                       transport=lambda *args, **kwargs: b"{}")
        try:
            provider.complete("system", [{"role": "user", "content": "hi"}])
        except ProviderError as error:
            yield name, str(error)
            continue
        raise AssertionError("{} did not fail on an empty response".format(name))


def test_a_failure_notice_never_names_the_model_vendor():
    """Which company answered is our routing, not a caption for the user.

    Every provider names itself in its errors, correctly - a log that says
    "returned HTTP 401" and not which service is useless. Those sentences were
    handed to the panel verbatim, so a turn that failed announced the vendor.
    """
    vendors = ("openai", "anthropic", "gemini", "ollama")
    seen = 0
    for name, detail in _real_provider_failures():
        notice = provider_failure_notice(detail)
        seen += 1
        assert name.split("_")[0] in detail.lower(), "the provider should name itself internally"
        for vendor in vendors:
            assert vendor not in notice.lower(), "{} leaked through {}".format(vendor, name)
    assert seen >= 4, "the registry should have produced several failures"


def test_masking_leaves_the_users_own_settings_label_alone():
    """"OpenAI-compatible" is the entry they picked in the dropdown."""
    from mapdex_qgis.byok import mask_vendor

    assert mask_vendor("an OpenAI-compatible provider needs a base URL") == (
        "an OpenAI-compatible provider needs a base URL")


def test_the_status_line_never_reads_the_provider_identity():
    """The panel used to say "Asking openai…" while the turn was in flight."""
    method = _method("_start_byok_turn")
    assert "describe" not in _attributes(method), (
        "provider identity must not be read for display")
    literals = " ".join(node.value for node in ast.walk(method)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)).lower()
    for vendor in ("openai", "anthropic", "gemini", "ollama"):
        assert vendor not in literals
