# SPDX-License-Identifier: MIT
"""Read-only PostGIS analytics built from typed intent, never from model SQL.

The product requirement is that a GIS user asks questions in their own language
instead of writing SQL. The tempting implementation - hand the question to a
model and run whatever SQL comes back - is prohibited here, because a model that
can emit SQL can emit ``DROP TABLE`` and no amount of prompt instruction is a
security boundary.

Instead the model produces a typed :class:`AnalyticalSpec`; trusted code in this
module turns that spec into parameterised SQL. Three independent layers keep it
read-only, so a defect in any one of them is not sufficient to write:

1. **Construction.** Only the builders below can emit SQL, and every one of them
   emits a single ``SELECT``. Statements are *composed*, never concatenated: a
   fixed :class:`SQL` template accepts only :class:`Composable` parts, so there
   is no code path that renders a caller string into a statement.
2. **Identifier validation.** Schema/table/column names are matched against the
   catalogue discovered from the live connection and then quoted by
   :class:`Identifier`. An identifier that was not discovered is rejected, so a
   forged name cannot reach the server.
3. **Execution.** :func:`guard_statement` re-inspects the finished SQL, and the
   executor opens a ``READ ONLY`` transaction with a statement timeout and row
   cap. This is belt-and-braces on purpose: the guard exists to catch a future
   builder bug, not to sanitise user input.

Credentials never appear here. Connections are addressed by the stable id of a
saved host connection; the DSN, host, user and password stay inside the
platform credential store and are never placed in a prompt, a log, or an error.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Sequence

MAX_ROWS = 1000
MAX_GROUPS = 200
DEFAULT_TIMEOUT_MS = 15_000
MAX_TIMEOUT_MS = 60_000
MAX_IDENTIFIER = 63  # PostgreSQL NAMEDATALEN - 1

# Statements that must never be produced. Checked as whole words so a column
# honestly called "created_at" or a table called "updates" is not blocked.
FORBIDDEN_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "upsert", "truncate", "drop", "create",
    "alter", "grant", "revoke", "comment", "reindex", "vacuum", "analyze", "cluster",
    "copy", "call", "do", "execute", "prepare", "deallocate", "listen", "notify",
    "lock", "refresh", "reassign", "security", "set", "reset", "begin", "commit",
    "rollback", "savepoint", "discard", "import", "load", "checkpoint",
})

# Functions that reach outside the query's own data or write to disk.
FORBIDDEN_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "dblink_exec", "pg_sleep",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "set_config", "pg_logdir_ls", "pg_file_write", "query_to_xml",
    "current_setting", "pg_notify", "table_to_xml",
})

# Families denied by prefix, because a list of names misses the next member of
# the family. dblink_connect and dblink_send_query open a separate session that
# a READ ONLY transaction does not cover; lo_* reads and writes large objects;
# pg_ls_* lists server directories; pg_advisory* takes locks. Builders quote
# every identifier and the scan blanks quoted text, so a column that happens to
# start with one of these cannot trip this.
FORBIDDEN_FUNCTION_PREFIXES = ("dblink", "lo_", "pg_ls_", "pg_advisory", "pg_read_", "pg_file_")

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Statistics a caller may request. Closed set: the name maps to a fixed SQL
# fragment, so a caller can never inject an expression through the statistic.
AGGREGATES = {
    "count": "count(*)",
    "sum": "sum({column})",
    "mean": "avg({column})",
    "min": "min({column})",
    "max": "max({column})",
    "median": "percentile_cont(0.5) within group (order by {column})",
    "stddev": "stddev_samp({column})",
    "distinct": "count(distinct {column})",
}

# Spatial predicates, mapped to the indexed PostGIS function. ST_Intersects and
# friends use the GiST index; the && bbox operator is added by PostGIS itself.
PREDICATES = {
    "intersects": "ST_Intersects",
    "within": "ST_Within",
    "contains": "ST_Contains",
    "overlaps": "ST_Overlaps",
    "touches": "ST_Touches",
    "crosses": "ST_Crosses",
    "disjoint": "ST_Disjoint",
}


class ReadOnlyViolation(Exception):
    """Raised when a statement is not a provably read-only single SELECT."""


def _strip_literals_and_comments(sql: str) -> str:
    """Blank out strings and comments so keyword scanning sees only code.

    Without this a perfectly legitimate ``WHERE name = 'update the plan'``
    would be rejected, and - far worse - a keyword hidden inside a comment
    would be missed by a naive scan.
    """
    out = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(" ")
            continue
        if char == '"':
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(" ")
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            while index < length and sql[index] != "\n":
                index += 1
            out.append(" ")
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            index += 2
            depth = 1
            while index < length and depth:
                if sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                elif sql.startswith("/*", index):
                    depth += 1
                    index += 2
                else:
                    index += 1
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def guard_statement(sql: str) -> str:
    """Prove a statement is a single read-only SELECT, or refuse it.

    This runs on SQL this module generated. It is the last line of defence, and
    it is intentionally stricter than the builders need: if a future change ever
    makes a builder emit something else, execution stops here.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlyViolation("empty statement")
    # The scan copy has string literals, quoted identifiers and comments blanked
    # so keyword matching sees only executable code. It is used for inspection
    # ONLY - the statement returned for execution is the original text.
    scan = _strip_literals_and_comments(sql).strip().rstrip(";").strip()
    if ";" in scan:
        raise ReadOnlyViolation("multiple statements are not allowed")
    lowered = scan.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ReadOnlyViolation("only SELECT statements are allowed")
    words = set(re.findall(r"[a-z_][a-z0-9_]*", lowered))
    forbidden = words & FORBIDDEN_KEYWORDS
    if forbidden:
        raise ReadOnlyViolation("forbidden statement keyword: {}".format(sorted(forbidden)[0]))
    functions = (words & FORBIDDEN_FUNCTIONS) | {
        word for word in words if word.startswith(FORBIDDEN_FUNCTION_PREFIXES)}
    if functions:
        raise ReadOnlyViolation("forbidden function: {}".format(sorted(functions)[0]))
    # `SELECT ... INTO new_table` writes despite starting with SELECT. There is
    # no exemption for `within group`: that phrase does not contain the word
    # `into`, so the exemption protected nothing, and it let
    # `percentile_cont(0.5) within group (order by a) into t2` through.
    if re.search(r"\binto\b", lowered):
        raise ReadOnlyViolation("SELECT INTO is not allowed")
    if re.search(r"\bfor\s+(update|share|no\s+key\s+update)\b", lowered):
        raise ReadOnlyViolation("row locking is not allowed")
    return sql.strip().rstrip(";").strip()


