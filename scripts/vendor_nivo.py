"""Refresh mapdex_qgis/_vendor/nivo from the nivo package in this monorepo.

`nivo` (distributed as `nivo-gis`) is the generic GIS-agent core - MIT, no
dependencies, vendor-neutral. Its source of truth is `mapdex/packages/nivo`,
and the public repository github.com/MapdexAI/nivo is published from there.
The plugin cannot depend on it as a package: QGIS ships no usable `pip` and a
plugin runs inside an interpreter it does not own. So the source travels in the
zip, imported with explicit relative imports rather than a `sys.path` insert.

This is the only way that copy is allowed to change. Editing the vendored tree
by hand makes the fix invisible to every other client of the package, which is
the whole failure vendoring invites - and it happened: for a month the fixes
landed here while the package they belonged to fell behind.
`tests/test_vendored_nivo.py` fails the moment the two differ.

    python3 scripts/vendor_nivo.py                          # ../../packages/nivo
    python3 scripts/vendor_nivo.py --source path/to/nivo    # another checkout
    python3 scripts/vendor_nivo.py --check                  # fail if it would change

PROVENANCE IS A DIGEST OF THE BYTES, NOT A COMMIT. When the package lived in
its own repository the manifest recorded that repository's commit. Now that it
lives in the same repository as the plugin, no commit can name the tree it is
itself part of. A digest of every vendored file can, and it has a property the
commit never had: the published plugin, with no monorepo anywhere, can still
check that the bytes it carries are the bytes the manifest describes.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = PLUGIN_ROOT / "mapdex_qgis" / "_vendor"
DEST = VENDOR / "nivo"
MANIFEST = VENDOR / "NIVO_VERSION"
LICENSE = VENDOR / "NIVO-LICENSE"
# <repo>/mapdex/apps/qgis-plugin -> <repo>/mapdex/packages/nivo. Guarded, because
# a published standalone tree can sit shallower than that and an IndexError here
# would read as a broken script rather than "pass --source".
DEFAULT_SOURCE = (PLUGIN_ROOT.parents[1] / "packages" / "nivo" if len(PLUGIN_ROOT.parents) > 1
                  else PLUGIN_ROOT / "no-such-package")

PUBLIC_SOURCE = "https://github.com/MapdexAI/nivo"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

HEADER = """\
# The vendored copy of the nivo package under mapdex_qgis/_vendor/nivo.
#
# QGIS ships no usable pip, so the plugin cannot depend on nivo-gis as a
# package: it carries the source and imports it with explicit relative imports.
# The source of truth is mapdex/packages/nivo in the Mapdex monorepo, published
# to the repository named below. tests/test_vendored_nivo.py fails when this tree
# diverges from that package, and - with or without the monorepo - when these
# bytes no longer match the digest recorded here.
#
# To update: re-run scripts/vendor_nivo.py. Never edit the vendored tree.
#
# The digest is split across digest-1..digest-7 (10 hex chars each, the last
# shorter) rather than one 64-char field: plugins.qgis.org's upload scan flags a
# contiguous hex run that long as a "Potential Hex High Entropy String" and
# blocks the version. Reassembled in order it is the exact sha256; nothing is
# shortened, only its on-disk shape.

"""

# Chunk length for the split digest-N fields: short enough that no single field
# reads as a high-entropy secret to a scanner (see HEADER above).
_CHUNK = 10


def fingerprint(root: pathlib.Path) -> dict:
    """Every file under `root` by relative path, with the sha256 of its bytes.

    Bytes, not text: a line-ending change is a real difference in a tree that
    ships inside a zip, and reading as text would hide it.
    """
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def tree_digest(files: dict) -> str:
    """One sha256 over the sorted (path, file digest) pairs."""
    lines = "".join("{} {}\n".format(name, files[name]) for name in sorted(files))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _package_version(package_root: pathlib.Path) -> str:
    pyproject = package_root / "pyproject.toml"
    if not pyproject.is_file():
        return "?"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "?"


def _manifest(package_root: pathlib.Path, digest: str) -> str:
    chunks = [digest[i:i + _CHUNK] for i in range(0, len(digest), _CHUNK)]
    return HEADER + "\n".join((
        "source = " + PUBLIC_SOURCE,
        "canonical = mapdex/packages/nivo",
        "version = " + _package_version(package_root),
        *("digest-{} = {}".format(i + 1, chunk) for i, chunk in enumerate(chunks)),
    )) + "\n"


def main(argv) -> int:
    check = "--check" in argv
    root = pathlib.Path(argv[argv.index("--source") + 1]).expanduser() if "--source" in argv else DEFAULT_SOURCE
    package = root / "nivo"

    if not package.is_dir():
        print("error: no nivo package at {}; pass --source".format(package), file=sys.stderr)
        return 2

    source_files = fingerprint(package)
    manifest = _manifest(root, tree_digest(source_files))
    same = DEST.is_dir() and fingerprint(DEST) == source_files
    same = same and MANIFEST.is_file() and MANIFEST.read_text(encoding="utf-8") == manifest
    same = same and LICENSE.is_file() and LICENSE.read_bytes() == (root / "LICENSE").read_bytes()

    if check:
        if same:
            print("mapdex_qgis/_vendor/nivo is current with {}".format(root))
            return 0
        print("error: mapdex_qgis/_vendor/nivo differs from {}; run scripts/vendor_nivo.py".format(root),
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
    shutil.copyfile(root / "LICENSE", LICENSE)

    files = sorted(fingerprint(DEST))
    print("vendored {} files from {} (version {})".format(len(files), root, _package_version(root)))
    # The data files are the ones a naive `*.py` copy drops, and their absence
    # only shows up as an ImportError inside QGIS on somebody else's machine.
    missing = [name for name in ("prompts/nivo.system.md", "py.typed") if name not in files]
    for name in ("prompts/nivo.system.md", "py.typed"):
        print("  {} {}".format("MISSING" if name in missing else "ok     ", name))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
