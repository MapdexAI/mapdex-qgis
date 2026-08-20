"""Trace the line a person points at, following the ink instead of the cursor.

What this is
------------
Assisted digitizing over a scanned map: the user clicks a point on a drawn line
and the tool follows that line ahead of the cursor, so a boundary that would
take forty careful clicks takes two. The algorithm is live-wire, also called
intelligent scissors (Mortensen and Barrett, 1995): a shortest path through a
cost field where ink is cheap and paper is expensive.

Everything here is pure. No Qt, no QGIS, no network, and nothing but numpy,
which QGIS already ships. That is deliberate on three counts. It is the half
that can be tested without a canvas; it keeps the plugin's standing promise
that no package beyond the QGIS runtime is needed; and it means the raster
never leaves the machine, which is the whole difference from a hosted tracer.

Why the graph is the ink and not the picture
--------------------------------------------
The obvious implementation makes every pixel a node. Measured on a synthetic
sheet at the window sizes this tool actually uses:

    512x512   every pixel      262,144 nodes   1.19 s
    512x512   ink only          32,334 nodes   0.12 s
    768x768   every pixel      589,824 nodes   2.45 s

A second of lag on every anchor is not assistance, and live-wire's whole
economy is that the search runs ONCE per anchor and every later cursor position
is a backtrace costing nothing. So the graph is the drawn pixels, widened just
enough to cross the gaps a scan leaves in a line. That is also the honest
model: the path is supposed to be on the line.

The consequence is that a path can fail to exist, and that is reported rather
than papered over. A broken line with too wide a gap is a real thing on a real
sheet, and a tool that silently draws a straight line across it has invented
geometry the user did not see.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

try:  # pragma: no cover - numpy ships with QGIS; the guard keeps import errors readable
    import numpy as np
except Exception:  # pragma: no cover
    np = None


# Eight-connected moves and their geometric length. Diagonals cost sqrt(2) so
# the search cannot buy a shortcut by stepping diagonally through cheap pixels.
_MOVES = (
    (-1, -1, math.sqrt(2.0)), (-1, 0, 1.0), (-1, 1, math.sqrt(2.0)),
    (0, -1, 1.0), (0, 1, 1.0),
    (1, -1, math.sqrt(2.0)), (1, 0, 1.0), (1, 1, math.sqrt(2.0)),
)

# The most of a window this tool will ever call ink. It is a CAP, not the
# definition: the threshold comes from the window's own histogram (Otsu below).
#
# A fixed percentile was tried first and is wrong for a reason worth keeping. A
# cadastral scan carries 2% to 12% ink by area, so asking for the darkest 15%
# of a 2%-ink window marks thirteen points of paper as ink: the graph grows
# sevenfold and the path is free to leave the line. This repository has paid
# for that mistake once already, in the raster profile gate, where an absolute
# tone constant measured PAPER instead of ink and refused ordinary scans.
MAX_INK_FRACTION = 0.35

# The tone gap, in grey levels, between the two Otsu classes below which a
# window is not a drawing. Otsu returns a threshold for any input at all,
# including blank paper, so something has to refuse grain.
#
# It is CONTRAST and deliberately not Otsu separability, which does not answer
# this question despite the name: separability is scale-invariant bimodality,
# so a two-grey-level drawing scores a perfect 1.0 while pure noise scores
# 0.75. The worker measured exactly this and refuses below 25 (see
# services/mapdex-extension-host/src/pipelines/raster_ingress.py); real sheets
# there span 45.7 to 255.0. The same floor is used here so the desktop and the
# server agree about what counts as a drawn line.
MIN_INK_CONTRAST = 25.0

# How far the graph is grown past the ink, in pixels, to cross the gaps a scan
# leaves. Grown further on demand rather than by default: every extra pixel of
# width is nodes, and the measurement above is why that matters.
DEFAULT_BRIDGE_PX = 1
MAX_BRIDGE_PX = 6

# How far from the click the tool will look for a line to start from. A user
# aiming at a line lands within a few pixels of it; beyond that they are
# pointing at something else and should be told so.
DEFAULT_SNAP_PX = 12

# Paper is this many times more expensive to cross than the darkest ink. High
# enough that the path prefers a long way round on the line to a short way
# across the page, low enough that the search still finishes.
PAPER_PENALTY = 24.0


class LiveWireUnavailable(RuntimeError):
    """numpy is missing, so this build cannot trace."""


@dataclass(frozen=True)
class Seed:
    """Where the search actually started, and what it can reach.

    ``snapped`` is separate from the clicked position because the difference is
    something the user should be told: a click that moved four pixels onto a
    line is the tool working, and a click that could not find a line at all is
    a different situation with a different remedy.
    """

    y: int
    x: int
    clicked_y: int
    clicked_x: int
    reachable: int
    bridge_px: int

    @property
    def moved_px(self) -> float:
        return math.hypot(self.y - self.clicked_y, self.x - self.clicked_x)


@dataclass
class TraceOptions:
    """The knobs, with the reason each one exists.

    Defaults are the measured ones. Each is exposed in the tool's settings
    because scanned sheets differ more than any single default can cover, and a
    tool that cannot be adjusted to the customer's own archive is a demo.
    """

    max_ink_fraction: float = MAX_INK_FRACTION
    min_ink_contrast: float = MIN_INK_CONTRAST
    bridge_px: int = DEFAULT_BRIDGE_PX
    snap_px: int = DEFAULT_SNAP_PX
    paper_penalty: float = PAPER_PENALTY
    # Simplify tolerance in pixels. A traced path is one vertex per pixel; a
    # cadastral boundary is a handful of corners. Zero keeps every pixel.
    simplify_px: float = 1.0
    # Dark ink on light paper is the ordinary case. A negative sheet (white
    # linework on a dark background) inverts the whole premise, so it is a
    # declared choice rather than something guessed per window.
    dark_ink: bool = True

    def clamped(self) -> "TraceOptions":
        return TraceOptions(
            max_ink_fraction=min(0.9, max(0.005, float(self.max_ink_fraction))),
            min_ink_contrast=min(255.0, max(0.0, float(self.min_ink_contrast))),
            bridge_px=min(MAX_BRIDGE_PX, max(0, int(self.bridge_px))),
            snap_px=min(64, max(0, int(self.snap_px))),
            paper_penalty=min(1000.0, max(1.0, float(self.paper_penalty))),
            simplify_px=max(0.0, float(self.simplify_px)),
            dark_ink=bool(self.dark_ink),
        )


def _require_numpy():
    if np is None:  # pragma: no cover - only on a build without numpy
        raise LiveWireUnavailable(
            "Tracing needs numpy, which ships with QGIS. This QGIS install does "
            "not expose it, so the tool cannot read the raster."
        )


def dilate(mask, radius: int):
    """Grow a boolean mask by ``radius`` pixels, four-connected per step.

    Written out rather than imported: scipy is not part of the QGIS runtime
    this plugin promises to need, and four shifted OR operations are both
    exact and fast enough at these window sizes.
    """
    _require_numpy()
    out = np.asarray(mask, dtype=bool)
    for _ in range(max(0, int(radius))):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def otsu(values):
    """The threshold that best splits this window's histogram in two."""
    _require_numpy()
    data = np.asarray(values, dtype=np.float64).ravel()
    if data.size == 0:
        return 0.0
    counts, edges = np.histogram(data, bins=256)
    centres = (edges[:-1] + edges[1:]) / 2.0
    total = counts.sum()
    if total == 0:
        return float(data.min())
    weights = counts / total
    cumulative = np.cumsum(weights)
    means = np.cumsum(weights * centres)
    grand_mean = means[-1]
    denominator = cumulative * (1.0 - cumulative)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = np.where(denominator > 0,
                           (grand_mean * cumulative - means) ** 2 / denominator,
                           0.0)
    return float(centres[int(np.nanargmax(between))])