def quote_identifier(name: str) -> str:
    """Quote a validated identifier for PostgreSQL."""
    text = str(name or "")
    if not text or len(text) > MAX_IDENTIFIER:
        raise ReadOnlyViolation("invalid identifier")
    if not IDENTIFIER_PATTERN.match(text):
        # Only ASCII identifiers are accepted. A quoted identifier containing a
        # double quote is the classic escape used to break out of quoting.
        raise ReadOnlyViolation("identifier contains unsupported characters")
    return '"{}"'.format(text)


class Composable:
    """A piece of SQL that knows how to render itself safely.

    This mirrors ``psycopg2.sql`` deliberately, because that is the API a
    PostgreSQL reviewer already trusts. It is reimplemented here rather than
    imported for one concrete reason: ``psycopg2.sql.Identifier.as_string``
    requires a live connection or cursor (it delegates to
    ``psycopg2.extensions.quote_ident``), and this module is a pure builder. It
    has no connection, holds no credentials, and must render a statement so
    that :func:`guard_statement` can inspect the finished text before anything
    is executed. Composing against a connection would move the guard behind the
    very boundary it protects.

    The safety property is the same one psycopg2 provides: a caller cannot
    interpolate its own text into a statement. Only a ``Composable`` may be
    formatted into :class:`SQL`, and the only ``Composable`` that carries a
    caller-derived name is :class:`Identifier`, which validates and quotes it.
    """

    __slots__ = ()

    def as_string(self) -> str:
        raise NotImplementedError


