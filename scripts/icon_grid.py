"""The grid, the two palettes and the save step every panel icon shares.

An icon is designed at the size it is seen. QGIS toolbars draw at 24 px, so
geometry is written in a 24-unit box and supersampled down from it, rather than
drawn large and hoped down.

The values are the brand tokens from docs/DESIGN.md 4.1.
"""
from PIL import Image

GRID = 24           # the size these are designed for
SS = 32             # supersample factor
S = GRID * SS

# Two palettes, because one glyph cannot serve both QGIS themes. A near-black
# icon on the Night Mapping theme is a dark shape on a dark bar, which is the
# same as no icon. Accent is lightened on dark deliberately: #4F46E5 is too
# close to a dark background to read.
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


def blank():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def save(image, size, path):
    out = image.resize((size, size), Image.LANCZOS)
    out.save(path)
    return out


def survives(small, name):
    """What is actually left at 24 px, measured rather than judged by eye.

    A glyph whose ink has thinned to nothing downscales into a grey smudge that
    still passes any "the file exists" check, so the generator reports its own
    output instead of assuming it.
    """
    pixels = small.load()
    size = small.size[0]
    faint = sum(1 for y in range(size) for x in range(size) if 20 < pixels[x, y][3] < 170)
    solid = sum(1 for y in range(size) for x in range(size) if pixels[x, y][3] >= 170)
    box = small.getbbox()
    return "{:22} solid {:4}  faint {:4}  bbox {}".format(name, solid, faint, box)
