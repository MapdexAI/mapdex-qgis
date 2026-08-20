"""Draw the tracer icon on a 24-unit grid, then render it at the sizes QGIS uses.

An icon is designed at the size it is seen. QGIS toolbars are 24 px, so the
geometry here is defined in a 24-unit box and everything is supersampled from
that, rather than drawn large and hoped down.

Two things the generated attempt got wrong and this fixes:
  * it used about 55% of the canvas, so at 24 px the drawing was 13 px;
  * the scanned line was a hairline, and a hairline is the first thing to
    disappear when you downscale -- taking the whole meaning with it, because
    the icon is ABOUT the difference between the two lines.
"""
from PIL import Image, ImageDraw

GRID = 24           # the size this is designed for
SS = 32             # supersample factor
S = GRID * SS

# Two palettes, because one glyph cannot serve both QGIS themes. A near-black
# icon on the Night Mapping theme is a dark shape on a dark bar, which is the
# same as no icon. Values are the brand tokens from docs/DESIGN.md 4.1: ink
# #12100B and accent #4F46E5 on light, and the Studio dark pair #F4F4F5 with
# accent #8A8AFF on dark, which is lightened deliberately because #4F46E5 is
# too close to a dark background to read.
PALETTES = {
    "light": {
        "ink": (18, 16, 11, 255),
        "scan": (18, 16, 11, 110),
        "accent": (79, 70, 229, 255),
    },
    "dark": {
        "ink": (244, 244, 245, 255),
        "scan": (244, 244, 245, 120),
        "accent": (138, 138, 255, 255),
    },
}


def u(value):
    """Grid units to supersampled pixels."""
    return value * SS


def draw_icon(palette):
    """Left half is the scan, right half is what the tracer made of it.

    The first attempt drew both lines on top of each other along the whole
    diagonal. At 24 px they merged into one fuzzy stroke and the icon lost the
    only thing it was saying. Twenty usable pixels cannot carry two
    distinguishable lines AND three nodes in the same place, so the two states
    are put side by side instead: the wavy drawn line runs in, a node marks
    where tracing starts, and the straight segmented result runs out.
    """
    import math

    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    mid = (11.5, 12.0)

    # The scan: wavy, lighter, entering from the lower left.
    scan = []
    for i in range(101):
        t = i / 100.0
        x = 1.8 + t * (mid[0] - 1.8)
        y = 20.4 + t * (mid[1] - 20.4)
        wobble = 1.5 * math.sin(t * math.pi * 1.8)
        scan.append((u(x + wobble), u(y + wobble * 0.35)))
    d.line(scan, fill=palette["scan"], width=int(u(2.4)), joint="curve")

    # The result: two straight segments leaving to the upper right.
    # A node has to be wider than the line it sits on or it is not a node.
    # At 2.4 against 2.6 they were the same object and only the coloured one
    # read at all; the vertices are the whole point of the right-hand half.
    nodes = [mid, (16.4, 9.4), (21.8, 3.4)]
    d.line([(u(x), u(y)) for x, y in nodes], fill=palette["ink"], width=int(u(1.7)))

    half = 3.4 / 2
    for index, (x, y) in enumerate(nodes):
        colour = palette["accent"] if index == len(nodes) - 1 else palette["ink"]
        d.rectangle([u(x - half), u(y - half), u(x + half), u(y + half)],
                    fill=colour)
    return image


def save(image, size, path):
    out = image.resize((size, size), Image.LANCZOS)
    out.save(path)
    return out


import os
import pathlib

OUT = os.environ.get("ICON_OUT") or str(
    pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "assets")
pathlib.Path(OUT).mkdir(parents=True, exist_ok=True)

for variant, palette in PALETTES.items():
    icon = draw_icon(palette)
    suffix = "" if variant == "light" else "_dark"
    save(icon, 128, os.path.join(OUT, "icon_vectorize{}.png".format(suffix)))
    small = save(icon, 24, os.path.join(OUT, "icon_vectorize{}_24.png".format(suffix)))

    # What actually survives at 24 px, measured rather than judged by eye.
    pixels = small.load()
    faint = sum(1 for y in range(24) for x in range(24)
                if 20 < pixels[x, y][3] < 170)
    solid = sum(1 for y in range(24) for x in range(24) if pixels[x, y][3] >= 170)
    box = small.getbbox()
    print("{:<5} at 24 px: {} solid, {} faint (the scanned line), "
          "fills {:.0%} of the canvas".format(
              variant, solid, faint,
              ((box[2] - box[0]) * (box[3] - box[1])) / (24 * 24)))
