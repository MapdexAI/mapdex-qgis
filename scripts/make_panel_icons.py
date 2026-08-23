"""Draw the four glyphs QGIS does not ship, on the same 24-unit grid.

Eight icons reach the panel and four of them come from QGIS itself
(`mIconCritical`, `mIconWarning`, `mIconInfo`, `mIconSuccess`), themed for
free. The four here are the ones QGIS has no equivalent for: the three pieces
of Mapdex work, plus send, which is the only control in the panel with no word
on it.

Every other glyph was deliberately cut. An icon earns its space when it carries
STATE or DESTINATION - what condition something is in, or where the work runs.
Layer kinds, zoom, tables, filters and tabs all had one and lost it, because a
picture beside a word that already says the same thing is decoration.

    python scripts/make_panel_icons.py

Writes a light and a dark variant of each at 128 and 24 px. `_dark` means "for
a dark interface", so the file whose name says dark is the light-coloured one -
the convention icon_vectorize.png already set here.
"""
import os
import pathlib
import sys

from PIL import ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from icon_grid import PALETTES, blank, save, survives, u  # noqa: E402


def georeference(d, p):
    """A sheet with a control point pinned on it.

    The frame says "a scanned sheet" and the target says "a known position";
    neither alone is the operation. The interior rules are faint so the target
    stays the thing the eye lands on at 24 px.
    """
    d.rectangle([u(2.6), u(2.6), u(21.4), u(21.4)], outline=p["ink"], width=int(u(1.7)))
    d.line([(u(2.6), u(9.0)), (u(21.4), u(9.0))], fill=p["scan"], width=int(u(1.2)))
    d.line([(u(9.0), u(2.6)), (u(9.0), u(21.4))], fill=p["scan"], width=int(u(1.2)))
    # The control point. Drawn over the frame, in accent, because it is what
    # the operation adds to the sheet.
    cx, cy, r = 15.0, 15.0, 3.9
    d.ellipse([u(cx - r), u(cy - r), u(cx + r), u(cy + r)], outline=p["accent"], width=int(u(1.7)))
    d.line([(u(cx - r - 1.8), u(cy)), (u(cx + r + 1.8), u(cy))], fill=p["accent"], width=int(u(1.3)))
    d.line([(u(cx), u(cy - r - 1.8)), (u(cx), u(cy + r + 1.8))], fill=p["accent"], width=int(u(1.3)))


def digitize(d, p):
    """One parcel, traced over the pixels it came from.

    The first attempt put the scan and the result side by side, the way the
    tracer glyph does. Rendered at 24 px it was a heavy grey block next to a
    small crooked ring, and the ring - the whole point - was the half that
    lost. So the two states are stacked instead of paired: coarse pixel squares
    behind, one clean parcel with its vertices in front. Two objects competing
    for twenty pixels is what failed; one object saying both things fits.
    """
    # Coarse pixels, deliberately square and aligned, so they read as a raster
    # rather than as a shadow.
    for gx, gy in ((3.2, 9.0), (8.4, 3.8), (3.2, 14.2), (13.6, 3.8)):
        d.rectangle([u(gx), u(gy), u(gx + 5.0), u(gy + 5.0)], fill=p["scan"])
    ring = [(5.6, 18.6), (7.4, 8.2), (17.0, 6.4), (19.8, 16.2), (12.2, 20.4)]
    d.line([(u(x), u(y)) for x, y in ring] + [(u(ring[0][0]), u(ring[0][1]))],
           fill=p["ink"], width=int(u(2.0)), joint="curve")
    # A vertex has to be wider than the line it sits on or it is not a vertex.
    half = 3.2 / 2
    for index, (x, y) in enumerate(ring):
        colour = p["accent"] if index in (1, 2) else p["ink"]
        d.rectangle([u(x - half), u(y - half), u(x + half), u(y + half)], fill=colour)


def validate(d, p):
    """A shield with a check: measured, and fit to hand over.

    Not a bare tick. A tick alone is "done", and this operation's claim is that
    the data was checked against something, which is what the shield carries.
    """
    shield = [(12.0, 2.4), (20.6, 5.6), (20.6, 12.4), (12.0, 21.6), (3.4, 12.4), (3.4, 5.6)]
    d.line([(u(x), u(y)) for x, y in shield] + [(u(shield[0][0]), u(shield[0][1]))],
           fill=p["ink"], width=int(u(1.8)), joint="curve")
    d.line([(u(7.8), u(11.6)), (u(10.9), u(14.8)), (u(16.4), u(8.4))],
           fill=p["accent"], width=int(u(2.4)), joint="curve")


def send(d, p):
    """An arrow leaving to the right.

    The composer's one unlabelled control, so it has to be the most ordinary
    shape in the set: anything clever here is a puzzle in the place a person is
    trying to press Enter.
    """
    d.line([(u(3.4), u(12.0)), (u(19.2), u(12.0))], fill=p["accent"], width=int(u(2.1)))
    d.line([(u(12.6), u(5.4)), (u(20.4), u(12.0)), (u(12.6), u(18.6))],
           fill=p["accent"], width=int(u(2.1)), joint="curve")


GLYPHS = {
    "icon_georeference": georeference,
    "icon_digitize": digitize,
    "icon_validate": validate,
    "icon_send": send,
}


def main():
    out = os.environ.get("ICON_OUT") or str(
        pathlib.Path(__file__).resolve().parents[1] / "mapdex_qgis" / "assets")
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)

    for name, draw in GLYPHS.items():
        for variant, palette in PALETTES.items():
            image = blank()
            draw(ImageDraw.Draw(image), palette)
            suffix = "" if variant == "light" else "_dark"
            stem = "{}{}".format(name, suffix)
            save(image, 128, os.path.join(out, stem + ".png"))
            small = save(image, 24, os.path.join(out, stem + "_24.png"))
            print(survives(small, stem))


if __name__ == "__main__":
    main()
