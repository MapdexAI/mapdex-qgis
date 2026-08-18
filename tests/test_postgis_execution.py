"""Executing a built PostGIS statement against a QGIS saved connection.

`postgis.py` composed the SQL and nothing ran it. The server cannot fill that
gap: `connections.py` deliberately sends an id and a user-authored label and has
no field that could carry a host, a user or a password, so a QGIS-local database
is unreachable from Mapdex however good the server's own implementation is.

The risk this file covers is the one place the composed-never-concatenated
discipline could be lost. QGIS's connection API executes a statement string and
takes no parameter list, so the numeric parameters have to be bound into the
text. The binder refuses anything that is not a finite number, and the bound
text is guarded again before it runs.
"""
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mapdex_qgis import postgis  # noqa: E402
from mapdex_qgis.postgis import ReadOnlyViolation, bind_numeric_parameters  # noqa: E402


# --------------------------------------------------------------------------
# The binder is the security boundary
# --------------------------------------------------------------------------

def test_numbers_are_bound_and_the_statement_stays_a_select():
    bound = bind_numeric_parameters("select * from t where a && ST_MakeEnvelope(%s, %s, %s, %s, %s)",
                                    [1.5, 2.0, 3.0, 4.0, 4326])
    assert "ST_MakeEnvelope(1.5, 2.0, 3.0, 4.0, 4326)" in bound
    assert "%s" not in bound


def test_a_string_parameter_is_refused_rather_than_escaped():
    # Not a case to handle carefully. Every builder here produces numbers, so a
    # string means a builder changed and this path must be revisited.
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("select * from t limit %s", ["10; drop table users"])


def test_a_boolean_is_refused_even_though_python_calls_it_an_int():
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("select * from t limit %s", [True])


def test_a_non_finite_number_is_refused():
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("select * from t limit %s", [float("inf")])


def test_a_parameter_count_mismatch_is_refused_in_both_directions():
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("select * from t limit %s", [])
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("select * from t", [10])


def test_the_bound_text_is_guarded_again_not_only_the_template():
    # The template is what the builders guard. The bound text is what runs, so a
    # template that somehow passed while its bound form does not must not run.
    with pytest.raises(ReadOnlyViolation):
        bind_numeric_parameters("delete from t where id = %s", [1])


# --------------------------------------------------------------------------
# The execution layer
# --------------------------------------------------------------------------

class FakeField:
    def __init__(self, name, type_name):
        self._name, self._type = name, type_name

    def name(self):
        return self._name

    def typeName(self):
        return self._type


class FakeCrs:
    def isValid(self):
        return True

    def authid(self):
        return "EPSG:27700"


class FakeTable:
    def __init__(self, name, geometry_column):
        self._name, self._geometry = name, geometry_column

    def tableName(self):
        return self._name

    def geometryColumn(self):
        return self._geometry

    def crsList(self):
        return [FakeCrs()]


class FakeConnection:
    def __init__(self, rows=None, fail_setup=False, fail_query=False):
        self.statements = []
        self._rows = rows if rows is not None else [[42]]
        self._fail_setup = fail_setup
        self._fail_query = fail_query

    def fields(self, _schema, _table):
        return [FakeField("id", "int4"), FakeField("name", "text"),
                FakeField("area", "float8"), FakeField("geom", "geometry")]

    def tables(self, _schema):
        return [FakeTable("parcels", "geom")]

    def executeSql(self, sql):
        self.statements.append(sql)
        if self._fail_setup and sql.startswith(("BEGIN", "SET LOCAL")):
            raise RuntimeError("no session")
        if self._fail_query and sql.lower().lstrip().startswith(("select", "with")):
            raise RuntimeError("relation does not exist")
        return list(self._rows)


def _runtime(connection, name="warehouse"):
    from mapdex_qgis.qgis_runtime import QGISRuntime

    metadata = types.SimpleNamespace(connections=lambda _save: {name: connection})
    registry = types.SimpleNamespace(providerMetadata=lambda _p: metadata)
    core = sys.modules.setdefault("qgis.core", types.ModuleType("qgis.core"))
    core.QgsProviderRegistry = types.SimpleNamespace(instance=lambda: registry)
    qgis = sys.modules.setdefault("qgis", types.ModuleType("qgis"))
    qgis.core = core
    return QGISRuntime(types.SimpleNamespace(), types.SimpleNamespace())


