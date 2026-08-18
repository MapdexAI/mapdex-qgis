"""Mapdex's own server-side capabilities, added to the shared Nivo registry.

The vendored `nivo` package ships every capability that runs on this machine -
inspection, analytics, spatial reasoning, read-only PostGIS, presentation,
field calculation, Processing. It deliberately ships none that run on a
vendor's server, because a public registry cannot carry one company's product
surface: an OSS user installing `nivo-gis` has no Mapdex account and no reason
to be shown `mapdex.workflow@1`.

So the five that do belong to Mapdex are declared here, through the package's
documented extension point (`nivo.capabilities.register`), with the execution
site `EXEC_REMOTE_SERVICE`. That one field is what makes the commercial split a
registry fact rather than a list somebody maintains: `offline_capability_ids`
subtracts exactly the remote-service capabilities, so a BYOK turn that never
contacts Mapdex is never offered work that could only happen there.

WHEN THIS RUNS
--------------
Before anything reads the catalogue, and structurally rather than by
convention. `mapdex_qgis/__init__.py` imports this module, and Python runs a
package's `__init__` before any of its submodules - including
`mapdex_qgis._vendor.nivo.capabilities` itself. There is no import path into
the registry that can skip it.

That ordering is load-bearing, not tidiness. `offline_capability_ids()` and the
`PLUGIN_BOUND_CAPABILITIES` parity check both answer questions ABOUT the
catalogue; asked of a registry missing these five they return a confident wrong
answer and pass, which is a green test proving nothing.
`tests/test_capability_registration_order.py` fails if that ever stops holding.
"""
from __future__ import annotations

from ._vendor.nivo.capabilities import (
    CLIENT_QGIS,
    CLIENT_WORKSPACE,
    EXEC_REMOTE_SERVICE,
    RISK_CONSEQUENTIAL,
    Capability,
    register,
    set_client_display_name,
)

#: The capabilities that execute as a durable Mapdex Run. Named here so a test
#: can assert the set rather than re-listing it, and so removing one is a
#: deliberate edit in one place.
MAPDEX_CAPABILITY_IDS = (
    "mapdex.workflow@1",
    "mapdex.batch@1",
    "mapdex.jobs@1",
    "mapdex.open_review@1",
    "mapdex.import_result@1",
)

# What the model is told it is running inside. The package defaults the
# workspace client to "the workspace"; Mapdex names its own surface, so the
# prompt reads the same as it did before the registry became vendor-neutral.
set_client_display_name(CLIENT_WORKSPACE, "the Mapdex workspace")

_BOTH = (CLIENT_QGIS, CLIENT_WORKSPACE)

register(Capability(
    "mapdex.workflow@1", "mapdex",
    "Run a Mapdex workflow (georeference, extract, validate, convert) on a source.",
    params={
        "workflow": {"type": "string", "required": True,
                     "enum": ["georeference_maps", "digitize_parcels", "validate_deliver"]},
        "source_id": {"type": "string", "required": True},
    },
    risk=RISK_CONSEQUENTIAL, execution=EXEC_REMOTE_SERVICE, produces=("run",),
    clients=_BOTH,
))
register(Capability(
    "mapdex.batch@1", "mapdex",
    "Run one verified workflow across several prepared sources.",
    params={
        "workflow": {"type": "string", "required": True},
        "source_ids": {"type": "list", "required": True},
    },
    risk=RISK_CONSEQUENTIAL, execution=EXEC_REMOTE_SERVICE, produces=("batch",),
    clients=_BOTH,
))
register(Capability(
    "mapdex.jobs@1", "mapdex",
    "List Mapdex runs with their state, and explain a failure.",
    params={"state": {"type": "string",
                      "enum": ["all", "active", "failed", "review_required", "completed"],
                      "default": "all"}},
    execution=EXEC_REMOTE_SERVICE, produces=("jobs",), clients=_BOTH,
))
register(Capability(
    "mapdex.open_review@1", "mapdex",
    "Open the Mapdex review surface for a run that needs review.",
    params={"run_id": {"type": "string"}},
    execution=EXEC_REMOTE_SERVICE, produces=("ui_effect",), clients=_BOTH,
))
register(Capability(
    "mapdex.import_result@1", "mapdex",
    "Add a completed Mapdex result to the map.",
    params={"run_id": {"type": "string", "required": True}},
    risk=RISK_CONSEQUENTIAL, execution=EXEC_REMOTE_SERVICE, produces=("layer",),
    clients=_BOTH,
))
