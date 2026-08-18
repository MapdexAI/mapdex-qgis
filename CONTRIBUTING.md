# Contributing

Thanks for looking. Two things are worth knowing before you spend time here.

## This repository is a mirror

Development happens in the Mapdex monorepo; this repository is published from
it automatically. A commit pushed straight to `main` here would be overwritten
by the next mirror run, so `main` only accepts the mirror.

That does not make contributions unwelcome — it changes the route:

- **Bugs and ideas:** open an issue. This is the tracker the plugin points at,
  and it is read.
- **Code:** open a pull request anyway. It cannot be merged here, but we apply
  it in the monorepo with your authorship preserved and it comes back on the
  next mirror. Say so in the PR and we will link the resulting commit.

## Running the tests

The test suite needs no QGIS install: everything that touches QGIS is either
behind a thin adapter or checked by reading the source.

```bash
python -m pytest tests -q
```

## Building a package

```bash
python scripts/package.py                       # development build
python scripts/package.py --channel production  # what gets published
```

A development build lets you point the plugin at a local Mapdex
(`http://127.0.0.1:8080`). The published build is pinned to the hosted service
and hides those fields.

Install the result with *Plugins → Manage and Install Plugins → Install from
ZIP*, then restart QGIS. Restarting matters: Python keeps the previously
imported modules, so a reinstall alone does not load your changes.

## Things to know before changing code

- **Qt5 and Qt6 both ship.** QGIS 3.34 uses PyQt5, QGIS 4 uses PyQt6. Never
  write `QMessageBox.Yes`; resolve enums through `qt_compat.enum_member`. A
  test scans for the flat form.
- **Signals on `iface` outlive the plugin.** Connect bound methods, never
  lambdas, and disconnect them in `unload()`. A test enforces both.
- **Keep a reference to every `QgsTask`.** A task collected by Python while it
  is still queued crashes QGIS with an access violation.
- **`mapdex_qgis/generated_contracts.py` is generated.** Do not edit it; it is
  produced by the contracts package in the monorepo. It holds only the symbols
  the plugin uses, so a new import from it needs the symbol added upstream
  (`packages/contracts/codegen/qgis_subset.go`, then `make gen`) rather than
  pasted in here. `tests/test_vendored_subset.py` fails if you forget, instead
  of letting the ImportError reach a user's QGIS.
