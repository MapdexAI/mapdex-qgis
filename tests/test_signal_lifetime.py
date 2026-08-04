"""Connections made on the QGIS interface must survive a plugin reload cleanly.

QGIS keeps `iface` alive across plugin reloads, so a connection made there
outlives the plugin instance that made it. If it is a lambda it cannot be
disconnected, and every reload leaves one more callback poking at widgets that
were destroyed with the old dock — QGIS then reports
"wrapped C/C++ object of type QComboBox has been deleted" on every layer click.
"""
import ast
import pathlib

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "plugin.py"
TREE = ast.parse(PLUGIN.read_text(encoding="utf-8"))


def _iface_signal_calls(method_name):
    """Yield (signal, argument node) for iface.<signal>.<method>(...) calls."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != method_name:
            continue
        signal = node.func.value
        if not isinstance(signal, ast.Attribute):
            continue
        owner = signal.value
        if isinstance(owner, ast.Attribute) and owner.attr == "iface":
            yield signal.attr, (node.args[0] if node.args else None)


def test_iface_connections_are_bound_methods_not_lambdas():
    for signal, argument in _iface_signal_calls("connect"):
        assert not isinstance(argument, ast.Lambda), (
            "iface.{} is connected to a lambda, which unload() cannot "
            "disconnect".format(signal)
        )


def test_every_iface_connection_is_disconnected_on_unload():
    connected = {signal for signal, _ in _iface_signal_calls("connect")}
    disconnected = {signal for signal, _ in _iface_signal_calls("disconnect")}
    missing = sorted(connected - disconnected)
    assert not missing, "No disconnect for iface signals: {}".format(missing)


def test_unload_clears_the_widget_handles():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "PANEL_WIDGET_REFS" in source
    unload = next(
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "unload"
    )
    body = ast.dump(unload)
    assert "PANEL_WIDGET_REFS" in body, "unload() must drop the panel widget handles"
