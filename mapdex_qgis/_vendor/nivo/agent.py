# SPDX-License-Identifier: MIT
"""The bounded agent loop: objective -> capability -> observation -> next step.

Nivo is not a prompt-to-single-action mapper. A real GIS objective ("which
districts have unusually high building density?") needs several steps whose
later choices depend on earlier results: you cannot pick graduated classes
before you know the distribution, or choose a categorised field before you know
its cardinality. So the loop runs:

    objective -> inspect context -> capability -> observation -> next capability

Three properties keep that safe:

* **The model never executes anything.** It returns JSON naming a capability id
  and parameters. :func:`parse_decision` extracts it; the registry validates it;
  trusted executors run it. Prose around the JSON is ignored, and a capability
  that is not registered simply does not resolve.
* **The loop is bounded.** A fixed step budget, a repeat-call guard and a
  terminal-state check mean a confused model cannot spin, and cannot quietly
  spend the user's provider credits in a loop.
* **Facts come from observations, not from the model.** Every number in the
  final answer is carried in the evidence list produced by a real capability
  execution. The model's job is to choose the next step and to explain the
  result, never to supply it.

Language is handled architecturally rather than by keyword lists: the objective
is passed through verbatim and the model is instructed to answer in the user's
language, so Turkish, French and mixed-language technical requests resolve to
the same typed capability as their English equivalent.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

from .prompt import system_prompt
from .capabilities import (
    CLIENT_QGIS,
    CapabilityError,
    client_display_name,
    catalog_for_prompt,
    get as get_capability,
    validate_request,
)

MAX_STEPS = 8
MAX_OBSERVATION_CHARS = 4000
MAX_EVIDENCE = 40
MAX_HISTORY_TURNS = 8
# How many replies in a row may carry no decision before the turn stops. A
# model that has answered in prose three times running is not one step away
# from JSON; continuing only spends the budget, and on a user's own key, their
# money, to reach the same dead end more slowly.
MAX_UNPARSED = 3
# Below this, a stray reply is a fragment rather than something a user
# could read as an answer, and showing it would be worse than the honest
# failure it replaces.
MIN_SALVAGE_CHARS = 12

# The two capability names an observation carries when no capability ran:
# the model named one the registry refused, or it named nothing at all.
REJECTED = "(rejected)"
UNPARSED = "(unparsed)"

# The three corrections the environment speaks back to the model. They are user
# turns, not narration: see AgentSession._messages for why that distinction is
# load-bearing rather than cosmetic.
NOT_A_DECISION = (
    "That reply was not a decision. Reply with exactly one JSON object and "
    "nothing else, in one of these three shapes: "
    '{"action":"call","capability":"<catalogue id>","params":{}}'
    " | "
    '{"action":"answer","message":"<the final answer, in the language the user wrote in>"}'
    " | "
    '{"action":"clarify","message":"<the single question you need answered>"}'
)
LAST_STEP = (
    "This is your final step: no budget remains for another capability. Reply now "
    "with a single answer object, in the language the user wrote in, stating what "
    "was measured and what changed on the map. If something could not be "
    "determined, say so rather than omitting it."
)

STATE_IDLE = "idle"
STATE_PLANNING = "planning"
STATE_CLARIFY = "clarification_required"
STATE_CONFIRM = "confirmation_required"
STATE_EXECUTING = "executing"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED})

# The system contract is NOT written here. It lives in
# nivo/prompts/nivo.system.md and is read by nivo.prompt, so every client
# drives the agent from one versioned text. An inline prompt would be a second
# vocabulary that drifts.


class AgentError(Exception):
    """The agent could not proceed. Always safe to show to the user."""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply.

    Models wrap JSON in prose or fences however they like, and a strict parse
    would turn a usable answer into a failure. Extraction is deliberately
    forgiving; validation afterwards is not.
    """
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = candidate.find("{")
        if start < 0:
            return None
        depth = 0
        end = -1
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end < 0:
            return None
        candidate = candidate[start:end]
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_decision(text: str, client: str = CLIENT_QGIS) -> dict[str, Any]:
    """Turn a model reply into a validated decision, or a failure.

    A reply that names no known action, or a capability the registry does not
    hold, becomes an explicit error rather than a guess. That is the boundary
    which stops "run this python" from ever being a possible outcome.
    """
    parsed = _extract_json(text)
    if parsed is None:
        raise AgentError("the model did not return a decision")
    action = str(parsed.get("action") or "").strip().lower()
    if action == "answer":
        message = str(parsed.get("message") or "").strip()
        if not message:
            raise AgentError("the model returned an empty answer")
        return {"action": "answer", "message": message}
    if action == "clarify":
        message = str(parsed.get("message") or "").strip()
        if not message:
            raise AgentError("the model returned an empty clarification")
        return {"action": "clarify", "message": message}
    if action != "call":
        raise AgentError("unsupported action: {}".format(action[:40] or "(none)"))
    request = validate_request(parsed.get("capability"), parsed.get("params"), client)
    request["action"] = "call"
    request["why"] = str(parsed.get("why") or "").strip()[:280]
    return request


