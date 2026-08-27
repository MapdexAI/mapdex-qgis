"""Directional LiveWire pathfinding engine.

Implements multi-feature cost surfaces, structure-tensor local orientation,
directional edge penalization, and contour-preserving Dijkstra search for
high-precision raster linework digitization.
"""
from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from .trace_contracts import (
    TraceOptions,
    TracePoint,
    TraceSegment,
    TraceSegmentMethod,
)


def _ensure_2d_float(raster_array: np.ndarray) -> np.ndarray:
    """Convert input raster image into a 2D float32 array in range [0, 1]."""
    arr = np.asarray(raster_array, dtype=np.float32)
    if arr.ndim == 3:
        # If RGB/RGBA, calculate luminance: 0.299 R + 0.587 G + 0.114 B
        if arr.shape[2] >= 3:
            arr = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        else:
            arr = arr[:, :, 0]
    elif arr.ndim != 2:
        raise ValueError(f"Expected 2D or 3D raster array, got shape {arr.shape}")

    min_val = float(arr.min())
    max_val = float(arr.max())
    if max_val > min_val:
        arr = (arr - min_val) / (max_val - min_val)
    else:
        arr = np.zeros_like(arr, dtype=np.float32)
    return arr


def _gaussian_blur(img: np.ndarray, sigma: float = 0.8) -> np.ndarray:
    """Fast separable Gaussian blur using SciPy/vectorized NumPy."""
    try:
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(img, sigma=sigma).astype(np.float32)
    except Exception:
        pass
    try:
        import cv2
        return cv2.GaussianBlur(img, (5, 5), sigma).astype(np.float32)
    except Exception:
        pass
    # Pure vectorized NumPy 1D separable convolution using slice operations
    p = np.pad(img, ((1, 1), (1, 1)), mode="edge")
    h = (p[:, :-2] + 2.0 * p[:, 1:-1] + p[:, 2:]) * 0.25
    v = (h[:-2, :] + 2.0 * h[1:-1, :] + h[2:, :]) * 0.25
    return v.astype(np.float32)


