"""Spatial correctness tests: units first, then the exact geometry kernel."""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.spatial import (  # noqa: E402
    area_conversion,
    bbox_contains,
    bbox_intersects,
    count_points_in_polygons,
    density_per_area,
    plan_distance,
    point_in_polygon,
    polygon_area,
    ring_area,
    to_metres,
    transform_bbox,
)

SQUARE = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]
SQUARE_WITH_HOLE = [
    [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
    [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)],
]


def test_metric_distance_on_a_projected_layer_converts_to_map_units():
    plan = plan_distance(crs_is_geographic=False, crs_map_units="m", distance=200, unit="m")
    assert plan["strategy"] == "map_units"
    assert plan["map_units"] == 200.0


def test_metric_distance_on_a_foot_based_crs_is_converted_not_copied():
    plan = plan_distance(crs_is_geographic=False, crs_map_units="ft", distance=304.8, unit="m")
    assert plan["strategy"] == "map_units"
    assert math.isclose(plan["map_units"], 1000.0, rel_tol=1e-9)


def test_metres_on_a_geographic_layer_never_become_degrees():
    # The whole point: 200 m must not silently turn into 200 degrees.
    plan = plan_distance(crs_is_geographic=True, crs_map_units="degrees", distance=200, unit="m")
    assert plan["strategy"] == "reproject"
    assert plan["metres"] == 200.0
    assert "map_units" not in plan


def test_angular_crs_unit_is_detected_even_when_the_geographic_flag_is_wrong():
    plan = plan_distance(crs_is_geographic=False, crs_map_units="degree", distance=5, unit="km")
    assert plan["strategy"] == "reproject"
    assert plan["metres"] == 5000.0


def test_unknown_units_are_refused_rather_than_assumed():
    assert plan_distance(False, "m", 10, "parsecs")["strategy"] == "unsupported"
    assert plan_distance(False, "smoots", 10, "m")["strategy"] == "unsupported"
    assert to_metres(1, "furlong") is None


def test_non_positive_and_non_finite_distances_are_refused():
    for value in (0, -5, float("nan"), float("inf"), "abc", None):
        assert plan_distance(False, "m", value, "m")["strategy"] == "unsupported"


def test_kilometres_and_miles_convert_exactly():
    assert to_metres(2, "km") == 2000.0
    assert math.isclose(to_metres(1, "mile"), 1609.344, rel_tol=1e-12)
    assert math.isclose(to_metres(1, "ft"), 0.3048, rel_tol=1e-12)


def test_area_conversion_squares_the_linear_factor():
    assert area_conversion("m") == 1.0
    assert math.isclose(area_conversion("km"), 1_000_000.0, rel_tol=1e-12)
    assert area_conversion("degrees") is None


def test_ring_and_polygon_area_use_the_shoelace_formula():
    assert ring_area(SQUARE[0]) == 100.0
    # 10x10 shell minus a 2x2 hole.
    assert polygon_area(SQUARE_WITH_HOLE) == 96.0


def test_point_in_polygon_handles_interior_exterior_and_boundary():
    assert point_in_polygon((5.0, 5.0), SQUARE) is True
    assert point_in_polygon((15.0, 5.0), SQUARE) is False
    # A point on a shared edge counts as inside, deterministically.
    assert point_in_polygon((0.0, 5.0), SQUARE) is True
    assert point_in_polygon((10.0, 10.0), SQUARE) is True


def test_point_inside_a_hole_is_outside_the_polygon():
    assert point_in_polygon((5.0, 5.0), SQUARE_WITH_HOLE) is False
    assert point_in_polygon((2.0, 2.0), SQUARE_WITH_HOLE) is True


def test_bbox_predicates():
    assert bbox_intersects([0, 0, 10, 10], [5, 5, 15, 15]) is True
    assert bbox_intersects([0, 0, 10, 10], [11, 11, 15, 15]) is False
    # Touching edges count as intersecting.
    assert bbox_intersects([0, 0, 10, 10], [10, 0, 20, 10]) is True
    assert bbox_contains([0, 0, 10, 10], [2, 2, 8, 8]) is True
    assert bbox_contains([0, 0, 10, 10], [2, 2, 12, 8]) is False


def test_points_in_polygons_counts_per_polygon_and_reports_strays():
    polygons = [
        {"id": "a", "rings": [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]},
        {"id": "b", "rings": [[(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)]]},
    ]
    points = [
        {"id": 1, "point": (5.0, 5.0)},
        {"id": 2, "point": (6.0, 6.0)},
        {"id": 3, "point": (25.0, 5.0)},
        {"id": 4, "point": (100.0, 100.0)},
    ]
    result = count_points_in_polygons(polygons, points)
    assert result["counts"] == {"a": 2, "b": 1}
    # The stray must be surfaced, not silently dropped.
    assert result["unmatched"] == 1
    assert result["matched"] == 3


def test_density_normalises_counts_by_area_and_names_the_unit():
    # 1 km² polygon in metre units with 50 points is 50 per km².
    result = density_per_area({"a": 50}, {"a": 1_000_000.0}, "m")
    assert result["unit"] == "per km²"
    assert math.isclose(result["values"]["a"], 50.0, rel_tol=1e-12)


def test_density_ranks_a_small_dense_polygon_above_a_large_sparse_one():
    counts = {"small": 100, "large": 200}
    areas = {"small": 1_000_000.0, "large": 100_000_000.0}
    values = density_per_area(counts, areas, "m")["values"]
    assert values["small"] > values["large"]


def test_zero_area_polygons_yield_none_not_infinity():
    result = density_per_area({"a": 5}, {"a": 0.0}, "m")
    assert result["values"]["a"] is None


def test_density_refuses_a_geographic_crs():
    assert density_per_area({"a": 5}, {"a": 1.0}, "degrees")["reason"] == "unknown_crs_unit"


def test_bbox_transform_densifies_edges_instead_of_using_only_corners():
    # A transform that bulges the middle of an edge outward: a corner-only
    # transform would report a box that does not contain the shape.
    def bulge(x, y):
        return (x, y + 5.0 * math.sin(math.pi * (x / 10.0)))

    box = transform_bbox([0.0, 0.0, 10.0, 10.0], bulge)
    assert box is not None
    assert box[3] > 10.0


def test_bbox_transform_returns_none_when_every_sample_fails():
    def broken(_x, _y):
        raise ValueError("no transform")

    assert transform_bbox([0.0, 0.0, 1.0, 1.0], broken) is None
