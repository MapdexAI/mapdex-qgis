"""The capability subsystem must be reachable from the running plugin.

For most of this repository's life `plugin.py` did not import `capabilities`,
`agent`, `analytics`, `postgis`, `spatial`, `presentation` or `qgis_runtime`.
Roughly 3,570 lines of registry, agent loop, analytics kernel, read-only PostGIS
builder and executor table sat behind green tests and reached no user on any
path, because nothing called them.

Test coverage measures whether code is correct, never whether it is called.
These tests measure the second thing.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")


def test_the_plugin_imports_the_capability_subsystem():
    # The registry now lives in the vendored `nivo` package, so the import is
    # `from ._vendor.nivo.capabilities import ...`; `qgis_runtime` is the
    # plugin's own. Both spellings are checked as written, because the question
    # is whether plugin.py reaches the subsystem, not what it is called today.
    for statement in (r"^from \._vendor\.nivo\.capabilities import ",
                      r"^from \.qgis_runtime import "):
        assert re.search(statement, PLUGIN, re.MULTILINE), (
            "plugin.py has no `{}`; the subsystem is unreachable again".format(statement)
        )


def test_a_canonical_capability_reaches_the_executor():
    # The dispatcher must route a dotted capability id through validation and
    # the executor table rather than only matching legacy `qgis:*` branches.
    assert "_run_capability" in PLUGIN
    assert "validate_request" in PLUGIN
    assert "build_executor" in PLUGIN


def test_the_capability_path_cannot_take_qgis_down():
    """A bare except around the executor call, deliberately.

    This runs inside the host application. An escaping exception is not a stack
    trace in a log, it is a broken QGIS, which is why the plugin grew an error
    boundary after one shipped.
    """
    body = PLUGIN[PLUGIN.index("def _run_capability"):]
    body = body[: body.index("\n    @staticmethod")]
    assert "except CapabilityError" in body, "a refusal must read as a refusal, not a crash"
    assert "except Exception" in body, "the host must survive an executor that raises anything"


def test_the_executor_is_built_lazily():
    # Building it at load time means anything in that chain raising stops the
    # plugin from loading at all, rather than degrading one feature.
    body = PLUGIN[PLUGIN.index("def _capability_executor"):]
    body = body[: body.index("\n    def _run_capability")]
    assert "_nivo_executor" in body and "if executor is None" in body
