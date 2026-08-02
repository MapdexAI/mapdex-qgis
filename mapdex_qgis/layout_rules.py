"""Width rules for the companion panel (Qt-free so it stays testable)."""
from __future__ import annotations

# The width the dock asks QGIS for when it first opens. The panel carries a
# project/workflow/source form plus a task card, and at the old ~320px it
# opened clipped, so every field had to be resized by hand before use.
PREFERRED_WIDTH = 420
PREFERRED_HEIGHT = 620

# Below this the label column of a two-column form eats the field, so labels
# stack above their field and paired rows become one control per row.
COMPACT_WIDTH = 380

# The panel must still be usable when the user drags the dock narrow.
MINIMUM_WIDTH = 260


def panel_layout_mode(width: int) -> str:
    """Return the layout mode for a dock width: "compact" or "regular"."""
    return "compact" if width < COMPACT_WIDTH else "regular"
