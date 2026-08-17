"""Known-answer tests for the geoprocessing kernel.

Every geometry here is a rectangle on a round-numbered grid in a metric CRS, so
each correct answer is arithmetic rather than opinion - the same discipline as
``testdata/README.md``. A test that only asserted ``kind == "clip"`` would pass
against an engine that returns the wrong shape, and the entire reason this
module is pure Python is so the shape can be checked.

The grid, in EPSG:32635 metres::

      y
    100 +---------+---------+
        |    A    |    B    |     A = (0,0)-(100,100),   area 10 000
        | 10 000  | 10 000  |     B = (100,0)-(200,100), area 10 000
      0 +---------+---------+     they share the edge x = 100
        0        100       200  x
"""
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import geoprocessing as gp  # noqa: E402

UTM = {"authid": "EPSG:32635", "is_geographic": False, "map_units": "m"}
WGS84 = {"authid": "EPSG:4326", "is_geographic": True, "map_units": "degrees"}
MERCATOR = {"authid": "EPSG:3857", "is_geographic": False, "map_units": "m"}


def rect(minx, miny, maxx, maxy):
    return {"kind": "polygon", "rings": [[(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]]}


def point(x, y):
    return {"kind": "point", "point": (x, y)}


def line(*coords):
    return {"kind": "line", "path": list(coords)}


ZONE_A = {"id": "A", "geometry": rect(0, 0, 100, 100), "attributes": {"zone": "A", "nufus": 400}}
ZONE_B = {"id": "B", "geometry": rect(100, 0, 200, 100), "attributes": {"zone": "B", "nufus": 600}}
ZONES = [ZONE_A, ZONE_B]

# P4 sits exactly on the shared edge, so it is inside both zones. Boundary
# membership is the case a join quietly gets wrong.
P1 = {"id": 1, "geometry": point(50, 50), "attributes": {"parsel": "P-001", "nufus": 100}}
P2 = {"id": 2, "geometry": point(150, 50), "attributes": {"parsel": "P-002", "nufus": 300}}
P3 = {"id": 3, "geometry": point(250, 50), "attributes": {"parsel": "P-003", "nufus": 999}}
P4 = {"id": 4, "geometry": point(100, 50), "attributes": {"parsel": "P-004", "nufus": None}}
POINTS = [P1, P2, P3, P4]


# --------------------------------------------------------------------------
# Validation happens before anything is read
# --------------------------------------------------------------------------

def test_unknown_operation_is_refused():
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("spatial_magic", {"source_crs": UTM})


def test_unknown_predicate_is_refused_by_name():
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.validate_request("select_by_location",
                            {"source_crs": UTM, "overlay_crs": UTM, "predicate": "beside"})
    assert "predicate" in str(error.value)


def test_a_layer_without_a_crs_is_refused_rather_than_assumed_wgs84():
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("dissolve", {"source_crs": {"map_units": "m"}})


def test_two_layers_in_different_crs_are_refused_and_both_are_named():
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.validate_request("spatial_join",
                            {"source_crs": UTM, "overlay_crs": WGS84, "join_fields": ["zone"]})
    message = str(error.value)
    assert "EPSG:32635" in message and "EPSG:4326" in message


def test_a_metre_distance_on_a_geographic_layer_is_refused_not_treated_as_degrees():
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.validate_request("select_by_location",
                            {"source_crs": WGS84, "overlay_crs": WGS84, "distance": 200, "unit": "m"})
    assert "degrees" in str(error.value)


def test_a_metre_buffer_on_a_geographic_layer_is_refused():
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.validate_request("buffer", {"source_crs": WGS84, "distance": 200, "unit": "m"})
    assert "degrees" in str(error.value)


def test_a_field_the_layer_does_not_have_is_refused_before_execution():
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.validate_request("zonal_statistics", {
            "source_crs": UTM, "overlay_crs": UTM, "statistic": "mean",
            "value_field": "nofus", "source_fields": ["parsel", "nufus"]})
    assert "nofus" in str(error.value)


def test_a_dissolve_rule_that_is_not_in_the_closed_set_is_refused():
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("dissolve", {"source_crs": UTM, "attribute_rules": {"nufus": "average"}})


def test_execute_refuses_a_request_that_never_went_through_validation():
    # A hand-built dict that merely names an operation must not run: that is how
    # the validation gate becomes optional and a bad request reaches the data.
    with pytest.raises(gp.GeoprocessingError):
        gp.execute({"operation": "clip", "bbox": [0, 0, 1, 1]}, [])
    with pytest.raises(gp.GeoprocessingError):
        gp.execute({}, [])
    assert gp.validate_request("clip", {"source_crs": UTM, "bbox": [0, 0, 1, 1]})["validated"] == gp.KERNEL_VERSION


def test_a_distance_cannot_be_smuggled_into_a_containment_predicate():
    # "within 50 m of" and "within" are different questions; answering the
    # first while the request says the second is a silent wrong answer.
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("select_by_location", {
            "source_crs": UTM, "overlay_crs": UTM, "predicate": "within", "distance": 50})


def test_distance_in_kilometres_is_converted_to_map_units_not_copied():
    validated = gp.validate_request("select_by_location",
                                    {"source_crs": UTM, "overlay_crs": UTM, "distance": 1.5, "unit": "km"})
    assert validated["distance_map_units"] == 1500.0


def test_a_foot_based_crs_converts_a_metre_request_into_feet():
    feet = {"authid": "EPSG:2263", "is_geographic": False, "map_units": "ft"}
    validated = gp.validate_request("buffer", {"source_crs": feet, "distance": 304.8, "unit": "m"})
    # 304.8 m is exactly 1000 international feet.
    assert math.isclose(validated["radius_map_units"], 1000.0, rel_tol=1e-9)


# --------------------------------------------------------------------------
# Geometry primitives
# --------------------------------------------------------------------------

def test_polygon_centroid_of_a_rectangle_is_its_middle():
    parts = [[[(0.0, 0.0), (10.0, 0.0), (10.0, 20.0), (0.0, 20.0)]]]
    assert gp.polygon_centroid(parts) == (5.0, 10.0)


def test_polygon_centroid_subtracts_a_hole():
    # A 10x10 square with a centred 2x2 hole is still symmetric about (5, 5).
    parts = [[
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)],
    ]]
    centre = gp.polygon_centroid(parts)
    assert math.isclose(centre[0], 5.0, abs_tol=1e-9)
    assert math.isclose(centre[1], 5.0, abs_tol=1e-9)


