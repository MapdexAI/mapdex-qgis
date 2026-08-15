"""The plugin version, read from metadata.txt rather than duplicated in code.

A hand-copied version string drifts: the packaged plugin said 0.9.13 while the
companion context it sent to the server reported whatever the last person
remembered to edit. metadata.txt is the file the QGIS plugin repository and the
release check already treat as authoritative, so it is the single source here
too.
"""
from __future__ import annotations

import os

# In a packaged install metadata.txt sits beside this package directory; in the
# source tree it is one level up from mapdex_qgis/.
_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata.txt"),
)


def _read_version() -> str:
    for path in _CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("version="):
                        value = line.split("=", 1)[1].strip()
                        if value:
                            return value
        except OSError:
            continue
    # An unreadable metadata file must not stop the plugin loading; the server
    # treats the version as advisory presentation data.
    return "0.0.0"


PLUGIN_VERSION = _read_version()
