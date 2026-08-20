"""Tracing a drawn line, on sheets built so the right answer is known.

Every window here is constructed, so what the path SHOULD do is settled before
the code runs. That is the only way to tell a tracer that follows the ink from
one that merely produces a plausible line between two clicks, and the two look
identical in a screenshot.

The load-bearing test is ``test_the_path_follows_the_line_not_the_shortcut``:
if it ever passes with a straight path, the tool has stopped tracing.

What these cannot prove is the canvas half: whether a click lands where the
user pointed, whether the rubber band repaints, whether the raster block read
returns the pixels on screen. Those need a running QGIS and live in
testdata/verify_qgis_end_to_end.py.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

np = pytest.importorskip("numpy", reason="livewire needs numpy, which QGIS ships")

from mapdex_qgis import livewire  # noqa: E402


PAPER = 236
INK = 40


def blank(h=64, w=64, value=PAPER):
    return np.full((h, w), value, dtype=np.uint8)


def horizontal_line(h=64, w=64, row=32, thickness=1, gap=None):
    """A drawn line across the sheet, optionally broken by a gap of N pixels."""
    sheet = blank(h, w)
    half = max(0, thickness // 2)
    sheet[row - half:row + half + 1, :] = INK
    if gap:
        start = w // 2 - gap // 2
        sheet[row - half:row + half + 1, start:start + gap] = PAPER
    return sheet


def elbow(h=64, w=64, row=48, col=48):
    """An L: along the bottom, then up the right. The diagonal is NOT drawn."""
    sheet = blank(h, w)
    sheet[row, :col + 1] = INK
    sheet[:row + 1, col] = INK
    return sheet


class TestCostField:
    def test_blank_paper_has_no_line_and_says_so(self):
        cost, ink = livewire.cost_field(blank(), livewire.TraceOptions())
        assert cost is None
        assert not ink.any()

    def test_ink_is_the_dark_pixels_and_costs_least(self):
        sheet = horizontal_line(thickness=3)
        cost, ink = livewire.cost_field(sheet, livewire.TraceOptions())
        assert ink[32, 10]
        assert not ink[5, 10]
        assert cost[32, 10] < cost[5, 10]
        assert cost.min() == pytest.approx(1.0)

    def test_a_negative_sheet_is_a_declared_choice_not_a_guess(self):
        """White linework on a dark ground inverts the premise entirely."""
        sheet = blank(value=INK)
        sheet[32, :] = PAPER
        default = livewire.cost_field(sheet, livewire.TraceOptions())[1]
        inverted = livewire.cost_field(
            sheet, livewire.TraceOptions(dark_ink=False))[1]
        assert not default[32, 10]
        assert inverted[32, 10]

    def test_dilate_grows_by_the_radius_asked_for(self):
        mask = np.zeros((9, 9), dtype=bool)
        mask[4, 4] = True
        assert livewire.dilate(mask, 0).sum() == 1
        assert livewire.dilate(mask, 1).sum() == 5      # four-connected plus
        assert livewire.dilate(mask, 2).sum() == 13


class TestSeeding:
    def test_a_click_on_the_line_anchors_where_it_was_clicked(self):
        wire = livewire.LiveWire(horizontal_line())
        seed = wire.seed(32, 10)
        assert seed is not None
        assert (seed.y, seed.x) == (32, 10)
        assert seed.moved_px == pytest.approx(0.0)
        assert wire.anchored

    def test_a_click_near_the_line_snaps_onto_it_and_reports_the_move(self):
        """Snapping is the tool working, and the user is told it happened."""
        wire = livewire.LiveWire(horizontal_line(),
                                 livewire.TraceOptions(bridge_px=0))
        seed = wire.seed(29, 10)
        assert seed is not None
        assert seed.y == 32
        assert seed.moved_px == pytest.approx(3.0)

    def test_a_click_on_blank_paper_anchors_nothing(self):
        """Silence would leave the user tracing from a point that is not there."""
        sheet = blank()
        sheet[2, :] = INK                       # a line, but far from the click
        wire = livewire.LiveWire(sheet, livewire.TraceOptions(snap_px=4))
        assert wire.seed(50, 30) is None
        assert not wire.anchored

    def test_a_click_outside_the_window_anchors_nothing(self):
        wire = livewire.LiveWire(horizontal_line())
        assert wire.seed(-1, 10) is None
        assert wire.seed(999, 10) is None


class TestTracing:
    def test_the_path_follows_the_line_not_the_shortcut(self):
        """The reason the module exists.

        The drawn shape is an L and the two clicks are its ends. A tracer
        follows the L. Anything that returns the diagonal has produced a
        plausible line between two clicks and traced nothing.
        """
        sheet = elbow()
        wire = livewire.LiveWire(sheet)
        assert wire.seed(48, 2) is not None
        result = wire.path_to(2, 48)

        assert result.ok, result.reason
        cost, ink = livewire.cost_field(sheet, livewire.TraceOptions())
        on_ink = sum(1 for y, x in result.points if ink[y, x])
        assert on_ink / len(result.points) > 0.98

        # The corner is on the path, and the diagonal shortcut is not.
        assert any(abs(y - 48) <= 1 and abs(x - 48) <= 1 for y, x in result.points)
        straight = livewire.path_length_px([(48, 2), (2, 48)])
        assert livewire.path_length_px(result.points) > straight * 1.3

    def test_a_straight_line_is_traced_straight(self):
        wire = livewire.LiveWire(horizontal_line())
        wire.seed(32, 4)
        result = wire.path_to(32, 60)
        assert result.ok
        assert all(abs(y - 32) <= 1 for y, x in result.points)

    def test_asking_before_anchoring_explains_itself(self):
        wire = livewire.LiveWire(horizontal_line())
        result = wire.path_to(32, 40)
        assert not result.ok
        assert "anchored" in result.reason

    def test_a_small_scan_gap_is_bridged(self):
        """A broken line is the ordinary condition of a scanned sheet."""
        wire = livewire.LiveWire(horizontal_line(gap=2))
        assert wire.seed(32, 4) is not None
        result = wire.path_to(32, 60)
        assert result.ok, result.reason
        assert result.bridged_px >= 1

    def test_a_gap_too_wide_to_bridge_is_refused_not_invented(self):
        """The honest failure.

        Drawing a straight line across a gap the user never drew is inventing
        geometry, and it is invisible in the result. The refusal names the
        condition instead.
        """
        wire = livewire.LiveWire(horizontal_line(w=96, gap=40))
        assert wire.seed(32, 4) is not None
        result = wire.path_to(32, 92)
        assert not result.ok
        assert "does not connect" in result.reason
        assert livewire.densify_straight((32, 4), (32, 92)) == [(32, 4), (32, 92)]

    def test_the_same_window_traces_the_same_path_twice(self):
        sheet = elbow()
        first = livewire.LiveWire(sheet)
        first.seed(48, 2)
        second = livewire.LiveWire(sheet)
        second.seed(48, 2)
        assert first.path_to(2, 48).points == second.path_to(2, 48).points

    def test_one_search_serves_many_cursor_positions(self):
        """The economy the interaction depends on.

        Live-wire is only live because the search runs once per anchor and
        every cursor position after it is a backtrace. If seeding were needed
        per cursor position, the preview could not follow the mouse.
        """
        wire = livewire.LiveWire(horizontal_line())
        wire.seed(32, 4)
        seed_before = wire.current_seed
        for column in (20, 30, 40, 50, 60):
            assert wire.path_to(32, column).ok
        assert wire.current_seed is seed_before


class TestSimplify:
    def test_a_straight_run_collapses_to_its_ends(self):
        points = [(0.0, float(x)) for x in range(20)]
        assert livewire.simplify(points, 1.0) == [(0.0, 0.0), (0.0, 19.0)]

    def test_a_corner_survives(self):
        points = [(0.0, float(x)) for x in range(10)] + \
                 [(float(y), 9.0) for y in range(1, 10)]
        simplified = livewire.simplify(points, 1.0)
        assert (0.0, 9.0) in simplified
        assert len(simplified) == 3

    def test_zero_tolerance_keeps_every_vertex(self):
        points = [(0.0, float(x)) for x in range(20)]
        assert livewire.simplify(points, 0.0) == points

    def test_a_long_path_does_not_exhaust_the_stack(self):
        """Recursive Douglas-Peucker dies on exactly the input this tool makes.

        A traced boundary is one vertex per pixel, so a long sheet-crossing
        line arrives with thousands of them.
        """
        points = [(float(i % 3), float(i)) for i in range(20000)]
        assert len(livewire.simplify(points, 0.5)) < len(points)


class TestOptions:
    def test_the_knobs_are_clamped_rather_than_trusted(self):
        wild = livewire.TraceOptions(max_ink_fraction=9.9, bridge_px=99,
                                     snap_px=-5, paper_penalty=0.0,
                                     simplify_px=-1)
        safe = wild.clamped()
        assert safe.max_ink_fraction <= 0.9
        assert safe.bridge_px <= livewire.MAX_BRIDGE_PX
        assert safe.snap_px >= 0
        assert safe.paper_penalty >= 1.0
        assert safe.simplify_px >= 0.0

    def test_the_ink_cap_bites_when_a_window_is_mostly_line(self):
        """Zoomed into one thick boundary, the window is largely ink.

        Refusing would take the tool away exactly where the user is working, so
        the cap keeps the darkest part instead.
        """
        # Wide enough that the cap is not fighting the ramp's own quantization:
        # at 32 columns the nearest achievable share above 0.20 is 7/32.
        gradient = np.linspace(20, 250, 200, dtype=np.float32)
        sheet = np.tile(gradient, (32, 1)).astype(np.uint8)   # a tonal ramp
        loose = livewire.cost_field(
            sheet, livewire.TraceOptions(max_ink_fraction=0.9))[1]
        capped = livewire.cost_field(
            sheet, livewire.TraceOptions(max_ink_fraction=0.20))[1]
        assert capped.mean() < loose.mean()
        assert capped.mean() <= 0.21

    def test_a_bitonal_window_that_is_mostly_ink_is_accepted_as_it_stands(self):
        """There is no darker subset to keep, so the cap cannot and must not bite.

        Refusing here would take the tool away from a user who has zoomed into
        the boundary they are tracing.
        """
        sheet = blank(32, 32, value=INK)
        sheet[:, 24:] = PAPER            # 75% ink, two tones only
        cost, ink = livewire.cost_field(
            sheet, livewire.TraceOptions(max_ink_fraction=0.35))
        assert cost is not None
        assert ink.mean() == pytest.approx(0.75)

    def test_grain_on_blank_paper_is_not_a_line(self):
        """Otsu answers for any input; separability is what refuses this one."""
        rng = np.random.default_rng(3)
        noise = np.clip(rng.normal(PAPER, 4, (64, 64)), 0, 255).astype(np.uint8)
        cost, ink = livewire.cost_field(noise, livewire.TraceOptions())
        assert cost is None
        assert not ink.any()

    def test_a_real_line_under_the_same_grain_is_still_found(self):
        """The separability floor must refuse noise without refusing a drawing."""
        rng = np.random.default_rng(3)
        sheet = np.clip(rng.normal(PAPER, 4, (64, 64)), 0, 255).astype(np.uint8)
        sheet[32, :] = INK
        cost, ink = livewire.cost_field(sheet, livewire.TraceOptions())
        assert cost is not None
        assert ink[32, 10]
        assert ink.mean() < 0.1