def test_convexity_is_detected_so_the_exact_paths_can_be_chosen():
    assert gp.ring_is_convex([(0, 0), (10, 0), (10, 10), (0, 10)])
    # An L: one reflex corner is enough to disqualify it.
    assert not gp.ring_is_convex([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)])


def test_touching_segments_intersect_but_do_not_properly_cross():
    assert gp.segments_intersect((0, 0), (10, 0), (10, 0), (10, 10))
    assert not gp.segments_properly_cross((0, 0), (10, 0), (10, 0), (10, 10))
    assert gp.segments_properly_cross((0, 0), (10, 10), (0, 10), (10, 0))


def test_geometry_distance_between_two_squares_is_the_gap_between_them():
    left = gp.normalize_geometry(rect(0, 0, 100, 100))
    right = gp.normalize_geometry(rect(150, 0, 250, 100))
    assert math.isclose(gp.geometry_distance(left, right), 50.0, abs_tol=1e-9)


def test_touching_squares_are_zero_apart_and_a_contained_point_is_zero_apart():
    assert gp.geometry_distance(gp.normalize_geometry(rect(0, 0, 100, 100)),
                                gp.normalize_geometry(rect(100, 0, 200, 100))) == 0.0
    assert gp.geometry_distance(gp.normalize_geometry(point(50, 50)),
                                gp.normalize_geometry(rect(0, 0, 100, 100))) == 0.0


def test_contains_is_true_for_a_square_inside_and_false_for_a_neighbour():
    outer = gp.normalize_geometry(rect(0, 0, 100, 100))
    inner = gp.normalize_geometry(rect(25, 25, 75, 75))
    neighbour = gp.normalize_geometry(rect(100, 0, 200, 100))
    assert gp.geometry_contains(outer, inner)
    assert not gp.geometry_contains(outer, neighbour)
    assert gp.relate(inner, outer, "within")
    assert gp.relate(outer, neighbour, "intersects")
    assert not gp.relate(outer, neighbour, "disjoint")


def test_an_edge_leaving_through_a_concave_notch_is_not_contained():
    # A C opening to the right: a bottom bar y 0-20, a top bar y 80-100 and a
    # left bar x 0-20. All FOUR corners of the rectangle below sit inside those
    # bars, so a vertex-only containment test says "contained" - but its left
    # and right edges run straight through the open middle. Only the
    # edge-crossing test catches it.
    c_shape = {"kind": "polygon", "rings": [[
        (0, 0), (100, 0), (100, 20), (20, 20), (20, 80), (100, 80), (100, 100), (0, 100)]]}
    outline = gp.normalize_geometry(c_shape)
    bridge = gp.normalize_geometry(rect(30, 10, 90, 90))
    for corner in gp.geometry_coordinates(bridge):
        assert gp.relate(gp.normalize_geometry(point(*corner)), outline, "within"), corner
    assert not gp.geometry_contains(outline, bridge)


