"""The plugin's copy of the generated contracts must not drift.

`mapdex_qgis/generated_contracts.py` is a vendored copy of the Python bindings
`make gen` produces in packages/contracts. The packager and the public-repo
sync both substitute the generated file, so a stale copy does not break a
build — it just quietly misleads anyone reading the plugin source, and it ships
as-is in the public mirror if the generator is ever skipped there.

Skipped in the public repository, where the monorepo path does not exist.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDORED = ROOT / "mapdex_qgis" / "generated_contracts.py"
GENERATED = ROOT.parents[1] / "packages" / "contracts" / "generated_python" / "contracts.py"


def test_vendored_contracts_match_the_generator_output():
    if not GENERATED.is_file():
        pytest.skip("standalone checkout: no monorepo contracts package")
    generated = GENERATED.read_text(encoding="utf-8")
    vendored = VENDORED.read_text(encoding="utf-8")
    assert vendored == generated, (
        "mapdex_qgis/generated_contracts.py is stale; copy "
        "packages/contracts/generated_python/contracts.py over it after `make gen`"
    )
