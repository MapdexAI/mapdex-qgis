"""Mapdex for QGIS.

Copyright (C) Oriance Labs.

This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later
version. The full text is in the LICENSE file shipped with this plugin.
"""


def classFactory(iface):
    from .plugin import MapdexPlugin
    return MapdexPlugin(iface)
