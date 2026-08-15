"""PostGIS read-only boundary and typed query-builder tests.

The security assertions here are the important ones: they prove that the only
way to reach the database is through a builder, and that the guard refuses
anything that is not a single provably read-only SELECT.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis.postgis import (  # noqa: E402
    ReadOnlyViolation,
    build_bbox_count,
    build_categorical,
    build_group_aggregate,
    build_nearest,
    build_numeric_stats,
    build_profile,
    build_spatial_join_counts,
    build_spatial_relationship_count,
    build_top_n,
    catalog_from_columns,
    describe_refusal,
    guard_statement,
    quote_identifier,
    session_setup,
)

PARCELS = catalog_from_columns(
    "public",
    "parcels",
    [
        {"name": "gid", "type": "integer"},
        {"name": "area", "type": "double precision"},
        {"name": "landuse", "type": "character varying"},
        {"name": "geom", "type": "geometry"},
    ],
    geometry_column="geom",
    srid=3857,
    connection_id="conn_a",
)
BUILDINGS = catalog_from_columns(
    "public",
    "buildings",
    [{"name": "gid", "type": "integer"}, {"name": "geom", "type": "geometry"}],
    geometry_column="geom",
    srid=4326,
    connection_id="conn_a",
)


# --------------------------------------------------------------------------
# The read-only boundary
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "statement",
    [
        "insert into parcels values (1)",
        "UPDATE parcels SET area = 0",
        "delete from parcels",
        "drop table parcels",
        "TRUNCATE parcels",
        "alter table parcels add column x int",
        "create index on parcels (gid)",
        "grant all on parcels to public",
        "merge into parcels using x on true",
        "vacuum full",
        "call do_something()",
        "select 1; drop table parcels",
        "select * into new_parcels from parcels",
        "select * from parcels for update",
        "select pg_read_file('/etc/passwd')",
        "select pg_sleep(60)",
        "select dblink('host=x', 'select 1')",
        "",
        "   ",
    ],
)
def test_write_and_side_effect_statements_are_refused(statement):
    with pytest.raises(ReadOnlyViolation):
        guard_statement(statement)


def test_a_plain_select_is_allowed():
    assert guard_statement("select count(*) from parcels").startswith("select")
    assert guard_statement("with x as (select 1) select * from x").startswith("with")


def test_trailing_semicolon_is_tolerated_but_a_second_statement_is_not():
    assert guard_statement("select 1;").strip() == "select 1"
    with pytest.raises(ReadOnlyViolation):
        guard_statement("select 1; select 2")


def test_a_forbidden_word_inside_a_string_literal_does_not_block_a_valid_query():
    # A parcel legitimately labelled 'update' must remain queryable.
    assert guard_statement("select * from parcels where note = 'update the plan'")


def test_a_forbidden_word_hidden_in_a_comment_is_still_caught_or_stripped():
    # The comment is stripped, so this is just "select 1" and must pass.
    assert guard_statement("select 1 -- drop table parcels")
    assert guard_statement("select 1 /* delete from parcels */")
    # But a real statement after a comment must not slip through.
    with pytest.raises(ReadOnlyViolation):
        guard_statement("select 1 /* x */ ; drop table parcels")


def test_median_percentile_within_group_is_not_mistaken_for_select_into():
    sql, _params = build_numeric_stats(PARCELS, "area")
    assert "within group" in sql


def test_identifier_quoting_rejects_injection_attempts():
    assert quote_identifier("area") == '"area"'
    for bad in ('a"; drop table x --', "a b", "1abc", "", "x" * 64, "área"):
        with pytest.raises(ReadOnlyViolation):
            quote_identifier(bad)


def test_unknown_columns_and_tables_cannot_be_referenced():
    with pytest.raises(ReadOnlyViolation):
        PARCELS.column("secret_column")
    with pytest.raises(ReadOnlyViolation):
        build_top_n(PARCELS, "not_a_column", "gid")


def test_catalog_discovery_drops_unquotable_columns_but_keeps_the_table_usable():
    catalog = catalog_from_columns(
        "public",
        "mixed",
        [{"name": "ok", "type": "int"}, {"name": 'bad"name', "type": "text"}],
    )
    assert "ok" in catalog.columns
    assert 'bad"name' not in catalog.columns


def test_a_geometry_column_that_was_not_discovered_is_not_trusted():
    catalog = catalog_from_columns("public", "t", [{"name": "gid", "type": "int"}], geometry_column="geom")
    assert catalog.geometry_column == ""
    with pytest.raises(ReadOnlyViolation):
        catalog.geometry()


def test_session_setup_opens_a_read_only_bounded_transaction():
    statements = session_setup(5000)
    assert statements[0] == "BEGIN READ ONLY"
    assert any("statement_timeout = 5000" in item for item in statements)
    assert any("default_transaction_read_only = on" in item for item in statements)


def test_session_timeout_is_clamped():
    assert "statement_timeout = 60000" in " ".join(session_setup(10**9))
    assert "statement_timeout = 1000" in " ".join(session_setup(1))


def test_refusal_explains_the_boundary_without_blaming_the_user():
    text = describe_refusal("delete rows")
    assert "read-only" in text.lower()
    assert "can't modify" in text.lower() or "cannot modify" in text.lower()


# --------------------------------------------------------------------------
# The typed builders
# --------------------------------------------------------------------------

def test_profile_reports_count_extent_srid_and_validity():
    sql, params = build_profile(PARCELS)
    assert params == []
    for fragment in ("count(*)", "ST_Extent", "ST_SRID", "ST_IsValid"):
        assert fragment in sql


def test_numeric_stats_are_parameterised_and_quoted():
    sql, params = build_numeric_stats(PARCELS, "area", bbox=[0, 0, 10, 10])
    assert '"area"' in sql and '"public"."parcels"' in sql
    # The bbox travels as parameters, never interpolated into the statement.
    assert params == [0.0, 0.0, 10.0, 10.0, 3857]
    assert "%s" in sql
    assert "10.0" not in sql


def test_categorical_query_is_bounded_by_a_parameterised_limit():
    sql, params = build_categorical(PARCELS, "landuse", limit=10_000)
    assert params[-1] == 200  # clamped to MAX_GROUPS
    assert "group by 1" in sql


def test_group_aggregate_supports_only_the_closed_statistic_set():
    sql, params = build_group_aggregate(PARCELS, "landuse", "mean", "area")
    assert "avg(" in sql
    assert params[-1] == 50
    with pytest.raises(ReadOnlyViolation):
        build_group_aggregate(PARCELS, "landuse", "'; drop table x --", "area")
    with pytest.raises(ReadOnlyViolation):
        build_group_aggregate(PARCELS, "landuse", "mean", None)


def test_top_n_returns_identifiers_and_orders_correctly():
    sql, params = build_top_n(PARCELS, "area", "gid", limit=20)
    assert "order by value desc" in sql
    assert params[-1] == 20
    ascending, _ = build_top_n(PARCELS, "area", "gid", ascending=True)
    assert "order by value asc" in ascending


def test_spatial_relationship_transforms_into_a_single_srid():
    sql, params = build_spatial_relationship_count(PARCELS, BUILDINGS, "intersects")
    assert "ST_Intersects" in sql
    # The right-hand geometry is transformed into the left table's SRID.
    assert "ST_Transform" in sql
    assert params == [3857]


def test_unsupported_predicates_are_refused():
    with pytest.raises(ReadOnlyViolation):
        build_spatial_relationship_count(PARCELS, BUILDINGS, "sql_injection")


def test_cross_connection_spatial_joins_are_refused():
    other = catalog_from_columns(
        "public", "t", [{"name": "geom", "type": "geometry"}], geometry_column="geom", connection_id="conn_b"
    )
    with pytest.raises(ReadOnlyViolation):
        build_spatial_relationship_count(PARCELS, other, "intersects")


def test_spatial_join_counts_builds_the_buildings_per_district_query():
    sql, params = build_spatial_join_counts(PARCELS, BUILDINGS, "landuse")
    assert "left join" in sql
    assert "ST_Intersects" in sql
    assert params[0] == 3857


def test_nearest_uses_the_knn_operator_and_reports_metres():
    sql, params = build_nearest(PARCELS, "gid", 10.0, 20.0, 4326, 5)
    assert "<->" in sql
    assert "::geography" in sql
    assert params[-1] == 5


def test_bbox_count_rejects_an_inverted_box():
    sql, params = build_bbox_count(PARCELS, [0, 0, 10, 10], 4326)
    assert "count(*)" in sql and params[:4] == [0.0, 0.0, 10.0, 10.0]
    with pytest.raises(ReadOnlyViolation):
        build_bbox_count(PARCELS, [10, 10, 0, 0], 4326)
    with pytest.raises(ReadOnlyViolation):
        build_bbox_count(PARCELS, [1, 2], 4326)


def test_every_builder_output_passes_the_guard():
    # Each builder guards its own output; this asserts none of them can drift.
    for sql, _params in (
        build_profile(PARCELS),
        build_numeric_stats(PARCELS, "area"),
        build_categorical(PARCELS, "landuse"),
        build_group_aggregate(PARCELS, "landuse", "count"),
        build_top_n(PARCELS, "area", "gid"),
        build_spatial_relationship_count(PARCELS, BUILDINGS, "within"),
        build_spatial_join_counts(PARCELS, BUILDINGS, "landuse"),
        build_nearest(PARCELS, "gid", 1.0, 2.0),
        build_bbox_count(PARCELS, [0, 0, 1, 1]),
    ):
        assert guard_statement(sql)
