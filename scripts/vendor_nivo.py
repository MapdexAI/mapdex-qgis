"""Refresh mapdex_qgis/_vendor/nivo from a checkout of the nivo-gis package.

`nivo-gis` is the generic GIS-agent core - MIT, no dependencies, deliberately
owned by nobody in particular. The plugin cannot depend on it as a package:
QGIS ships no usable `pip`, the package is not on PyPI, and a plugin runs inside
an interpreter it does not own. So the source travels in the zip, imported with
explicit relative imports rather than a `sys.path` insert.

This is the only way that copy is allowed to change. Editing the vendored tree
by hand makes the fix invisible to every other client of the package, which is
the whole failure vendoring invites; `tests/test_vendored_nivo.py` fails the
moment the two differ.

    python3 scripts/vendor_nivo.py                      # ../../../nivo-gis
    python3 scripts/vendor_nivo.py --source ~/src/nivo-gis
    python3 scripts/vendor_nivo.py --check              # fail if it would change

A DIRTY CHECKOUT IS REFUSED. `NIVO_VERSION` records a commit sha, and a sha
that does not describe the bytes beside it is worse than no provenance at all:
it reads as a claim anybody can check and cannot.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = PLUGIN_ROOT / "mapdex_qgis" / "_vendor"
DEST = VENDOR / "nivo"
MANIFEST = VENDOR / "NIVO_VERSION"
LICENSE = VENDOR / "NIVO-LICENSE"
# <somewhere>/mapdex/mapdex/apps/qgis-plugin next to <somewhere>/nivo-gis.
# Guarded, because a published standalone tree can sit shallower than that and
# an IndexError here would read as a broken script rather than "pass --source".
DEFAULT_SOURCE = (PLUGIN_ROOT.parents[3] / "nivo-gis" if len(PLUGIN_ROOT.parents) > 3
                  else PLUGIN_ROOT / "no-such-checkout")

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

HEADER = """\
# The vendored copy of nivo-gis under mapdex_qgis/_vendor/nivo.
#
# nivo-gis is not on PyPI and QGIS ships no usable pip, so the plugin cannot
# depend on it as a package: it carries the source and imports it with explicit
# relative imports. tests/test_vendored_nivo.py fails when this tree diverges
# from the checkout named below, and skips when that checkout is absent - a
# standalone plugin repository has no monorepo sibling to compare against.
#
# To update: re-run scripts/vendor_nivo.py against a clean nivo-gis checkout.

"""


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _package_version(repo: pathlib.Path) -> str:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return "?"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "?"


def _manifest(repo: pathlib.Path) -> str:
    return HEADER + "\n".join((
        "source = https://github.com/MapdexAI/nivo-gis",
        "version = " + _package_version(repo),
        "commit = " + _git(repo, "rev-parse", "HEAD"),
        "committed = " + _git(repo, "log", "-1", "--format=%cI"),
        "subject = " + _git(repo, "log", "-1", "--format=%s"),
        "worktree = clean",
    )) + "\n"


def _fingerprint(root: pathlib.Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def main(argv) -> int:
    check = "--check" in argv
    repo = pathlib.Path(argv[argv.index("--source") + 1]).expanduser() if "--source" in argv else DEFAULT_SOURCE
    package = repo / "nivo"

    if not package.is_dir():
        print("error: no nivo package at {}; pass --source".format(package), file=sys.stderr)
        return 2
    if not (repo / ".git").exists():
        print("error: {} is not a git checkout, so no commit can be recorded".format(repo), file=sys.stderr)
        return 2
    if _git(repo, "status", "--porcelain"):
        print("error: {} has uncommitted changes. Commit them first - a recorded sha that does "
              "not describe these bytes is provenance nobody can check.".format(repo), file=sys.stderr)
        return 2

    manifest = _manifest(repo)
    same = _fingerprint(package) == _fingerprint(DEST) if DEST.is_dir() else False
    same = same and MANIFEST.is_file() and MANIFEST.read_text(encoding="utf-8") == manifest
    same = same and LICENSE.is_file() and LICENSE.read_bytes() == (repo / "LICENSE").read_bytes()

    if check:
        if same:
            print("mapdex_qgis/_vendor/nivo is current with {}".format(repo))
            return 0
        print("error: mapdex_qgis/_vendor/nivo differs from {}; run scripts/vendor_nivo.py".format(repo),
              file=sys.stderr)
        return 1

    if DEST.is_dir():
        shutil.rmtree(DEST)
    VENDOR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package, DEST, ignore=IGNORE)
    MANIFEST.write_text(manifest, encoding="utf-8")
    # MIT requires the licence text to travel with the source. It sits beside
    # the package rather than inside it, so a copy of `nivo/` alone would ship
    # the code and drop the terms. It lives outside DEST deliberately: the drift
    # check compares `_vendor/nivo` to the package tree, and a file the package
    # does not have there would read as local drift.
    shutil.copyfile(repo / "LICENSE", LICENSE)

    files = sorted(_fingerprint(DEST))
    print("vendored {} files from {} @ {}".format(len(files), repo, _git(repo, "rev-parse", "--short", "HEAD")))
    # The data files are the ones a naive `*.py` copy drops, and their absence
    # only shows up as an ImportError inside QGIS on somebody else's machine.
    missing = [name for name in ("prompts/nivo.system.md", "py.typed") if name not in files]
    for name in ("prompts/nivo.system.md", "py.typed"):
        print("  {} {}".format("MISSING" if name in missing else "ok     ", name))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
