# Panel icon brief — what to draw, and the shape the plugin needs it in

> Hand this file to whoever draws the icons. It carries the exact sizes, file
> names, colours and constraints, because every one of them is enforced by a
> test or read by a resolver, and a beautiful icon in the wrong file is an
> empty button.

## 0. What exists today, and why it looks the way it does

**The panel icons are already PNG.** They are not SVG. They are drawn by
`scripts/make_panel_icons.py`, which composes them from PIL primitives —
`d.ellipse`, `d.line`, `d.rectangle` — on a 24-unit grid. That is why they read
as crude: they are geometry typed out by hand in Python, not artwork.

The only SVGs in the panel are `assets/lucide-new-chat.svg` and
`assets/lucide-history.svg`, used for the two icon-only header controls.

So the ask is not "PNG instead of SVG". It is: **draw these properly, and keep
the PNG files the plugin loads.**

## 1. The route in — pick one, and it changes a test

The repository deliberately refuses hand-dropped binaries. `tests/test_branding.py`:

- `test_the_drawn_glyphs_are_generated_rather_than_mystery_binaries` requires
  every `ACTION_ICONS` entry to name a file the generator draws.
- `test_every_drawn_glyph_has_both_variants_on_disk` requires all four files per
  glyph to exist.
- `test_the_eight_are_eight` fixes the SET: exactly the four `BatchKind` values
  plus `send`. A fifth decorative icon fails the build on purpose.

That guard exists because an icon nobody can regenerate is one nobody can
recolour when the theme changes. Two honest ways to satisfy it:

