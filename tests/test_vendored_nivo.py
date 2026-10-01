"""The plugin's copy of the nivo package must not drift from the package.

`mapdex_qgis/_vendor/nivo/` is a vendored copy of the MIT `nivo-gis` package,
whose source of truth is `mapdex/packages/nivo` in the Mapdex monorepo. QGIS
ships no usable `pip`, so the plugin cannot depend on it: it carries the source
and imports it relatively.

Vendoring's one failure mode is a local edit. Somebody fixes a bug here, the
package never learns about it, and the two diverge until "the same question
answered the same way in every client" quietly stops being true. That is not
hypothetical: it is how six modules came to exist only in this copy. This fails
the moment the copy differs from the package, in either direction, including
files added or removed on either side.

The byte comparison skips when the package is absent - the plugin is published
to its own repository with no monorepo - exactly as test_vendored_contracts.py
skips. The digest check does not skip, so a hand edit is caught in the public
repository too.
"""
import hashlib
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = ROOT / "mapdex_qgis" / "_vendor"
VENDORED = VENDOR / "nivo"
MANIFEST = VENDOR / "NIVO_VERSION"

# <repo>/mapdex/apps/qgis-plugin -> <repo>/mapdex/packages/nivo. Derived from
# this file rather than from $HOME so it holds on a CI runner, and overridable.
#
# Guarded, because the published tree is copied to a shallow temporary directory
# where that path does not exist, and an IndexError at import time would take
# the whole module down as a collection error rather than reaching the skip
# below. That is what scripts/standalone_check.sh caught the first time this
# file existed.
_ANCESTORS = ROOT.parents
DEFAULT_PACKAGE_ROOT = _ANCESTORS[1] / "packages" / "nivo" if len(_ANCESTORS) > 1 else ROOT / "no-such-package"
PACKAGE_ROOT = pathlib.Path(os.environ.get("NIVO_PACKAGE_ROOT") or DEFAULT_PACKAGE_ROOT)
PACKAGE = PACKAGE_ROOT / "nivo"

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


def _tree_digest(files: dict) -> str:
    """Must match scripts/vendor_nivo.py: sha256 over sorted "path digest" lines."""
    lines = "".join("{} {}\n".format(name, files[name]) for name in sorted(files))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _manifest_fields() -> dict:
    """Parse NIVO_VERSION, reassembling the split digest-1..digest-N fields.

    The full sha256 is written across several short `digest-N` lines rather than
    one 64-char field so plugins.qgis.org's upload scanner does not flag a
    contiguous hex run as a high-entropy secret (see the file's own header).
    Callers still see one `digest` key with the complete value, in order.
    """
    fields: dict = {}
    parts: dict = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("digest-") and key[len("digest-"):].isdigit():
            parts[int(key[len("digest-"):])] = value
        else:
            fields[key] = value
    if parts:
        fields["digest"] = "".join(parts[i] for i in sorted(parts))
    return fields


def test_the_vendored_package_matches_the_package():
    """Byte-for-byte, including the licence that has to travel with it."""
    if not PACKAGE.is_dir():
        pytest.skip("standalone checkout: no nivo package in a monorepo beside the plugin")

    vendored = _fingerprint(VENDORED)
    source = _fingerprint(PACKAGE)

    missing = sorted(set(source) - set(vendored))
    extra = sorted(set(vendored) - set(source))
    differing = sorted(name for name in set(vendored) & set(source) if vendored[name] != source[name])

    assert not (missing or extra or differing), (
        "mapdex_qgis/_vendor/nivo has drifted from {}: missing {}, unexpected {}, changed {}. "
        "The package is the source of truth - land the change there and re-run "
        "scripts/vendor_nivo.py; never edit the vendored copy.".format(
            PACKAGE, missing or "none", extra or "none", differing or "none")
    )

    assert (PACKAGE_ROOT / "LICENSE").read_bytes() == (VENDOR / "NIVO-LICENSE").read_bytes(), (
        "_vendor/NIVO-LICENSE is not the package's LICENSE; MIT requires the terms to travel "
        "with the source we ship"
    )


def test_the_manifest_describes_the_bytes_that_ship():
    """A version file nobody checks is worse than none: it reads as a fact.

    Deliberately does not skip. The shipped tree is exactly where somebody reads
    this file to find out what they installed, and the digest lets that tree
    prove it was not edited after vendoring with no monorepo to compare against.
    """
    fields = _manifest_fields()
    assert fields.get("source"), "NIVO_VERSION does not say where the package came from"
    assert fields.get("version"), "NIVO_VERSION does not record the package version"
    assert len(fields.get("digest", "")) == 64, "NIVO_VERSION records no full tree digest: {!r}".format(fields)
    assert fields["digest"] == _tree_digest(_fingerprint(VENDORED)), (
        "mapdex_qgis/_vendor/nivo no longer matches the digest in NIVO_VERSION, so it was edited "
        "after vendoring. Land the change in the nivo package and re-run scripts/vendor_nivo.py."
    )
    assert (VENDOR / "NIVO-LICENSE").is_file(), "the vendored package ships without its licence"


def _detect_secrets_hex_entropy(value: str) -> float:
    """detect-secrets' HexHighEntropyString score, which plugins.qgis.org runs.

    Shannon entropy over the hex charset, lowered for an all-digit value. The
    upload scan flags a value scoring above 3.0.
    """
    import math

    entropy = 0.0
    for char in "0123456789abcdefABCDEF":
        share = value.count(char) / len(value)
        if share > 0:
            entropy -= share * math.log(share, 2)
    if len(value) > 1 and value.isdigit():
        entropy -= 1.2 / math.log(len(value), 2)
    return entropy


def test_no_manifest_value_reads_as_a_secret_on_upload():
    """plugins.qgis.org flagged digest-1 and digest-3 of version 0.13.7.

    The digest was already split into 10-char chunks for this reason, and a
    10-char chunk passes only when its characters happen to repeat. This checks
    every value against the scanner's own formula rather than against a length
    somebody believed was short enough.
    """
    flagged = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value and all(c in "0123456789abcdefABCDEF" for c in value):
            if _detect_secrets_hex_entropy(value) > 3.0:
                flagged[key] = value
    assert not flagged, "NIVO_VERSION values the upload scan reports as secrets: {}".format(flagged)


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
