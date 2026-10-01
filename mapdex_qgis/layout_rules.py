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


# A dock can be dragged as wide as the QGIS window, and a line of text that
# runs 1,400 px is not read, it is scanned and abandoned. Everything the user
# READS - the conversation, the first-open choice - is capped and the rest of
# the width stays empty, which is the same rule any book applies.
READING_WIDTH = 720

# A message from the user is capped harder than the surface it sits on, so the
# two speakers are told apart by shape as well as by colour.
BUBBLE_WIDTH = 560
