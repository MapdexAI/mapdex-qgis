"""The plugin's copy of nivo-gis must not drift from the package.

`mapdex_qgis/_vendor/nivo/` is a vendored copy of the MIT `nivo-gis` package.
QGIS ships no usable `pip` and the package is not on PyPI, so the plugin cannot
depend on it: it carries the source and imports it relatively.

Vendoring's one failure mode is a local edit. Somebody fixes a bug here, the
package never learns about it, and the two diverge until "the same question
answered the same way in every client" quietly stops being true. This fails the
moment the copy differs from the checkout named in `_vendor/NIVO_VERSION`, in
either direction, including files added or removed on either side.

Skipped when that checkout is absent - the plugin is published to its own
repository with no monorepo and no sibling package to compare against - exactly
as test_vendored_contracts.py skips.
"""
import hashlib
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = ROOT / "mapdex_qgis" / "_vendor"
VENDORED = VENDOR / "nivo"
MANIFEST = VENDOR / "NIVO_VERSION"

# The package checked out beside the monorepo, which is the layout the
# extraction produced: <somewhere>/mapdex/mapdex/apps/qgis-plugin and
# <somewhere>/nivo-gis. Derived from this file rather than from $HOME so it
# holds on a CI runner, and overridable for a checkout kept elsewhere.
#
# `parents[3]` deliberately, but guarded: the published tree is copied to a
# shallow temporary directory that has no fourth parent, and an IndexError at
# import time would take the whole module down as a collection error rather than
# reaching the skip below. That is not hypothetical - it is what
# scripts/standalone_check.sh caught the first time this file existed.
_ANCESTORS = ROOT.parents
DEFAULT_SOURCE_REPO = _ANCESTORS[3] / "nivo-gis" if len(_ANCESTORS) > 3 else ROOT / "no-such-checkout"
SOURCE_REPO = pathlib.Path(os.environ.get("NIVO_GIS_REPO") or DEFAULT_SOURCE_REPO)
SOURCE = SOURCE_REPO / "nivo"

# Compiled bytecode is a build artifact of whichever interpreter ran last, so it
# is never part of the comparison.
IGNORED_PARTS = {"__pycache__"}


def _fingerprint(root: pathlib.Path) -> dict:
    """Every file under `root` by relative path, with the sha256 of its bytes.

    Bytes, not text: a line-ending change is a real difference in a tree that
    ships inside a zip, and reading as text would hide it.
    """
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if set(relative.parts) & IGNORED_PARTS or path.suffix == ".pyc":
            continue
        out[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _manifest_fields() -> dict:
    """Parse NIVO_VERSION, reassembling the split commit-1..commit-N fields.

    The full sha is written across several short `commit-N` lines rather than
    one 40-char field so plugins.qgis.org's upload scanner does not flag a
    contiguous hex run as a high-entropy secret (see the file's own header).
    Callers still see one `commit` key with the complete sha, in order.
    """
    fields: dict = {}
    commit_parts: dict = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("commit-") and key[len("commit-"):].isdigit():
            commit_parts[int(key[len("commit-"):])] = value
        else:
            fields[key] = value
    if commit_parts:
        fields["commit"] = "".join(commit_parts[i] for i in sorted(commit_parts))
    return fields


def test_the_vendored_package_matches_its_source_checkout():
    """Byte-for-byte, and the manifest names the commit those bytes came from.

    Both halves are one test because both need the same absent thing, and two
    tests skipping for one reason reads in the standalone report as two separate
    monorepo dependencies rather than one.
    """
    if not SOURCE.is_dir():
        pytest.skip("standalone checkout: no nivo-gis package beside the monorepo")

    vendored = _fingerprint(VENDORED)
    source = _fingerprint(SOURCE)

    missing = sorted(set(source) - set(vendored))
    extra = sorted(set(vendored) - set(source))
    differing = sorted(name for name in set(vendored) & set(source) if vendored[name] != source[name])

    assert not (missing or extra or differing), (
        "mapdex_qgis/_vendor/nivo has drifted from {}: missing {}, unexpected {}, changed {}. "
        "The package is the source of truth - land the change there and re-run "
        "scripts/vendor_nivo.py; never edit the vendored copy.".format(
            SOURCE, missing or "none", extra or "none", differing or "none")
    )

    assert (SOURCE_REPO / "LICENSE").read_bytes() == (VENDOR / "NIVO-LICENSE").read_bytes(), (
        "_vendor/NIVO-LICENSE is not the package's LICENSE; MIT requires the terms to travel "
        "with the source we ship"
    )

    head = subprocess.run(
        ["git", "-C", str(SOURCE_REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode != 0:  # pragma: no cover - a checkout without git metadata
        return  # not a git checkout; the shape checks below still ran
    assert _manifest_fields()["commit"] == head.stdout.strip(), (
        "NIVO_VERSION says {} but the checkout is at {}; re-run scripts/vendor_nivo.py".format(
            _manifest_fields()["commit"][:12], head.stdout.strip()[:12])
    )


def test_the_manifest_is_readable_provenance():
    """A version file nobody checks is worse than none: it reads as a fact.

    Deliberately does not skip. The shipped tree is exactly where somebody reads
    this file to find out what they installed.
    """
    fields = _manifest_fields()
    assert len(fields.get("commit", "")) == 40, "NIVO_VERSION records no full commit sha: {!r}".format(fields)
    assert fields.get("source"), "NIVO_VERSION does not say where the package came from"
    assert fields.get("version"), "NIVO_VERSION does not record the package version"
    assert (VENDOR / "NIVO-LICENSE").is_file(), "the vendored package ships without its licence"


def test_the_package_data_the_prompt_needs_is_here():
    """`nivo.prompt` reads its markdown at import time.

    A vendoring step that copies only `*.py` produces a tree that imports fine
    in the monorepo, where nothing notices, and raises FileNotFoundError inside
    QGIS on a user's machine. So the data file is asserted present rather than
    assumed. This one does not skip: it is a property of the shipped tree, and
    the standalone repository is exactly where it matters.
    """
    assert (VENDORED / "prompts" / "nivo.system.md").is_file()
    assert (VENDORED / "py.typed").is_file()
