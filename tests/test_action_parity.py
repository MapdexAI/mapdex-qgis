"""Capability negotiation must not lie to the server.

The plugin sends ``supported_action_kinds`` and the server only emits actions
from that list. When the list contained kinds the dispatcher had no branch for,
the user got "Nivo prepared a QGIS action" and a map that never moved - the
assistant looked broken rather than incomplete.

These tests read the dispatcher source and fail when the advertised set and the
implemented set drift apart, in either direction.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.capabilities import get as get_capability  # noqa: E402
from mapdex_qgis.nivo import (  # noqa: E402
    ALLOWED_ACTIONS,
    IMPLEMENTED_ACTIONS,
    LEGACY_ACTIONS,
    companion_context,
)
from mapdex_qgis.qgis_runtime import bound_capability_ids  # noqa: E402

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
PLUGIN_TREE = ast.parse(PLUGIN)

# Handled through the confirmation flow (_confirm_nivo_action ->
# _run_processing_operation) rather than the direct dispatcher, so it appears in
# neither dispatch table.
CONFIRMATION_PATH = frozenset({"qgis:processing_operation@1"})


def _plugin_table(name: str) -> dict:
    """Read a module-level dict literal out of plugin.py.

    The dispatcher used to be an if/elif chain, so this test read its
    `tool == "..."` comparisons. It is now two tables - one translating the
    legacy vocabulary to registered capabilities, one naming the handlers for
    the legacy ids no capability covers - so the same question is asked of them.
    """
    for node in PLUGIN_TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("plugin.py has no {} table".format(name))


LEGACY_CAPABILITY_IDS = _plugin_table("LEGACY_CAPABILITY_IDS")
PLUGIN_NATIVE_ACTIONS = _plugin_table("PLUGIN_NATIVE_ACTIONS")


def _dispatcher_branches() -> set[str]:
    """Every legacy id the dispatcher can actually route somewhere."""
    return set(LEGACY_CAPABILITY_IDS) | set(PLUGIN_NATIVE_ACTIONS)


def test_a_translated_legacy_id_names_a_registered_capability():
    # Translating to an id the registry does not hold would fail validation at
    # the moment the user asked, which is the failure mode this whole table
    # exists to remove.
    for legacy, canonical in LEGACY_CAPABILITY_IDS.items():
        assert get_capability(canonical) is not None, (
            "{} translates to {}, which is not registered".format(legacy, canonical)
        )


def test_a_translated_legacy_id_has_a_bound_executor():
    # A registered capability with no executor raises "not available in this
    # QGIS build yet" - correct, but for a legacy action it would be a
    # regression, because the old if/elif chain could perform it.
    # No second list here on purpose. `bound_capability_ids()` now unions the
    # runtime table with PLUGIN_BOUND_CAPABILITIES, so a plugin-bound id kept
    # separately in this test would be a third place to forget.
    for legacy, canonical in LEGACY_CAPABILITY_IDS.items():
        assert canonical in bound_capability_ids(), (
            "{} translates to {}, which nothing executes".format(legacy, canonical)
        )


def test_the_plugin_native_handlers_exist():
    methods = {
        node.name
        for node in ast.walk(PLUGIN_TREE)
        if isinstance(node, ast.FunctionDef)
    }
    for tool, handler in PLUGIN_NATIVE_ACTIONS.items():
        assert handler in methods, "{} names a missing handler {}".format(tool, handler)


def test_a_legacy_id_is_never_in_both_tables():
    overlap = sorted(set(LEGACY_CAPABILITY_IDS) & set(PLUGIN_NATIVE_ACTIONS))
    assert not overlap, "two routes for the same id: {}".format(overlap)


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
