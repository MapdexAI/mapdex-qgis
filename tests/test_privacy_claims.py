"""A privacy claim may not outrun the code that would make it true.

The plugin told plugins.qgis.org, its README and its own settings panel that a
configured model key meant "requests go straight to that provider" and "this
request does not pass through Mapdex servers". None of it was true. The BYOK
machinery existed and was good - five providers, encrypted key storage, a
bounded agent loop - and nothing called it: `resolve_runtime` had one call site
that set a label, `build_provider` had no call site at all, and `ask_nivo`
called `/v1/compose` unconditionally. So a user who read the promise, pasted a
key and asked a question uploaded their map context to Mapdex believing they had
just stopped doing that.

That is the one defect class a test has to hold shut, because it is invisible
from either side alone: the copy reads as a description of the code, and the
code reads as a feature waiting to be advertised. Only the pair is wrong.

The invariant, in both directions:

* no user-facing surface may assert a direct-to-provider path while `plugin.py`
  has no branch that actually takes one;
* once that branch exists, the panel MUST say so - a product that quietly
  stopped sending context to Mapdex and still says it is sending it has made
  the user's decision for them in the other direction.

Developer documentation is deliberately out of scope and listed below with the
reason. `NIVO-AGENT.md` and `providers.py` describe the design a contributor is
implementing; they carry an explicit "not wired yet" note instead. The surfaces
guarded here are the ones a user reads and cannot check.

The pattern is `apps/web/src/lib/honest-claims.test.ts`: every forbidden phrase
carries the fact that forbids it, and a self-check proves the guard is not
blind before it is trusted to pass.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mapdex_qgis"
sys.path.insert(0, str(ROOT))

from mapdex_qgis.credentials import describe_privacy  # noqa: E402

# Surfaces a user reads and has no way to verify. A false sentence here is the
# product lying to somebody about where their data went.
USER_FACING = (
    "metadata.txt",   # the plugins.qgis.org listing
    "README.md",      # the repository front page
    "mapdex_qgis/panel.py",  # the settings panel the key is typed into
)

# Files that may describe the intended design, because their reader is the
# person implementing it rather than the person trusting it. Both carry an
# explicit statement that the path is not wired.
DESIGN_DOCS = ("NIVO-AGENT.md", "mapdex_qgis/providers.py")


class Claim:
    """A phrasing that asserts the turn bypasses Mapdex, and why that is load-bearing."""

    def __init__(self, identifier: str, pattern: str, because: str):
        self.id = identifier
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.because = because


DIRECT_PATH_CLAIMS = (
    Claim(
        "requests-go-direct",
        r"requests?\s+(go|goes)\s+(straight|direct(ly)?)\s+to",
        "The shipped 0.10.0 metadata said 'requests go straight to that provider'. Every request "
        "went to /v1/compose.",
    ),
    Claim(
        "does-not-pass-through-mapdex",
        r"(never|not|n't)\s+(pass|passes|transit|transits|go|goes)\s+through\s+Mapdex",
        "describe_privacy rendered 'this request does not pass through Mapdex servers' in the "
        "settings panel, next to the field the key was typed into.",
    ),
    Claim(
        "not-sent-to-mapdex",
        r"(is|are)\s+not\s+sent\s+to\s+Mapdex",
        "The local-provider branch claimed the map context 'is not sent to Mapdex' while "
        "companion_context(...) was being posted to Mapdex on the same turn.",
    ),
    Claim(
        "stays-on-this-machine",
        r"stays?\s+on\s+(this|your)\s+(machine|computer)",
        "An Ollama user was told their project metadata stayed local. It did not: the local model "
        "was never contacted at all.",
    ),
)

# The exact sentences that shipped. If the patterns stop matching these, the
# guard has been narrowed into uselessness and would pass on the original bug.
HISTORICAL_VIOLATIONS = (
    "and requests go straight to that provider; keys are stored only in the encrypted QGIS "
    "Authentication Manager",
    "Requests go directly to your provider; keys live only in the QGIS Authentication Manager",
    "When a key is present, requests go **directly to that provider** and never pass through "
    "Mapdex servers.",
    "Your key, sent directly to openai: this request does not pass through Mapdex servers.",
    "Local model: your map context stays on this machine and is not sent to Mapdex.",
)


def _claims_in(text: str) -> list[str]:
    return [claim.id for claim in DIRECT_PATH_CLAIMS if claim.pattern.search(text)]


# -- is there actually a branch? -------------------------------------------

# The function whose entire job is to render the label. A reference to the
# runtime decision here proves nothing about where a turn goes.
LABEL_ONLY = "_refresh_assistant_privacy"

# Consuming the decision means building a provider and driving the local loop.
# Naming `resolve_runtime` is not enough: that is what the label already did.
BYOK_SYMBOLS = frozenset({"build_provider", "AgentSession"})


def _symbols_used_outside_the_label() -> set[str]:
    """Every name referenced by a plugin function that is not the label renderer."""
    tree = ast.parse((PACKAGE / "plugin.py").read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == LABEL_ONLY:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                used.add(inner.id)
    return used


def byok_is_wired() -> bool:
    """True when plugin.py can actually take a turn to the user's own provider."""
    return BYOK_SYMBOLS <= _symbols_used_outside_the_label()


