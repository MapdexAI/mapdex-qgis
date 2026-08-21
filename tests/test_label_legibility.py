"""The cartographic decisions, made rather than asked.

QGIS labels a layer with plain text and no halo. That is legible on a white
page and invisible the moment the map has a satellite basemap, a hillshade or a
choropleth under it, which is every map anybody publishes. A person asking to
label a layer is asking to be able to READ the labels, and the difference
between those two readings is most of what separates a map from a screenshot.
"""

import pytest

from mapdex_qgis._vendor.nivo import presentation


def test_a_label_gets_a_halo_without_being_asked():
    # The default is reversed from QGIS's own, deliberately.
    settings = presentation.label_settings("name")
    assert settings["halo"] is True
    assert settings["halo_size_mm"] == presentation.DEFAULT_HALO_MM


def test_the_halo_colour_is_computed_from_the_text_colour():
    # The decision a cartographer makes without thinking and a form never asks.
    assert presentation.label_settings("name", color="#1A1A1A")["halo_color"] == "#FFFFFF"
    assert presentation.label_settings("name", color="#FFFFFF")["halo_color"] == "#1A1A1A"


def test_luminance_is_weighted_rather_than_averaged():
    # Pure blue is DARK and pure green is light, by a wide margin. A simple
    # channel average calls blue a mid-tone and puts a dark halo on it, which
    # is the one combination that reads as a smudge.
    assert presentation.relative_luminance("#0000FF") < 0.1
    assert presentation.relative_luminance("#00FF00") > 0.6
    assert presentation.contrasting_halo("#0000FF") == "#FFFFFF"
    assert presentation.contrasting_halo("#00FF00") == "#1A1A1A"


def test_black_and_white_sit_at_the_ends():
    assert presentation.relative_luminance("#000000") == pytest.approx(0.0)
    assert presentation.relative_luminance("#FFFFFF") == pytest.approx(1.0)


def test_a_three_digit_hex_is_the_same_colour():
    assert presentation.relative_luminance("#FFF") == pytest.approx(
        presentation.relative_luminance("#FFFFFF"))
    assert presentation.relative_luminance("000") == pytest.approx(0.0)


@pytest.mark.parametrize("color", ["", "blue", "#12345", "#GGGGGG", None])
def test_a_colour_that_is_not_a_hex_triple_is_refused(color):
    # Silently falling back would put a label in a colour nobody chose and give
    # it a halo computed from that colour, so the map is wrong twice.
    with pytest.raises(ValueError):
        presentation.relative_luminance(color)


def test_an_explicit_halo_colour_wins_over_the_computed_one():
    settings = presentation.label_settings("name", color="#1A1A1A", halo_color="#FFCC00")
    assert settings["halo_color"] == "#FFCC00"


def test_turning_the_halo_off_leaves_no_width_behind():
    # A halo of 1 mm with the halo disabled is a contradiction a renderer will
    # resolve however it likes.
    settings = presentation.label_settings("name", halo=False)
    assert settings["halo"] is False
    assert settings["halo_size_mm"] == 0.0


@pytest.mark.parametrize("requested,expected", [
    (0.0, presentation.MIN_HALO_MM),
    (99.0, presentation.MAX_HALO_MM),
    (1.5, 1.5),
])
def test_the_halo_width_is_clamped_to_what_is_legible(requested, expected):
    # Under the floor it stops separating the glyph from what is behind it;
    # over the ceiling it becomes a blob with a letter in it.
    assert presentation.label_settings("name", halo_size_mm=requested)["halo_size_mm"] == expected


@pytest.mark.parametrize("geometry,placement", [
    ("point", "around_point"),
    ("line", "along_line"),
    ("polygon", "horizontal"),
])
def test_the_placement_follows_the_geometry_when_nobody_says(geometry, placement):
    # A road label sitting horizontally beside the road instead of following it
    # is the second most common way a map reads as automatic.
    assert presentation.label_settings("name", geometry=geometry)["placement"] == placement


def test_an_unknown_geometry_falls_back_rather_than_failing():
    # The placement is a default, not the request. Refusing to label a layer
    # because its geometry could not be named would cost the whole answer.
    assert presentation.label_settings("name", geometry="mesh")["placement"] == "around_point"


def test_an_explicit_placement_wins_over_the_geometry():
    settings = presentation.label_settings("name", geometry="line", placement="horizontal")
    assert settings["placement"] == "horizontal"


def test_an_unknown_placement_is_refused_with_the_list():
    with pytest.raises(ValueError) as raised:
        presentation.label_settings("name", placement="diagonal")
    assert "along_line" in str(raised.value)


def test_a_label_needs_a_field():
    for field in ("", "   ", None):
        with pytest.raises(ValueError):
            presentation.label_settings(field)


def test_the_text_size_is_clamped_to_something_readable():
    assert presentation.label_settings("name", size=1)["size"] == 4.0
    assert presentation.label_settings("name", size=200)["size"] == 48.0


# -- Scale-dependent visibility ----------------------------------------------

def test_a_scale_band_is_two_denominators():
    band = presentation.scale_range(1000, 100000)
    assert band == {"minimum_scale": 1000.0, "maximum_scale": 100000.0}
    assert "1:1,000" in presentation.describe_scale_range(band)


def test_an_inverted_range_is_refused_rather_than_swapped():
    # Swapping silently would be guessing at intent. A person who typed them the
    # other way round should be told, so the next one they type is right - and
    # a band applied backwards hides the layer at exactly the zooms it was meant
    # for, which looks like nothing happened.
    with pytest.raises(ValueError) as raised:
        presentation.scale_range(100000, 1000)
    assert "inverted" in str(raised.value)


@pytest.mark.parametrize("low,high", [(0, 1000), (-5, 1000), (1000, 0)])
def test_a_denominator_must_be_positive(low, high):
    with pytest.raises(ValueError):
        presentation.scale_range(low, high)


def test_a_denominator_beyond_the_ends_of_a_web_map_is_refused():
    with pytest.raises(ValueError):
        presentation.scale_range(1000, presentation.MAX_SCALE_DENOMINATOR + 1)


@pytest.mark.parametrize("value", ["near", None, [1000]])
def test_a_range_that_is_not_two_numbers_is_refused(value):
    with pytest.raises(ValueError):
        presentation.scale_range(value, 100000)


def test_an_equal_range_is_allowed():
    # One scale exactly. Unusual and not wrong: a layer that belongs on one
    # printed sheet and nowhere else.
    assert presentation.scale_range(5000, 5000)["minimum_scale"] == 5000.0


def test_both_capabilities_are_declared_and_bound():
    from mapdex_qgis._vendor.nivo import capabilities
    from mapdex_qgis.qgis_runtime import PLUGIN_BOUND_CAPABILITIES, bound_capability_ids

    bound = bound_capability_ids() | PLUGIN_BOUND_CAPABILITIES
    for identifier in ("style.labels@1", "layer.scale_range@1"):
        assert capabilities.get(identifier) is not None, identifier
        assert identifier in bound, identifier


def test_the_label_capability_declares_the_new_parameters():
    from mapdex_qgis._vendor.nivo import capabilities

    declared = set(capabilities.get("style.labels@1").params)
    assert {"color", "halo", "halo_size", "halo_color", "placement"} <= declared