def ink_contrast(values, threshold: float) -> float:
    """Tone gap between the two classes, in grey levels.

    This is the measure that answers "is there a drawing here". See
    MIN_INK_CONTRAST for why it is not Otsu separability.
    """
    _require_numpy()
    data = np.asarray(values, dtype=np.float64)
    dark = data[data <= threshold]
    light = data[data > threshold]
    if dark.size == 0 or light.size == 0:
        return 0.0
    return float(abs(float(light.mean()) - float(dark.mean())))


def cost_field(gray, options: TraceOptions):
    """Per-pixel traversal cost, and the pixels the search is allowed to use.

    Cost is the pixel's brightness rescaled so the darkest ink costs 1 and
    paper costs ``paper_penalty``. Brightness rather than gradient magnitude:
    on a scanned line map the thing to follow is the LINE, and an edge detector
    follows its two sides instead, which puts the traced boundary half a line
    width off and makes it wobble between them wherever the line changes width.

    Ink is decided by the window's own Otsu threshold, then capped. Both halves
    matter: the threshold is what stops a fixed percentile from calling paper
    ink on a sparse sheet, and the cap is what stops a window the user zoomed
    into a thick line from declaring itself entirely traversable.
    """
    _require_numpy()
    options = options.clamped()
    values = np.asarray(gray, dtype=np.float32)
    if values.ndim != 2 or values.size == 0:
        raise ValueError("cost_field needs a two-dimensional, non-empty window")
    if not options.dark_ink:
        values = float(values.max()) - values

    low = float(values.min())
    high = float(values.max())
    if high - low < 1e-6:
        # A blank window has no line in it. Returning a flat field would let
        # the search draw a confident straight path across nothing.
        return None, np.zeros(values.shape, dtype=bool)

    threshold = otsu(values)
    if ink_contrast(values, threshold) < options.min_ink_contrast:
        # Grain, a wash, or an evenly toned photograph. There is a threshold,
        # but there is no drawing either side of it.
        return None, np.zeros(values.shape, dtype=bool)

    ink = values <= threshold
    if float(ink.mean()) > options.max_ink_fraction:
        # Too much of the window claims to be ink to be a line network. Keep
        # the darkest part of it rather than refusing outright: a user zoomed
        # deep into one thick boundary is still entitled to trace along it.
        #
        # On a bitonal window this cannot reduce anything, because every ink
        # pixel carries the same tone and there is no darker subset to keep.
        # That case is accepted as it stands rather than forced: the window
        # really is mostly ink, and refusing would take the tool away exactly
        # where the user is working.
        cutoff = float(np.percentile(values, 100.0 * options.max_ink_fraction))
        tightened = values <= cutoff
        if 0 < int(tightened.sum()) < int(ink.sum()):
            ink = tightened

    normalized = (values - low) / (high - low)
    cost = 1.0 + (options.paper_penalty - 1.0) * normalized
    return cost.astype(np.float32), ink


