# SPDX-License-Identifier: MIT
"""The canonical Nivo system contract, read from the package's own data.

The prompt lives as markdown at ``nivo/prompts/nivo.system.md`` and is shipped
as package data. It is text, not code, so it can be reviewed as prose, diffed
line by line and versioned in its own front matter - none of which survives
being pasted into a Python string literal.

Every client drives the agent from THIS text. Two surfaces running different
prompts answer the same question differently, and neither answer is
reproducible; that is the whole reason the contract is a single file rather
than a paragraph in each client.

Only the client's name and its capability catalogue may differ between
surfaces. :func:`system_prompt` substitutes the name; the catalogue is appended
by :class:`nivo.agent.AgentSession`.
"""
from __future__ import annotations

import os

CLIENT_PLACEHOLDER = "{{CLIENT_NAME}}"

PROMPT_FILENAME = "nivo.system.md"
_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", PROMPT_FILENAME)


def prompt_body(text: str) -> str:
    """The contract itself, with the YAML front matter removed.

    The front matter is registry bookkeeping - version, evaluation suite, token
    budget - that a server uses to select and score the prompt. Sending it to a
    model would spend tokens telling it about its own filing.
    """
    if text.startswith("---"):
        return text.split("---", 2)[2].lstrip("\n")
    return text


def _load() -> str:
    with open(_PROMPT_PATH, encoding="utf-8") as handle:
        return handle.read()


#: The raw markdown, front matter included. Hosts that mirror the contract into
#: their own prompt registry copy THIS, so the two never diverge.
NIVO_SYSTEM_PROMPT_SOURCE = _load()

#: The contract as sent to a model.
NIVO_SYSTEM_PROMPT = prompt_body(NIVO_SYSTEM_PROMPT_SOURCE)


def system_prompt(client_name: str) -> str:
    """The shared contract, named for the client it is running in."""
    return NIVO_SYSTEM_PROMPT.replace(CLIENT_PLACEHOLDER, client_name)
