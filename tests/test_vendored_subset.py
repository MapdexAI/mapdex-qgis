"""The vendored contracts excerpt must cover everything the plugin imports.

`mapdex_qgis/generated_contracts.py` is a generated *subset* of the canonical
Python bindings, so it can be missing a symbol in a way the full copy never
could. The failure would otherwise be an ImportError at QGIS startup, in a
published release, on a user's machine.

This is the guard that makes the subset safe to keep small: it reads every
`from ... generated_contracts import ...` in the tree and proves the name is
defined in the excerpt. It parses rather than imports, so it runs in the public
repository with no QGIS and no monorepo present.

Adding a symbol is one entry in packages/contracts/codegen/qgis_subset.go
followed by `make gen`.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDORED = ROOT / "mapdex_qgis" / "generated_contracts.py"

SKIP_DIRS = {"__pycache__", ".pytest_cache", "dist", ".git"}


def _python_sources():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path == VENDORED:
            continue
        yield path


def _imported_names():
    """Every name imported from generated_contracts, with its importing file."""
    wanted = {}
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            # `.generated_contracts` and `mapdex_qgis.generated_contracts`.
            module = node.module or ""
            if module.split(".")[-1] != "generated_contracts":
                continue
            for alias in node.names:
                wanted.setdefault(alias.name, set()).add(
                    path.relative_to(ROOT).as_posix()
                )
    return wanted


def _defined_names():
    tree = ast.parse(VENDORED.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_every_imported_contract_symbol_is_in_the_excerpt():
    wanted = _imported_names()
    assert wanted, (
        "no import of generated_contracts found; if the plugin genuinely stopped "
        "using the contracts, drop the vendored file rather than leaving this test "
        "passing vacuously"
    )

    defined = _defined_names()
    missing = {
        name: sorted(files) for name, files in wanted.items() if name not in defined
    }
    assert not missing, (
        "the vendored excerpt is missing symbols the plugin imports: "
        + "; ".join(f"{name} (used by {', '.join(files)})" for name, files in sorted(missing.items()))
        + ". Add them to qgis_subset.go in the monorepo and run `make gen`."
    )


def test_a_star_import_would_defeat_the_guard():
    """`import *` hides which symbols are needed, so the excerpt cannot be checked."""
    offenders = []
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if (node.module or "").split(".")[-1] != "generated_contracts":
                continue
            if any(alias.name == "*" for alias in node.names):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        "import * from generated_contracts in "
        + ", ".join(sorted(offenders))
        + "; name the symbols so the excerpt can be proven complete"
    )
