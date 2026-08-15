"""Qt object-ownership guards for the panel.

QGIS 4 exited with a Windows heap corruption (0xc0000374) carrying no Python
frame, which is the signature of a C++ object lifetime problem rather than a
Python exception. panel.py already documents this class: a responsive pair
mutates its QBoxLayout direction and alignment on every resize, and handing that
layout to a QFormLayout row means mutating a layout the form owns.

These are source-level guards. They cannot prove the absence of a crash - only a
run in real QGIS does that - but they do stop the construct that matches the
documented hazard from being reintroduced.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = (ROOT / "mapdex_qgis" / "panel.py").read_text(encoding="utf-8")


def test_no_responsive_pair_is_nested_inside_a_form_row():
    # addRow(label, register_pair(...)) hands a resize-mutated layout to a
    # QFormLayout, which owns and re-lays-out its rows itself.
    offenders = re.findall(r"addRow\([^)]*register_pair", PANEL)
    assert not offenders, "a responsive pair was nested in a form row: {}".format(offenders)


def test_responsive_pairs_are_only_added_as_top_level_layouts():
    # The proven pattern: addLayout(root.register_pair(...)) into a QVBoxLayout.
    # The definition itself is skipped; only call sites are checked.
    for line in PANEL.splitlines():
        if "register_pair(" not in line or line.strip().startswith("def "):
            continue
        assert "addLayout(" in line, (
            "register_pair used outside the addLayout pattern: {}".format(line.strip())
        )


def test_each_paired_widget_is_added_to_its_row_exactly_once():
    body = PANEL[PANEL.index("def register_pair"):PANEL.index("def sizeHint")]
    # Re-parenting widgets between cells at runtime is what deleted a QComboBox
    # under PyQt6; each widget must be added once and only once.
    assert body.count("row.addWidget(") == 2
    # The panel must never move a widget out of a layout after construction.
    # Matched as a CALL, so the docstring that documents the hazard does not
    # trip the guard.
    assert ".removeWidget(" not in PANEL


def test_the_api_key_row_uses_a_plain_container():
    assert "api_key_row = QWidget()" in PANEL
    assert 'assistant_form.addRow("API key", api_key_row)' in PANEL