def test_malformed_geometry_is_reported_as_unusable_rather_than_crashing():
    assert gp.normalize_geometry({"kind": "polygon", "rings": [[(0, 0), (1, 1)]]}) is None
    assert gp.normalize_geometry({"kind": "point", "point": ("x", 1)}) is None
    assert gp.normalize_geometry(None) is None


# --------------------------------------------------------------------------
# Spatial join
# --------------------------------------------------------------------------

def test_spatial_join_brings_the_zone_across_and_keeps_the_unmatched_feature():
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["zone"], "rule": "first"})
    result = gp.spatial_join(validated, POINTS, ZONES)
    assert result["rows_out"] == 4          # one row per input point, nothing dropped
    assert result["matched"] == 3           # P1 in A, P2 in B, P4 on the shared edge
    assert result["unmatched"] == 1         # P3 at x=250 is outside both
    zones = {row["id"]: row["attributes"]["zone"] for row in result["rows"]}
    assert zones == {1: "A", 2: "B", 3: None, 4: "A"}


def test_a_point_on_a_shared_edge_is_reported_as_a_multiple_match_not_silently_picked():
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["zone"]})
    result = gp.spatial_join(validated, POINTS, ZONES)
    assert result["multiple_matches"] == 1
    assert [row["match_count"] for row in result["rows"]] == [1, 1, 0, 2]


def test_the_all_rule_multiplies_the_row_and_still_keeps_the_unmatched_one():
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["zone"], "rule": "all"})
    result = gp.spatial_join(validated, POINTS, ZONES)
    # 1 (P1) + 1 (P2) + 1 (P3, unmatched, kept) + 2 (P4 in both) = 5
    assert result["rows_out"] == 5
    assert sorted(row["attributes"]["zone"] or "" for row in result["rows"]) == ["", "A", "A", "B", "B"]


def test_the_count_only_rule_reports_the_number_of_matches_per_feature():
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["zone"], "rule": "count_only"})
    result = gp.spatial_join(validated, POINTS, ZONES)
    assert [row["attributes"]["join_count"] for row in result["rows"]] == [1, 1, 0, 2]
    assert "zone" not in result["rows"][0]["attributes"]


def test_a_clashing_field_name_is_renamed_and_the_original_value_survives():
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["nufus"]})
    result = gp.spatial_join(validated, [P1], ZONES)
    row = result["rows"][0]["attributes"]
    assert row["nufus"] == 100            # the parcel's own value is untouched
    assert row["joined_nufus"] == 400     # the zone's value arrives beside it
    assert result["renamed_fields"] == {"nufus": "joined_nufus"}


def test_a_join_field_absent_from_the_declared_overlay_is_refused_up_front():
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("spatial_join", {
            "source_crs": UTM, "overlay_crs": UTM,
            "join_fields": ["district"], "overlay_fields": ["zone", "nufus"]})


def test_the_pair_budget_refuses_an_unbounded_comparison(monkeypatch):
    monkeypatch.setattr(gp, "MAX_PAIR_TESTS", 3)
    validated = gp.validate_request("spatial_join", {
        "source_crs": UTM, "overlay_crs": UTM, "join_fields": ["zone"]})
    with pytest.raises(gp.GeoprocessingError):
        gp.spatial_join(validated, POINTS, ZONES)   # 4 x 2 = 8 pairs


# --------------------------------------------------------------------------
# Select by location
# --------------------------------------------------------------------------

def test_select_by_location_returns_the_ids_inside_the_zone():
    validated = gp.validate_request("select_by_location", {
        "source_crs": UTM, "overlay_crs": UTM, "predicate": "intersects"})
    result = gp.select_by_location(validated, POINTS, [ZONE_A])
    # P1 is interior, P4 is exactly on the boundary at x=100 and counts.
    assert result["feature_ids"] == [1, 4]
    assert result["considered"] == 4


def test_select_by_location_with_a_distance_uses_the_real_gap_not_the_bounding_box():
    validated = gp.validate_request("select_by_location", {
        "source_crs": UTM, "overlay_crs": UTM, "distance": 50, "unit": "m"})
    result = gp.select_by_location(validated, POINTS, [ZONE_A])
    # P2 at (150,50) is exactly 50 m from A's edge at x=100, so it is included;
    # P3 at (250,50) is 150 m away and is not.
    assert result["feature_ids"] == [1, 2, 4]
    assert result["distance_map_units"] == 50.0


def test_a_distance_just_short_of_the_gap_excludes_the_feature():
    validated = gp.validate_request("select_by_location", {
        "source_crs": UTM, "overlay_crs": UTM, "distance": 49.9, "unit": "m"})
    result = gp.select_by_location(validated, POINTS, [ZONE_A])
    assert result["feature_ids"] == [1, 4]


