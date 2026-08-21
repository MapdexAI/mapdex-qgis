"""Composing a plan sheet, with every number checkable on paper.

A3 landscape is 420 by 297 mm. With a 10 mm margin, a 45 mm legend down the
right and 24 mm of furniture along the bottom, the map frame is 355 by 253 mm -
and at 1:1000 that frame covers 355 by 253 metres, because one millimetre on the
page is one metre on the ground. Every expectation below is that arithmetic.
"""

import pytest

from mapdex_qgis._vendor.nivo import sheet


def test_a3_landscape_is_the_page_turned_on_its_side():
    assert sheet.page_size_mm("A3", "landscape") == (420.0, 297.0)
    assert sheet.page_size_mm("A3", "portrait") == (297.0, 420.0)


def test_an_unknown_page_is_refused_with_the_list():
    with pytest.raises(ValueError) as raised:
        sheet.page_size_mm("letter")
    assert "A3" in str(raised.value)


def test_one_millimetre_at_one_to_one_thousand_is_one_metre():
    # The whole arithmetic of a plan is this line.
    assert sheet.ground_size_m(355.0, 253.0, 1000) == pytest.approx((355.0, 253.0))
    assert sheet.ground_size_m(100.0, 100.0, 500) == pytest.approx((50.0, 50.0))


def test_the_map_frame_is_the_page_less_its_furniture():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000)
    frame = next(item for item in composed["items"] if item["kind"] == "map")
    # 420 - 20 margin - 45 legend = 355. 297 - 20 margin - 24 furniture = 253.
    assert frame["width_mm"] == pytest.approx(355.0)
    assert frame["height_mm"] == pytest.approx(253.0)
    assert composed["covers_width_m"] == pytest.approx(355.0)
    assert composed["covers_height_m"] == pytest.approx(253.0)


def test_dropping_the_legend_gives_the_map_its_width_back():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000, legend=False)
    frame = next(item for item in composed["items"] if item["kind"] == "map")
    assert frame["width_mm"] == pytest.approx(400.0)
    assert not any(item["kind"] == "legend" for item in composed["items"])


def test_a_bare_sheet_has_no_furniture_band():
    composed = sheet.compose_sheet(
        "A4", "portrait", scale=1000, legend=False,
        scale_bar=False, north_arrow=False)
    frame = next(item for item in composed["items"] if item["kind"] == "map")
    assert frame["height_mm"] == pytest.approx(277.0)
    assert [item["kind"] for item in composed["items"]] == ["map"]


# -- The scale is a promise about ground --------------------------------------

def test_a_scale_that_would_crop_the_data_is_refused_with_one_that_fits():
    # A plan that quietly omits a third of the site is worse than no plan, and
    # the person who receives it has no way to tell.
    with pytest.raises(ValueError) as raised:
        sheet.compose_sheet("A3", "landscape", scale=1000, extent_m=(900, 400))
    message = str(raised.value)
    assert "crop" in message
    # 900 m across a 355 mm frame needs 1:2535, and the next standard scale up
    # is 1:5000. I first wrote 1:2500 here, which is the value BELOW what is
    # needed - exactly the rounding mistake the module refuses to make.
    assert "1:5000" in message


def test_data_that_fits_at_the_requested_scale_is_accepted():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000, extent_m=(300, 200))
    assert composed["scale"] == 1000
    assert composed["scale_fitted"] is False


def test_a_sheet_with_no_scale_computes_one_from_the_data():
    # 900 m across a 355 mm frame needs 1:2535, which rounds up to 1:5000.
    composed = sheet.compose_sheet("A3", "landscape", extent_m=(900, 400))
    assert composed["scale"] == 5000
    assert composed["scale_fitted"] is True
    assert composed["covers_width_m"] >= 900


def test_a_fitted_scale_is_a_scale_somebody_can_use():
    # Survey plans are drawn at rounded scales because a person reads distances
    # off them with a rule. 1:1373 is not one of them.
    for extent in ((120, 80), (487, 300), (2100, 900), (48000, 20000)):
        composed = sheet.compose_sheet("A3", "landscape", extent_m=extent)
        assert composed["scale"] in sheet.STANDARD_SCALES


