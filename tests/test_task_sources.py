"""The source LIST: what a batch is made of, and what it refuses.

Qt is absent from this suite, which is why the list model is a plain module.
These tests are about the decisions - dedupe, compatibility, wording - not the
widgets that render them; `test_panel_wiring.py` covers whether anything calls
this at all.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.task_sources import (  # noqa: E402
    DATA_RASTER,
    DATA_VECTOR,
    KIND_FILE,
    KIND_LAYER,
    Source,
    add_sources,
    file_source,
    incompatible,
    is_batch,
    layer_source,
    missing_files,
    ready_notice,
    remove_source,
    has_pdf,
    start_label,
    summary,
    wanted_data_kind,
)


def _raster(tmp_path, name="sheet.tif", size=2048):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


def _vector(tmp_path, name="parcels.gpkg", size=1024):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


# -- what a source is -------------------------------------------------------

def test_a_file_source_measures_the_bytes_on_disk(tmp_path):
    source = file_source(_raster(tmp_path, size=1536))
    assert source.kind == KIND_FILE
    assert source.label == "sheet.tif"
    assert source.data_kind == DATA_RASTER
    assert source.size == 1536
    assert source.detail == "1.5 KB"


def test_a_layer_source_states_an_absent_crs_rather_than_leaving_it_blank():
    source = layer_source("layer_1", "Cadastre 1943", DATA_RASTER, crs="")
    assert source.kind == KIND_LAYER
    assert source.detail == "No CRS"


def test_a_layer_key_is_the_layer_id_and_never_a_path():
    # Reading `key` as a path is how a layer id reaches open(). The two are
    # separate fields precisely so that cannot happen.
    source = layer_source("layer_9", "Sheet", DATA_RASTER, crs="EPSG:2154")
    assert source.key == "layer_9"
    assert source.path == ""


def test_a_raster_layer_backed_by_a_local_file_carries_both_its_crs_and_its_size(tmp_path):
    path = _raster(tmp_path, size=4096)
    source = layer_source("layer_2", "Sheet", DATA_RASTER, crs="EPSG:2154", path=path)
    assert source.path == path
    assert source.size == 4096
    assert source.detail == "EPSG:2154 · 4.0 KB"


# -- the list ---------------------------------------------------------------

def test_adding_the_same_file_twice_keeps_one_and_reports_the_repeat(tmp_path):
    first = file_source(_raster(tmp_path))
    sources, added, duplicates = add_sources([], [first])
    assert added == 1 and duplicates == 0
    sources, added, duplicates = add_sources(sources, [file_source(_raster(tmp_path))])
    assert len(sources) == 1
    # Reported rather than swallowed: a count that does not move looks like the
    # picker lost the selection.
    assert added == 0 and duplicates == 1


def test_two_layers_pointing_at_one_file_are_two_sheets(tmp_path):
    path = _raster(tmp_path)
    sources, added, _ = add_sources([], [
        layer_source("layer_1", "North", DATA_RASTER, path=path),
        layer_source("layer_2", "South", DATA_RASTER, path=path),
    ])
    assert added == 2 and len(sources) == 2


def test_files_and_layers_live_in_one_list(tmp_path):
    sources, added, _ = add_sources([], [
        file_source(_raster(tmp_path, "a.tif")),
        layer_source("layer_1", "Open sheet", DATA_RASTER),
        file_source(_raster(tmp_path, "b.tif")),
    ])
    assert added == 3
    assert [source.kind for source in sources] == [KIND_FILE, KIND_LAYER, KIND_FILE]


def test_add_preserves_arrival_order(tmp_path):
    sources, _, _ = add_sources([], [
        file_source(_raster(tmp_path, "b.tif")),
        file_source(_raster(tmp_path, "a.tif")),
    ])
    assert [source.label for source in sources] == ["b.tif", "a.tif"]


def test_a_source_with_no_key_is_ignored():
    sources, added, duplicates = add_sources([], [Source(KIND_LAYER, "", "Nameless"), None])
    assert sources == [] and added == 0 and duplicates == 0


def test_removing_a_sheet_leaves_the_rest(tmp_path):
    sources, _, _ = add_sources([], [
        file_source(_raster(tmp_path, "a.tif")),
        file_source(_raster(tmp_path, "b.tif")),
    ])
    remaining = remove_source(sources, KIND_FILE, sources[0].key)
    assert [source.label for source in remaining] == ["b.tif"]


def test_removing_something_absent_is_not_an_error(tmp_path):
    sources, _, _ = add_sources([], [file_source(_raster(tmp_path))])
    assert remove_source(sources, KIND_LAYER, "layer_404") == sources


def test_more_than_one_source_is_a_batch(tmp_path):
    one = [file_source(_raster(tmp_path, "a.tif"))]
    two = one + [file_source(_raster(tmp_path, "b.tif"))]
    assert not is_batch(one)
    assert is_batch(two)


# -- compatibility ----------------------------------------------------------

def test_only_validate_deliver_reads_geometry():
    assert wanted_data_kind("validate_deliver") == DATA_VECTOR
    for workflow in ("georeference", "digitize_parcels", "full_pipeline"):
        assert wanted_data_kind(workflow) == DATA_RASTER


def test_an_unknown_workflow_is_treated_as_reading_a_scan():
    # A BatchKind this build has not heard of must not silently become the
    # geometry case, which is the one that accepts vector uploads.
    assert wanted_data_kind("some_future_kind") == DATA_RASTER


def test_the_wrong_sheet_is_named_not_counted(tmp_path):
    sources, _, _ = add_sources([], [
        file_source(_raster(tmp_path, "sheet.tif")),
        file_source(_vector(tmp_path, "parcels.geojson")),
    ])
    offenders = incompatible(sources, "georeference")
    assert [source.label for source in offenders] == ["parcels.geojson"]
    result = summary(sources, "georeference")
    assert not result["valid"]
    assert "parcels.geojson" in result["error"], (
        "a list of twelve with one wrong file is fixable in one click, but only "
        "once the message says which one"
    )


def test_several_wrong_sheets_are_counted_rather_than_listed(tmp_path):
    sources, _, _ = add_sources([], [
        file_source(_vector(tmp_path, "a.geojson")),
        file_source(_vector(tmp_path, "b.geojson")),
    ])
    result = summary(sources, "georeference")
    assert not result["valid"]
    assert "2 of these are not" in result["error"]


def test_a_vector_workflow_refuses_a_scan(tmp_path):
    sources, _, _ = add_sources([], [file_source(_raster(tmp_path))])
    result = summary(sources, "validate_deliver")
    assert not result["valid"]
    assert "vector" in result["error"]


def test_a_layer_awaiting_export_is_not_reported_as_missing():
    # A vector layer has no path until submit time. Checking one for a file on
    # disk would refuse a source that is open on screen.
    sources = [layer_source("layer_1", "Parcels", DATA_VECTOR)]
    assert missing_files(sources) == []
    assert summary(sources, "validate_deliver")["valid"]


def test_a_file_that_vanished_is_refused_by_name(tmp_path):
    source = file_source(_raster(tmp_path, "gone.tif"))
    (tmp_path / "gone.tif").unlink()
    result = summary([source], "georeference")
    assert not result["valid"]
    assert "gone.tif" in result["error"]


def test_an_empty_list_asks_for_a_source():
    result = summary([], "georeference")
    assert not result["valid"]
    assert result["summary"] == "No sources selected"


# -- what the page says -----------------------------------------------------

def test_the_summary_counts_files_and_layers_apart(tmp_path):
    sources, _, _ = add_sources([], [
        file_source(_raster(tmp_path, "a.tif", size=1024)),
        file_source(_raster(tmp_path, "b.tif", size=1024)),
        layer_source("layer_1", "Open sheet", DATA_RASTER),
    ])
    # Apart, because they behave differently on the way out: a file is uploaded
    # as it is, a layer may be exported first.
    assert summary(sources, "georeference")["summary"] == "2 files · 1 layer · 2.0 KB"


def test_one_file_is_singular(tmp_path):
    sources = [file_source(_raster(tmp_path, size=1024))]
    assert summary(sources, "georeference")["summary"] == "1 file · 1.0 KB"


def test_the_start_control_says_batch_when_it_starts_one(tmp_path):
    one = [file_source(_raster(tmp_path, "a.tif"))]
    assert start_label(one) == "Start task"
    twelve = one + [file_source(_raster(tmp_path, "b{}.tif".format(index))) for index in range(11)]
    assert start_label(twelve) == "Start batch · 12 sheets"


def test_the_start_control_carries_the_money(tmp_path):
    # The figure belongs on the control that spends it: pressing this is the
    # moment it goes, and somebody who presses without reading the sentence
    # beside it has still been told.
    one = [file_source(_raster(tmp_path, "a.tif"))]
    assert start_label(one, "$2") == "Start task · $2"
    twelve = one + [file_source(_raster(tmp_path, "b{}.tif".format(index))) for index in range(11)]
    assert start_label(twelve, "$24") == "Start batch · 12 sheets · $24"


def test_no_price_means_the_button_says_nothing_about_money(tmp_path):
    # An unpriced or free workflow leaves the label as it was rather than
    # putting a zero on it. This module computes no price; it renders the
    # string it is handed, and "" is a real answer.
    one = [file_source(_raster(tmp_path, "a.tif"))]
    assert start_label(one, "") == "Start task"
    assert start_label(one) == "Start task"


def test_a_pdf_in_the_list_is_what_makes_the_page_option_mean_anything(tmp_path):
    # "Treat every PDF page as its own sheet" over a list of TIFFs is not
    # clutter, it is a promise about work that cannot happen - reported as
    # exactly that from a real session.
    tifs = [file_source(_raster(tmp_path, "a.tif"))]
    assert has_pdf(tifs) is False
    (tmp_path / "atlas.PDF").write_bytes(b"%PDF-1.4")
    assert has_pdf(tifs + [file_source(str(tmp_path / "atlas.PDF"))]) is True
    assert has_pdf([]) is False


def test_a_layer_with_no_file_is_not_read_as_a_pdf():
    # A layer's key is an id, never a path, so nothing here may be handed to
    # splitext as though it were one.
    layer = layer_source("layer_1.pdf", "Parcels", "vector")
    assert has_pdf([layer]) is False


def test_the_ready_notice_names_the_sheet_or_the_count(tmp_path):
    one = [file_source(_raster(tmp_path, "sheet.tif"))]
    assert ready_notice(one, "Georeference maps") == "Ready to run Georeference maps on sheet.tif."
    two = one + [file_source(_raster(tmp_path, "other.tif"))]
    assert "2 sheets as one batch" in ready_notice(two, "Georeference maps")


def test_an_empty_list_has_no_ready_notice():
    assert ready_notice([], "Georeference maps") == ""