def test_disjoint_with_a_distance_selects_what_is_further_away_not_what_is_near():
    validated = gp.validate_request("select_by_location", {
        "source_crs": UTM, "overlay_crs": UTM, "predicate": "disjoint", "distance": 50, "unit": "m"})
    result = gp.select_by_location(validated, POINTS, [ZONE_A])
    assert result["feature_ids"] == [3]


def test_within_selects_the_parcels_a_district_contains():
    parcels = [
        {"id": 10, "geometry": rect(10, 10, 40, 40), "attributes": {}},
        {"id": 11, "geometry": rect(90, 10, 140, 40), "attributes": {}},   # straddles the A/B edge
    ]
    validated = gp.validate_request("select_by_location", {
        "source_crs": UTM, "overlay_crs": UTM, "predicate": "within"})
    assert gp.select_by_location(validated, parcels, [ZONE_A])["feature_ids"] == [10]


# --------------------------------------------------------------------------
# Clip
# --------------------------------------------------------------------------

def test_clip_to_a_bbox_cuts_the_polygon_to_the_exact_overlap_area():
    big = [{"id": 1, "geometry": rect(0, 0, 200, 200), "attributes": {"parsel": "P-001"}}]
    validated = gp.validate_request("clip", {"source_crs": UTM, "bbox": [50, 50, 150, 150]})
    result = gp.clip(validated, big)
    # The window is 100 x 100 and lies wholly inside the polygon.
    assert math.isclose(result["area_map_units"], 10000.0, rel_tol=1e-12)
    assert result["output"] == 1
    assert result["features"][0]["attributes"] == {"parsel": "P-001"}


def test_clip_drops_only_what_is_outside_the_window_and_reports_how_many():
    validated = gp.validate_request("clip", {"source_crs": UTM, "bbox": [0, 0, 100, 100]})
    result = gp.clip(validated, POINTS)
    assert [feature["id"] for feature in result["features"]] == [1, 4]
    assert result["dropped_outside_window"] == 2
    assert result["input"] == 4


def test_clip_trims_a_line_to_the_window_and_keeps_the_length_arithmetic():
    crossing = [{"id": 1, "geometry": line((0, 100), (200, 100)), "attributes": {}}]
    validated = gp.validate_request("clip", {"source_crs": UTM, "bbox": [50, 50, 150, 150]})
    result = gp.clip(validated, crossing)
    path = result["features"][0]["geometry"]["paths"][0]
    assert path[0] == (50.0, 100.0)
    assert path[-1] == (150.0, 100.0)


def test_clip_without_a_bbox_uses_the_second_layers_own_extent():
    big = [{"id": 1, "geometry": rect(0, 0, 300, 300), "attributes": {}}]
    validated = gp.validate_request("clip", {"source_crs": UTM, "overlay_crs": UTM})
    result = gp.clip(validated, big, ZONES)
    # A and B together span (0,0)-(200,100): 200 x 100 = 20 000.
    assert result["window"] == [0.0, 0.0, 200.0, 100.0]
    assert math.isclose(result["area_map_units"], 20000.0, rel_tol=1e-12)
    assert result["window_source"] == "overlay_extent"


def test_clipping_a_concave_polygon_keeps_the_right_area_and_says_it_may_bridge():
    # A U with a 20-wide notch: 100x100 minus a 20x80 bite = 10 000 - 1 600.
    u_shape = {"kind": "polygon", "rings": [[
        (0, 0), (100, 0), (100, 100), (60, 100), (60, 20), (40, 20), (40, 100), (0, 100)]]}
    validated = gp.validate_request("clip", {"source_crs": UTM, "bbox": [0, 0, 100, 100]})
    result = gp.clip(validated, [{"id": 1, "geometry": u_shape, "attributes": {}}])
    assert math.isclose(result["area_map_units"], 8400.0, rel_tol=1e-9)
    assert result["concave_parts_may_be_bridged"] == 1


# --------------------------------------------------------------------------
# Intersect
# --------------------------------------------------------------------------

def test_intersect_of_two_squares_is_the_overlap_with_both_attribute_sets():
    left = [{"id": 1, "geometry": rect(0, 0, 100, 100), "attributes": {"parsel": "P-001"}}]
    right = [{"id": "A", "geometry": rect(50, 50, 150, 150), "attributes": {"zone": "A"}}]
    validated = gp.validate_request("intersect", {"source_crs": UTM, "overlay_crs": UTM})
    result = gp.intersect(validated, left, right)
    # Overlap is (50,50)-(100,100): 50 x 50 = 2 500.
    assert math.isclose(result["area_map_units"], 2500.0, rel_tol=1e-12)
    assert result["features"][0]["attributes"] == {"parsel": "P-001", "zone": "A"}


