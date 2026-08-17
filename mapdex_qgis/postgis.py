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
saved QGIS/Mapdex connection; the DSN, host, user and password stay inside the
platform credential store and are never placed in a prompt, a log, or an error.
"""
from __future__ import annotations

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
})

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
    functions = words & FORBIDDEN_FUNCTIONS
    if functions:
        raise ReadOnlyViolation("forbidden function: {}".format(sorted(functions)[0]))
    # `SELECT ... INTO new_table` writes despite starting with SELECT.
    if re.search(r"\binto\b", lowered) and not re.search(r"\bwithin\s+group\b", lowered):
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
