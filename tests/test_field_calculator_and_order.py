"""The field calculator, layer reorder, and the measure tool's state machine.

The runtime methods are driven with doubles. What that proves is the decisions:
that an existing column is never overwritten, that a bad expression is refused
before any row is touched, that preview and apply compute the same values, and
that a half-finished measurement does not survive being put away. What it does
not prove is that any of it binds to a real QGIS canvas, which needs a running
QGIS and belongs to the founder's verification pass.
"""
import sys
import types

import pytest


from mapdex_qgis._vendor.nivo import capabilities
from mapdex_qgis.maptools import MeasureState


@pytest.fixture(autouse=True)
def qgis_stubs():
    """Add the symbols calculate_field needs to whatever qgis stub exists.

    Per-test and augmenting, not once at import and replacing. Another test
    module installs its own stub of qgis.core by assigning over sys.modules, so
    an import-time installer here would have its symbols deleted or delete
    theirs depending on file ordering. That is exactly the failure this suite
    hit: green in isolation, eleven failures in the full run.
    """
    qgis = sys.modules.get("qgis") or types.ModuleType("qgis")
    core = sys.modules.get("qgis.core") or types.ModuleType("qgis.core")

    if not hasattr(core, "QgsField"):
        class QgsField:
            def __init__(self, name, storage=None):
                self.name = name
                self.storage = storage

        core.QgsField = QgsField

    qgis.core = core
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = core

    pyqt = sys.modules.get("qgis.PyQt") or types.ModuleType("qgis.PyQt")
    qtcore = sys.modules.get("qgis.PyQt.QtCore") or types.ModuleType("qgis.PyQt.QtCore")
    if not hasattr(qtcore, "QMetaType"):
        class QMetaType:
            class Type:
                Double = "double"
                Int = "int"
                QString = "string"

        qtcore.QMetaType = QMetaType
    pyqt.QtCore = qtcore
    qgis.PyQt = pyqt
    sys.modules["qgis.PyQt"] = pyqt
    sys.modules["qgis.PyQt.QtCore"] = qtcore
    yield


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_the_new_capabilities_are_declared_and_bound():
    from mapdex_qgis.qgis_runtime import bound_capability_ids

    bound = bound_capability_ids()
    for identifier in ("field.calculate@1", "layer.reorder@1"):
        assert capabilities.get(identifier) is not None, identifier
        assert identifier in bound, "{} is declared but nothing executes it".format(identifier)


# Writing a column into someone's data is not undoable outside QGIS's own edit
# buffer, so it must be gated.
def test_field_calculate_is_consequential_and_gated():
    capability = capabilities.get("field.calculate@1")
    assert capability.risk == capabilities.RISK_CONSEQUENTIAL
    assert capability.requires_confirmation is True
    assert capability.reversible is False


def test_layer_reorder_is_reversible_and_ungated():
    capability = capabilities.get("layer.reorder@1")
    assert capability.risk == capabilities.RISK_SAFE
    assert capability.reversible is True
    assert capability.requires_confirmation is False


def test_reorder_rejects_a_position_outside_the_enum():
    with pytest.raises(capabilities.CapabilityError):
        capabilities.validate_request("layer.reorder@1", {"layer_id": "a", "position": "sideways"})
    validated = capabilities.validate_request("layer.reorder@1", {"layer_id": "a"})
    assert validated["params"]["position"] == "top"


def test_field_calculate_requires_a_name_and_an_expression():
    for params in ({"layer_id": "a", "field": "x"}, {"layer_id": "a", "expression": "1"}):
        with pytest.raises(capabilities.CapabilityError):
            capabilities.validate_request("field.calculate@1", params)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------

class FakeFeature:
    def __init__(self, identifier, values):
        self._id = identifier
        self._values = values

    def id(self):
        return self._id

    def __getitem__(self, key):
        return self._values.get(key)


class FakeProvider:
    def __init__(self, accepts=True):
        self.accepts = accepts
        self.added = []
        self.changes = {}

    def addAttributes(self, fields):  # noqa: N802 - QGIS naming
        if not self.accepts:
            return False
        self.added.extend(fields)
        return True

    def changeAttributeValues(self, changes):  # noqa: N802
        self.changes.update(changes)
        return True


class FakeFields:
    def __init__(self, names):
        self.names = list(names)

    def indexOf(self, name):  # noqa: N802
        lowered = [n.lower() for n in self.names]
        return lowered.index(name.lower()) if name.lower() in lowered else -1


class FakeLayer:
    def __init__(self, names, rows):
        self._names = list(names)
        self._rows = rows
        self.provider = FakeProvider()
        self.repainted = False

    def dataProvider(self):  # noqa: N802
        return self.provider

    def getFeatures(self):  # noqa: N802
        return list(self._rows)

    def fields(self):
        return FakeFields(self._names + [f.name for f in self.provider.added])

    def updateFields(self):  # noqa: N802
        return None

    def triggerRepaint(self):  # noqa: N802
        self.repainted = True


class RuntimeDouble:
    """Only the parts calculate_field touches."""

    def __init__(self, layer):
        self._layer = layer

    def vector(self, layer_id):
        return self._layer

    def field_names(self, layer):
        return list(layer._names)


def run_calculate(layer, **kwargs):
    """Call the real method with a double bound as self."""
    from mapdex_qgis.qgis_runtime import QGISRuntime

    return QGISRuntime.calculate_field(RuntimeDouble(layer), "L1", **kwargs)