def test_rounding_is_always_up():
    # Down produces a sheet at a scale the data does not fit on, which is the
    # one failure this module exists to prevent.
    assert sheet.round_to_standard_scale(1001) == 1250
    assert sheet.round_to_standard_scale(1000) == 1000
    assert sheet.round_to_standard_scale(1) == 50


def test_past_the_largest_listed_scale_it_still_rounds_up_to_something_readable():
    assert sheet.round_to_standard_scale(1_400_000) == 2_000_000


def test_the_tighter_axis_decides_the_fit():
    # Fitting the width and cropping the height is the failure mode, not a
    # compromise. On a 1000 by 100 mm frame a 250 m square needs 1:250 by width
    # and 1:2500 by height, so the answer must be the height's.
    assert sheet.scale_to_fit(250.0, 250.0, 1000.0, 100.0) == 2500
    # And the other way round on a frame that is tall rather than wide.
    assert sheet.scale_to_fit(250.0, 250.0, 100.0, 1000.0) == 2500


# -- Refusals that prevent an unreadable sheet --------------------------------

def test_a_page_with_no_room_left_for_the_map_is_refused():
    with pytest.raises(ValueError) as raised:
        sheet.compose_sheet("A5", "portrait", margin_mm=60)
    assert "no room" in str(raised.value)


@pytest.mark.parametrize("margin", [-1, 101])
def test_an_impossible_margin_is_refused(margin):
    with pytest.raises(ValueError):
        sheet.compose_sheet("A3", scale=1000, margin_mm=margin)


def test_a_sheet_with_neither_a_scale_nor_an_extent_is_refused():
    # There is no default ground for a plan to show.
    with pytest.raises(ValueError) as raised:
        sheet.compose_sheet("A3")
    assert "scale" in str(raised.value)


@pytest.mark.parametrize("extent", [(0, 100), (100, -1), (100,), (1, 2, 3)])
def test_an_impossible_extent_is_refused(extent):
    with pytest.raises(ValueError):
        sheet.compose_sheet("A3", extent_m=extent)


@pytest.mark.parametrize("scale", [0, -1000])
def test_an_impossible_scale_is_refused(scale):
    with pytest.raises(ValueError):
        sheet.compose_sheet("A3", scale=scale)


def test_an_unknown_orientation_is_refused():
    with pytest.raises(ValueError):
        sheet.page_size_mm("A3", "sideways")


# -- What the sheet says about itself -----------------------------------------

def test_the_composition_says_whether_the_scale_was_chosen_or_computed():
    # A printed sheet says 1:1000 either way, and the two mean different things
    # to whoever signs it.
    chosen = sheet.compose_sheet("A3", scale=1000)
    computed = sheet.compose_sheet("A3", extent_m=(300, 200))
    assert chosen["scale_fitted"] is False
    assert computed["scale_fitted"] is True
    assert "fitted to the data" in sheet.describe_sheet(computed)
    assert "fitted to the data" not in sheet.describe_sheet(chosen)


def test_every_item_lands_inside_the_page():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000, title="Site plan")
    for item in composed["items"]:
        assert item["x_mm"] >= 0
        assert item["y_mm"] >= 0
        assert item["x_mm"] + item["width_mm"] <= composed["page_width_mm"] + 1e-6, item
        assert item["y_mm"] + item["height_mm"] <= composed["page_height_mm"] + 1e-6, item


def test_a_title_is_bounded_rather_than_trusted():
    composed = sheet.compose_sheet("A3", scale=1000, title="x" * 500)
    title = next(item for item in composed["items"] if item["kind"] == "title")
    assert len(title["text"]) == 200


def test_the_furniture_sits_below_the_map_rather_than_over_it():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000, title="Site plan")
    frame = next(item for item in composed["items"] if item["kind"] == "map")
    bottom_of_map = frame["y_mm"] + frame["height_mm"]
    for kind in ("scale_bar", "north_arrow", "title"):
        item = next(entry for entry in composed["items"] if entry["kind"] == kind)
        assert item["y_mm"] >= bottom_of_map - 1e-6, kind


def test_the_legend_sits_beside_the_map_rather_than_over_it():
    composed = sheet.compose_sheet("A3", "landscape", scale=1000)
    frame = next(item for item in composed["items"] if item["kind"] == "map")
    legend = next(item for item in composed["items"] if item["kind"] == "legend")
    assert legend["x_mm"] >= frame["x_mm"] + frame["width_mm"] - 1e-6
