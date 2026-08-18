"""No module may be reachable only from its own test.

This repository shipped roughly 3,600 lines of registry, agent loop, analytics
kernel, read-only PostGIS builder, executor table and canvas map tool that
`plugin.py` never imported. Every one of them had passing tests. Test coverage
measures whether code is correct; it says nothing about whether anything calls
it, and the two questions had never been separated.

The failure mode is worse than dead code. `spatial.py` was unreachable while the
server actively emitted four capabilities that needed it, and `maptools.py` was
unreachable while `measure.distance@1` was advertised as a bound capability -
so the only way to measure was to already know both coordinates, which is the
exact gap that module was written to close.

This test walks the real import graph from the plugin entry point. A module that
is deliberately not yet wired belongs in ALLOWED with a reason, so the gap is
recorded rather than merely absent.
"""
import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis"

# Entry points QGIS itself loads. Everything the plugin can do must be reachable
# from one of these.
ROOTS = ("__init__", "plugin")

# Modules that are knowingly not wired yet. Each needs a reason, and the reason
# has to be a decision rather than an oversight.
ALLOWED = {
    "agent": (
        "The bounded BYOK agent loop. It is a second execution model - the "
        "client driving its own provider instead of the server composing - and "
        "wiring it is a product decision about local inference, not a missing "
        "import. Tracked as sprint scope, not closed here."
    ),
    "nivo_prompt": (
        "The vendored system prompt the BYOK loop feeds its provider. It is "
        "reachable exactly when `agent` is, and is listed with it."
    ),
}


def _relative_imports(path: pathlib.Path, modules: set[str]) -> set[str]:
    """Sibling modules this file imports, from `from . import x` and `from .x import y`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            found.add(node.module.split(".")[0])
        for alias in node.names:
            if alias.name in modules:
                found.add(alias.name)
    return found & modules


def _graph() -> tuple[set[str], dict[str, set[str]]]:
    modules = {path.stem for path in PACKAGE.glob("*.py") if path.stem != "__init__"}
    edges = {path.stem: _relative_imports(path, modules) for path in PACKAGE.glob("*.py")}
    return modules, edges


def _reachable() -> set[str]:
    modules, edges = _graph()
    seen: set[str] = set()
    stack = list(ROOTS)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(edges.get(module, ()))
    return seen & modules


def test_every_module_is_reachable_from_the_plugin():
    modules, _edges = _graph()
    unreachable = sorted(modules - _reachable() - set(ALLOWED))
    assert not unreachable, (
        "these modules are imported by nothing the plugin loads, so no user can reach them: {}. "
        "Wire them, delete them, or add them to ALLOWED with the decision that keeps them.".format(unreachable)
    )


def test_the_allowlist_does_not_outlive_its_reason():
    # An entry that has since been wired is a stale exemption: it would hide the
    # next module that falls out of the graph.
    reachable = _reachable()
    stale = sorted(name for name in ALLOWED if name in reachable)
    assert not stale, "these are wired now and should leave ALLOWED: {}".format(stale)


def test_the_allowlist_names_only_modules_that_exist():
    modules, _edges = _graph()
    missing = sorted(name for name in ALLOWED if name not in modules)
    assert not missing, "ALLOWED names modules that are gone: {}".format(missing)


# The server's half of the same contract. Read rather than transcribed: this
# check used to name eight capabilities by hand, which answered the question for
# those eight and for nothing added afterwards - the same shape of blindness the
# module graph above exists to remove.
CATALOGUE = (
    PACKAGE.parents[2] / "packages" / "contracts" / "companion_capabilities.go"
)

# Catalogue entries this build knowingly cannot execute. An entry needs a reason,
# and the reason has to be a decision.
SERVER_SELECTABLE_BUT_UNBOUND = {
    "report.build@1": (
        "The only catalogue entry bound to an intent (REPORT) that nothing here "
        "executes, so every REPORT turn is answered with 'This QGIS installation "
        "does not support that Nivo action' - true, and it reads as the user's "
        "install being at fault. It needs a session ledger of what was actually "
        "measured, which no module keeps: results.py holds Mapdex run payloads, "
        "not this session's measurements. Building a report from anything less "
        "would be a report of things nobody measured. Tracked as sprint scope."
    ),
}


def _catalogue_ids() -> set[str]:
    import re

    source = CATALOGUE.read_text(encoding="utf-8")
    ids = set(re.findall(r'ID:\s*"([a-z_]+\.[a-z_0-9]+@[0-9]+)"', source))
    assert ids, "{} parsed to zero capabilities; this guard is now blind".format(CATALOGUE)
    return ids


def _bound() -> set[str]:
    import sys

    sys.path.insert(0, str(PACKAGE.parent))
    from mapdex_qgis.qgis_runtime import bound_capability_ids

    return set(bound_capability_ids())


def test_the_capabilities_the_server_emits_are_bound_here():
    """The registry may not advertise what the executor cannot perform.

    `bound_capability_ids()` is what the plugin negotiates with, so an unbound
    capability is refused rather than silently dropped - but a capability the
    server has a code path for and the desktop cannot run is still a hole in the
    product, and these five were exactly that.
    """
    if not CATALOGUE.exists():
        return  # standalone plugin checkout: the server catalogue is not present
    unbound = sorted(_catalogue_ids() - _bound() - set(SERVER_SELECTABLE_BUT_UNBOUND))
    assert not unbound, (
        "the server may select these and nothing here executes them: {}. "
        "Bind them, or record the decision in SERVER_SELECTABLE_BUT_UNBOUND.".format(unbound)
    )


def test_the_unbound_catalogue_list_does_not_outlive_its_reason():
    if not CATALOGUE.exists():
        return
    bound = _bound()
    stale = sorted(name for name in SERVER_SELECTABLE_BUT_UNBOUND if name in bound)
    assert not stale, "these are bound now and should leave SERVER_SELECTABLE_BUT_UNBOUND: {}".format(stale)


def test_the_unbound_catalogue_list_names_only_catalogue_entries():
    if not CATALOGUE.exists():
        return
    catalogue = _catalogue_ids()
    missing = sorted(name for name in SERVER_SELECTABLE_BUT_UNBOUND if name not in catalogue)
    assert not missing, "SERVER_SELECTABLE_BUT_UNBOUND names non-catalogue capabilities: {}".format(missing)
