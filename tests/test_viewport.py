"""Server viewport -> canvas extent.

Regression: "zoom to istanbul" returned the right coordinates and the canvas did
not move. Two defects had to line up - the server never sent the desktop an
action, and the desktop applied WGS84 degrees straight onto an EPSG:3857 canvas,
which is a point next to null island rather than Istanbul.
"""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.nivo import SAFE_PARAM_KEYS, allowed_actions  # noqa: E402
from mapdex_qgis.viewport import (  # noqa: E402
    DEFAULT_HALF_SPAN_M,
    MAX_HALF_SPAN_M,
    degrees_for_metres,
    half_span_for_zoom,
    resolve_extent,
)

ISTANBUL = (28.9759, 41.0064)


def test_a_real_bbox_is_used_as_given():
    resolved = resolve_extent({"crs": "EPSG:4326", "bbox": [28.5, 40.8, 29.4, 41.3]})
    assert resolved["bbox"] == [28.5, 40.8, 29.4, 41.3]
    assert resolved["crs"] == "EPSG:4326"


def test_a_centre_without_an_extent_becomes_a_usable_box_around_the_point():
    resolved = resolve_extent({"crs": "EPSG:4326", "center": list(ISTANBUL), "zoom": 11})
    box = resolved["bbox"]
    # The point must be inside its own extent, and the extent must have area.
    assert box[0] < ISTANBUL[0] < box[2]
    assert box[1] < ISTANBUL[1] < box[3]
    assert box[2] > box[0] and box[3] > box[1]


def test_a_degenerate_point_bbox_is_padded_rather_than_zoomed_infinitely():
    resolved = resolve_extent({"crs": "EPSG:4326", "bbox": [29.0, 41.0, 29.0, 41.0]})
    box = resolved["bbox"]
    assert box[2] > box[0] and box[3] > box[1]


def test_unusable_payloads_are_refused():
    for params in (
        {},
        {"crs": "EPSG:4326"},
        {"bbox": [1, 2, 3]},
        {"bbox": [10, 10, 0, 0]},
        {"bbox": ["a", "b", "c", "d"]},
        {"center": [float("nan"), 1]},
        {"center": [1]},
    ):
        assert resolve_extent(params) is None


def test_longitude_degrees_shrink_toward_the_poles():
    # 10 km is a wider longitude span in Oslo than at the equator; assuming a
    # fixed degree size is how northern extents come out far too narrow.
    equator_lon, _ = degrees_for_metres(10_000.0, 0.0)
    oslo_lon, _ = degrees_for_metres(10_000.0, 60.0)
    assert oslo_lon > equator_lon
    assert math.isclose(oslo_lon, equator_lon * 2.0, rel_tol=0.02)


def test_latitude_degrees_do_not_depend_on_latitude():
    _lon_a, lat_a = degrees_for_metres(10_000.0, 0.0)
    _lon_b, lat_b = degrees_for_metres(10_000.0, 60.0)
    assert math.isclose(lat_a, lat_b, rel_tol=1e-9)


def test_a_pole_adjacent_latitude_does_not_divide_by_zero():
    lon, _lat = degrees_for_metres(10_000.0, 89.999999)
    assert lon <= 180.0
    resolved = resolve_extent({"crs": "EPSG:4326", "center": [0.0, 89.999999], "zoom": 3})
    assert resolved["bbox"][3] <= 90.0
    assert resolved["bbox"][1] >= -90.0


def test_a_higher_zoom_frames_a_smaller_area():
    assert half_span_for_zoom(14, 41.0) < half_span_for_zoom(11, 41.0) < half_span_for_zoom(5, 41.0)


def test_zoom_spans_are_clamped_and_degrade_safely():
    assert half_span_for_zoom(0, 0) == DEFAULT_HALF_SPAN_M
    assert half_span_for_zoom("nonsense", 0) == DEFAULT_HALF_SPAN_M
    assert half_span_for_zoom(float("inf"), 0) == DEFAULT_HALF_SPAN_M
    assert half_span_for_zoom(1, 0) <= MAX_HALF_SPAN_M


def test_a_projected_crs_centre_is_not_converted_through_degrees():
    resolved = resolve_extent({"crs": "EPSG:3857", "center": [3225000.0, 5010000.0], "zoom": 11})
    box = resolved["bbox"]
    # Linear units already: the span must be metres, not a fraction of a degree.
    assert (box[2] - box[0]) > 1000.0


def test_the_action_allowlist_accepts_the_centre_and_zoom_payload():
    # The server may send centre+zoom when the geocoder has no extent; the
    # allowlist must not silently drop the action for carrying them.
    assert {"bbox", "crs", "center", "zoom"} == SAFE_PARAM_KEYS["qgis:zoom_to_extent@1"]
    actions = allowed_actions({"companion_actions": [{
        "action_id": "act_zoom",
        "kind": "qgis:zoom_to_extent@1",
        "params": {"crs": "EPSG:4326", "center": [28.97, 41.0], "zoom": 11},
    }]})
    assert len(actions) == 1
    assert actions[0]["params"]["center"] == [28.97, 41.0]


def test_an_action_carrying_an_unknown_parameter_is_still_rejected():
    actions = allowed_actions({"companion_actions": [{
        "action_id": "act_zoom",
        "kind": "qgis:zoom_to_extent@1",
        "params": {"bbox": [0, 0, 1, 1], "python": "import os"},
    }]})
    assert actions == []