class Observation:
    """One executed capability and the facts it produced.

    ``reply`` is the model text that produced this observation, kept so the
    transcript sent back on the next step can show the model its own turn
    verbatim instead of a reconstruction of it.
    """

    def __init__(self, capability: str, params: Mapping[str, Any], result: Any, ok: bool = True,
                 error: str = "", reply: str = ""):
        self.capability = capability
        self.params = dict(params or {})
        self.result = result
        self.ok = bool(ok)
        self.error = str(error or "")
        self.reply = str(reply or "")

    def as_evidence(self) -> dict[str, Any]:
        """The provenance record that travels with an answer or report."""
        return {
            "capability": self.capability,
            "params": self.params,
            "ok": self.ok,
            "error": self.error,
            "result": self.result,
        }

    def summarize(self) -> str:
        """A bounded textual form for the next model turn."""
        if not self.ok:
            return "{} FAILED: {}".format(self.capability, self.error[:400])
        try:
            body = json.dumps(self.result, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(self.result)
        return "{} -> {}".format(self.capability, body[:MAX_OBSERVATION_CHARS])

    def decision_text(self) -> str:
        """The assistant turn that produced this observation.

        The model's own words when they are available. A session constructed in
        a test may carry none, so the decision it made is rendered in the shape
        the model was asked for rather than left blank - an empty assistant turn
        is rejected outright by some providers.
        """
        if self.reply.strip():
            return self.reply.strip()[:MAX_OBSERVATION_CHARS]
        return json.dumps({"action": "call", "capability": self.capability, "params": self.params},
                          default=str, ensure_ascii=False)[:MAX_OBSERVATION_CHARS]

    def feedback(self) -> str:
        """What the environment says back, as a user turn.

        Three different things, because the model can only act on the
        difference: a result to reason from, a step that did not run and needs
        a different choice, and a reply that was not a decision at all - which
        needs the output contract restated, not a fact.
        """
        if self.ok:
            return "Result of {}: {}".format(self.capability, self.summarize().split(" -> ", 1)[-1])
        if self.capability == UNPARSED:
            return NOT_A_DECISION
        return "{} did not run: {} Choose a different decision.".format(
            self.capability, self.error[:400].rstrip(".") + ".")


class AgentSession:
    """One multi-step GIS objective.

    ``executor`` runs a validated request and returns real facts;
    ``confirm`` is asked before any consequential capability and returns
    True/False. Both are injected so the loop is testable without QGIS, a
    network, or a model.
    """

    def __init__(
        self,
        provider: Any,
        executor: Callable[[Mapping[str, Any]], Any],
        client: str = CLIENT_QGIS,
        confirm: Callable[[Mapping[str, Any]], bool] | None = None,
        max_steps: int = MAX_STEPS,
        allowed: Sequence[str] | None = None,
        client_name: str = "",
    ):
        self.provider = provider
        self.executor = executor
        self.client = client
        # What the model is told it is running inside. Defaults to the name the
        # registry holds for this client, so a host names itself once instead of
        # at every call site.
        self.client_name = str(client_name or client_display_name(client))
        self.confirm = confirm
        self.max_steps = max(1, min(int(max_steps or MAX_STEPS), MAX_STEPS))
        # A narrower catalogue than the client's. A session driving the user's
        # own provider with no host account can reach everything the client can
        # do locally and nothing that executes as a server-side Run - so the
        # subset is passed in rather than inferred, and `None` means "the
        # client's full catalogue" so every existing caller is unchanged.
        self.allowed = frozenset(allowed) if allowed is not None else None
        self.state = STATE_IDLE
        self.observations: list[Observation] = []
        self.history: list[dict[str, str]] = []
        self.steps = 0
        self._unparsed = 0
        self._called: set[str] = set()
        self._objective = ""
        self._system = ""

    def permits(self, capability: str) -> bool:
        return self.allowed is None or str(capability) in self.allowed

    # -- context -----------------------------------------------------------

    def _context_block(self, context: Mapping[str, Any]) -> str:
        try:
            return json.dumps(context or {}, default=str, ensure_ascii=False)[:MAX_OBSERVATION_CHARS]
        except (TypeError, ValueError):
            return "{}"

    def _system_prompt(self, context: Mapping[str, Any]) -> str:
        """The shared contract, plus this client's context and catalogue.

        The rules come from the versioned prompt every client shares; only the
        capability catalogue differs between one client and another, which is
        what keeps the same question answerable the same way on all of them.
        """
        catalog = [entry for entry in catalog_for_prompt(self.client) if self.permits(entry.get("id"))]
        return "\n\n".join((
            system_prompt(self.client_name),
            "<context>\n" + self._context_block(context) + "\n</context>",
            "<capabilities>\n" + json.dumps(catalog, ensure_ascii=False) + "\n</capabilities>",
        ))

    def _messages(self, objective: str) -> list[dict[str, str]]:
        """The transcript for the next step: who said what, honestly.

        Each completed step is a PAIR - the model's own reply as an assistant
        turn, the environment's report as a user turn - and the ordering is not
        cosmetic. Observations used to be appended as assistant turns, so as
        soon as one capability ran the conversation ended on the assistant, and
        the model was being asked to CONTINUE a sentence rather than to answer.
        Two of the four providers here treat a trailing assistant turn as a
        prefill, so the reply came back as more of that sentence, never parsed
        as a decision, and every remaining step went the same way until the
        budget was gone. The first step of a turn always worked, because there
        were no observations yet - which is exactly why the failure looked like
        the model losing the plot on the second question rather than a defect
        in the transcript.

        It also stops the model being shown its own voice reporting facts it
        did not produce, and removes every run of same-role messages, which
        some providers reject and others silently merge.
        """
        messages = list(self.history[-MAX_HISTORY_TURNS * 2:])
        messages.append({"role": "user", "content": objective})
        for observation in self.observations[-MAX_HISTORY_TURNS:]:
            messages.append({"role": "assistant", "content": observation.decision_text()})
            messages.append({"role": "user", "content": observation.feedback()})
        if self.final_step():
            # Folded into the last user turn rather than appended as its own,
            # so the transcript keeps strictly alternating roles.
            joined = messages[-1]["content"] + "\n\n" + LAST_STEP
            messages[-1] = {"role": "user", "content": joined}
        return messages

    # -- the loop ----------------------------------------------------------

    def start(self, objective: str, context: Mapping[str, Any] | None = None) -> "AgentSession":
        """Fix the objective and the system prompt for this turn."""
        objective = str(objective or "").strip()
        if not objective:
            raise AgentError("an objective is required")
        self._objective = objective
        self._system = self._system_prompt(dict(context or {}))
        self.state = STATE_PLANNING
        return self

    def prompt(self) -> tuple[str, list[dict[str, str]]]:
        """The exact (system, messages) pair for the next model call.

        Handed out rather than called internally so a caller can put the model
        call somewhere the loop must not be: in QGIS the network round trip has
        to happen on a worker thread while every capability executes on the main
        thread, because iface, the canvas and the layer tree are main-thread
        only. One loop, two threads, and the loop does not need to know.
        """
        return self._system, self._messages(self._objective)

    def budget_spent(self) -> bool:
        return self.steps >= self.max_steps

    def final_step(self) -> bool:
        """The next call is the last one this turn can afford.

        The budget is a real bound, so the last of it is spent asking for a
        conclusion rather than for another capability nothing will be left to
        follow. A turn that measured what the user asked for and then reported
        only that it ran out of steps has thrown the answer away.
        """
        return self.steps >= self.max_steps - 1

    def advance(self, reply: str) -> dict[str, Any] | None:
        """Consume one model reply. A terminal result, or None to ask again.

        None means the turn is unfinished for a recoverable reason - an
        unparseable reply, a capability the registry rejected, a repeat call, or
        a capability that ran and produced an observation. In every one of those
        the model gets to choose again within the same budget.
        """
        self.steps += 1
        try:
            decision = parse_decision(reply, self.client)
        except CapabilityError as exc:
            # A rejected capability is a recoverable mistake: tell the model
            # what went wrong and let it choose again within the budget.
            self.observations.append(Observation(REJECTED, {}, None, ok=False, error=str(exc), reply=reply))
            self._unparsed = 0
            return None
        except AgentError as exc:
            self.observations.append(Observation(UNPARSED, {}, None, ok=False, error=str(exc), reply=reply))
            self._unparsed += 1
            if self._unparsed >= MAX_UNPARSED:
                return self._incomplete(
                    "The model did not return a usable decision {} times in a row, so I stopped "
                    "instead of asking it again.".format(self._unparsed))
            return None
        self._unparsed = 0
        if decision["action"] == "answer":
            self.state = STATE_COMPLETED
            return self._result(decision["message"], None)
        if decision["action"] == "clarify":
            self.state = STATE_CLARIFY
            return self._result(decision["message"], None, kind="clarify")
        if self.budget_spent():
            # The last step was spent asking for a conclusion, and this is not
            # one. Running it anyway is how a turn came to add a layer to the
            # project and then report that it had run out of steps: real work
            # done, never explained, and nothing said about the part of the
            # objective that was never reached.
            return self._incomplete(
                "I ran out of steps before I could finish that, so I did not start another "
                "operation.")
        if not self.permits(decision["capability"]):
            # Second gate, after the catalogue filter. The prompt is guidance;
            # this is the boundary. A session with no host account must not
            # reach a server-side Run because a model named one anyway.
            self.observations.append(
                Observation(decision["capability"], decision["params"], None, ok=False,
                            error="{} is not available in this session".format(decision["capability"]),
                            reply=reply)
            )
            return None
        signature = decision["capability"] + json.dumps(decision["params"], sort_keys=True, default=str)
        if signature in self._called:
            # Repeating an identical call cannot produce new information;
            # it only burns the budget and the user's provider credits.
            self.observations.append(
                Observation(decision["capability"], decision["params"], None, ok=False,
                            error="already called with these parameters; use the existing observation",
                            reply=reply)
            )
            return None
        self._called.add(signature)
        if decision["requires_confirmation"]:
            if self.confirm is None or not self.confirm(decision):
                self.state = STATE_CONFIRM
                return self._result(
                    "I prepared this step but did not run it: {}".format(
                        (get_capability(decision["capability"]) or _Unknown()).summary
                    ),
                    decision,
                    kind="confirmation_required",
                )
        self.state = STATE_EXECUTING
        try:
            result = self.executor(decision)
            self.observations.append(
                Observation(decision["capability"], decision["params"], result, reply=reply))
        except Exception as exc:
            self.observations.append(
                Observation(decision["capability"], decision["params"], None, ok=False, error=str(exc),
                            reply=reply)
            )
        self.state = STATE_PLANNING
        return None

    def exhausted(self) -> dict[str, Any]:
        """The budget is a real bound, so it produces an honest outcome
        rather than an invented conclusion."""
        return self._incomplete("I ran out of steps before I could finish that.")

    def _incomplete(self, reason: str) -> dict[str, Any]:
        """Stop short, saying why and whether anything was actually measured.

        The old message promised "here is what I measured so far" whatever had
        happened, so a turn that measured nothing at all still pointed at
        evidence it did not have.
        """
        salvaged = self._salvaged_answer()
        if salvaged:
            self.state = STATE_COMPLETED
            return self._result(salvaged, None)
        self.state = STATE_FAILED
        tail = (" Here is what was measured before that." if self.measured_facts()
                else " Nothing was measured, so there is nothing to report.")
        return self._result(reason + tail, None, kind="incomplete")

    def _salvaged_answer(self) -> str:
        """The model's last plain-prose reply, when the turn is ending anyway.

        A model that answers the question in a sentence instead of in the JSON
        it was asked for has still answered it, and discarding that to show
        "I ran out of steps" is the worst of both: the user loses a reply that
        was sitting right there, and a plain greeting - which carries no
        decision either - ends a turn as a failure.

        Nothing is weakened by accepting it. An `answer` decision is free text
        too; the contract exists to keep the model from EXECUTING things, and
        no capability runs on this path. A reply that opens with a brace is a
        broken decision rather than prose, so it is left alone.
        """
        for observation in reversed(self.observations):
            if observation.capability != UNPARSED:
                continue
            text = observation.reply.strip()
            if len(text) >= MIN_SALVAGE_CHARS and not text.startswith("{"):
                return text[:MAX_OBSERVATION_CHARS]
        return ""

    def run(self, objective: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pursue one objective to an answer, a clarification, or a failure.

        The synchronous driver. It is the same loop the asynchronous caller
        runs, expressed in one place, so the two cannot diverge into two
        different agents that answer the same question differently.
        """
        self.start(objective, context)
        for _step in range(self.max_steps):
            system, messages = self.prompt()
            try:
                reply = self.provider.complete(system, messages)
            except Exception as exc:
                self.state = STATE_FAILED
                raise AgentError(str(exc))
            outcome = self.advance(reply)
            if outcome is not None:
                return outcome
        return self.exhausted()

    def _result(self, message: str, pending: Mapping[str, Any] | None, kind: str = "answer") -> dict[str, Any]:
        # The objective, not an empty string: a caller that reuses a session
        # across turns replays this history, and an empty user turn is both a
        # lie about what was asked and a payload some providers refuse.
        self.history.append({"role": "user", "content": self._objective})
        self.history.append({"role": "assistant", "content": message})
        return {
            "kind": kind,
            "state": self.state,
            "message": message,
            "steps": self.steps,
            "pending_confirmation": dict(pending) if pending else None,
            "evidence": self.evidence(),
            "map_effects": self.map_effects(),
        }

    # -- evidence ----------------------------------------------------------

    def evidence(self) -> list[dict[str, Any]]:
        return [observation.as_evidence() for observation in self.observations[-MAX_EVIDENCE:]]

    def map_effects(self) -> list[dict[str, Any]]:
        """Successful observations whose capability changes what the map shows.

        This is what makes an analysis land on the canvas: the caller applies
        these without asking the model what the map should look like.
        """
        effects = []
        for observation in self.observations:
            if not observation.ok:
                continue
            capability = get_capability(observation.capability)
            if capability is None:
                continue
            if {"map_effect", "selection"} & set(capability.produces):
                effects.append({
                    "capability": capability.id,
                    "params": observation.params,
                    "result": observation.result,
                    "reversible": capability.reversible,
                })
        return effects

    def measured_facts(self) -> list[dict[str, Any]]:
        """Only successful measurements - the input to a report."""
        return [
            observation.as_evidence()
            for observation in self.observations
            if observation.ok and observation.result is not None
        ]

    def cancel(self) -> None:
        self.state = STATE_CANCELLED


class _Unknown:
    summary = "an unavailable step"


def objective_needs_confirmation(decisions: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(decision.get("requires_confirmation")) for decision in decisions)
