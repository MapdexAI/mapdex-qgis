"""Pure release checks for the QGIS plugin package.

Kept free of network and QGIS imports so the release rules are unit tested:
a bad upload is only discovered by a human reviewer days later.
"""
from __future__ import annotations

import zipfile


class ReleaseError(RuntimeError):
    """A packaging or versioning problem that must stop the release."""


def version_from_tag(tag: str) -> str:
    """`refs/tags/v0.9.10` or `v0.9.10` -> `0.9.10`."""
    value = str(tag or "").strip()
    if value.startswith("refs/tags/"):
        value = value[len("refs/tags/"):]
    if value.startswith("v"):
        value = value[1:]
    if not value:
        raise ReleaseError("empty release tag")
    return value


def read_metadata(zip_path) -> dict:
    """Return the plugin metadata from the packaged zip."""
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        folders = {name.split("/")[0] for name in names}
        if len(folders) != 1:
            raise ReleaseError(
                "the zip must contain exactly one plugin folder, found: "
                + ", ".join(sorted(folders))
            )
        folder = folders.pop()
        metadata_name = "{}/metadata.txt".format(folder)
        if metadata_name not in names:
            raise ReleaseError("no metadata.txt inside {}/".format(folder))
        raw = archive.read(metadata_name).decode("utf-8")
        present = set(names)

    metadata = {"folder": folder}
    for line in raw.splitlines():
        if line.startswith((" ", "\t")) or "=" not in line or line.startswith("["):
            continue  # continuation line of a multi-line value, or a section
        key, _, value = line.partition("=")
        metadata[key.strip()] = value.strip()
    metadata["_files"] = present
    return metadata


def check_package(metadata: dict, tag: str = "") -> None:
    """Fail loudly on the things plugins.qgis.org rejects."""
    folder = metadata["folder"]
    required = ("name", "description", "about", "version", "qgisMinimumVersion", "author", "email")
    missing = [field for field in required if not metadata.get(field)]
    if missing:
        raise ReleaseError("metadata.txt is missing: " + ", ".join(missing))

    files = metadata.get("_files") or set()
    if "{}/LICENSE".format(folder) not in files:
        raise ReleaseError("no LICENSE in the package; the repository requires GPL-2.0-or-later")
    if not any(name.endswith("README.md") for name in files):
        raise ReleaseError("no README.md in the package; plugins need minimal documentation")
    icon = metadata.get("icon")
    if icon and "{}/{}".format(folder, icon) not in files:
        raise ReleaseError("metadata icon {} is not in the package".format(icon))
    leftovers = [name for name in files if "__pycache__" in name or name.endswith(".pyc")]
    if leftovers:
        raise ReleaseError("build leftovers in the package: " + ", ".join(sorted(leftovers)[:3]))

    repository = metadata.get("repository", "")
    if not repository.startswith("http"):
        raise ReleaseError("metadata repository must be a public URL, got: " + repr(repository))

    if tag:
        expected = version_from_tag(tag)
        if metadata["version"] != expected:
            raise ReleaseError(
                "tag {} does not match metadata version {}".format(expected, metadata["version"])
            )