def make_layer():
    return FakeLayer(
        ["area_m2", "population"],
        [FakeFeature(1, {"area_m2": 10000.0, "population": 50.0}),
         FakeFeature(2, {"area_m2": 20000.0, "population": 0.0}),
         FakeFeature(3, {"area_m2": None, "population": 5.0})],
    )


# --------------------------------------------------------------------------
# calculate_field
# --------------------------------------------------------------------------

# Silently replacing a column the user already had is the one mistake here that
# destroys data rather than merely adding a wrong number.
def test_an_existing_field_is_never_overwritten():
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    layer = make_layer()
    with pytest.raises(CapabilityError) as error:
        run_calculate(layer, field="population", expression="area_m2 * 2")
    assert "already exists" in str(error.value)
    assert layer.provider.added == []


def test_a_bad_expression_is_refused_before_any_row_is_touched():
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    layer = make_layer()
    with pytest.raises(CapabilityError):
        run_calculate(layer, field="density", expression="__import__('os')")
    assert layer.provider.added == []
    assert layer.provider.changes == {}


def test_a_reference_to_a_missing_field_is_refused():
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    layer = make_layer()
    with pytest.raises(CapabilityError):
        run_calculate(layer, field="x", expression="elevation + 1")


def test_the_calculation_writes_one_value_per_feature():
    layer = make_layer()
    result = run_calculate(layer, field="hectares", expression="area_m2 / 10000")
    assert result["kind"] == "field_calculated"
    assert result["rows"] == 3
    assert len(layer.provider.added) == 1
    index = layer.fields().indexOf("hectares")
    assert layer.provider.changes[1][index] == pytest.approx(1.0)
    assert layer.provider.changes[2][index] == pytest.approx(2.0)


# One unusable row must not abandon the layer, but the count is reported rather
# than hidden: a column that is quietly a third null is worse than a warning.
def test_an_unusable_row_becomes_null_and_is_counted():
    layer = make_layer()
    result = run_calculate(layer, field="hectares", expression="area_m2 / 10000")
    index = layer.fields().indexOf("hectares")
    assert layer.provider.changes[3][index] is None
    assert result["nulls"] == 1
    assert result["problems"]


def test_division_by_zero_is_a_null_not_a_failed_run():
    layer = make_layer()
    result = run_calculate(layer, field="per_person", expression="area_m2 / population")
    index = layer.fields().indexOf("per_person")
    assert layer.provider.changes[2][index] is None
    assert result["rows"] == 3


# A preview the user approves must be the run that lands, or approval means
# nothing.
def test_preview_computes_the_same_values_and_writes_nothing():
    layer = make_layer()
    preview = run_calculate(layer, field="hectares", expression="area_m2 / 10000", preview=True)
    assert preview["kind"] == "field_preview"
    assert layer.provider.added == []
    assert layer.provider.changes == {}

    applied = run_calculate(make_layer(), field="hectares", expression="area_m2 / 10000")
    assert preview["rows"] == applied["rows"]
    assert preview["nulls"] == applied["nulls"]
    assert preview["sample"] == applied["sample"]


def test_an_integer_field_writes_integers():
    layer = make_layer()
    run_calculate(layer, field="rounded", expression="area_m2 / 10000", field_type="integer")
    index = layer.fields().indexOf("rounded")
    assert isinstance(layer.provider.changes[1][index], int)


def test_a_text_field_writes_text():
    layer = make_layer()
    run_calculate(layer, field="label", expression="concat('P-', area_m2)", field_type="text")
    index = layer.fields().indexOf("label")
    assert layer.provider.changes[1][index] == "P-10000"


def test_a_provider_that_refuses_a_new_field_is_an_error_not_a_silent_success():
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    layer = make_layer()
    layer.provider.accepts = False
    with pytest.raises(CapabilityError):
        run_calculate(layer, field="hectares", expression="area_m2 / 10000")


def test_an_empty_field_name_is_refused():
    from mapdex_qgis._vendor.nivo.capabilities import CapabilityError

    layer = make_layer()
    with pytest.raises(CapabilityError):
        run_calculate(layer, field="   ", expression="1")


# --------------------------------------------------------------------------
# The measure tool's state machine
# --------------------------------------------------------------------------

def test_the_first_click_waits_for_a_second():
    state = MeasureState()
    outcome = state.click(10.0, 50.0)
    assert outcome["kind"] == "awaiting_second"
    assert state.waiting_for_second is True


def test_the_second_click_yields_a_measurement_and_resets():
    state = MeasureState()
    state.click(10.0, 50.0)
    outcome = state.click(11.0, 51.0)
    assert outcome == {"kind": "measure", "from": (10.0, 50.0), "to": (11.0, 51.0)}
    assert state.waiting_for_second is False


def test_a_third_click_starts_a_new_measurement():
    state = MeasureState()
    state.click(0.0, 0.0)
    state.click(1.0, 1.0)
    assert state.click(5.0, 5.0)["kind"] == "awaiting_second"


# If the caller raises while handling the result, the next click must start a
# fresh measurement rather than pair with a point from the abandoned one.
def test_the_first_point_is_cleared_before_the_result_is_handed_over():
    state = MeasureState()
    state.click(0.0, 0.0)
    outcome = state.click(1.0, 1.0)
    assert state.first is None
    assert outcome["from"] == (0.0, 0.0)


def test_reset_drops_a_half_finished_measurement():
    state = MeasureState()
    state.click(0.0, 0.0)
    state.reset()
    assert state.waiting_for_second is False
    assert state.click(9.0, 9.0)["kind"] == "awaiting_second"
