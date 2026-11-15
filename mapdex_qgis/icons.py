"""The panel's icons, as SVG text coloured for the theme on screen.

Every glyph in the panel is a Lucide icon (ISC, `assets/lucide/LICENSE.txt`),
vendored as a file with `stroke="currentColor"` and coloured here from the
active token set. Colouring at load time rather than shipping a `_dark` and a
light copy of each is what lets one file follow both QGIS themes, and it keeps
these files away from the raster dark-variant resolver, which once returned a
path that did not exist and left a header button showing its text instead.

Status marks are not Lucide: they are a solid filled circle with a white check
or exclamation mark (or a blue ring while working), the web app's status shape.
They are drawn here as SVG so a QLabel can show one at any size.

No Qt import: what an icon looks like is decided where a test can reach it.
"""
from __future__ import annotations

import os
import re

LUCIDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "lucide")

# Names the panel asks for, so a typo is a test failure rather than a blank
# button at runtime.
NAMES = (
    "message-square-plus", "history", "sliders", "arrow-up", "arrow-left",
    "ruler", "layers", "layers-2", "layer", "crosshair", "external-link",
    "chevron-right", "chevron-down", "x", "image", "file", "upload", "user",
    "log-out", "check", "square", "shield-check", "table", "circle-help", "map",
    "zoom-in", "message-circle", "palette",
)

_STROKE_WIDTH = re.compile(r'stroke-width="[^"]*"')


def lucide_path(name: str) -> str:
    return os.path.join(LUCIDE_DIR, "{}.svg".format(name))


def lucide_svg(name: str, colour: str, stroke_width: float = 1.75) -> bytes:
    """The named icon with its stroke set to `colour`.

    Raises FileNotFoundError for a name that is not vendored: an icon that
    silently draws nothing is the failure this module exists to remove.
    """
    with open(lucide_path(name), "r", encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("currentColor", str(colour))
    text = _STROKE_WIDTH.sub('stroke-width="{:g}"'.format(float(stroke_width)), text, count=1)
    return text.encode("utf-8")


_STATUS_CHECK = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10" fill="{c}"/>'
    '<path d="m8.5 12 2.5 2.5 4.5-5" fill="none" stroke="#FFFFFF" stroke-width="2.25" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_STATUS_ALERT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="10" fill="{c}"/>'
    '<path d="M12 7.5v5.5" stroke="#FFFFFF" stroke-width="2.25" stroke-linecap="round"/>'
    '<circle cx="12" cy="16.5" r="1.25" fill="#FFFFFF"/></svg>'
)
_STATUS_WORKING = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="{c}" stroke-width="2.5" stroke-linecap="round">'
    '<path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>'
)
_STATUS_NEUTRAL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="9" fill="none" stroke="{c}" stroke-width="2"/></svg>'
)


def status_svg(tone: str, colour: str) -> bytes:
    """The status mark for a tone: ok is a check, warn and err an exclamation.

    The SHAPE differs between ok and the two others, so the state is carried by
    the glyph and the word even where the colour cannot be told apart.
    """
    tone = str(tone or "neutral")
    if tone == "ok":
        template = _STATUS_CHECK
    elif tone in ("warn", "err"):
        template = _STATUS_ALERT
    elif tone == "working":
        template = _STATUS_WORKING
    else:
        template = _STATUS_NEUTRAL
    return template.format(c=colour).encode("utf-8")


def radio_svg(checked: bool, colour: str) -> bytes:
    """A 16 px radio indicator: a ring, and a dot inside it when chosen."""
    dot = '<circle cx="8" cy="8" r="4" fill="{c}"/>'.format(c=colour) if checked else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        '<circle cx="8" cy="8" r="6.75" fill="none" stroke="{c}" stroke-width="1.5"/>{dot}</svg>'
    ).format(c=colour, dot=dot).encode("utf-8")
