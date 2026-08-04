"""Pure source inspection helpers for pre-upload QGIS feedback."""
from __future__ import annotations

import os


RASTER_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".pdf"}
VECTOR_EXTENSIONS = {".gpkg", ".geojson", ".json", ".shp"}


def human_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit == "B" else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "0 B"


def inspect_paths(paths: list[str], workflow: str) -> dict:
    if not paths:
        return {"valid": False, "summary": "No source selected", "error": "Choose a source first."}
    missing = [path for path in paths if not os.path.isfile(path)]
    if missing:
        return {"valid": False, "summary": "Source file is unavailable", "error": "The selected file no longer exists."}
    kinds = []
    total = 0
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        kinds.append("raster" if ext in RASTER_EXTENSIONS else "vector" if ext in VECTOR_EXTENSIONS else "unknown")
        total += os.path.getsize(path)
    wants_vector = workflow == "validate_deliver"
    incompatible = any(kind != ("vector" if wants_vector else "raster") for kind in kinds)
    label = "{} · {}".format(kinds[0].title(), human_size(total)) if len(paths) == 1 else "{} files · {}".format(len(paths), human_size(total))
    if incompatible:
        expected = "vector" if wants_vector else "raster/PDF"
        return {"valid": False, "summary": label, "error": "This workflow requires a {} source.".format(expected)}
    return {"valid": True, "summary": label, "error": ""}
