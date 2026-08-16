"""The field calculator's grammar is a security boundary, not a convenience.

`eval()` on a model-supplied string, or a general expression engine, gives
whatever produced that string the run of the process. So the tests that matter
most here are the refusals.
"""
import pytest

from mapdex_qgis.expressions import (
    ExpressionError,
    compile_expression,
    evaluate,
    referenced_fields,
)

FIELDS = ["area_m2", "population", "land_use", "name"]


def calc(source, row):
    return evaluate(compile_expression(source, FIELDS), row)


def test_arithmetic_over_fields():
    row = {"area_m2": 1000.0, "population": 50.0}
    assert calc("area_m2 / 10000", row) == pytest.approx(0.1)
    assert calc("population * 2 + 1", row) == pytest.approx(101.0)
    assert calc("(population + 50) * 2", row) == pytest.approx(200.0)
    assert calc("-population", row) == pytest.approx(-50.0)


def test_operator_precedence_is_not_left_to_right():
    assert calc("1 + 2 * 3", {}) == pytest.approx(7.0)
    assert calc("(1 + 2) * 3", {}) == pytest.approx(9.0)


def test_the_closed_function_set_works():
    row = {"area_m2": 1234.567, "name": "Parcel A", "land_use": None}
    assert calc("round(area_m2, 1)", row) == pytest.approx(1234.6)
    assert calc("upper(name)", row) == "PARCEL A"
    assert calc("length(name)", row) == pytest.approx(8.0)
    assert calc("concat(name, ' x')", row) == "Parcel A x"
    assert calc("coalesce(land_use, 'unknown')", row) == "unknown"
    assert calc("max(1, 5, 3)", row) == pytest.approx(5.0)


# The decisive refusal. A name that is not a field of this layer cannot become
# one, which is what stops an expression reaching anything outside the row.
def test_an_unknown_name_is_refused():
    for source in ["__import__", "os", "secret_column", "self"]:
        with pytest.raises(ExpressionError) as error:
            compile_expression(source, FIELDS)
        assert "not a field" in str(error.value)


def test_python_syntax_is_not_a_backdoor():
    for source in [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "area_m2.__class__",
        "area_m2; population",
        "[x for x in range(10)]",
        "lambda: 1",
        "area_m2 if 1 else 2",
    ]:
        with pytest.raises(ExpressionError):
            compile_expression(source, FIELDS)


def test_an_unknown_function_is_refused_rather_than_ignored():
    with pytest.raises(ExpressionError) as error:
        compile_expression("system('ls')", FIELDS)
    assert "not a function" in str(error.value)


# A calculator that silently drops the part it did not understand writes a wrong
# number into someone's data.
def test_trailing_input_is_an_error_not_a_truncation():
    with pytest.raises(ExpressionError):
        compile_expression("area_m2 100", FIELDS)
    with pytest.raises(ExpressionError):
        compile_expression("area_m2 +", FIELDS)
    with pytest.raises(ExpressionError):
        compile_expression("(area_m2", FIELDS)


def test_wrong_arity_is_refused():
    with pytest.raises(ExpressionError):
        compile_expression("round()", FIELDS)
    with pytest.raises(ExpressionError):
        compile_expression("abs(1, 2)", FIELDS)


# One bad row must not abandon a calculation over the whole layer, and a null is
# visibly missing where a zero would be silently wrong.
def test_division_by_zero_is_null_rather_than_an_exception():
    assert calc("area_m2 / population", {"area_m2": 10.0, "population": 0.0}) is None


def test_arithmetic_on_null_is_refused_at_the_row():
    with pytest.raises(ExpressionError):
        calc("area_m2 * 2", {"area_m2": None})


def test_comparison_yields_one_or_zero():
    row = {"area_m2": 5000.0, "land_use": "residential"}
    assert calc("area_m2 > 1000", row) == pytest.approx(1.0)
    assert calc("area_m2 < 1000", row) == pytest.approx(0.0)
    assert calc("land_use = 'residential'", row) == pytest.approx(1.0)
    assert calc("land_use <> 'residential'", row) == pytest.approx(0.0)


def test_comparison_with_null_is_null():
    assert calc("land_use = 'x'", {"land_use": None}) is None


def test_an_expression_that_parses_but_is_pathological_is_refused():
    with pytest.raises(ExpressionError):
        compile_expression("1" + " + 1" * 300, FIELDS)
    with pytest.raises(ExpressionError):
        compile_expression("a" * 600, FIELDS)


def test_an_empty_expression_is_refused():
    for source in ["", "   ", None]:
        with pytest.raises(ExpressionError):
            compile_expression(source, FIELDS)


def test_an_unclosed_string_is_refused():
    with pytest.raises(ExpressionError):
        compile_expression("concat(name, 'oops)", FIELDS)


# A caller checks the referenced fields before running over a layer, so a
# missing column is a refusal rather than a column of nulls.
def test_referenced_fields_are_reported():
    node = compile_expression("round(area_m2 / population, 2)", FIELDS)
    assert referenced_fields(node) == {"area_m2", "population"}
    assert referenced_fields(compile_expression("1 + 2", FIELDS)) == set()


def test_field_names_are_matched_case_insensitively_but_reported_as_declared():
    node = compile_expression("AREA_M2 * 2", FIELDS)
    assert referenced_fields(node) == {"area_m2"}
