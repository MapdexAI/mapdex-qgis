"""Mapdex for QGIS.

Copyright (C) Oriance Labs.

This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later
version. The full text is in the LICENSE file shipped with this plugin.

The generic agent core is the vendored MIT package in `_vendor/nivo` (see
`_vendor/__init__.py`). Importing `mapdex_capabilities` here is what adds
Mapdex's five server-side capabilities to that shared registry, and it is here
rather than in `plugin.py` on purpose: Python runs this file before any
submodule of the package, `_vendor.nivo.capabilities` included, so no import
path can read the catalogue before the five are in it. It is a stdlib-only
import, so it cannot make loading the plugin depend on QGIS being ready.
"""

from . import mapdex_capabilities  # noqa: F401  - imported for its registrations


def classFactory(iface):
    """Build the plugin from the files that are installed now.

    QGIS replaces plugin files in-place when a ZIP update is installed, but it
    can leave already imported package submodules in ``sys.modules`` for the
    rest of the desktop session.  That produced a particularly confusing
    half-upgrade: the new panel and connection gate came from ``plugin.py``,
    while the old ``nivo.py`` action parser silently discarded Processing
    confirmations.  Reload the small policy module before importing the plugin
    class so an in-session update is atomic from the user's point of view.
    """
    import importlib
    import sys

    nivo_name = "{}.nivo".format(__name__)
    plugin_name = "{}.plugin".format(__name__)
    if nivo_name in sys.modules:
        importlib.reload(sys.modules[nivo_name])
    if plugin_name in sys.modules:
        importlib.reload(sys.modules[plugin_name])

    from .plugin import MapdexPlugin
    return MapdexPlugin(iface)
