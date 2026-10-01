"""Third-party source the plugin carries because it cannot install it.

`nivo-gis` is the generic GIS-agent core - the capability registry, the bounded
agent loop, the analytics kernel, the read-only PostGIS builder, the spatial,
survey and presentation code - kept vendor-neutral so it belongs to nobody in
particular. It is MIT and has no dependencies. Its source of truth is
`mapdex/packages/nivo` in the Mapdex monorepo, published as
github.com/MapdexAI/nivo.

QGIS ships no usable `pip`, and a plugin runs inside an interpreter it does not
own, so "add it to requirements" is not available: the source travels in the
zip. Everything under here is imported with explicit relative imports
(`from ._vendor.nivo.capabilities import ...`) rather than by putting this
directory on `sys.path`. A path insert is a side effect that happens at import
time, is invisible at the call site, and behaves differently inside QGIS than
under pytest; a relative import is the same statement in both.

Nothing in here is edited in place. `_vendor/NIVO_VERSION` records a digest of
the exact bytes vendored and `tests/test_vendored_nivo.py` fails when the tree
diverges from it or from the package, which is the same discipline `generated_contracts.py` and the Nivo
prompt already use.
"""