def test_intersect_emits_one_feature_per_overlapping_pair():
    parcel = [{"id": 1, "geometry": rect(50, 0, 150, 100), "attributes": {}}]
    validated = gp.validate_request("intersect", {"source_crs": UTM, "overlay_crs": UTM})
    result = gp.intersect(validated, parcel, ZONES)
    assert result["output"] == 2
    # 50 x 100 in A plus 50 x 100 in B: the parcel's own 10 000 m², split.
    assert math.isclose(result["area_map_units"], 10000.0, rel_tol=1e-12)


def test_intersect_refuses_a_concave_pair_instead_of_returning_a_plausible_shape():
    l_shape = {"kind": "polygon", "rings": [[
        (0, 0), (100, 0), (100, 40), (40, 40), (40, 100), (0, 100)]]}
    validated = gp.validate_request("intersect", {"source_crs": UTM, "overlay_crs": UTM})
    result = gp.intersect(validated,
                          [{"id": 1, "geometry": rect(0, 0, 100, 100), "attributes": {}}],
                          [{"id": "L", "geometry": l_shape, "attributes": {}}])
    assert result["output"] == 0
    assert result["refused_pairs"] == [
        {"source_id": 1, "overlay_id": "L", "reason": "overlay_ring_is_not_convex"}]


def test_a_point_intersected_with_a_polygon_survives_only_if_it_is_inside():
    validated = gp.validate_request("intersect", {"source_crs": UTM, "overlay_crs": UTM})
    result = gp.intersect(validated, POINTS, [ZONE_A])
    assert [feature["id"] for feature in result["features"]] == [1, 4]


# --------------------------------------------------------------------------
# Dissolve
# --------------------------------------------------------------------------

PARCELS = [
    {"id": 1, "geometry": rect(0, 0, 100, 100), "attributes": {"kullanim": "konut", "nufus": 100, "sahip": "a"}},
    {"id": 2, "geometry": rect(100, 0, 200, 100), "attributes": {"kullanim": "konut", "nufus": None, "sahip": "b"}},
    {"id": 3, "geometry": rect(0, 100, 200, 200), "attributes": {"kullanim": "ticari", "nufus": 500, "sahip": "c"}},
]


def test_dissolve_groups_by_field_and_sums_the_member_areas():
    validated = gp.validate_request("dissolve", {
        "source_crs": UTM, "field": "kullanim", "attribute_rules": {"nufus": "sum"}})
    result = gp.dissolve(validated, PARCELS)
    groups = {group["group"]: group for group in result["groups"]}
    assert groups["konut"]["members"] == 2
    assert groups["konut"]["parts"] == 2
    # Two 100x100 parcels: 20 000 m². The third is 200x100 = 20 000 on its own.
    assert math.isclose(groups["konut"]["area_sum_of_parts"], 20000.0, rel_tol=1e-12)
    assert math.isclose(groups["ticari"]["area_sum_of_parts"], 20000.0, rel_tol=1e-12)


def test_dissolve_reports_that_shared_boundaries_are_still_there():
    validated = gp.validate_request("dissolve", {"source_crs": UTM, "field": "kullanim"})
    result = gp.dissolve(validated, PARCELS)
    assert result["boundaries_removed"] is False
    # The two konut parcels only touch, so the summed area IS the union area.
    konut = [group for group in result["groups"] if group["group"] == "konut"][0]
    assert konut["overlap"] == {"checked": True, "overlapping_pairs": 0}


def test_dissolve_detects_genuinely_overlapping_members_so_the_sum_is_not_trusted():
    overlapping = [
        {"id": 1, "geometry": rect(0, 0, 100, 100), "attributes": {"k": "x"}},
        {"id": 2, "geometry": rect(50, 50, 150, 150), "attributes": {"k": "x"}},
    ]
    validated = gp.validate_request("dissolve", {"source_crs": UTM, "field": "k"})
    result = gp.dissolve(validated, overlapping)
    assert result["groups"][0]["overlap"] == {"checked": True, "overlapping_pairs": 1}
    # 10 000 + 10 000 counts the shared 2 500 twice; the union is 17 500.
    assert math.isclose(result["groups"][0]["area_sum_of_parts"], 20000.0, rel_tol=1e-12)


def test_a_dissolved_mean_uses_only_the_values_that_exist():
    validated = gp.validate_request("dissolve", {
        "source_crs": UTM, "field": "kullanim", "attribute_rules": {"nufus": "mean"}})
    result = gp.dissolve(validated, PARCELS)
    konut = [group for group in result["groups"] if group["group"] == "konut"][0]
    # One of the two konut parcels has no nufus. 100/1 = 100, not 100/2 = 50.
    assert konut["attributes"]["nufus"] == 100.0
    assert konut["attribute_detail"]["nufus"] == {"rule": "mean", "values_used": 1, "nulls": 1}


