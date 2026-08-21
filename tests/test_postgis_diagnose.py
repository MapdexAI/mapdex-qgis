"""The catalogue question, on the desktop.

It is the only question in this domain that names no table, because "which of
my tables has no spatial index" is about the set: asking it one table at a time
is not a narrower version of it, it is a different question with no answer.

That makes it the narrowest surface here rather than the widest. It takes no
caller identifier at all, so two of the three read-only layers - identifier
validation and the guard - have nothing to validate, and the third, a builder
that can only emit SELECT, carries the whole claim on its own.
"""

import pytest

from mapdex_qgis._vendor.nivo import postgis


def test_the_statement_names_no_table_and_takes_no_identifier():
    sql, params = postgis.build_diagnostics()
    # One parameter: the cap. Nothing a caller could have written.
    assert params == [postgis.MAX_DIAGNOSTIC_TABLES]
    assert sql.lower().startswith("select")


def test_the_guard_accepts_it():
    # It runs through the same last line of defence as every other builder,
    # which is what makes "read-only by construction" a property of this module
    # rather than a promise about one function.
    sql, _ = postgis.build_diagnostics()
    assert postgis.guard_statement(sql) == sql


def test_it_looks_for_a_spatial_index_on_the_geometry_column():
    # A btree on an id is an index and does nothing for a spatial query.
    # Counting it would answer the opposite of the question being asked.
    sql, _ = postgis.build_diagnostics()
    lowered = sql.lower()
    assert "gist" in lowered and "spgist" in lowered
    assert "a.attname = g.f_geometry_column" in lowered


def test_it_estimates_rather_than_counting_every_row():
    # Counting every row to answer a diagnostic question is a scan nobody asked
    # for, and it is slowest on exactly the tables where the answer matters.
    sql, _ = postgis.build_diagnostics()
    assert "reltuples" in sql.lower()
    assert "count(*)" not in sql.lower().split("from geometry_columns")[0].replace(
        "select count(*) from pg_index", "")


def test_the_system_schemas_are_left_out():
    # A person asking about their tables does not mean `topology.topology`.
    sql, _ = postgis.build_diagnostics()
    for schema in postgis.SYSTEM_SCHEMAS:
        assert "'{}'".format(schema) in sql


def _row(schema, table, geometry_type, srid, estimate, size, indexes):
    return [schema, table, "geom", geometry_type, srid, estimate, size, indexes]


def test_an_unanalysed_table_is_unknown_rather_than_empty():
    # reltuples is -1 when the planner has never looked. Reading that as zero
    # rows turns an absence of information into a fact about the data.
    answer = postgis.read_diagnostics([_row("public", "parcels", "POLYGON", 4326, -1, 8192, 1)])
    table = answer["tables"][0]
    assert table["analysed"] is False
    assert table["row_estimate"] == 0
    assert answer["never_analysed"] == [table]


def test_a_row_estimate_is_never_reported_as_exact():
    for estimate in (-1, 0, 1200):
        answer = postgis.read_diagnostics([_row("public", "p", "POLYGON", 4326, estimate, 1, 1)])
        assert answer["tables"][0]["exact"] is False


def test_the_three_questions_are_answered_as_their_own_lists():
    # Handing back a register and expecting the reader to spot the four rows
    # that matter has not answered them.
    answer = postgis.read_diagnostics([
        _row("public", "parcels", "POLYGON", 4326, 6, 8192, 1),
        _row("public", "roads", "LINESTRING", 4326, 40, 8192, 0),
        _row("public", "points", "POINT", 0, 5, 8192, 1),
    ])
    assert answer["table_count"] == 3
    assert [t["table"] for t in answer["missing_spatial_index"]] == ["roads"]
    assert [t["table"] for t in answer["undeclared_srid"]] == ["points"]
    assert answer["never_analysed"] == []


def test_a_capped_register_says_so():
    # "The first two hundred" and "all of them" are different answers to
    # "which tables have no index".
    rows = [_row("public", "t{}".format(i), "POLYGON", 4326, 1, 1, 1)
            for i in range(postgis.MAX_DIAGNOSTIC_TABLES)]
    assert postgis.read_diagnostics(rows)["truncated"] is True
    assert postgis.read_diagnostics(rows[:-1])["truncated"] is False


def test_a_short_row_is_dropped_rather_than_read_as_zeros():
    # A row the query did not produce is not a table with no index; it is a
    # result somebody else's code returned, and guessing at it invents a finding.
    answer = postgis.read_diagnostics([["public", "parcels"]])
    assert answer["tables"] == []


def test_the_capability_declares_only_the_connection():
    from mapdex_qgis._vendor.nivo import capabilities

    capability = capabilities.get("postgis.diagnose@1")
    assert capability is not None
    assert set(capability.params) == {"connection_id"}
    assert capability.params["connection_id"]["required"] is True


def test_a_request_naming_a_table_is_refused():
    # Not a narrower question. A caller who sends one has misunderstood what
    # this answers, and accepting the parameter silently would confirm it.
    from mapdex_qgis._vendor.nivo import capabilities

    with pytest.raises(Exception):
        capabilities.validate_request(
            "postgis.diagnose@1", {"connection_id": "gis", "table": "parcels"})


def test_the_capability_is_bound_to_an_executor():
    # A capability with no executor is a promise the panel cannot keep. This
    # repository has that scar in several places; the check is cheap.
    from mapdex_qgis.qgis_runtime import PLUGIN_BOUND_CAPABILITIES, bound_capability_ids

    assert "postgis.diagnose@1" in (bound_capability_ids() | PLUGIN_BOUND_CAPABILITIES)


# The ceiling is the module's, not the caller's. The floor of 1 is `_limit`'s
# shared behaviour across every builder here rather than a decision about
# diagnostics, and changing it for this one would move the others with it.
@pytest.mark.parametrize("limit,expected", [(5, 5), (10_000, 200), (0, 1), (-1, 1)])
def test_the_ceiling_is_the_modules_decision_not_the_callers(limit, expected):
    _, params = postgis.build_diagnostics(limit)
    assert params == [expected]
