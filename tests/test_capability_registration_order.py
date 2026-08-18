"""Mapdex's five capabilities must be registered before anything reads the catalogue.

The vendored `nivo` package ships no `mapdex.*` capability - a public registry
cannot carry one vendor's server-side product surface - so
`mapdex_qgis/mapdex_capabilities.py` adds them through the package's `register`
extension point. That import has to have run before any code asks the registry a
question, and "has to" is doing real work here.

Every consumer of the catalogue asks a question ABOUT it rather than for one
entry:

* `offline_capability_ids()` returns everything MINUS the remote-service
  capabilities, so on a registry missing the five it returns them as if they
  were local - and a BYOK session that promises never to contact Mapdex would be
  offered `mapdex.workflow@1`;
* the `PLUGIN_BOUND_CAPABILITIES` parity check compares declared against bound,
  so on a short registry it compares a shorter list to a shorter list and
  passes.

Neither raises. Both return a confident wrong answer, and the tests that stand
on them go green while proving nothing. So the ordering is structural -
`mapdex_qgis/__init__.py` imports the registrations, and Python runs a package's
`__init__` before any of its submodules, `_vendor.nivo.capabilities` included -
and this file is what fails if that ever stops being true.

The first test runs in a SUBPROCESS on purpose. Inside this suite some earlier
test has always already imported the plugin package, so the registry is
populated no matter what `__init__.py` does; asking in-process would answer a
different question and pass either way.
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis._vendor.nivo.capabilities import (  # noqa: E402
    CLIENT_QGIS,
    CLIENT_WORKSPACE,
    EXEC_REMOTE_SERVICE,
    all_capabilities,
    client_display_name,
    for_client,
    offline_capability_ids,
)
from mapdex_qgis.mapdex_capabilities import MAPDEX_CAPABILITY_IDS  # noqa: E402

# The most direct route into the registry anyone could take: import the
# vendored capabilities module and nothing else. If the five are absent here,
# they are absent for every consumer that starts the same way.
PROBE = """
import sys
sys.path.insert(0, {root!r})
from mapdex_qgis._vendor.nivo import capabilities
print(",".join(sorted(c.id for c in capabilities.all_capabilities() if c.id.startswith("mapdex."))))
"""


def _probe(body: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", body.format(root=str(ROOT))],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_the_registry_alone_already_has_the_mapdex_capabilities():
    """A fresh interpreter, the shortest import, no plugin module loaded."""
    registered = _probe(PROBE)
    assert registered == ",".join(sorted(MAPDEX_CAPABILITY_IDS)), (
        "importing mapdex_qgis._vendor.nivo.capabilities produced {!r}. The registrations in "
        "mapdex_qgis/mapdex_capabilities.py did not run first, so every question asked of this "
        "catalogue is answered against an incomplete set - silently, and in the passing "
        "direction.".format(registered)
    )


def test_the_ordering_is_structural_rather_than_a_convention():
    """`__init__.py` is what guarantees it; a comment somewhere would not."""
    source = (ROOT / "mapdex_qgis" / "__init__.py").read_text(encoding="utf-8")
    assert "from . import mapdex_capabilities" in source, (
        "mapdex_qgis/__init__.py no longer imports mapdex_capabilities. Whatever imports it now "
        "has to run before every catalogue read on every path, which a package __init__ is the "
        "only thing that guarantees."
    )


def test_the_probe_can_fail():
    """A probe that cannot come back empty would freeze this guard permanently."""
    empty = _probe("""
import sys
sys.path.insert(0, {root!r})
from mapdex_qgis._vendor.nivo import capabilities
capabilities._REGISTRY.clear()
print(",".join(sorted(c.id for c in capabilities.all_capabilities() if c.id.startswith("mapdex."))))
""")
    assert empty == "", "the probe reports mapdex capabilities from a registry that was emptied"


# -- what the ordering buys, stated as the questions that would go wrong ----

def test_the_offline_allowance_actually_subtracts_something():
    allowance = offline_capability_ids(CLIENT_QGIS)
    assert not (allowance & set(MAPDEX_CAPABILITY_IDS)), sorted(allowance & set(MAPDEX_CAPABILITY_IDS))
    remote = {c.id for c in for_client(CLIENT_QGIS) if c.execution == EXEC_REMOTE_SERVICE}
    assert remote == set(MAPDEX_CAPABILITY_IDS), (
        "the remote-service half of the catalogue is {}, not the five Mapdex capabilities; "
        "offline_capability_ids is subtracting the wrong set".format(sorted(remote))
    )


def test_every_mapdex_capability_is_offered_to_both_clients():
    declared = {c.id: c for c in all_capabilities()}
    for identifier in MAPDEX_CAPABILITY_IDS:
        capability = declared.get(identifier)
        assert capability is not None, "{} is not registered".format(identifier)
        assert capability.execution == EXEC_REMOTE_SERVICE, identifier
        assert set(capability.clients) == {CLIENT_QGIS, CLIENT_WORKSPACE}, identifier


@pytest.mark.parametrize("client,name", [(CLIENT_QGIS, "QGIS"), (CLIENT_WORKSPACE, "the Mapdex workspace")])
def test_the_host_names_its_own_clients(client, name):
    """The package cannot know a vendor's product name, so Mapdex supplies it.

    The name reaches the model as `{{CLIENT_NAME}}` in the shared prompt, so a
    default of "the workspace" here would quietly change what the assistant is
    told it is running inside.
    """
    assert client_display_name(client) == name
