"""The toolbar carries the Mapdex mark, in the variant the interface needs.

The plugin put the INDIGO symbol on its toolbar button, its Measure and Draw
actions and its layer-menu entries. MEMORY hard rule 27 makes the black/white
symbol the default identity and reserves the blue one for explicit accent
placements, so that was the wrong mark - and it was also one picture for every
theme QGIS ships, on a bar whose background the user chooses.

Two things have to hold and neither is visible from the other side alone: the
files have to be the kit geometry rather than something redrawn, and the code
has to actually ask for the variant that suits the palette.
"""
import ast
import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mapdex_qgis"
sys.path.insert(0, str(ROOT))

from mapdex_qgis import branding  # noqa: E402
from mapdex_qgis.generated_contracts import BatchKind  # noqa: E402

PLUGIN = (PACKAGE / "plugin.py").read_text(encoding="utf-8")
REPORT_ASSETS = ROOT.parents[1] / "services" / "mapdex-extension-host" / "src" / "report" / "assets"


def _method(name: str) -> str:
    for node in ast.walk(ast.parse(PLUGIN)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(PLUGIN, node) or ""
    raise AssertionError("plugin.py has no {!r}".format(name))


def _png(path: pathlib.Path):
    """Size and the set of opaque fill colours, without a Pillow dependency.

    The test suite runs in CI with no image library, and the one thing worth
    asserting about these two files is that they are the same shape in two
    colours. That is readable straight out of the IDAT.
    """
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", path
    width, height, depth, colour = struct.unpack(">IIBB", raw[16:26])
    assert (depth, colour) == (8, 6), "expected 8-bit RGBA, got {}".format((depth, colour))
    data = b"".join(
        raw[i + 8:i + 8 + struct.unpack(">I", raw[i:i + 4])[0]]
        for i in _chunks(raw, b"IDAT")
    )
    pixels = zlib.decompress(data)
    stride = width * 4
    fills, alpha = set(), []
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        line = _unfilter(pixels[start], bytearray(pixels[start + 1:start + 1 + stride]), previous)
        for x in range(0, stride, 4):
            r, g, b, a = line[x:x + 4]
            alpha.append(a)
            if a > 200:
                fills.add((r, g, b))
        previous = line
    return {"size": (width, height), "fills": fills, "alpha": alpha}


def _chunks(raw: bytes, kind: bytes):
    offset = 8
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        if raw[offset + 4:offset + 8] == kind:
            yield offset
        offset += 12 + length


def _unfilter(kind: int, line: bytearray, previous: bytearray) -> bytearray:
    """One PNG scanline, RFC 2083 filter types 0-4, 4 bytes per pixel."""
    for i in range(len(line)):
        left = line[i - 4] if i >= 4 else 0
        up = previous[i]
        upper_left = previous[i - 4] if i >= 4 else 0
        if kind == 1:
            line[i] = (line[i] + left) & 0xFF
        elif kind == 2:
            line[i] = (line[i] + up) & 0xFF
        elif kind == 3:
            line[i] = (line[i] + (left + up) // 2) & 0xFF
        elif kind == 4:
            # Paeth: the neighbour closest to left + up - upper_left, with ties
            # broken left, then up, then upper_left.
            estimate = left + up - upper_left
            best = min(
                (abs(estimate - left), 0, left),
                (abs(estimate - up), 1, up),
                (abs(estimate - upper_left), 2, upper_left),
            )[2]
            line[i] = (line[i] + best) & 0xFF
    return line


# -- the files are the kit, not a redrawing -------------------------------

def test_the_two_marks_are_one_shape_in_two_colours():
    """DESIGN.md forbids redrawing the mark, so these must be the same geometry.

    Identical alpha with different fills is exactly that, and it is also what
    makes the pair swappable: anything else would move the mark when the theme
    changes.
    """
    light = _png(pathlib.Path(branding.mark_path(dark_interface=False)))
    dark = _png(pathlib.Path(branding.mark_path(dark_interface=True)))
    assert light["size"] == dark["size"], (light["size"], dark["size"])
    assert light["alpha"] == dark["alpha"], "the two marks are not the same shape"
    assert light["fills"] == {(0, 0, 0)}, sorted(light["fills"])[:4]
    assert dark["fills"] == {(255, 255, 255)}, sorted(dark["fills"])[:4]


def test_the_marks_came_from_the_report_assets_unchanged():
    """One source of truth for the symbol. If the kit file moves or is
    replaced, this fails rather than letting the plugin drift into carrying its
    own private copy of the brand."""
    if not REPORT_ASSETS.is_dir():
        # The public plugin repository ships without the monorepo around it.
        return
    pairs = ((False, "mapdex-logo-black.png"), (True, "mapdex-logo-white.png"))
    for dark, name in pairs:
        source = REPORT_ASSETS / name
        if not source.is_file():
            continue
        assert pathlib.Path(branding.mark_path(dark)).read_bytes() == source.read_bytes(), (
            "{} has drifted from {}".format(branding.mark_asset(dark), source)
        )


# -- the dark file is the light-coloured one ------------------------------

def test_the_dark_suffix_means_for_a_dark_interface():
    """Read the other way round this paints a black mark on a black toolbar,
    which on screen is indistinguishable from an icon that failed to load."""
    assert _png(pathlib.Path(branding.mark_path(True)))["fills"] == {(255, 255, 255)}
    assert _png(pathlib.Path(branding.mark_path(False)))["fills"] == {(0, 0, 0)}


def test_the_naming_matches_the_convention_themed_asset_icon_uses():
    """`themed_asset_icon` builds the dark name by suffixing the stem. A file
    named anything else is simply never found, and the fallback hides it."""
    stem, _, extension = branding.MARK_FOR_LIGHT_INTERFACE.rpartition(".")
    assert branding.MARK_FOR_DARK_INTERFACE == "{}_dark.{}".format(stem, extension)


# -- the code asks for it -------------------------------------------------

def test_no_action_inside_qgis_carries_the_accent_mark():
    """`plugin_icon()` is the indigo symbol. It keeps two jobs - the plugin
    manager listing, and the last-resort fallback - and neither is a button."""
    for method in ("initGui", "_install_measure_action", "_install_draw_action",
                   "_install_layer_menu_actions"):
        assert "plugin_icon()" not in _method(method), method
        assert "mapdex_mark_icon()" in _method(method), method


def test_the_listing_icon_is_still_the_one_metadata_names():
    """metadata.txt points plugins.qgis.org at a file; renaming it silently
    would leave the listing with no icon at all."""
    metadata = (ROOT / "metadata.txt").read_text(encoding="utf-8")
    assert "icon={}".format(branding.LISTING_MARK) in metadata, metadata
    assert (PACKAGE / branding.LISTING_MARK).is_file()
    assert "branding.LISTING_MARK" in _method("plugin_icon")


def test_the_mark_inherits_the_existing_fallback_chain():
    """A QIcon with no file draws nothing, and an invisible toolbar button is
    worse than a wrongly coloured one."""
    assert "themed_asset_icon(" in _method("mapdex_mark_icon")


def test_a_palette_change_re_chooses_every_mark():
    """A theme sampled once at build time is not theme-aware. QGIS can be
    switched to Night Mapping with the plugin already loaded, and on Windows
    and macOS the system can darken the application with no QGIS setting
    changing at all."""
    body = _method("_reicon_actions")
    assert "mapdex_mark_icon()" in body and "setIcon" in body, body
    assert "_measure_action" in body and "_draw_action" in body, (
        "a palette change re-iconed some actions and left others"
    )
    assert "_layer_menu_actions" in body, body
    assert "on_palette_change = self._reicon_actions" in PLUGIN, (
        "nothing subscribes to the palette change, so the refresh never runs"
    )


def test_the_panel_forwards_the_palette_change():
    panel = (PACKAGE / "panel.py").read_text(encoding="utf-8")
    assert "def changeEvent" in panel
    assert "PaletteChange" in panel
    assert "on_palette_change" in panel


def test_a_panel_glyph_is_chosen_for_the_surface_not_the_interface():
    """The panel paints #191919 whatever theme QGIS is wearing.

    Choosing by the INTERFACE palette put a near-black glyph on that ground on
    every light QGIS theme - measured at relative luminance 0.06 against the
    surface's own 0.09, which on screen is an icon that failed to load. The
    toolbar keeps the interface rule, because that bar really is whatever
    colour the theme makes it.
    """
    assert "surface_asset_icon(asset)" in _method("_action_icon"), (
        "panel chips follow the interface again, so they vanish on a light theme"
    )
    assert "themed_asset_icon(" in _method("mapdex_mark_icon"), (
        "the toolbar mark stopped following the interface"
    )
    body = _method("surface_asset_icon")
    assert "_dark" in body, body


def test_the_eight_are_eight():
    """An icon earns its place by carrying state or destination. The set grew
    once already, from four to nine, and every addition was decoration on a
    word that was already clear."""
    assert len(branding.SEVERITY_ICONS) == 3, sorted(branding.SEVERITY_ICONS)
    # The rule rather than a count. The action set is exactly the four pieces
    # of work that leave this machine, plus send - so it is derived from
    # BatchKind, and a fifth workflow arrives with a glyph or fails here. The
    # count was `== 4` and went red for `full_pipeline`, which is not the
    # decoration this guard exists to stop: the Task page looks an icon up by
    # the workflow's own id, so three of four workflows having one is the
    # defect, not the fix.
    workflow_keys = {
        value for name, value in vars(BatchKind).items() if not name.startswith("_")
    }
    assert set(branding.ACTION_ICONS) == workflow_keys | {"send"}, sorted(branding.ACTION_ICONS)
    assert "info" not in branding.SEVERITY_ICONS, (
        "info is the absence of a problem; a glyph there adds nothing the "
        "sentence does not say, and QGIS draws it as a speech bubble beside a "
        "line that is already a message"
    )


def test_every_drawn_glyph_has_both_variants_on_disk():
    for asset in branding.ACTION_ICONS.values():
        stem, _, extension = asset.rpartition(".")
        for name in (asset, "{}_dark.{}".format(stem, extension)):
            assert (PACKAGE / "assets" / name).is_file(), name


def test_the_drawn_glyphs_are_generated_rather_than_mystery_binaries():
    generator = ROOT / "scripts" / "make_panel_icons.py"
    assert generator.is_file()
    body = generator.read_text(encoding="utf-8")
    # Matched on the FILE this entry names, against the generator's own GLYPHS
    # keys. The old check looked for the dictionary key as a substring, which
    # passed for "digitize_parcels" on the strength of "digitize" appearing
    # somewhere in the file - so a hand-dropped binary whose name shared a word
    # with a real glyph would have satisfied it.
    for work, filename in branding.ACTION_ICONS.items():
        stem = filename.rsplit(".", 1)[0]
        assert '"{}"'.format(stem) in body, (
            "{} names {}, which the generator does not draw".format(work, filename)
        )


def test_the_assistant_avatar_is_left_alone():
    """Nivo's avatar is its own artwork and is not the corporate mark. This
    change was about the toolbar identity only."""
    panel = (PACKAGE / "panel.py").read_text(encoding="utf-8")
    assert "NIVO_AVATAR_PATH" in panel
    assert (PACKAGE / "assets" / "assistant" / "nivo.png").is_file()
