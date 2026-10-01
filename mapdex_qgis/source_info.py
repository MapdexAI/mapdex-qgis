"""Which extension is which kind, and how to say a byte count.

The one owner of the format tables. `task_sources` reads them rather than
restating them, so adding a supported extension is one edit.

`inspect_paths` used to live here and answered {"valid", "summary",
"error"} for a list of PATHS. It was superseded by `task_sources.summary`,
which answers the same question for a list of SOURCES - the sheets a task
runs over, which may be open QGIS layers as well as files on disk. Keeping
a path-only version importable would let a caller reach for it and drop
the layer half of a batch without anything failing.
"""
from __future__ import annotations


RASTER_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".pdf"}
VECTOR_EXTENSIONS = {".gpkg", ".geojson", ".json", ".shp"}


def human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit == "B" else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "0 B"