def test_an_attribute_with_no_rule_is_dropped_and_listed_not_taken_from_the_first_member():
    validated = gp.validate_request("dissolve", {
        "source_crs": UTM, "field": "kullanim", "attribute_rules": {"nufus": "sum"}})
    result = gp.dissolve(validated, PARCELS)
    assert result["dropped_fields"] == ["sahip"]
    assert "sahip" not in result["groups"][0]["attributes"]


def test_null_and_empty_string_are_kept_as_separate_groups():
    records = [
        {"id": 1, "geometry": rect(0, 0, 10, 10), "attributes": {"k": None}},
        {"id": 2, "geometry": rect(20, 0, 30, 10), "attributes": {"k": ""}},
        {"id": 3, "geometry": rect(40, 0, 50, 10), "attributes": {"k": "konut"}},
    ]
    validated = gp.validate_request("dissolve", {"source_crs": UTM, "field": "k"})
    result = gp.dissolve(validated, records)
    assert result["group_count"] == 3
    assert sorted(group["group_kind"] for group in result["groups"]) == ["empty", "null", "value"]


def test_dissolve_without_a_field_merges_everything_into_one_group():
    validated = gp.validate_request("dissolve", {"source_crs": UTM})
    result = gp.dissolve(validated, PARCELS)
    assert result["group_count"] == 1
    assert result["groups"][0]["members"] == 3
    assert math.isclose(result["groups"][0]["area_sum_of_parts"], 40000.0, rel_tol=1e-12)


def test_the_count_rule_counts_members_not_non_null_values():
    validated = gp.validate_request("dissolve", {
        "source_crs": UTM, "field": "kullanim", "attribute_rules": {"nufus": "count"}})
    result = gp.dissolve(validated, PARCELS)
    konut = [group for group in result["groups"] if group["group"] == "konut"][0]
    assert konut["attributes"]["nufus"] == 2.0


# --------------------------------------------------------------------------
# Buffer
# --------------------------------------------------------------------------

def test_a_one_segment_buffer_is_a_square_whose_area_is_twice_r_squared():
    validated = gp.validate_request("buffer", {"source_crs": UTM, "distance": 10, "unit": "m", "segments": 1})
    result = gp.buffer(validated, [P1])
    assert result["vertices"] == 4
    # A square inscribed in a circle of radius r has area 2r² = 200.
    assert math.isclose(result["area_per_buffer"], 200.0, rel_tol=1e-12)
    assert math.isclose(result["circle_area"], 100.0 * math.pi, rel_tol=1e-12)
    assert math.isclose(result["area_ratio"], 2.0 / math.pi, rel_tol=1e-12)
    ring = result["features"][0]["geometry"]["parts"][0][0]
    assert len(ring) == 4
    assert math.isclose(ring[0][0], 60.0, abs_tol=1e-9)   # first vertex at angle 0, centre (50,50)


def test_a_finely_segmented_buffer_converges_on_the_circle_area():
    validated = gp.validate_request("buffer", {"source_crs": UTM, "distance": 10, "unit": "m", "segments": 90})
    result = gp.buffer(validated, [P1])
    assert result["vertices"] == 360
    # 0.5 * 360 * 100 * sin(1°) = 314.1433, within 0.01% of 100π = 314.1593.
    assert math.isclose(result["area_per_buffer"], 314.1433, rel_tol=1e-6)
    assert result["area_ratio"] < 1.0     # a vector buffer is always inside its circle


def test_the_buffer_ring_actually_reaches_the_requested_radius():
    validated = gp.validate_request("buffer", {"source_crs": UTM, "distance": 10, "unit": "m", "segments": 4})
    ring = gp.buffer(validated, [P1])["features"][0]["geometry"]["parts"][0][0]
    for x, y in ring:
        assert math.isclose(math.hypot(x - 50.0, y - 50.0), 10.0, rel_tol=1e-12)


def test_buffering_a_polygon_is_refused_with_a_reason_rather_than_approximated():
    validated = gp.validate_request("buffer", {"source_crs": UTM, "distance": 10, "unit": "m"})
    result = gp.buffer(validated, [{"id": 1, "geometry": rect(0, 0, 10, 10), "attributes": {}}])
    assert result["output"] == 0
    assert result["refused"][0]["reason"] == "offset_geometry_requires_the_geometry_engine"


def test_the_buffer_result_states_both_the_request_and_the_map_unit_radius():
    validated = gp.validate_request("buffer", {"source_crs": UTM, "distance": 2, "unit": "km"})
    result = gp.buffer(validated, [P1])
    assert result["requested"] == 2.0 and result["unit"] == "km"
    assert result["metres"] == 2000.0 and result["radius_map_units"] == 2000.0


# --------------------------------------------------------------------------
# Reproject
# --------------------------------------------------------------------------

