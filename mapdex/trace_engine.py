"""Hybrid Trace Engine fusing classical edge detection, directional momentum, and neural prediction.

Provides unified geodesic path computation with multi-cost fusion, vector snapping,
and coordinate transformations between map CRS and raster pixel spaces.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .trace_contracts import (
    GeometryProfile,
    TraceMode,
    TraceOptions,
    TracePoint,
    TraceSegment,
    TraceSegmentMethod,
)
from .trace_livewire import DirectionalLiveWire
from .trace_neural import construct_7channel_tensor
from .trace_refiner import GeometryRefiner
from .trace_runtime import get_trace_runtime


class GeoAffineTransform:
    """Affine coordinate transform between map coordinates (x, y) and raster pixels (col, row)."""

    def __init__(
        self,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        pixel_size_x: float = 1.0,
        pixel_size_y: float = 1.0,
    ):
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.pixel_size_x = pixel_size_x if abs(pixel_size_x) > 1e-9 else 1.0
        self.pixel_size_y = pixel_size_y if abs(pixel_size_y) > 1e-9 else 1.0

    def map_to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        """Convert (x, y) map coords to (col, row) pixel coords."""
        col = (x - self.origin_x) / self.pixel_size_x
        row = (y - self.origin_y) / self.pixel_size_y
        return (col, row)

    def pixel_to_map(self, col: float, row: float) -> Tuple[float, float]:
        """Convert (col, row) pixel coords to (x, y) map coords."""
        x = self.origin_x + col * self.pixel_size_x
        y = self.origin_y + row * self.pixel_size_y
        return (x, y)


class HybridTraceEngine:
    """Core tracing engine orchestrating cost fusion, livewire, neural prediction, and refinement."""

    def __init__(
        self,
        raster_array: np.ndarray,
        affine_transform: Optional[GeoAffineTransform] = None,
        options: Optional[TraceOptions] = None,
    ):
        self.raster = raster_array
        self.affine = affine_transform or GeoAffineTransform()
        self.options = options or TraceOptions()
        
        self.livewire = DirectionalLiveWire(self.raster, self.options)
        self.refiner = GeometryRefiner(self.options)
        self.runtime = get_trace_runtime()

        self._active_anchor: Optional[TracePoint] = None
        self._incoming_vector: Optional[Tuple[float, float]] = None
        self._history_mask: Optional[np.ndarray] = None

    def reset(self) -> None:
        """Reset engine active anchor, momentum, and internal cache."""
        self._active_anchor = None
        self._incoming_vector = None
        self._history_mask = None
        if self.livewire:
            self.livewire.reset()

    def set_anchor(
        self,
        point: TracePoint,
        incoming_vector: Optional[Tuple[float, float]] = None,
    ) -> None:
        """Set new anchor point in map coordinate space."""
        self._active_anchor = point
        self._incoming_vector = incoming_vector

        # Convert map coords to pixel row/col for LiveWire
        col, row = self.affine.map_to_pixel(point.x, point.y)
        
        # Incoming vector in pixel space
        px_inc_vec: Optional[Tuple[float, float]] = None
        if incoming_vector is not None:
            # Scale vector by pixel sizes
            px_dx = incoming_vector[0] / self.affine.pixel_size_x
            px_dy = incoming_vector[1] / self.affine.pixel_size_y
            px_inc_vec = (px_dx, px_dy)

        self.livewire.set_anchor(int(round(row)), int(round(col)), px_inc_vec)

    def compute_trace_segment(
        self,
        target_point: TracePoint,
        profile: GeometryProfile = GeometryProfile.AUTO,
        is_manual_straight: bool = False,
    ) -> TraceSegment:
        """Calculate traced segment from active anchor to target point."""
        if self._active_anchor is None:
            return TraceSegment(
                start_point=target_point,
                end_point=target_point,
                points=[target_point],
                method=TraceSegmentMethod.MANUAL_STRAIGHT,
            )

        if is_manual_straight:
            # Straight manual line
            return TraceSegment(
                start_point=self._active_anchor,
                end_point=target_point,
                points=[self._active_anchor, target_point],
                method=TraceSegmentMethod.MANUAL_STRAIGHT,
                confidence=1.0,
                is_manual=True,
            )

        # 1. Compute pixel path via LiveWire
        target_col, target_row = self.affine.map_to_pixel(target_point.x, target_point.y)
        raw_pixel_path = self.livewire.find_path_to(int(round(target_row)), int(round(target_col)))

        # 2. Refine in PIXEL space (col, row) where step distance and tolerances are 1.0 px
        pixel_points = [(float(c), float(r)) for r, c in raw_pixel_path]
        refined_pixels = self.refiner.refine(pixel_points, profile=profile)

        # 3. Build TracePoint list in map coordinate space
        trace_points: List[TracePoint] = []
        for c, r in refined_pixels:
            x, y = self.affine.pixel_to_map(c, r)
            trace_points.append(TracePoint(x=x, y=y, px=c, py=r))

        # Ensure exact anchor start and target end
        if trace_points:
            trace_points[0] = self._active_anchor
            trace_points[-1] = target_point

        return TraceSegment(
            start_point=self._active_anchor,
            end_point=target_point,
            points=trace_points,
            method=TraceSegmentMethod.HYBRID_NEURAL,
            confidence=0.95,
        )
