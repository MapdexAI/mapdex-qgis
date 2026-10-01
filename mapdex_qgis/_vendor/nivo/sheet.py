# SPDX-License-Identifier: MIT
"""Composing a printable map sheet, as arithmetic rather than as a canvas.

A surveyor's deliverable is frequently a PLAN: a page at a stated scale with a
legend, a scale bar, a north arrow and a title block. A data pipeline delivers
DATA, and the gap between those two is the last thing between a run and something a client
will accept.

Everything here is pure. Where each item sits on the page, what ground the map
frame covers at a stated scale, and whether the data actually fits are all
arithmetic, and doing them here means they can be checked without a rendering
engine and are the same on any surface that adopts them.

Three decisions carry it.

**A stated scale is a promise about ground, not a preference.** 1:1000 on an A3
frame covers exactly the ground the frame's millimetres buy, and if the layer is
larger than that the sheet silently crops it. The composition therefore reports
what the frame covers and REFUSES when the requested extent does not fit, naming
the scale that would - because a plan that quietly omits a third of the site is
worse than no plan.

**A scale printed on a sheet must be a scale somebody can use.** Survey plans
are drawn at rounded scales - 1:100, 1:200, 1:500, 1:1000, 1:2500 - because a
person reads distances off them with a rule. When a caller asks to fit the data
rather than naming a scale, the fitted scale is ROUNDED UP to the next such
value, never to an arbitrary 1:1373.

**The page is millimetres and the ground is metres, and they are never mixed.**
Every dimension here carries its unit in its name.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# ISO A series, portrait, in millimetres. Landscape swaps them.
PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}

ORIENTATIONS = ("portrait", "landscape")

DEFAULT_MARGIN_MM = 10.0
MIN_MARGIN_MM = 0.0
MAX_MARGIN_MM = 100.0

# Room reserved along the bottom of the page for the scale bar, the north arrow
# and the title block, in millimetres. The map frame gets what is left.
FURNITURE_HEIGHT_MM = 24.0
# Room reserved down the right-hand side for the legend.
LEGEND_WIDTH_MM = 45.0

# The scales a survey plan is drawn at, ascending. A fitted scale is rounded UP
# to one of these: a plan labelled 1:1373 is a plan nobody can take a
# measurement off with a rule.
STANDARD_SCALES = (
    50, 100, 200, 250, 500, 1000, 1250, 2500, 5000, 10000,
    20000, 25000, 50000, 100000, 250000, 500000, 1000000,
)

MM_PER_METRE = 1000.0


def page_size_mm(size: str, orientation: str = "landscape") -> tuple[float, float]:
    """Width and height in millimetres, or a refusal naming what is available."""
    key = str(size or "").strip().upper()
    if key not in PAGE_SIZES_MM:
        raise ValueError(
            "unknown page size {!r}; available: {}".format(
                size, ", ".join(sorted(PAGE_SIZES_MM))))
    facing = str(orientation or "landscape").strip().lower()
    if facing not in ORIENTATIONS:
        raise ValueError(
            "orientation must be one of: {}".format(", ".join(ORIENTATIONS)))
    width, height = PAGE_SIZES_MM[key]
    return (height, width) if facing == "landscape" else (width, height)


def round_to_standard_scale(denominator: float) -> int:
    """The next standard survey scale at or above this one.

    Up rather than to the nearest, always: rounding DOWN produces a sheet at a
    scale the data does not fit on, which is the one failure this whole module
    exists to prevent.
    """
    if denominator <= 0:
        raise ValueError("a scale denominator is greater than zero")
    for candidate in STANDARD_SCALES:
        if candidate >= denominator:
            return candidate
    # Past the largest listed value, round up to a whole million so the label
    # is still something a person can read.
    return int(-(-denominator // 1_000_000) * 1_000_000)


def ground_size_m(frame_width_mm: float, frame_height_mm: float, denominator: float) -> tuple[float, float]:
    """What ground a frame of this size covers at this scale.

    One millimetre on the page is `denominator` millimetres on the ground, which
    is `denominator / 1000` metres. The whole arithmetic of a plan is this line.
    """
    if denominator <= 0:
        raise ValueError("a scale denominator is greater than zero")
    metres_per_mm = denominator / MM_PER_METRE
    return (frame_width_mm * metres_per_mm, frame_height_mm * metres_per_mm)


def scale_to_fit(extent_width_m: float, extent_height_m: float,
                 frame_width_mm: float, frame_height_mm: float) -> int:
    """The standard scale at which this ground fits inside this frame.

    Both axes are checked and the tighter one wins: fitting the width and
    cropping the height is the failure mode, not a compromise.
    """
    if extent_width_m <= 0 or extent_height_m <= 0:
        raise ValueError("an extent has a positive width and height")
    if frame_width_mm <= 0 or frame_height_mm <= 0:
        raise ValueError("a map frame has a positive width and height")
    needed = max(
        extent_width_m * MM_PER_METRE / frame_width_mm,
        extent_height_m * MM_PER_METRE / frame_height_mm,
    )
    return round_to_standard_scale(needed)


def _frame(page_width_mm: float, page_height_mm: float, margin_mm: float,
           legend: bool, furniture: bool) -> dict[str, float]:
    """Where the map frame sits once the furniture has taken its room."""
    left = margin_mm
    top = margin_mm
    width = page_width_mm - 2 * margin_mm - (LEGEND_WIDTH_MM if legend else 0.0)
    height = page_height_mm - 2 * margin_mm - (FURNITURE_HEIGHT_MM if furniture else 0.0)
    if width <= 0 or height <= 0:
        raise ValueError(
            "the margins and furniture leave no room for the map on this page; "
            "use a larger page or a smaller margin")
    return {"x_mm": left, "y_mm": top, "width_mm": width, "height_mm": height}


def compose_sheet(
    page: str = "A3",
    orientation: str = "landscape",
    margin_mm: Any = None,
    scale: Any = None,
    extent_m: Sequence[float] | None = None,
    legend: bool = True,
    scale_bar: bool = True,
    north_arrow: bool = True,
    title: str = "",
) -> dict[str, Any]:
    """Everything a renderer needs to draw the sheet, and nothing it can guess.

    `extent_m` is the ground the plan must show, as width and height in metres.
    When `scale` is absent it is computed from that extent and rounded up to a
    standard survey scale. When `scale` IS given, the extent is checked against
    it and a request that does not fit is refused with the scale that would.
    """
    page_width_mm, page_height_mm = page_size_mm(page, orientation)
    margin = DEFAULT_MARGIN_MM if margin_mm is None else float(margin_mm)
    if not (MIN_MARGIN_MM <= margin <= MAX_MARGIN_MM):
        raise ValueError(
            "a margin must be between {:g} and {:g} mm".format(MIN_MARGIN_MM, MAX_MARGIN_MM))

    furniture = bool(scale_bar or north_arrow or title)
    frame = _frame(page_width_mm, page_height_mm, margin, bool(legend), furniture)

    extent = None
    if extent_m is not None:
        values = list(extent_m)
        if len(values) != 2:
            raise ValueError("extent_m is a width and a height, in metres")
        extent = (float(values[0]), float(values[1]))
        if extent[0] <= 0 or extent[1] <= 0:
            raise ValueError("an extent has a positive width and height")

    if scale is None:
        if extent is None:
            raise ValueError(
                "a sheet needs either a scale or the ground it must show")
        denominator = scale_to_fit(extent[0], extent[1], frame["width_mm"], frame["height_mm"])
        fitted = True
    else:
        denominator = float(scale)
        if denominator <= 0:
            raise ValueError("a scale denominator is greater than zero")
        fitted = False
        if extent is not None:
            covers_width_m, covers_height_m = ground_size_m(
                frame["width_mm"], frame["height_mm"], denominator)
            if extent[0] > covers_width_m + 1e-6 or extent[1] > covers_height_m + 1e-6:
                would_fit = scale_to_fit(
                    extent[0], extent[1], frame["width_mm"], frame["height_mm"])
                # Refused rather than cropped. A plan that quietly omits a third
                # of the site is worse than no plan, and the person who receives
                # it has no way to tell.
                raise ValueError(
                    "at 1:{:g} this frame covers {:.0f} by {:.0f} m and the data is "
                    "{:.0f} by {:.0f} m, so the sheet would crop it. 1:{} fits.".format(
                        denominator, covers_width_m, covers_height_m,
                        extent[0], extent[1], would_fit))

    covers_width_m, covers_height_m = ground_size_m(
        frame["width_mm"], frame["height_mm"], denominator)

    items: list[dict[str, Any]] = [dict(frame, kind="map")]
    if legend:
        items.append({
            "kind": "legend",
            "x_mm": page_width_mm - margin - LEGEND_WIDTH_MM,
            "y_mm": margin,
            "width_mm": LEGEND_WIDTH_MM,
            "height_mm": frame["height_mm"],
        })
    furniture_y = margin + frame["height_mm"]
    if scale_bar:
        items.append({
            "kind": "scale_bar", "x_mm": margin, "y_mm": furniture_y,
            "width_mm": 60.0, "height_mm": FURNITURE_HEIGHT_MM,
        })
    if north_arrow:
        items.append({
            "kind": "north_arrow", "x_mm": margin + 70.0, "y_mm": furniture_y,
            "width_mm": 15.0, "height_mm": FURNITURE_HEIGHT_MM,
        })
    if title:
        items.append({
            "kind": "title", "x_mm": margin + 90.0, "y_mm": furniture_y,
            "width_mm": max(10.0, page_width_mm - 2 * margin - 90.0),
            "height_mm": FURNITURE_HEIGHT_MM, "text": str(title)[:200],
        })

    return {
        "page": str(page).strip().upper(),
        "orientation": str(orientation).strip().lower(),
        "page_width_mm": page_width_mm,
        "page_height_mm": page_height_mm,
        "margin_mm": margin,
        "scale": int(denominator) if float(denominator).is_integer() else denominator,
        # Whether the scale was CHOSEN by the caller or computed to fit. A
        # printed sheet says 1:1000 either way and the two mean different
        # things to whoever signs it.
        "scale_fitted": fitted,
        "covers_width_m": covers_width_m,
        "covers_height_m": covers_height_m,
        "items": items,
    }


def describe_sheet(sheet: Mapping[str, Any]) -> str:
    """One line naming what was composed, in the terms a plan is read in."""
    fitted = " (fitted to the data)" if sheet.get("scale_fitted") else ""
    return "{} {} at 1:{:g}{}, covering {:.0f} by {:.0f} m.".format(
        sheet["page"], sheet["orientation"], sheet["scale"], fitted,
        sheet["covers_width_m"], sheet["covers_height_m"])