def test_a_profile_reports_the_discovered_shape_and_the_statement_it_ran():
    connection = FakeConnection(rows=[[120, 27700]])
    result = _runtime(connection).postgis_profile("warehouse", "public", "parcels")
    assert result["geometry_column"] == "geom"
    assert result["srid"] == 27700
    assert set(result["columns"]) == {"id", "name", "area", "geom"}
    # The statement travels back because a claim nobody can inspect is not a claim.
    assert result["sql"].lower().startswith("select")
    assert result["enforced_read_only"] is True


def test_the_read_only_transaction_is_opened_before_the_query():
    connection = FakeConnection()
    _runtime(connection).postgis_profile("warehouse", "public", "parcels")
    assert connection.statements[0] == "BEGIN READ ONLY"
    assert any("statement_timeout" in statement for statement in connection.statements)
    assert connection.statements[-1] == "COMMIT"


def test_a_session_that_cannot_be_made_read_only_says_so_instead_of_pretending():
    # QGIS pools its own libpq connections and does not promise two executeSql
    # calls share a session. The construction-side layers still hold; reporting
    # which guarantees were in force is the difference from a hope.
    connection = FakeConnection(fail_setup=True)
    result = _runtime(connection).postgis_profile("warehouse", "public", "parcels")
    assert result["enforced_read_only"] is False
    assert result["rows"]


def test_an_unknown_connection_names_what_was_asked_for():
    from mapdex_qgis.qgis_runtime import RuntimeUnavailable

    with pytest.raises(RuntimeUnavailable) as error:
        _runtime(FakeConnection()).postgis_profile("not-saved", "public", "parcels")
    assert "not-saved" in str(error.value)


def test_an_unknown_column_is_refused_naming_the_table():
    from mapdex_qgis.qgis_runtime import RuntimeUnavailable

    with pytest.raises(RuntimeUnavailable) as error:
        _runtime(FakeConnection()).postgis_analyze(
            "warehouse", "public", "parcels", "numeric", field="population"
        )
    assert "public" in str(error.value) and "parcels" in str(error.value)


def test_an_unregistered_operation_is_refused():
    from mapdex_qgis.capabilities import CapabilityError

    with pytest.raises(CapabilityError):
        _runtime(FakeConnection()).postgis_analyze("warehouse", "public", "parcels", "train_a_model")


def test_a_failing_query_reports_the_connection_rather_than_a_traceback():
    from mapdex_qgis.qgis_runtime import RuntimeUnavailable

    with pytest.raises(RuntimeUnavailable) as error:
        _runtime(FakeConnection(fail_query=True)).postgis_profile("warehouse", "public", "parcels")
    assert "warehouse" in str(error.value)


def test_the_analysis_builders_are_reachable_for_every_declared_operation():
    # The manifest declares six operations. Each must resolve to a builder, or
    # the capability advertises a question it cannot ask.
    connection = FakeConnection()
    runtime = _runtime(connection)
    for operation, kwargs in (
        ("numeric", {"field": "area"}),
        ("categories", {"field": "name"}),
        ("group", {"group_field": "name", "field": "area", "statistic": "sum"}),
        ("top_n", {"field": "area", "id_field": "id"}),
        ("bbox_count", {"bbox": [0, 0, 1, 1]}),
        ("nearest", {"id_field": "id", "x": 0.5, "y": 0.5}),
    ):
        result = runtime.postgis_analyze("warehouse", "public", "parcels", operation, **kwargs)
        assert result["operation"] == operation
        assert result["sql"].lower().startswith(("select", "with"))


def test_no_declared_parameter_can_carry_sql():
    """The property the whole design rests on, asserted rather than described."""
    from mapdex_qgis.capabilities import get as get_capability

    for capability_id in ("postgis.profile@1", "postgis.analyze@1", "postgis.spatial@1"):
        capability = get_capability(capability_id)
        for name, spec in capability.params.items():
            assert "sql" not in name.lower(), "{} declares a parameter named {}".format(capability_id, name)
            assert "query" not in name.lower(), "{} declares a parameter named {}".format(capability_id, name)
            # Every declared type is a scalar the registry validates. SQL would
            # have to arrive as text, and every text field here is an identifier
            # checked against the catalogue read from the live connection.
            assert spec.get("type") in {"string", "integer", "number", "boolean", "list", "bbox"}