def test_web_mercator_matches_its_closed_form_at_the_known_corners():
    assert gp.wgs84_to_web_mercator(0.0, 0.0) == (0.0, 0.0)
    x, y = gp.wgs84_to_web_mercator(180.0, 0.0)
    # R * pi with R = 6378137: the eastern edge of the Web Mercator square.
    assert math.isclose(x, 20037508.342789244, rel_tol=1e-12)
    assert math.isclose(y, 0.0, abs_tol=1e-9)
    _x, top = gp.wgs84_to_web_mercator(0.0, gp.WEB_MERCATOR_MAX_LATITUDE)
    assert math.isclose(top, 20037508.342789244, rel_tol=1e-9)


def test_reprojection_round_trips_to_the_original_coordinate():
    forward = gp.wgs84_to_web_mercator(28.9784, 41.0082)
    back = gp.web_mercator_to_wgs84(*forward)
    assert math.isclose(back[0], 28.9784, abs_tol=1e-9)
    assert math.isclose(back[1], 41.0082, abs_tol=1e-9)


def test_a_latitude_outside_the_projection_is_refused_and_the_feature_is_named():
    validated = gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": MERCATOR})
    result = gp.reproject(validated, [
        {"id": "ok", "geometry": point(0.0, 0.0), "attributes": {}},
        {"id": "pole", "geometry": point(0.0, 89.0), "attributes": {}},
    ])
    assert result["output"] == 1
    assert result["failed"] == [
        {"id": "pole", "reason": "a_vertex_falls_outside_the_target_projection"}]
    # Input count minus output count is explained, not a silent gap.
    assert result["input"] == 2


def test_reprojection_moves_every_vertex_of_a_polygon():
    validated = gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": MERCATOR})
    result = gp.reproject(validated, [{"id": 1, "geometry": rect(0, 0, 1, 1), "attributes": {}}])
    ring = result["features"][0]["geometry"]["parts"][0][0]
    assert len(ring) == 4
    # 1 degree of longitude at the equator: R * pi/180.
    assert math.isclose(ring[1][0], 6378137.0 * math.pi / 180.0, rel_tol=1e-12)
    assert result["unit_change"] is True


def test_reprojecting_to_the_same_crs_is_refused_as_a_no_op():
    with pytest.raises(gp.GeoprocessingError):
        gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": WGS84})


def test_a_transform_this_kernel_cannot_perform_is_refused_not_faked():
    validated = gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": UTM})
    with pytest.raises(gp.GeoprocessingError) as error:
        gp.reproject(validated, [P1])
    assert "EPSG:32635" in str(error.value)


def test_an_injected_transform_is_used_when_one_is_supplied():
    validated = gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": UTM})
    result = gp.reproject(validated, [P1], transform=lambda x, y: (x * 2.0, y * 3.0))
    assert result["features"][0]["geometry"]["points"] == [(100.0, 150.0)]


# --------------------------------------------------------------------------
# Zonal statistics
# --------------------------------------------------------------------------

ZONAL_POINTS = [
    {"id": 1, "geometry": point(25, 50), "attributes": {"nufus": 100}},
    {"id": 2, "geometry": point(75, 50), "attributes": {"nufus": None}},   # in A, no value
    {"id": 3, "geometry": point(150, 50), "attributes": {"nufus": 300}},
    {"id": 4, "geometry": point(250, 50), "attributes": {"nufus": 999}},   # in no zone at all
]
ZONE_C = {"id": "C", "geometry": rect(0, 500, 100, 600), "attributes": {"zone": "C"}}


def test_a_zonal_mean_divides_by_the_values_it_used_not_by_the_features_it_saw():
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "mean",
        "value_field": "nufus", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, ZONAL_POINTS, ZONES)
    rows = {row["zone"]: row for row in result["zones"]}
    # Zone A holds two points but only one nufus. 100/1 = 100, not 100/2 = 50.
    assert rows["A"]["value"] == 100.0
    assert rows["A"]["features"] == 2
    assert rows["A"]["values_used"] == 1
    assert rows["A"]["nulls"] == 1
    assert rows["B"]["value"] == 300.0


def test_a_feature_in_no_zone_is_counted_and_its_value_reaches_no_total():
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "sum",
        "value_field": "nufus", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, ZONAL_POINTS, ZONES)
    total = math.fsum(row["value"] or 0.0 for row in result["zones"])
    # 100 + 300 = 400. The stray 999 outside both zones is reported, not added.
    assert math.isclose(total, 400.0, rel_tol=1e-12)
    assert result["unassigned"] == 1


