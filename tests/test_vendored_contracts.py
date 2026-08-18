"""The plugin's copy of the generated contracts must not drift.

`mapdex_qgis/generated_contracts.py` is a vendored copy of the plugin-scoped
excerpt `make gen` produces at packages/contracts/generated_python/
contracts_qgis.py. The plugin ships standalone -- published to its own
repository and uploaded to plugins.qgis.org as a zip -- so it cannot import the
contracts package and has to carry the symbols it uses.

It carries an excerpt rather than the whole module on purpose: the full
bindings are ~5,000 lines, of which the plugin uses two symbols, and vendoring
all of it made the plugin look far more coupled to the monorepo than it is.

The packager and the public-repo publish both work from this file, so a stale
copy does not break a build -- it just quietly misleads anyone reading the
plugin source, and ships as-is in the public mirror.

Skipped in the public repository, where the monorepo path does not exist.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDORED = ROOT / "mapdex_qgis" / "generated_contracts.py"
GENERATED = ROOT.parents[1] / "packages" / "contracts" / "generated_python" / "contracts_qgis.py"


def test_vendored_contracts_match_the_generator_output():
    if not GENERATED.is_file():
        pytest.skip("standalone checkout: no monorepo contracts package")
    generated = GENERATED.read_text(encoding="utf-8")
    vendored = VENDORED.read_text(encoding="utf-8")
    assert vendored == generated, (
        "mapdex_qgis/generated_contracts.py is stale; run `make gen` in the "
        "monorepo, which regenerates the excerpt and copies it over"
    )