# -- the guard --------------------------------------------------------------

def test_the_patterns_still_match_what_shipped():
    """A ratchet that cannot fail on the original defect is not a ratchet."""
    for sentence in HISTORICAL_VIOLATIONS:
        assert _claims_in(sentence), (
            "no pattern matches a sentence that actually shipped and was false: {!r}. "
            "The guard has been narrowed until it would pass on the bug it exists for.".format(sentence)
        )


def test_the_wiring_probe_is_not_blind():
    """A probe that can never say "wired" would freeze this guard permanently.

    Three ways it could go blind, each checked: the names it looks for get
    renamed, the label function it excludes gets renamed (so every reference
    would be excluded), or plugin.py stops parsing into functions at all.
    """
    from mapdex_qgis.agent import AgentSession  # noqa: F401  - the name must still exist
    from mapdex_qgis.providers import build_provider  # noqa: F401

    source = (PACKAGE / "plugin.py").read_text(encoding="utf-8")
    assert "def {}".format(LABEL_ONLY) in source, (
        "the label renderer was renamed; LABEL_ONLY excludes a function that no longer exists, so "
        "a reference inside it would now count as a real branch."
    )
    used = _symbols_used_outside_the_label()
    assert len(used) > 50, (
        "plugin.py parsed to {} referenced names, which is not a 3,700-line plugin; the AST walk "
        "is looking at the wrong thing.".format(len(used))
    )


def test_user_facing_copy_claims_no_direct_path_without_one():
    """The headline invariant: no promise the code does not keep."""
    if byok_is_wired():
        return  # the branch exists; the converse test below owns this case
    offenders = []
    for relative in USER_FACING:
        found = _claims_in((ROOT / relative).read_text(encoding="utf-8"))
        offenders.extend("{}: {}".format(relative, claim) for claim in found)
    panel_text = " ".join(
        describe_privacy({"runtime": "byok", "provider": provider})
        for provider in ("openai", "anthropic", "gemini", "ollama", "openai_compatible")
    )
    offenders.extend("describe_privacy: {}".format(claim) for claim in _claims_in(panel_text))
    assert not offenders, (
        "these assert the turn bypasses Mapdex while plugin.py has no branch that takes that "
        "path, so every turn goes to /v1/compose and the sentence is false: {}. Either wire the "
        "BYOK branch in ask_nivo, or say what the code does.".format(offenders)
    )


def test_the_panel_states_the_direct_path_once_it_exists():
    """The converse. Understating is a lie in the other direction.

    A user who wired their own key and is still told their context goes to
    Mapdex will keep paying for a hosted path they left, or will avoid asking
    the question they actually wanted to ask.
    """
    if not byok_is_wired():
        return
    for provider in ("openai", "anthropic", "gemini", "ollama"):
        sentence = describe_privacy({"runtime": "byok", "provider": provider})
        assert _claims_in(sentence), (
            "plugin.py now takes BYOK turns straight to the provider, but the panel still tells a "
            "{} user their context is sent to Mapdex: {!r}".format(provider, sentence)
        )


def test_the_hosted_sentence_is_never_ambiguous():
    """The hosted path must say Mapdex receives the context, in every state."""
    sentence = describe_privacy({"runtime": "hosted", "provider": "mapdex"})
    assert "sent to Mapdex" in sentence, sentence
    assert not _claims_in(sentence), (
        "the hosted sentence reads like a bypass claim: {!r}".format(sentence)
    )


def test_the_design_docs_are_exempt_on_purpose():
    """An exemption that stops being needed has to be noticed.

    If a design doc no longer describes the direct path at all, the entry is
    stale and would hide the next surface that quietly acquires the claim.
    """
    for relative in DESIGN_DOCS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert _claims_in(text) or "direct" in text.lower(), (
            "{} is exempted as a design document but no longer describes the direct-to-provider "
            "path; drop it from DESIGN_DOCS.".format(relative)
        )
        assert re.search(r"not\s+(wired|consumed|used)", text, re.IGNORECASE) or byok_is_wired(), (
            "{} describes the direct path as the design while it is not wired, and does not say "
            "so. A contributor reads that as shipped behaviour.".format(relative)
        )
