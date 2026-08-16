"""Capability negotiation must not lie to the server.

The plugin sends ``supported_action_kinds`` and the server only emits actions
from that list. When the list contained kinds the dispatcher had no branch for,
the user got "Nivo prepared a QGIS action" and a map that never moved - the
assistant looked broken rather than incomplete.

These tests read the dispatcher source and fail when the advertised set and the
implemented set drift apart, in either direction.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import (  # noqa: E402
    ALLOWED_ACTIONS,
    IMPLEMENTED_ACTIONS,
    LEGACY_ACTIONS,
    companion_context,
)
from mapdex_qgis.qgis_runtime import bound_capability_ids  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")

# Handled through the confirmation flow (_confirm_nivo_action ->
# _run_processing_operation) rather than the direct dispatcher, so it has no
# `tool == "..."` branch to find.
CONFIRMATION_PATH = frozenset({"qgis:processing_operation@1"})


def _dispatcher_branches() -> set[str]:
    start = PLUGIN.index("def _apply_nivo_action")
    end = PLUGIN.index("def _zoom_to_server_extent")
    return set(re.findall(r"tool == \"([^\"]+)\"", PLUGIN[start:end]))


def test_every_advertised_legacy_action_has_a_dispatcher_branch():
    # The legacy vocabulary is hand-written on both sides, so it is the half
    # that can still drift.
    missing = sorted(LEGACY_ACTIONS - _dispatcher_branches() - CONFIRMATION_PATH)
    assert not missing, "advertised but not implemented: {}".format(missing)


def test_every_advertised_capability_has_a_bound_executor():
    # The canonical half is derived from the executor table rather than listed,
    # so this asserts the derivation rather than a copy of it: advertising a
    # capability with no executor is what produces "Nivo prepared a QGIS action"
    # and a canvas that never moves.
    canonical = {action for action in IMPLEMENTED_ACTIONS if not action.startswith("qgis:")}
    assert canonical, "no canonical capability is advertised; the subsystem is unreachable again"
    assert canonical <= bound_capability_ids()


def test_every_implemented_legacy_action_is_advertised():
    # The opposite drift wastes a capability: the desktop can do it, but the
    # server is never told and so never chooses it.
    extra = sorted(_dispatcher_branches() - IMPLEMENTED_ACTIONS)
    assert not extra, "implemented but not advertised: {}".format(extra)


def test_the_advertised_set_is_a_subset_of_the_protocol_allowlist():
    assert IMPLEMENTED_ACTIONS <= ALLOWED_ACTIONS


def test_companion_context_advertises_the_implemented_set():
    advertised = companion_context({"crs": "EPSG:3857"})["supported_action_kinds"]
    assert set(advertised) == set(IMPLEMENTED_ACTIONS)


def test_actions_the_dispatcher_cannot_perform_are_not_advertised():
    # These are accepted by the protocol parser but have no desktop branch yet.
    # Advertising them is what made the server pick a dead end.
    for kind in ("qgis:preview_filter@1", "qgis:semantic_style@1",
                 "qgis:open_review@1", "qgis:open_results@1"):
        assert kind in ALLOWED_ACTIONS
        assert kind not in IMPLEMENTED_ACTIONS