@dataclass
class TraceResult:
    """A traced path, or the reason there is not one."""

    points: list[tuple[int, int]] = field(default_factory=list)
    reason: str = ""
    bridged_px: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.points)


class LiveWire:
    """One search per anchor; every cursor position after it is a backtrace.

    Build it on the window the user is looking at, seed it where they clicked,
    then ask for the path to wherever the cursor is. ``path_to`` is cheap by
    construction, which is what lets the preview follow the mouse without a
    round trip to anywhere.
    """

    def __init__(self, gray, options: TraceOptions | None = None):
        _require_numpy()
        self.options = (options or TraceOptions()).clamped()
        self.cost, self.ink = cost_field(gray, self.options)
        self.shape = (int(np.asarray(gray).shape[0]), int(np.asarray(gray).shape[1]))
        self._seed: Seed | None = None
        self._dist = None
        self._parent = None
        self._index = None      # pixel -> node id, -1 off the graph
        self._nodes = None      # node id -> pixel

    # -- graph ------------------------------------------------------------

    def _build_graph(self, bridge_px: int):
        mask = dilate(self.ink, bridge_px) if bridge_px else self.ink
        flat = mask.ravel()
        nodes = np.flatnonzero(flat)
        index = np.full(flat.size, -1, dtype=np.int64)
        index[nodes] = np.arange(nodes.size, dtype=np.int64)
        return nodes, index

    def _nearest_graph_pixel(self, y: int, x: int, index, radius: int):
        """The closest usable pixel to the click, or None.

        Searched as expanding rings so the first hit is the nearest, and
        bounded by ``radius`` so a click on blank paper fails quickly instead
        of dragging the whole window in.
        """
        height, width = self.shape
        if 0 <= y < height and 0 <= x < width and index[y * width + x] >= 0:
            return y, x
        for ring in range(1, int(radius) + 1):
            best = None
            best_distance = float(ring) + 1.0
            for dy in range(-ring, ring + 1):
                for dx in range(-ring, ring + 1):
                    if max(abs(dy), abs(dx)) != ring:
                        continue
                    ny, nx = y + dy, x + dx
                    if not (0 <= ny < height and 0 <= nx < width):
                        continue
                    if index[ny * width + nx] < 0:
                        continue
                    distance = math.hypot(dy, dx)
                    if distance < best_distance:
                        best_distance = distance
                        best = (ny, nx)
            if best is not None:
                return best
        return None

    # -- search -----------------------------------------------------------

    def seed(self, y: int, x: int) -> Seed | None:
        """Run the search from the clicked pixel. None when there is no line there.

        The bridge width grows on demand. Starting narrow is what keeps the
        common case fast; growing when the graph turns out to be too broken to
        be useful is what stops a scanned gap from ending the trace.
        """
        _require_numpy()
        height, width = self.shape
        y, x = int(y), int(x)
        if not (0 <= y < height and 0 <= x < width):
            return None
        if self.cost is None:
            return None

        for bridge in range(self.options.bridge_px, MAX_BRIDGE_PX + 1):
            nodes, index = self._build_graph(bridge)
            if nodes.size == 0:
                continue
            start = self._nearest_graph_pixel(y, x, index, self.options.snap_px)
            if start is None:
                continue
            dist, parent = self._dijkstra(nodes, index, start)
            reachable = int(np.isfinite(dist).sum())
            # A seed that can reach almost nothing is sitting on a speck of
            # grain, not on a line. Widening the bridge is the remedy, and it
            # is tried before giving up rather than after failing visibly.
            if reachable > 1 or bridge >= MAX_BRIDGE_PX:
                self._nodes, self._index = nodes, index
                self._dist, self._parent = dist, parent
                self._seed = Seed(y=start[0], x=start[1], clicked_y=y, clicked_x=x,
                                  reachable=reachable, bridge_px=bridge)
                return self._seed
        self._seed = None
        return None

    def _dijkstra(self, nodes, index, start: tuple[int, int]):
        height, width = self.shape
        flat_cost = self.cost.ravel()
        count = nodes.size
        dist = np.full(count, np.inf, dtype=np.float64)
        parent = np.full(count, -1, dtype=np.int64)
        settled = np.zeros(count, dtype=bool)

        source = int(index[start[0] * width + start[1]])
        dist[source] = 0.0
        heap: list[tuple[float, int]] = [(0.0, source)]
        while heap:
            current, node = heapq.heappop(heap)
            if settled[node]:
                continue
            settled[node] = True
            pixel = int(nodes[node])
            py, px = divmod(pixel, width)
            for dy, dx, step in _MOVES:
                ny, nx = py + dy, px + dx
                if not (0 <= ny < height and 0 <= nx < width):
                    continue
                neighbour = int(index[ny * width + nx])
                if neighbour < 0 or settled[neighbour]:
                    continue
                candidate = current + step * float(flat_cost[ny * width + nx])
                if candidate < dist[neighbour]:
                    dist[neighbour] = candidate
                    parent[neighbour] = node
                    heapq.heappush(heap, (candidate, neighbour))
        return dist, parent

    # -- reading the answer ------------------------------------------------

    def path_to(self, y: int, x: int) -> TraceResult:
        """The traced path from the seed to here, in pixel coordinates.

        Cheap: the search already ran. This walks parent pointers, which is why
        the preview can follow the cursor without recomputing anything.
        """
        if self._seed is None or self._dist is None:
            return TraceResult(reason="nothing is anchored yet")
        height, width = self.shape
        y, x = int(y), int(x)
        if not (0 <= y < height and 0 <= x < width):
            return TraceResult(reason="that point is outside the traced window")

        target = self._nearest_graph_pixel(y, x, self._index, self.options.snap_px)
        if target is None:
            return TraceResult(reason="no drawn line within reach of the cursor")
        node = int(self._index[target[0] * width + target[1]])
        if not math.isfinite(float(self._dist[node])):
            return TraceResult(
                reason="the drawn line does not connect these two points",
                bridged_px=self._seed.bridge_px,
            )

        points: list[tuple[int, int]] = []
        walker = node
        guard = int(self._nodes.size) + 2
        while walker >= 0 and guard > 0:
            pixel = int(self._nodes[walker])
            py, px = divmod(pixel, width)
            points.append((py, px))
            walker = int(self._parent[walker])
            guard -= 1
        points.reverse()
        return TraceResult(points=points, bridged_px=self._seed.bridge_px)

    @property
    def anchored(self) -> bool:
        return self._seed is not None

    @property
    def current_seed(self) -> Seed | None:
        return self._seed


