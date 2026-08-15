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

from .capabilities import (
    CLIENT_QGIS,
    CapabilityError,
    catalog_for_prompt,
    get as get_capability,
    validate_request,
)

MAX_STEPS = 8
MAX_OBSERVATION_CHARS = 4000
MAX_EVIDENCE = 40
MAX_HISTORY_TURNS = 8

STATE_IDLE = "idle"
STATE_PLANNING = "planning"
STATE_CLARIFY = "clarification_required"
STATE_CONFIRM = "confirmation_required"
STATE_EXECUTING = "executing"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED})

SYSTEM_PROMPT = """You are Nivo, a GIS analyst working inside {client}.

You do not write code, SQL, expressions, algorithm ids, or file paths. You act \
only by choosing one capability from the catalogue below and supplying its \
parameters. Anything you name that is not in the catalogue does not exist.

Reply with ONE JSON object and nothing else:

  {{"action": "call", "capability": "<id>", "params": {{...}}, "why": "<short reason>"}}
  {{"action": "answer", "message": "<final answer to the user>"}}
  {{"action": "clarify", "message": "<the single question you need answered>"}}

Rules:
- Never state a number, count, area or distance that is not present in the \
observations. If you need a fact, call a capability to measure it.
- Inspect before you decide. Check a field's type and distribution before \
choosing how to classify or style it.
- When a result is spatial, follow it with a capability that makes it visible \
on the map. An analysis the user cannot see on the map is only half an answer.
- Ask for clarification only when two plausible targets exist. If exactly one \
obvious target exists, use it.
- Answer in the same language the user wrote in. Keep normal GIS terms in \
their conventional form.
- When you have enough to answer, use "answer". Do not keep calling capabilities.

Context:
{context}

Capabilities:
{catalog}
"""


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
    """One executed capability and the facts it produced."""

    def __init__(self, capability: str, params: Mapping[str, Any], result: Any, ok: bool = True, error: str = ""):
        self.capability = capability
        self.params = dict(params or {})
        self.result = result
        self.ok = bool(ok)
        self.error = str(error or "")

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
    ):
        self.provider = provider
        self.executor = executor
        self.client = client
        self.confirm = confirm
        self.max_steps = max(1, min(int(max_steps or MAX_STEPS), MAX_STEPS))
        self.state = STATE_IDLE
        self.observations: list[Observation] = []
        self.history: list[dict[str, str]] = []
        self.steps = 0
        self._called: set[str] = set()

    # -- context -----------------------------------------------------------

    def _context_block(self, context: Mapping[str, Any]) -> str:
        try:
            return json.dumps(context or {}, default=str, ensure_ascii=False)[:MAX_OBSERVATION_CHARS]
        except (TypeError, ValueError):
            return "{}"

    def _system_prompt(self, context: Mapping[str, Any]) -> str:
        catalog = catalog_for_prompt(self.client)
        return SYSTEM_PROMPT.format(
            client="QGIS" if self.client == CLIENT_QGIS else "the Mapdex workspace",
            context=self._context_block(context),
            catalog=json.dumps(catalog, ensure_ascii=False),
        )

    def _messages(self, objective: str) -> list[dict[str, str]]:
        messages = list(self.history[-MAX_HISTORY_TURNS * 2:])
        messages.append({"role": "user", "content": objective})
        for observation in self.observations[-MAX_HISTORY_TURNS:]:
            messages.append({"role": "assistant", "content": "Observation: " + observation.summarize()})
        return messages

    # -- the loop ----------------------------------------------------------

    def run(self, objective: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pursue one objective to an answer, a clarification, or a failure."""
        objective = str(objective or "").strip()
        if not objective:
            raise AgentError("an objective is required")
        self.state = STATE_PLANNING
        context = dict(context or {})
        system = self._system_prompt(context)
        pending_confirmation: dict[str, Any] | None = None
        for _step in range(self.max_steps):
            self.steps += 1
            try:
                reply = self.provider.complete(system, self._messages(objective))
            except Exception as exc:
                self.state = STATE_FAILED
                raise AgentError(str(exc))
            try:
                decision = parse_decision(reply, self.client)
            except CapabilityError as exc:
                # A rejected capability is a recoverable mistake: tell the model
                # what went wrong and let it choose again within the budget.
                self.observations.append(Observation("(rejected)", {}, None, ok=False, error=str(exc)))
                continue
            except AgentError as exc:
                self.observations.append(Observation("(unparsed)", {}, None, ok=False, error=str(exc)))
                continue
            if decision["action"] == "answer":
                self.state = STATE_COMPLETED
                return self._result(decision["message"], pending_confirmation)
            if decision["action"] == "clarify":
                self.state = STATE_CLARIFY
                return self._result(decision["message"], None, kind="clarify")
            signature = decision["capability"] + json.dumps(decision["params"], sort_keys=True, default=str)
            if signature in self._called:
                # Repeating an identical call cannot produce new information;
                # it only burns the budget and the user's provider credits.
                self.observations.append(
                    Observation(decision["capability"], decision["params"], None, ok=False,
                                error="already called with these parameters; use the existing observation")
                )
                continue
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
                self.observations.append(Observation(decision["capability"], decision["params"], result))
            except Exception as exc:
                self.observations.append(
                    Observation(decision["capability"], decision["params"], None, ok=False, error=str(exc))
                )
            self.state = STATE_PLANNING
        # The budget is a real bound, so it must produce an honest outcome
        # rather than an invented conclusion.
        self.state = STATE_FAILED
        return self._result(
            "I ran out of steps before I could finish that. Here is what I measured so far.",
            None,
            kind="incomplete",
        )

    def _result(self, message: str, pending: Mapping[str, Any] | None, kind: str = "answer") -> dict[str, Any]:
        self.history.append({"role": "user", "content": ""})
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