def test_an_empty_zone_is_reported_with_no_value_rather_than_omitted_or_zeroed():
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "mean",
        "value_field": "nufus", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, ZONAL_POINTS, ZONES + [ZONE_C])
    empty = [row for row in result["zones"] if row["zone"] == "C"][0]
    assert empty["features"] == 0
    assert empty["values_used"] == 0
    assert empty["value"] is None      # a mean of nothing is unknown, not 0.0


def test_a_zonal_count_of_an_empty_zone_is_genuinely_zero():
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "count", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, ZONAL_POINTS, ZONES + [ZONE_C])
    counts = {row["zone"]: row["value"] for row in result["zones"]}
    assert counts == {"A": 2.0, "B": 1.0, "C": 0.0}


def test_min_and_max_come_from_the_real_values_in_each_zone():
    records = [
        {"id": 1, "geometry": point(25, 50), "attributes": {"v": 5}},
        {"id": 2, "geometry": point(75, 50), "attributes": {"v": 45}},
        {"id": 3, "geometry": point(150, 50), "attributes": {"v": 300}},
    ]
    for statistic, expected in (("min", {"A": 5.0, "B": 300.0}), ("max", {"A": 45.0, "B": 300.0})):
        validated = gp.validate_request("zonal_statistics", {
            "source_crs": UTM, "overlay_crs": UTM, "statistic": statistic,
            "value_field": "v", "zone_field": "zone"})
        result = gp.zonal_statistics(validated, records, ZONES)
        assert {row["zone"]: row["value"] for row in result["zones"]} == expected


def test_centroid_assignment_puts_a_parcel_in_exactly_one_zone():
    # The parcel straddles the A/B edge but its centroid at x=100 lands on the
    # boundary, so it is assigned once - to the first zone that contains it.
    straddling = [{"id": 1, "geometry": rect(50, 20, 150, 80), "attributes": {"v": 10}}]
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "count", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, straddling, ZONES)
    assert {row["zone"]: row["value"] for row in result["zones"]} == {"A": 1.0, "B": 0.0}
    assert result["double_counted"] is False


def test_intersects_assignment_counts_the_parcel_in_both_zones_and_says_so():
    straddling = [{"id": 1, "geometry": rect(50, 20, 150, 80), "attributes": {"v": 10}}]
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "sum", "value_field": "v",
        "zone_field": "zone", "assignment": "intersects"})
    result = gp.zonal_statistics(validated, straddling, ZONES)
    assert {row["zone"]: row["value"] for row in result["zones"]} == {"A": 10.0, "B": 10.0}
    assert result["assigned"] == 2
    assert result["double_counted"] is True


def test_a_line_cannot_be_assigned_by_centroid_and_is_reported_not_guessed():
    lines = [{"id": 1, "geometry": line((10, 10), (90, 90)), "attributes": {"v": 1}}]
    validated = gp.validate_request("zonal_statistics", {
        "source_crs": UTM, "overlay_crs": UTM, "statistic": "count", "zone_field": "zone"})
    result = gp.zonal_statistics(validated, lines, ZONES)
    assert result["unassignable"] == 1
    assert result["assigned"] == 0


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def test_execute_routes_every_operation_to_its_kernel():
    cases = [
        (gp.validate_request("spatial_join", {"source_crs": UTM, "overlay_crs": UTM,
                                              "join_fields": ["zone"]}), POINTS, ZONES),
        (gp.validate_request("select_by_location", {"source_crs": UTM, "overlay_crs": UTM}), POINTS, ZONES),
        (gp.validate_request("clip", {"source_crs": UTM, "bbox": [0, 0, 100, 100]}), POINTS, ()),
        (gp.validate_request("intersect", {"source_crs": UTM, "overlay_crs": UTM}), POINTS, ZONES),
        (gp.validate_request("dissolve", {"source_crs": UTM, "field": "kullanim"}), PARCELS, ()),
        (gp.validate_request("buffer", {"source_crs": UTM, "distance": 5}), POINTS, ()),
        (gp.validate_request("zonal_statistics", {"source_crs": UTM, "overlay_crs": UTM,
                                                  "statistic": "count"}), ZONAL_POINTS, ZONES),
    ]
    for validated, source, overlay in cases:
        result = gp.execute(validated, source, overlay)
        assert result["kind"] == validated["operation"]
    reprojected = gp.execute(
        gp.validate_request("reproject", {"source_crs": WGS84, "target_crs": MERCATOR}),
        [{"id": 1, "geometry": point(0.0, 0.0), "attributes": {}}])
    assert reprojected["kind"] == "reproject"


def test_every_declared_operation_has_a_dispatch_route():
    # A new operation added to OPERATIONS without a branch in execute() would
    # silently fall through to zonal statistics; this is the guard.
    for operation in gp.OPERATIONS:
        assert operation in {
            "spatial_join", "select_by_location", "clip", "intersect",
            "dissolve", "buffer", "reproject", "zonal_statistics",
        }
