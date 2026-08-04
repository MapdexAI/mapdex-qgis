"""The release gate: what plugins.qgis.org would otherwise reject days later."""
import pathlib
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_checks import ReleaseError, check_package, read_metadata, version_from_tag

METADATA = """[general]
name=Mapdex for QGIS
description=Send map data to Mapdex.
about=Requires a Mapdex account.
version=1.2.3
qgisMinimumVersion=3.34
author=Mapdex
email=support@mapdex.ai
icon=icon.png
repository=https://github.com/MapdexAI/mapdex-qgis
changelog=1.2.3
    - first line
    - second line
"""


def build(tmp_path, *, metadata=METADATA, folder="mapdex", extra=(), skip=()):
    path = tmp_path / "plugin.zip"
    files = {
        "metadata.txt": metadata,
        "LICENSE": "GPL",
        "README.md": "# doc",
        "icon.png": "png",
        "__init__.py": "",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            if name in skip:
                continue
            archive.writestr("{}/{}".format(folder, name), content)
        for name in extra:
            archive.writestr("{}/{}".format(folder, name), "")
    return path


def test_version_from_tag_accepts_the_shapes_ci_passes():
    assert version_from_tag("refs/tags/v0.9.10") == "0.9.10"
    assert version_from_tag("v1.0.0") == "1.0.0"
    assert version_from_tag("1.0.0") == "1.0.0"
    with pytest.raises(ReleaseError):
        version_from_tag("")


def test_a_good_package_passes(tmp_path):
    metadata = read_metadata(build(tmp_path))
    assert metadata["version"] == "1.2.3"
    assert metadata["folder"] == "mapdex"
    check_package(metadata, tag="v1.2.3")


def test_multi_line_changelog_does_not_become_metadata_keys(tmp_path):
    metadata = read_metadata(build(tmp_path))
    assert "- first line" not in metadata
    assert metadata["changelog"] == "1.2.3"


def test_tag_must_match_the_packaged_version(tmp_path):
    metadata = read_metadata(build(tmp_path))
    with pytest.raises(ReleaseError, match="does not match"):
        check_package(metadata, tag="v9.9.9")


def test_missing_licence_or_readme_stops_the_release(tmp_path):
    with pytest.raises(ReleaseError, match="LICENSE"):
        check_package(read_metadata(build(tmp_path, skip=("LICENSE",))))
    with pytest.raises(ReleaseError, match="README"):
        check_package(read_metadata(build(tmp_path, skip=("README.md",))))


def test_icon_named_in_metadata_must_be_packaged(tmp_path):
    with pytest.raises(ReleaseError, match="icon"):
        check_package(read_metadata(build(tmp_path, skip=("icon.png",))))


def test_build_leftovers_are_rejected(tmp_path):
    with pytest.raises(ReleaseError, match="leftovers"):
        check_package(read_metadata(build(tmp_path, extra=("__pycache__/x.pyc",))))


def test_private_or_missing_repository_is_rejected(tmp_path):
    metadata = read_metadata(build(tmp_path, metadata=METADATA.replace(
        "repository=https://github.com/MapdexAI/mapdex-qgis", "repository="
    )))
    with pytest.raises(ReleaseError, match="repository"):
        check_package(metadata)


def test_a_zip_with_two_top_level_folders_is_rejected(tmp_path):
    path = tmp_path / "two.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mapdex/metadata.txt", METADATA)
        archive.writestr("other/file.py", "")
    with pytest.raises(ReleaseError, match="exactly one plugin folder"):
        read_metadata(path)


def test_the_real_package_is_releasable():
    built = ROOT / "dist" / "mapdex-qgis.zip"
    if not built.is_file():
        pytest.skip("run scripts/package.py first")
    metadata = read_metadata(built)
    check_package(metadata, tag="v{}".format(metadata["version"]))


def test_publish_returns_a_failing_exit_code_so_ci_stops(tmp_path):
    """CI only blocks a bad release if the script exits non-zero."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish

    built = ROOT / "dist" / "mapdex-qgis.zip"
    if not built.is_file():
        pytest.skip("run scripts/package.py first")
    version = read_metadata(built)["version"]

    assert publish.main([str(built), "v{}".format(version), "--dry-run"]) == 0
    assert publish.main([str(built), "v9.9.9", "--dry-run"]) == 1
    assert publish.main([str(tmp_path / "absent.zip")]) == 1
    assert publish.main([]) == 2