def compute_derivatives_and_gradients(img: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute smoothed horizontal/vertical derivatives and gradient magnitude."""
    # Scharr kernels for superior rotation invariance
    # Gx kernel: [[-3, 0, 3], [-10, 0, 10], [-3, 0, 3]] / 32
    padded = np.pad(img, ((1, 1), (1, 1)), mode="edge")
    
    # Fast vectorized convolution for 3x3 Scharr
    gx = (
        3.0 * (padded[0:-2, 2:] - padded[0:-2, 0:-2]) +
        10.0 * (padded[1:-1, 2:] - padded[1:-1, 0:-2]) +
        3.0 * (padded[2:, 2:] - padded[2:, 0:-2])
    ) / 32.0

    gy = (
        3.0 * (padded[2:, 0:-2] - padded[0:-2, 0:-2]) +
        10.0 * (padded[2:, 1:-1] - padded[0:-2, 1:-1]) +
        3.0 * (padded[2:, 2:] - padded[0:-2, 2:])
    ) / 32.0

    grad_mag = np.hypot(gx, gy).astype(np.float32)
    max_g = float(grad_mag.max())
    if max_g > 1e-6:
        grad_mag /= max_g
    return gx, gy, grad_mag


def compute_structure_tensor_orientation(gx: np.ndarray, gy: np.ndarray, sigma: float = 1.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute structure tensor orientation field and coherence.
    
    Returns:
        tangent_x: cos(tangent_angle)
        tangent_y: sin(tangent_angle)
        coherence: local anisotropy [0, 1]
    """
    j_xx = _gaussian_blur(gx * gx, sigma)
    j_yy = _gaussian_blur(gy * gy, sigma)
    j_xy = _gaussian_blur(gx * gy, sigma)

    # Orientation angle of dominant eigenvector
    # Normal to gradient is the line tangent
    theta = 0.5 * np.arctan2(2.0 * j_xy, j_xx - j_yy) + (np.pi / 2.0)
    
    tangent_x = np.cos(theta).astype(np.float32)
    tangent_y = np.sin(theta).astype(np.float32)

    # Coherence (anisotropy)
    diff = j_xx - j_yy
    trace = j_xx + j_yy + 1e-6
    coherence = np.sqrt(diff * diff + 4.0 * j_xy * j_xy) / trace
    coherence = np.clip(coherence, 0.0, 1.0).astype(np.float32)
    return tangent_x, tangent_y, coherence


def compute_static_cost_map(
    img: np.ndarray,
    options: TraceOptions,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute base static cost map supporting both grayscale and multi-channel RGB data."""
    arr = np.asarray(img, dtype=np.float32)
    
    if arr.ndim == 3 and arr.shape[2] >= 3:
        # Multi-channel color image (RGB)
        smoothed_rgb = np.zeros_like(arr[:, :, :3], dtype=np.float32)
        for c in range(3):
            smoothed_rgb[:, :, c] = _gaussian_blur(arr[:, :, c], 0.6)

        # Di Zenzo multi-channel color structure tensor & gradient
        gry, grx = np.gradient(smoothed_rgb[:, :, 0])
        ggy, ggx = np.gradient(smoothed_rgb[:, :, 1])
        gby, gbx = np.gradient(smoothed_rgb[:, :, 2])

        j_xx = _gaussian_blur(grx * grx + ggx * ggx + gbx * gbx, 0.8)
        j_yy = _gaussian_blur(gry * gry + ggy * ggy + gby * gby, 0.8)
        j_xy = _gaussian_blur(grx * gry + ggx * ggy + gbx * gby, 0.8)

        grad_mag = np.sqrt(np.maximum(0.0, j_xx + j_yy)).astype(np.float32)
        p_grad = float(np.percentile(grad_mag, 98))
        if p_grad > 1e-4:
            norm_grad = np.clip(grad_mag / p_grad, 0.0, 1.0)
        else:
            norm_grad = grad_mag

        theta = 0.5 * np.arctan2(2.0 * j_xy, j_xx - j_yy) + (np.pi / 2.0)
        tx = np.cos(theta).astype(np.float32)
        ty = np.sin(theta).astype(np.float32)

        diff = j_xx - j_yy
        trace = j_xx + j_yy + 1e-6
        coherence = np.clip(np.sqrt(diff * diff + 4.0 * j_xy * j_xy) / trace, 0.0, 1.0).astype(np.float32)

        edge_cost = (1.0 - norm_grad) ** 3.0

        # Luminance channel for linework
        gray = 0.299 * smoothed_rgb[:, :, 0] + 0.587 * smoothed_rgb[:, :, 1] + 0.114 * smoothed_rgb[:, :, 2]
        p1, p99 = float(np.percentile(gray, 2)), float(np.percentile(gray, 98))
        if p99 - p1 > 1e-4:
            norm_gray = np.clip((gray - p1) / (p99 - p1), 0.0, 1.0)
        else:
            norm_gray = gray
        mean_val = float(np.mean(norm_gray))
        ink = (1.0 - norm_gray) if mean_val < 0.5 else norm_gray
        line_cost = ink ** 2.0

        static_cost = np.minimum(line_cost, edge_cost)
        static_cost = np.clip(static_cost, 0.001, 1.0).astype(np.float32)
        return static_cost, tx, ty, coherence

    # Single-band / 2D grayscale image
    if arr.ndim == 3:
        arr = arr[:, :, 0]
    smoothed = _gaussian_blur(arr, 0.5)

    p1, p99 = float(np.percentile(smoothed, 1)), float(np.percentile(smoothed, 99))
    if p99 - p1 > 1e-4:
        norm_img = np.clip((smoothed - p1) / (p99 - p1), 0.0, 1.0)
    else:
        norm_img = np.clip(smoothed, 0.0, 1.0)

    gx, gy, grad_mag = compute_derivatives_and_gradients(norm_img)
    tx, ty, coherence = compute_structure_tensor_orientation(gx, gy, sigma=0.8)

    mean_val = float(np.mean(norm_img))
    ink_norm = (1.0 - norm_img) if mean_val < 0.5 else norm_img

    p_grad = float(np.percentile(grad_mag, 98))
    if p_grad > 1e-4:
        norm_grad = np.clip(grad_mag / p_grad, 0.0, 1.0)
    else:
        norm_grad = grad_mag

    line_cost = ink_norm ** 2.0
    edge_cost = (1.0 - norm_grad) ** 3.0

    static_cost = np.minimum(line_cost, edge_cost)
    static_cost = np.clip(static_cost, 0.001, 1.0).astype(np.float32)
    return static_cost, tx, ty, coherence


# 8-connected neighbor offsets: (dr, dc, step_distance, direction_angle)
_NEIGHBORS_8 = [
    (-1, 0, 1.0, -np.pi / 2.0),
    (1, 0, 1.0, np.pi / 2.0),
    (0, -1, 1.0, np.pi),
    (0, 1, 1.0, 0.0),
    (-1, -1, math.sqrt(2.0), -3.0 * np.pi / 4.0),
    (-1, 1, math.sqrt(2.0), -np.pi / 4.0),
    (1, -1, math.sqrt(2.0), 3.0 * np.pi / 4.0),
    (1, 1, math.sqrt(2.0), np.pi / 4.0),
]


class DirectionalLiveWire:
    """Directional LiveWire graph solver.
    
    Builds cost surfaces and finds optimal smooth geodesic boundaries with
    directional momentum and contour curvature tolerance.
    """

    def __init__(self, raster_array: np.ndarray, options: Optional[TraceOptions] = None):
        self.options = options or TraceOptions()
        arr = np.asarray(raster_array, dtype=np.float32)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            self.raw_image = arr[:, :, :3]
            self.height, self.width = self.raw_image.shape[0], self.raw_image.shape[1]
        else:
            self.raw_image = _ensure_2d_float(raster_array)
            self.height, self.width = self.raw_image.shape

        self.static_cost, self.tx, self.ty, self.coherence = compute_static_cost_map(
            self.raw_image, self.options
        )

        self._current_anchor: Optional[Tuple[int, int]] = None
        self._incoming_vector: Optional[Tuple[float, float]] = None

    def reset(self) -> None:
        """Reset current anchor and state cache."""
        self._current_anchor = None
        self._incoming_vector = None

    def snap_to_ridge(self, r: int, c: int, radius: int = 5) -> Tuple[int, int]:
        """Snap coordinate to local ink minimum if current point is on background."""
        r = max(0, min(self.height - 1, int(round(r))))
        c = max(0, min(self.width - 1, int(round(c))))
        
        # If already on a low-cost ridge/ink line, keep exact point
        if self.static_cost[r, c] < 0.15:
            return (r, c)

        r_min = max(0, r - radius)
        r_max = min(self.height, r + radius + 1)
        c_min = max(0, c - radius)
        c_max = min(self.width, c + radius + 1)
        sub = self.static_cost[r_min:r_max, c_min:c_max]
        min_idx = int(np.argmin(sub))
        lr = min_idx // sub.shape[1]
        lc = min_idx % sub.shape[1]
        best_r = r_min + lr
        best_c = c_min + lc
        if self.static_cost[best_r, best_c] < self.static_cost[r, c] * 0.7:
            return (best_r, best_c)
        return (r, c)

    def set_anchor(
        self,
        anchor_row: int,
        anchor_col: int,
        incoming_vector: Optional[Tuple[float, float]] = None,
        search_radius_px: Optional[int] = None,
    ) -> None:
        """Set active anchor point in pixel coordinates."""
        sr, sc = self.snap_to_ridge(anchor_row, anchor_col, radius=5)
        self._current_anchor = (sr, sc)
        self._incoming_vector = incoming_vector

    def find_path_to(
        self,
        target_row: int,
        target_col: int,
    ) -> List[Tuple[float, float]]:
        """Find optimal geodesic path from anchor to target using A* graph search."""
        if self._current_anchor is None:
            return [(float(target_row), float(target_col))]

        start_r, start_c = self._current_anchor
        target_row = max(0, min(self.height - 1, int(round(target_row))))
        target_col = max(0, min(self.width - 1, int(round(target_col))))

        if start_r == target_row and start_c == target_col:
            return [(float(start_r), float(start_c))]

        # Local search window
        margin = 96
        r_min = max(0, min(start_r, target_row) - margin)
        r_max = min(self.height, max(start_r, target_row) + margin + 1)
        c_min = max(0, min(start_c, target_col) - margin)
        c_max = min(self.width, max(start_c, target_col) + margin + 1)

        win_h = r_max - r_min
        win_w = c_max - c_min

        # Distance array and parent map
        dist = np.full((win_h, win_w), np.inf, dtype=np.float32)
        parent = np.full((win_h, win_w), -1, dtype=np.int32)

        start_lr = start_r - r_min
        start_lc = start_c - c_min
        target_lr = target_row - r_min
        target_lc = target_col - c_min

        dist[start_lr, start_lc] = 0.0

        # Admissible heuristic: Euclidean distance * minimum possible step cost
        min_cost = 0.001
        h_start = math.hypot(target_lr - start_lr, target_lc - start_lc) * min_cost

        # Priority queue: (f_score, g_cost, local_r, local_c, in_dx, in_dy)
        in_dx, in_dy = 0.0, 0.0
        if self._incoming_vector is not None:
            norm = math.hypot(self._incoming_vector[0], self._incoming_vector[1])
            if norm > 1e-5:
                in_dx, in_dy = self._incoming_vector[0] / norm, self._incoming_vector[1] / norm

        pq: List[Tuple[float, float, int, int, float, float]] = [
            (h_start, 0.0, start_lr, start_lc, in_dx, in_dy)
        ]

        w_static = 1.0 - (self.options.weight_direction + self.options.weight_orientation)
        w_dir = self.options.weight_direction
        w_orient = self.options.weight_orientation

        static_sub = self.static_cost[r_min:r_max, c_min:c_max]
        tx_sub = self.tx[r_min:r_max, c_min:c_max]
        ty_sub = self.ty[r_min:r_max, c_min:c_max]
        coh_sub = self.coherence[r_min:r_max, c_min:c_max]

        steps = 0
        max_steps = min(30000, win_h * win_w)
        reached = False

        while pq and steps < max_steps:
            steps += 1
            f_curr, g_curr, lr, lc, curr_dx, curr_dy = heapq.heappop(pq)
            if g_curr > dist[lr, lc] + 1e-6:
                continue

            if lr == target_lr and lc == target_lc:
                reached = True
                break

            for dr, dc, step_dist, _angle in _NEIGHBORS_8:
                nlr = lr + dr
                nlc = lc + dc
                if nlr < 0 or nlr >= win_h or nlc < 0 or nlc >= win_w:
                    continue

                step_ux = dc / step_dist
                step_uy = dr / step_dist

                c_dir = 0.0
                if abs(curr_dx) > 1e-4 or abs(curr_dy) > 1e-4:
                    cos_alpha = curr_dx * step_ux + curr_dy * step_uy
                    if cos_alpha >= 0.0:
                        c_dir = (1.0 - cos_alpha) * 0.5
                    else:
                        c_dir = 0.5 + (abs(cos_alpha) * 1.5)

                t_x = tx_sub[nlr, nlc]
                t_y = ty_sub[nlr, nlc]
                c_orient = (1.0 - abs(step_ux * t_x + step_uy * t_y)) * coh_sub[nlr, nlc]

                c_stat = static_sub[nlr, nlc]
                edge_cost = (
                    w_static * c_stat +
                    w_dir * c_dir +
                    w_orient * c_orient
                ) * step_dist

                new_g = g_curr + edge_cost
                if new_g < dist[nlr, nlc]:
                    dist[nlr, nlc] = new_g
                    parent[nlr, nlc] = lr * win_w + lc
                    h = math.hypot(target_lr - nlr, target_lc - nlc) * min_cost
                    heapq.heappush(pq, (new_g + h, new_g, nlr, nlc, step_ux, step_uy))

        if not reached and math.isinf(dist[target_lr, target_lc]):
            # Straight line fallback if unreachable
            return [
                (float(start_r), float(start_c)),
                (float(target_row), float(target_col)),
            ]

        # Backtrack path
        path: List[Tuple[float, float]] = []
        curr_idx = target_lr * win_w + target_lc
        start_idx = start_lr * win_w + start_lc

        safety = 0
        while curr_idx != -1 and curr_idx != start_idx and safety < max_steps:
            lr = curr_idx // win_w
            lc = curr_idx % win_w
            path.append((float(r_min + lr), float(c_min + lc)))
            curr_idx = int(parent[lr, lc])
            safety += 1

        path.append((float(start_r), float(start_c)))
        path.reverse()
        if path:
            path[0] = (float(start_r), float(start_c))
            path[-1] = (float(target_row), float(target_col))
        return path


def path_length_px(path: Sequence[Tuple[float, float]]) -> float:
    """Calculate total Euclidean length in pixels along polyline."""
    if len(path) < 2:
        return 0.0
    total = 0.0
    for i in range(len(path) - 1):
        total += math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
    return total
