def classFactory(iface):
    from .plugin import MapdexPlugin
    return MapdexPlugin(iface)
