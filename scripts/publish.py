"""Publish the built plugin zip to plugins.qgis.org.

    OSGEO_USERNAME=… OSGEO_PASSWORD=… python scripts/publish.py dist/mapdex-qgis.zip [tag]

The QGIS plugin repository accepts uploads over XML-RPC with the OSGeo account
that owns the plugin. `--dry-run` (or no credentials) runs every check and
stops before the upload, which is what CI does on a pull request.
"""
from __future__ import annotations

import os
import pathlib
import sys
import xmlrpc.client
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from release_checks import ReleaseError, check_package, read_metadata  # noqa: E402

ENDPOINT = "https://{credentials}plugins.qgis.org/plugins/RPC2/"


def upload(zip_path: pathlib.Path, username: str, password: str) -> None:
    credentials = "{}:{}@".format(quote(username, safe=""), quote(password, safe=""))
    server = xmlrpc.client.ServerProxy(ENDPOINT.format(credentials=credentials), verbose=False)
    with open(zip_path, "rb") as handle:
        server.plugin.upload(xmlrpc.client.Binary(handle.read()))


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    zip_path = pathlib.Path(argv[0])
    tag = argv[1] if len(argv) > 1 else os.environ.get("RELEASE_TAG", "")
    dry_run = "--dry-run" in argv

    if not zip_path.is_file():
        print("error: {} does not exist; run scripts/package.py first".format(zip_path))
        return 1

    try:
        metadata = read_metadata(zip_path)
        check_package(metadata, tag=tag)
    except ReleaseError as error:
        print("error: {}".format(error))
        return 1

    print("{} {} ({}, {} KB)".format(
        metadata.get("name"), metadata["version"], metadata["folder"], zip_path.stat().st_size // 1024
    ))

    username = os.environ.get("OSGEO_USERNAME", "")
    password = os.environ.get("OSGEO_PASSWORD", "")
    if dry_run or not (username and password):
        print("checks passed; not uploading (no OSGeo credentials or --dry-run)")
        return 0

    try:
        upload(zip_path, username, password)
    except xmlrpc.client.Fault as fault:
        # The repository reports a duplicate version, a rejected package or a
        # permission problem this way. Surface it verbatim.
        print("plugins.qgis.org refused the upload: {}".format(fault.faultString))
        return 1
    except OSError as error:
        print("could not reach plugins.qgis.org: {}".format(error))
        return 1

    print("uploaded {} {} to plugins.qgis.org".format(metadata.get("name"), metadata["version"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
