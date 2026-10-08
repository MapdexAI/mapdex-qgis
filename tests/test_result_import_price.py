"""Importing a result into QGIS: priced first, and a refusal keeps what arrived.

A placed sheet costs one placement the first time it leaves Mapdex, and the
import of its GeoTIFF is it leaving. Two defects are held here:

* the import downloaded first and priced never, so "Get result from Mapdex" -
  and the import a finished task starts on its own - spent money nobody had
  been shown. Nothing is downloaded now until the server says the import is
  free, or the person agreed to the price it stated;
* a later raster refused for a short balance threw away the results already
  fetched, so three sheets with money for two imported none.

`plugin.py` imports QGIS at module scope and there is no QGIS in CI, so
`_fetch_result_files` is lifted out of its AST and executed against a fake
panel and a fake API, the way test_placement_prerequisite.py does.
"""
import ast
import json
import os
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import leave_price  # noqa: E402
from mapdex_qgis.api_client import MapdexAPIError, safe_filename_part  # noqa: E402
from mapdex_qgis.results import (  # noqa: E402
    collect_layer_imports,
    fallback_artifact_imports,
    geojson_truncation_notice,
    review_run_ids,
    succeeded_run_ids,
)

SOURCE = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _lifted(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {
                "__name__": "mapdex_qgis._lifted",
                "__package__": "mapdex_qgis",
                "leave_price": leave_price,
                "MapdexAPIError": MapdexAPIError,
                "safe_filename_part": safe_filename_part,
                "collect_layer_imports": collect_layer_imports,
                "fallback_artifact_imports": fallback_artifact_imports,
                "geojson_truncation_notice": geojson_truncation_notice,
                "review_run_ids": review_run_ids,
                "succeeded_run_ids": succeeded_run_ids,
                "tempfile": tempfile,
                "os": os,
                "json": json,
            }
            exec(compile(module, "plugin.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError("{} not found in plugin.py".format(name))


FETCH = _lifted("_fetch_result_files")

BATCH = {"items": [
    {"state": "succeeded", "run_id": "run_a"},
    {"state": "succeeded", "run_id": "run_b"},
    {"state": "succeeded", "run_id": "run_c"},
]}


class FakeAPI:
    """Three runs, each placing one sheet; the balance covers `affordable` of them."""

    def __init__(self, charge_cents=200, affordable=3, quote_ok=True):
        self.charge_cents = charge_cents
        self.affordable = affordable
        self.quote_ok = quote_ok
        self.downloads = []
        self.quoted = []

    def run(self, run_id, project_id=""):
        sheet = run_id[-1]
        return {"id": run_id, "result_references": [
            {"kind": "layer", "id": "layer_" + sheet, "summary": "Sheet " + sheet.upper(), "geometry_type": "raster"},
        ]}

    def layer(self, layer_id, project_id=""):
        return {"geometry_type": "raster", "source_file_id": "file_" + layer_id[-1]}

    def placement_quote(self, file_id, project_id=""):
        self.quoted.append(file_id)
        if not self.quote_ok:
            return None
        return {"sheets": 1, "unpaid_sheets": 1 if self.charge_cents else 0,
                "charge_cents": self.charge_cents, "available_cents": 1100}

    def file_bytes(self, file_id, project_id=""):
        if len(self.downloads) >= self.affordable:
            raise MapdexAPIError(
                "balance does not cover it", status=402, code="INSUFFICIENT_CREDITS",
                details={"reason": "placement_leave", "required": 200, "available": 100},
            )
        self.downloads.append(file_id)
        return b"II*\x00"


def panel(api):
    return types.SimpleNamespace(
        api=api,
        imported_layer_ids=set(),
        _active_project_id=lambda: "proj_1",
        _create_survey_layers=lambda drawings: None,
        _announce=lambda *a, **k: None,
        _draft_review_layers=lambda *a, **k: [],
    )


def test_nothing_is_downloaded_until_the_price_was_stated():
    api = FakeAPI(charge_cents=200)
    answer = FETCH(panel(api), BATCH, interactive=True)
    assert answer["needs_consent"] is True
    assert answer["interactive"] is True
    assert answer["quote"]["charge_cents"] == 600
    assert api.downloads == [], "an import downloaded a placed sheet before its price was shown"
    assert sorted(api.quoted) == ["file_a", "file_b", "file_c"]


def test_an_unreadable_price_is_never_read_as_free():
    api = FakeAPI(quote_ok=False)
    answer = FETCH(panel(api), BATCH)
    assert answer["needs_consent"] is True and answer["quote"] is None
    assert api.downloads == []


def test_a_free_import_goes_ahead_on_its_own():
    api = FakeAPI(charge_cents=0)
    answer = FETCH(panel(api), BATCH)
    assert "needs_consent" not in answer
    assert len(answer["files"]) == 3 and answer["refused"] == []


def test_a_refused_sheet_keeps_the_ones_already_fetched_and_is_named():
    api = FakeAPI(charge_cents=200, affordable=2)
    answer = FETCH(panel(api), BATCH, interactive=True, consented=True)
    assert [item["name"] for item in answer["files"]] == ["Mapdex · Sheet A", "Mapdex · Sheet B"]
    assert answer["refused"] == ["Sheet C"]
    # The refusal's numbers travel with it to the dialog.
    assert answer["refusal"]["required"] == 200 and answer["refusal"]["available"] == 100
    message = leave_price.partial_import_message(len(answer["files"]), answer["refused"], answer["refusal"])
    assert message.startswith("Added 2 result layer(s).") and "Sheet C" in message
    assert "It costs $2 and the workspace has $1." in message


def test_any_other_failure_still_fails_the_import():
    api = FakeAPI(charge_cents=0)

    def broken(file_id, project_id=""):
        raise MapdexAPIError("gone", status=404, code="NOT_FOUND")

    api.file_bytes = broken
    try:
        FETCH(panel(api), BATCH, consented=True)
    except MapdexAPIError as exc:
        assert exc.code == "NOT_FOUND"
    else:
        raise AssertionError("a missing file was reported as a partial import")