class SQL(Composable):
    """Fixed SQL written in this module - never a caller-supplied string.

    Every instance in this file is constructed from a literal or from one of
    the closed :data:`AGGREGATES` / :data:`PREDICATES` fragments.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str):
        # Exactly `str`, not a subclass: a subclass can override the behaviour
        # the rest of this class relies on, and every template here is a literal.
        if type(text) is not str:
            raise ReadOnlyViolation("SQL text must be a plain string")
        self._text = text

    def as_string(self) -> str:
        return self._text

    def format(self, **parts: Composable) -> "Composed":
        """Substitute composed parts into this template.

        Passing a bare ``str`` raises: that rejection is the whole mechanism,
        and it is what makes "a caller cannot reach the statement text" a
        property of the code rather than a convention.
        """
        rendered: dict[str, str] = {}
        for name, part in parts.items():
            if not isinstance(part, Composable):
                raise ReadOnlyViolation("only composed SQL may be formatted into a statement")
            rendered[name] = part.as_string()
        return Composed(self._text.format(**rendered))


class Identifier(Composable):
    """One or more identifiers, validated and quoted before they reach SQL.

    Construction validates eagerly, so an identifier that was not discovered in
    the live catalogue fails where it is named rather than inside a statement.
    """

    __slots__ = ("_parts",)

    def __init__(self, *parts: str):
        if not parts:
            raise ReadOnlyViolation("invalid identifier")
        self._parts = tuple(quote_identifier(part) for part in parts)

    def as_string(self) -> str:
        return ".".join(self._parts)


class Composed(Composable):
    """The rendered result of :meth:`SQL.format`, reusable as a nested part."""

    __slots__ = ("_text",)

    def __init__(self, text: str):
        self._text = text

    def as_string(self) -> str:
        return self._text


def _where(statement: Composable, clause: Composable | None) -> Composable:
    """Append a WHERE clause, or return the statement unchanged."""
    if clause is None:
        return statement
    return SQL("{statement} where {clause}").format(statement=statement, clause=clause)


class TableCatalog:
    """The discovered shape of one PostGIS table.

    Nothing outside this catalogue may be referenced by a query. It is built by
    inspecting the live connection, so a name the model invented - or a name a
    stale conversation still remembers - simply does not resolve.
    """

    def __init__(
        self,
        schema: str,
        table: str,
        columns: Mapping[str, str],
        geometry_column: str = "",
        srid: int = 0,
        connection_id: str = "",
    ):
        self.schema = schema
        self.table = table
        self.columns = dict(columns)
        self.geometry_column = geometry_column
        self.srid = int(srid or 0)
        self.connection_id = connection_id

    @property
    def qualified(self) -> Identifier:
        return Identifier(self.schema, self.table)

    def column(self, name: str) -> Identifier:
        if name not in self.columns:
            raise ReadOnlyViolation("unknown column")
        return Identifier(name)

    def numeric_columns(self) -> list[str]:
        numeric = ("int", "float", "double", "numeric", "decimal", "real", "serial", "money")
        return [name for name, kind in self.columns.items() if any(item in str(kind).lower() for item in numeric)]

    def text_columns(self) -> list[str]:
        textual = ("char", "text", "uuid", "bool", "enum", "name")
        return [name for name, kind in self.columns.items() if any(item in str(kind).lower() for item in textual)]

    def geometry(self) -> Identifier:
        if not self.geometry_column:
            raise ReadOnlyViolation("table has no geometry column")
        return self.column(self.geometry_column)


def _limit(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


def _bbox_filter(
    catalog: TableCatalog,
    bbox: Sequence[float] | None,
    srid: int,
) -> tuple[Composable | None, list[Any]]:
    """Build an index-usable bbox predicate, or nothing when no bbox is given."""
    if not bbox or len(bbox) != 4:
        return None, []
    values = [float(item) for item in bbox]
    if values[0] > values[2] or values[1] > values[3]:
        raise ReadOnlyViolation("invalid bounding box")
    # && is the bbox operator backed by the GiST index; ST_Intersects on the
    # envelope keeps the semantics exact for non-rectangular geometry.
    clause = SQL("{geom} && ST_MakeEnvelope(%s, %s, %s, %s, %s)").format(geom=catalog.geometry())
    return clause, values + [int(srid or catalog.srid or 4326)]


def build_profile(catalog: TableCatalog) -> tuple[str, list[Any]]:
    """Feature count, extent and SRID in one pass - the "profile this" answer."""
    if catalog.geometry_column:
        statement = SQL(
            "select count(*) as feature_count, "
            "ST_XMin(ST_Extent({geom}))::float8 as minx, ST_YMin(ST_Extent({geom}))::float8 as miny, "
            "ST_XMax(ST_Extent({geom}))::float8 as maxx, ST_YMax(ST_Extent({geom}))::float8 as maxy, "
            "max(ST_SRID({geom})) as srid, "
            "count(*) filter (where {geom} is null) as null_geometry, "
            "count(*) filter (where {geom} is not null and not ST_IsValid({geom})) as invalid_geometry "
            "from {table}"
        ).format(geom=catalog.geometry(), table=catalog.qualified)
    else:
        statement = SQL("select count(*) as feature_count from {table}").format(table=catalog.qualified)
    return guard_statement(statement.as_string()), []


# Schemas PostGIS and its friends own. A person asking which of THEIR tables
# has no spatial index does not mean `topology.topology`, and including them
# would bury the four rows they are looking for.
SYSTEM_SCHEMAS = ("postgis", "topology", "tiger", "tiger_data")

MAX_DIAGNOSTIC_TABLES = 200


def build_diagnostics(limit: int = MAX_DIAGNOSTIC_TABLES) -> tuple[str, list[Any]]:
    """The catalogue: which spatial tables exist and what state they are in.

    The only builder here that names no table, because the question cannot be
    asked one table at a time - "which of my tables has no spatial index" is
    about the set. It therefore takes no caller identifier at all, which makes
    it the narrowest surface in this module rather than the widest.

    Three deliberate choices, the same three the server's version makes, because
    a desktop answer that disagreed with the hosted one about the same database
    would be worse than no desktop answer.

    `reltuples` rather than `count(*)`: counting every row to answer a
    diagnostic question is a scan nobody asked for, and it is slowest on exactly
    the tables where the answer matters. -1 means never analysed, which is
    reported as unknown rather than as zero rows.

    The index check looks for a GiST or SP-GiST index ON THE GEOMETRY COLUMN,
    not any index on the table. A btree on an id is an index and does nothing
    for a spatial query, so counting it would answer the opposite question.

    `pg_total_relation_size` includes indexes and TOAST, because "how big is
    this table" means the space it occupies rather than the heap alone.
    """
    # The exclusion list is written into the statement rather than formatted
    # into it. Nothing here comes from a caller - the names are this module's
    # own constant - but a SQL string assembled by .format() cannot be told
    # apart from an injected one by a reader or by a scanner, and "it happens
    # to be safe today" is not a property anybody can check at a glance. As a
    # literal the statement is a constant; test_the_system_schemas_are_left_out
    # checks it against SYSTEM_SCHEMAS in both directions, so the two cannot
    # drift apart.
    statement = (
        "select g.f_table_schema, g.f_table_name, g.f_geometry_column, "
        "coalesce(g.type, '') as geometry_type, coalesce(g.srid, 0) as srid, "
        "coalesce(c.reltuples, -1)::bigint as row_estimate, "
        "coalesce(pg_total_relation_size(c.oid), 0)::bigint as total_bytes, "
        "coalesce((select count(*) from pg_index x "
        "join pg_class i on i.oid = x.indexrelid "
        "join pg_am am on am.oid = i.relam "
        "join pg_attribute a on a.attrelid = c.oid and a.attnum = any(x.indkey) "
        "where x.indrelid = c.oid and am.amname in ('gist', 'spgist') "
        "and a.attname = g.f_geometry_column), 0) as spatial_indexes "
        "from geometry_columns g "
        "join pg_class c on c.relname = g.f_table_name "
        "join pg_namespace n on n.oid = c.relnamespace and n.nspname = g.f_table_schema "
        "where g.f_table_schema not in ('postgis', 'topology', 'tiger', 'tiger_data') "
        "order by g.f_table_schema, g.f_table_name limit %s"
    )
    return guard_statement(statement), [_limit(limit, MAX_DIAGNOSTIC_TABLES, MAX_DIAGNOSTIC_TABLES)]


def read_diagnostics(rows: Iterable[Sequence[Any]], limit: int = MAX_DIAGNOSTIC_TABLES) -> dict[str, Any]:
    """Turn the rows into the answers a person actually asked for.

    Named lists rather than a register to search. Somebody asking which tables
    have no spatial index is handed those tables; handing back two hundred rows
    and expecting them to spot the four that matter has not answered them.
    """
    tables = []
    for row in rows or ():
        values = list(row)
        if len(values) < 8:
            continue
        estimate = int(values[5])
        analysed = estimate >= 0
        tables.append({
            "schema": str(values[0]),
            "table": str(values[1]),
            "geometry_column": str(values[2]),
            "geometry_type": str(values[3] or ""),
            "srid": int(values[4]),
            # An unknown row count is reported as unknown. Reading -1 as zero
            # turns an absence of information into a fact about the data.
            "row_estimate": estimate if analysed else 0,
            "analysed": analysed,
            # Never exact, even immediately after a statistics refresh.
            "exact": False,
            "has_spatial_index": int(values[7]) > 0,
            "total_bytes": int(values[6]),
        })
    return {
        "tables": tables,
        "table_count": len(tables),
        "missing_spatial_index": [t for t in tables if not t["has_spatial_index"]],
        "undeclared_srid": [t for t in tables if t["srid"] == 0],
        "never_analysed": [t for t in tables if not t["analysed"]],
        # The cap is stated rather than hidden: "the first two hundred" and
        # "all of them" are different answers to "which tables have no index".
        "truncated": len(tables) >= limit,
    }


def build_numeric_stats(
    catalog: TableCatalog,
    column: str,
    bbox: Sequence[float] | None = None,
) -> tuple[str, list[Any]]:
    """Descriptive statistics computed in the database, not in the client."""
    target = catalog.column(column)
    where, params = _bbox_filter(catalog, bbox, catalog.srid)
    statement = SQL(
        "select count(*) as total, count({col}) as usable, "
        "count(*) - count({col}) as nulls, "
        "min({col})::float8 as minimum, max({col})::float8 as maximum, "
        "sum({col})::float8 as total_sum, avg({col})::float8 as mean, "
        "stddev_samp({col})::float8 as stddev, "
        "percentile_cont(0.25) within group (order by {col})::float8 as p25, "
        "percentile_cont(0.5) within group (order by {col})::float8 as median, "
        "percentile_cont(0.75) within group (order by {col})::float8 as p75 "
        "from {table}"
    ).format(col=target, table=catalog.qualified)
    return guard_statement(_where(statement, where).as_string()), params


def build_categorical(
    catalog: TableCatalog,
    column: str,
    limit: int = 25,
    bbox: Sequence[float] | None = None,
) -> tuple[str, list[Any]]:
    """Category frequencies, ordered and bounded."""
    target = catalog.column(column)
    where, params = _bbox_filter(catalog, bbox, catalog.srid)
    statement = SQL("select {col}::text as value, count(*) as feature_count from {table}").format(
        col=target, table=catalog.qualified
    )
    statement = SQL(
        "{statement} group by 1 order by feature_count desc, value asc limit %s"
    ).format(statement=_where(statement, where))
    params = params + [_limit(limit, 25, MAX_GROUPS)]
    return guard_statement(statement.as_string()), params


def build_group_aggregate(
    catalog: TableCatalog,
    group_column: str,
    statistic: str = "count",
    value_column: str | None = None,
    limit: int = 50,
    bbox: Sequence[float] | None = None,
) -> tuple[str, list[Any]]:
    """The "X per Y" query: a typed group-by with a closed statistic set."""
    statistic = str(statistic or "count").lower()
    if statistic not in AGGREGATES:
        raise ReadOnlyViolation("unsupported statistic")
    group = catalog.column(group_column)
    if statistic == "count":
        expression: Composable = SQL(AGGREGATES["count"])
    else:
        if not value_column:
            raise ReadOnlyViolation("statistic requires a value column")
        expression = SQL(AGGREGATES[statistic]).format(column=catalog.column(value_column))
    where, params = _bbox_filter(catalog, bbox, catalog.srid)
    statement = SQL(
        "select {group}::text as group_value, ({expr})::float8 as value, "
        "count(*) as feature_count from {table}"
    ).format(group=group, expr=expression, table=catalog.qualified)
    statement = SQL(
        "{statement} group by 1 order by value desc nulls last, group_value asc limit %s"
    ).format(statement=_where(statement, where))
    return guard_statement(statement.as_string()), params + [_limit(limit, 50, MAX_GROUPS)]


def build_top_n(
    catalog: TableCatalog,
    column: str,
    id_column: str,
    limit: int = 10,
    ascending: bool = False,
    bbox: Sequence[float] | None = None,
) -> tuple[str, list[Any]]:
    """Rank features and return their identifiers so the map can select them."""
    target = catalog.column(column)
    identifier = catalog.column(id_column)
    where, params = _bbox_filter(catalog, bbox, catalog.srid)
    statement = SQL("select {id} as feature_id, {col}::float8 as value from {table}").format(
        id=identifier, col=target, table=catalog.qualified
    )
    clause: Composable = SQL("{col} is not null").format(col=target)
    if where is not None:
        clause = SQL("{first} and {second}").format(first=clause, second=where)
    # The direction is one of two fixed fragments, never a caller string.
    direction = SQL("asc") if ascending else SQL("desc")
    statement = SQL("{statement} order by value {direction} limit %s").format(
        statement=_where(statement, clause), direction=direction
    )
    return guard_statement(statement.as_string()), params + [_limit(limit, 10, MAX_ROWS)]


def build_spatial_relationship_count(
    left: TableCatalog,
    right: TableCatalog,
    predicate: str = "intersects",
) -> tuple[str, list[Any]]:
    """Count features of ``left`` that relate to any feature of ``right``.

    Both geometries are brought into the left table's SRID explicitly. Comparing
    geometries in different SRIDs is either an error or - worse, on some setups -
    a silently meaningless comparison of unrelated coordinates.
    """
    function = PREDICATES.get(str(predicate or "").lower())
    if function is None:
        raise ReadOnlyViolation("unsupported spatial predicate")
    if left.connection_id and right.connection_id and left.connection_id != right.connection_id:
        raise ReadOnlyViolation("cross-connection spatial joins are not supported")
    statement = SQL(
        "select count(*) as feature_count from {left_table} as l where exists ("
        "select 1 from {right_table} as r where {fn}(l.{left_geom}, ST_Transform(r.{right_geom}, %s)))"
    ).format(
        left_table=left.qualified,
        right_table=right.qualified,
        fn=SQL(function),
        left_geom=left.geometry(),
        right_geom=right.geometry(),
    )
    return guard_statement(statement.as_string()), [int(left.srid or 4326)]


def build_spatial_join_counts(
    polygons: TableCatalog,
    points: TableCatalog,
    group_column: str,
    limit: int = 50,
) -> tuple[str, list[Any]]:
    """Count features of one table inside each feature of another.

    This is "buildings per district" - the single most requested spatial
    aggregation - executed where the spatial index lives instead of pulling
    every geometry into the client.
    """
    group = polygons.column(group_column)
    statement = SQL(
        "select {group}::text as group_value, count(r.*) as value, 1 as feature_count "
        "from {poly} as l left join {pts} as r "
        "on ST_Intersects(l.{poly_geom}, ST_Transform(r.{pt_geom}, %s)) "
        "group by 1 order by value desc, group_value asc limit %s"
    ).format(
        group=SQL("l.{column}").format(column=group),
        poly=polygons.qualified,
        pts=points.qualified,
        poly_geom=polygons.geometry(),
        pt_geom=points.geometry(),
    )
    return guard_statement(statement.as_string()), [int(polygons.srid or 4326), _limit(limit, 50, MAX_GROUPS)]


def build_nearest(
    catalog: TableCatalog,
    id_column: str,
    x: float,
    y: float,
    srid: int = 4326,
    limit: int = 10,
) -> tuple[str, list[Any]]:
    """K-nearest features using the KNN operator so the index is actually used.

    Distance is reported in metres via the geography cast rather than in raw
    CRS units, because "the nearest 10 features are 0.004 away" is not an answer
    anybody can act on.
    """
    identifier = catalog.column(id_column)
    geom = catalog.geometry()
    statement = SQL(
        "select {id} as feature_id, "
        "ST_Distance({geom}::geography, ST_SetSRID(ST_MakePoint(%s, %s), %s)::geography)::float8 as distance_m "
        "from {table} where {geom} is not null "
        "order by {geom} <-> ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), %s), ST_SRID({geom})) limit %s"
    ).format(id=identifier, geom=geom, table=catalog.qualified)
    point = [float(x), float(y), int(srid or 4326)]
    return guard_statement(statement.as_string()), point + point + [_limit(limit, 10, MAX_ROWS)]


def build_bbox_count(catalog: TableCatalog, bbox: Sequence[float], srid: int = 4326) -> tuple[str, list[Any]]:
    """Count features in the current viewport."""
    where, params = _bbox_filter(catalog, bbox, srid)
    if where is None:
        raise ReadOnlyViolation("invalid bounding box")
    statement = SQL("select count(*) as feature_count from {table} where {where}").format(
        table=catalog.qualified, where=where
    )
    return guard_statement(statement.as_string()), params


def session_setup(timeout_ms: int = DEFAULT_TIMEOUT_MS) -> list[str]:
    """Session statements that make the connection read-only and bounded.

    These are the only non-SELECT statements this module produces. They are
    returned separately so they can never be concatenated into a data query, and
    they are what makes the read-only promise enforced by the server rather than
    only by our own code.
    """
    timeout = max(1000, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS))
    return [
        "BEGIN READ ONLY",
        "SET LOCAL statement_timeout = {}".format(timeout),
        "SET LOCAL idle_in_transaction_session_timeout = {}".format(timeout),
        # A read-only transaction still allows a function to open its own write
        # path on some extensions; default_transaction_read_only closes that.
        "SET LOCAL default_transaction_read_only = on",
    ]


# The two columns read_only_query puts in front of every row.
READ_ONLY_PROBE_COLUMNS = 2


def read_only_query(statement: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """One statement batch that runs `statement` read-only and says whether it was.

    A host that sends setup and query as separate calls cannot promise they
    share a session: QGIS pools libpq connections, so `BEGIN READ ONLY`, the
    SELECT and `COMMIT` could each land on a different one, and a failure after
    `BEGIN` left a transaction open on a pooled connection. PostgreSQL runs the
    statements of one simple-query call as ONE implicit transaction that ends
    with the batch, so setting the transaction read-only inside the same batch
    covers exactly this query and leaves nothing open.

    Every returned row carries the server's own answer, read inside that
    transaction: `current_setting('transaction_read_only')` first, then a
    marker that is NULL on the single row an empty result still produces (the
    query is LEFT JOINed to a one-row probe, so there is always a row to read
    the setting from). Read the result with `unwrap_read_only_rows`.

    `statement` is guarded again here; the wrapper only adds fixed text.
    """
    guarded = guard_statement(statement)
    timeout = max(1000, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS))
    # B608 is a string-built query by construction here, and deliberately so:
    # `guarded` has just passed guard_statement (one read-only SELECT), the
    # timeout is a clamped int, and everything else is fixed text.
    return (
        "SET TRANSACTION READ ONLY; "  # nosec B608
        "SET LOCAL statement_timeout = {timeout}; "
        "SET LOCAL idle_in_transaction_session_timeout = {timeout}; "
        "SELECT current_setting('transaction_read_only') AS nivo_read_only, nivo_result.* "
        "FROM (SELECT 1) AS nivo_probe "
        "LEFT JOIN LATERAL (SELECT true AS nivo_row, nivo_rows.* FROM ({statement}) AS nivo_rows) "
        "AS nivo_result ON true"
    ).format(timeout=timeout, statement=guarded)


def unwrap_read_only_rows(rows: Sequence[Sequence[Any]] | None) -> tuple[list[list[Any]], bool]:
    """The query's own rows, and whether the server ran them read-only.

    Refuses rather than guesses when the result does not have the wrapper's
    shape. A connection that split the batch and returned another statement's
    result would otherwise read as an empty answer, or as a read-only one, and
    neither would be true.
    """
    rows = [list(row) for row in (rows or [])]
    if not rows or any(len(row) < READ_ONLY_PROBE_COLUMNS for row in rows):
        raise ReadOnlyViolation("the connection did not return the read-only probe with the result")
    settings = {str(row[0]).strip().lower() for row in rows}
    if not settings <= {"on", "off"}:
        raise ReadOnlyViolation("the connection did not report the transaction's read-only setting")
    data = [row[READ_ONLY_PROBE_COLUMNS:] for row in rows if row[1] is not None]
    return data, settings == {"on"}


def bind_numeric_parameters(sql: str, params: Sequence[Any]) -> str:
    """Substitute the `%s` placeholders, accepting numbers and nothing else.

    QGIS's own database connection API executes a statement string and takes no
    parameter list, so a statement built here has to arrive complete. That is
    the one place where the composed-never-concatenated discipline could be
    lost, so the binder is deliberately the narrowest thing that works.

    Every parameter these builders produce is a number: an SRID from `int(...)`,
    a row cap from :func:`_limit`, and bounding-box or point coordinates as
    floats. Nothing user-authored and nothing textual ever reaches here, so the
    binder refuses anything that is not a finite int or float rather than
    trying to escape it. A string parameter is not a case to handle carefully,
    it is a sign that a builder changed and this function must be revisited.

    Booleans are refused explicitly: `bool` is a subclass of `int` in Python, so
    accepting it would silently render `True` as `1` and hide a builder bug.
    """
    values = list(params or [])
    rendered: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReadOnlyViolation("only numeric query parameters may be bound")
        if not math.isfinite(value):
            raise ReadOnlyViolation("a non-finite number cannot be bound")
        rendered.append(repr(int(value)) if isinstance(value, int) else repr(float(value)))
    parts = sql.split("%s")
    if len(parts) - 1 != len(rendered):
        raise ReadOnlyViolation(
            "statement expects {} parameters and {} were supplied".format(len(parts) - 1, len(rendered))
        )
    bound = parts[0]
    for literal, tail in zip(rendered, parts[1:]):
        bound += literal + tail
    # Re-guarded because the bound text, not the template, is what will run.
    return guard_statement(bound)


def describe_refusal(request: str) -> str:
    """The honest answer when someone asks Nivo to change PostGIS data."""
    return (
        "I can read and analyse PostGIS data, but I can't modify it. Nivo's database access is "
        "read-only by design, so inserts, updates, deletes and schema changes aren't available "
        "from here. Run that change in QGIS or your database client, and I'll analyse the result."
    )


def catalog_from_columns(
    schema: str,
    table: str,
    columns: Iterable[Mapping[str, Any]],
    geometry_column: str = "",
    srid: int = 0,
    connection_id: str = "",
) -> TableCatalog:
    """Build a catalogue from a discovery result, validating every identifier.

    An identifier that cannot be quoted is dropped rather than carried forward,
    so a table with one exotic column is still analysable on its other columns.
    """
    safe: dict[str, str] = {}
    for column in columns:
        name = str(column.get("name") or "").strip()
        try:
            quote_identifier(name)
        except ReadOnlyViolation:
            continue
        safe[name] = str(column.get("type") or "")
    quote_identifier(schema)
    quote_identifier(table)
    if geometry_column and geometry_column not in safe:
        geometry_column = ""
    return TableCatalog(schema, table, safe, geometry_column, srid, connection_id)