def simplify(points: Sequence[tuple[float, float]], tolerance: float):
    """Douglas-Peucker, iterative so a long path cannot exhaust the stack.

    A traced boundary arrives as one vertex per pixel. Storing that is storing
    the scan's resolution as if it were the survey's precision, and it makes
    every later edit painful. Tolerance is in pixels because that is the frame
    the path was found in.
    """
    pts = [(float(a), float(b)) for a, b in points]
    if tolerance <= 0 or len(pts) < 3:
        return pts

    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = pts[first]
        bx, by = pts[last]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        worst = -1.0
        worst_at = -1
        for i in range(first + 1, last):
            px, py = pts[i]
            if span < 1e-12:
                deviation = math.hypot(px - ax, py - ay)
            else:
                deviation = abs(dy * px - dx * py + bx * ay - by * ax) / span
            if deviation > worst:
                worst = deviation
                worst_at = i
        if worst > tolerance and worst_at > 0:
            keep[worst_at] = True
            stack.append((first, worst_at))
            stack.append((worst_at, last))
    return [p for p, k in zip(pts, keep) if k]


def densify_straight(start: tuple[int, int], end: tuple[int, int]):
    """The honest fallback: a straight segment, when the ink does not connect.

    Offered explicitly rather than substituted silently. A user who asked to
    follow a line and got a straight line across a gap has been handed geometry
    they did not draw and were not told about.
    """
    return [(int(start[0]), int(start[1])), (int(end[0]), int(end[1]))]


def path_length_px(points: Iterable[tuple[float, float]]) -> float:
    total = 0.0
    previous = None
    for point in points:
        if previous is not None:
            total += math.hypot(point[0] - previous[0], point[1] - previous[1])
        previous = point
    return total