**(A) Recommended — commit the SVG source, rasterise in the pipeline.**
Draw each icon as an SVG at `assets/source/<name>.svg`, commit it, and extend
`make_panel_icons.py` to rasterise those instead of drawing primitives. The
guard then becomes "every action icon has committed source art", which is still
enforceable and still regenerable, and the art is properly designed. Rasterising
needs `cairosvg` (or `qgis`'s own `QSvgRenderer`, already available in the
plugin's runtime).

**(B) Drop finished PNGs and relax the guard.**
Only with a recorded reason in the test, and it costs the ability to recolour
for a new theme without going back to the designer. Do not do this silently.

## 2. Hard technical constraints

Every icon ships as **four files** in `mapdex_qgis/assets/`:

| File | Size | For |
| --- | --- | --- |
| `icon_<name>.png` | 128×128 | light QGIS interface |
| `icon_<name>_24.png` | 24×24 | light QGIS interface, toolbar size |
| `icon_<name>_dark.png` | 128×128 | **dark** interface — so this is the LIGHT-COLOURED glyph |
| `icon_<name>_dark_24.png` | 24×24 | same, toolbar size |

- **`_dark` means "for a dark interface", so the file whose name says dark is
  the white one.** Reading it the other way paints a black mark on a black
  toolbar, which on screen is identical to an icon that failed to load.
- RGBA, **fully transparent background**. No plate, no rounded square, no
  shadow behind the glyph — the card and the toolbar draw their own surface.
- Designed on a **24-unit grid** and rendered at 128 for supersampling. An icon
  is designed at the size it is seen: the toolbar draws at 24 px, and the
  workflow cards draw at 22 px.
- **It must survive 24 px.** `scripts/icon_grid.py:survives()` measures how much
  solid ink is left after downscaling; a glyph that thins to a grey smudge still
  passes any "the file exists" check. Aim for ≥ 50 solid pixels at 24×24.
- Stroke weight around **1.7–2.2 grid units** (so ~2 px at 24). Thinner
  disappears; thicker merges into a blob.
- Keep roughly **1.5 units of margin** inside the 24 box.

## 3. Palette — three roles, two themes

From `docs/DESIGN.md` §4.1, already encoded in `scripts/icon_grid.py`:

| Role | Light interface | Dark interface | Meaning |
| --- | --- | --- | --- |
| `ink` | `#12100B` | `#F4F4F5` | the main shape |
| `scan` | `#12100B` at 43% alpha | `#F4F4F5` at 47% alpha | the secondary/context shape |
| `accent` | `#4F46E5` | `#8A8AFF` | **the one thing the operation adds** |

Rules that are not negotiable:

- **At most one accent element per icon**, and it must be the thing the
  operation produces. Accent used decoratively makes four icons look identical
  at a glance.
- The accent is lightened on dark (`#8A8AFF`, not `#4F46E5`) because the brand
  indigo is too close to the dark surface to read. This is measured, not taste.
- Never use any other hue. No gradients, no 3D, no drop shadows.

## 4. The icons

Seven. **The first four are the ones being complained about** — the
georeference / extract / validate / pipeline glyphs. They are drawn in exactly
two places, both measured rather than assumed:

| Surface | Size | Which file | Code |
| --- | --- | --- | --- |
| Task page workflow cards, 2×2 with the label underneath | 22 px | always the `_dark` variant | `panel.py` `WorkflowChooser` |
| Nivo action chips ("Georeference this sheet with Mapdex") | 16 px | always the `_dark` variant | `plugin.py` `_action_chip` → `_action_icon` |

Both surfaces are painted on `#191919` whatever theme QGIS is wearing, so **in
practice the `_dark` (light-coloured) file is the one people actually see.**
Draw that one first and judge it on a dark background; the light-interface pair
still has to exist and be correct, but nothing currently shows it.

The layer right-click menu ("Georeference with Mapdex") deliberately does **not**
use these — every entry there wears the Mapdex mark, because in QGIS's own menu
the question is whose plugin this is, not which of our four operations it is.
So redrawing these does not change that menu.

### 4.0 What is wrong with the four that exist

Judged at 24 px on `#191919`, which is the only size and surface they are drawn
at. `scripts/make_panel_icons.py` prints a `survives()` line for each; the
numbers below are from that plus looking at them side by side.

| Icon | What it does at 24 px |
| --- | --- |
| `georeference` | The frame is a heavy slab and the two interior rules are as thick as it is, so the sheet reads as a filled grid rather than as paper. The target survives; everything around it is noise competing with it. |
| `digitize_parcels` | The worst of the four. The `scan` pixel blocks and the traced polygon are the same visual weight, so they merge into one grey mass and neither the raster nor the parcel is legible. The vertex handles disappear entirely. |
| `validate_deliver` | The one that works. Shield plus check, clear silhouette, reads at 24 px. Keep the idea; it can be redrawn more finely but it is not the problem. |
| `full_pipeline` | Reads at 128 and collapses to three indistinct dots at 24. The direction chevrons vanish, so "in order" is lost and it becomes an ellipsis. |

Two rules fall out of that, and they matter more than any single drawing:
**one idea per icon**, and **the secondary `scan` shape must be clearly lighter
than the `ink` shape** — where they came out the same weight, the icon died.

### 4.1 `georeference` — "Georeference maps"
Placing a scanned sheet onto real-world coordinates.

> Draw a 24×24 line icon of a scanned map sheet being pinned to a known
> position. A rectangular sheet frame in `ink` at 1.7 units stroke, with two
> faint interior rules in `scan` suggesting a graticule. Over its lower-right
> quadrant, a survey control-point target in `accent`: a small circle with a
> crosshair whose arms extend just past the circle. The target is the only
> accent element and must be the thing the eye lands on at 24 px. Transparent
> background, no plate.

### 4.2 `digitize_parcels` — "Digitize parcels"
Reading vector boundaries out of raster pixels.

> Draw a 24×24 line icon showing one clean parcel polygon traced over the coarse
> pixels it came from. Behind: four square, axis-aligned blocks in `scan`
> reading as raster pixels — square and aligned, so they read as a raster and
> not as a shadow. In front: a single irregular closed polygon in `ink` at 2.0
> units stroke with visible square vertex handles at its corners, two of those
> handles in `accent`. Do not draw the scan and the result side by side; stack
> them, because two objects competing for 20 pixels both lose.

### 4.3 `validate_deliver` — "Validate & deliver"
Data that was checked against something and is fit to hand over.

> Draw a 24×24 line icon of a shield with a check mark inside it. Shield outline
> in `ink` at 1.8 units; the check in `accent` at 2.4 units, drawn boldly enough
> to survive downscaling. Not a bare tick — a tick alone says "done", and this
> operation's claim is that the data was measured against something, which is
> what the shield carries.

### 4.4 `full_pipeline` — "Full pipeline"
Several stages, in order. The only workflow that is not one operation.

> Draw a 24×24 line icon of three stages chained left to right. Three small
> circles on a horizontal axis; the first two outlined in `ink`, the last in
> `accent` because it is the delivered end. Between them, short connectors in
> `scan` with small chevrons showing direction. The connectors must stop short
> of each circle so the three stay separate objects at 24 px rather than merging
> into a bar. Do not reuse the georeference target or the parcel polygon here —
> a variant of another icon reads as that operation, not as "all of them".

### 4.5 `send` — the composer's submit control
The one control in the panel with no word on it, so the glyph IS the label.

> Draw a 24×24 arrow pointing right, in `accent`, at 2.1 units stroke: one
> horizontal shaft and a chevron head. Make it the most ordinary shape in the
> set — anything clever here is a puzzle in the place a person is trying to
> press Enter.

### 4.6 `new_chat` — start a conversation
> Draw a 24×24 plus sign in `ink` at 2.2 units, centred, with ~4.6 units margin.
> Deliberately not a sheet of paper: QGIS's own `mActionFileNew` says "document"
> beside a control that starts a conversation.

### 4.7 `history` — earlier conversations
> Draw a 24×24 clock: a circle in `ink` at 1.9 units with two hands, the minute
> hand in `accent`. Deliberately not QGIS's `mActionHistory`, whose
> multi-coloured arrow belongs to nothing else in this panel.

## 5. What I did NOT add, and why you may disagree

The mockup for the Task page also showed small icons beside **Project**
(folder), **Source** (document), **Options** (gear) and **Start** (play
triangle). They are not in the build, on the repository's own rule:

> An icon earns its space when it carries STATE — what condition this is in — or
> DESTINATION — where this work runs. Anything a label already says stays a
> label.  *(`mapdex_qgis/branding.py`)*

"Project" beside the word Project is decoration, and at a 396 px dock decoration
is width the map does not get. `test_the_eight_are_eight` enforces the closed
set, so adding them is a deliberate decision that changes that test — not an
oversight. If you want them, say so and they go in with the guard updated.

## 6. Accepting the work

For each icon, before it lands:

1. All four files present, correct sizes, RGBA, transparent background.
2. `_dark` files are the light-coloured variant. Open one on a dark background
   and confirm it is visible.
3. Run `python scripts/make_panel_icons.py` (or the new rasteriser) and read the
   `survives()` line it prints: solid ink at 24 px should be ≥ 50 and the bbox
   should not be clipped at the frame edge.
4. `python -m pytest tests/test_branding.py -q` green.
5. Look at the four workflow cards together at 22 px. The test is whether you
   can tell them apart at a glance — four line icons at that size drift toward
   looking like the same grey smudge, and that is the failure mode to check for.
