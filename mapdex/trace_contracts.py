"""Data models, enums, and configuration contracts for Mapdex AI Trace.

Defines the core data structures for raster-to-vector tracing, geometry
profiling (angular vs curvilinear), trace options, session states, junction
candidates, and benchmark metrics across phases T0 to T10.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Sequence, Tuple


class LayerGeometryKind(str, Enum):
    """Supported vector layer geometry types."""
    POLYGON = "Polygon"
    MULTIPOLYGON = "MultiPolygon"
    LINESTRING = "LineString"
    MULTILINESTRING = "MultiLineString"
    POINT = "Point"
    MULTIPOINT = "MultiPoint"
    UNKNOWN = "Unknown"

    @classmethod
    def from_qgis_layer(cls, layer: Any) -> "LayerGeometryKind":
        """Determine LayerGeometryKind from a QgsVectorLayer or geometry type int/str."""
        if layer is None:
            return cls.UNKNOWN
        
        # If QgsVectorLayer
        wkb_type = getattr(layer, "wkbType", None)
        if callable(wkb_type):
            try:
                # Import dynamically or inspect
                type_val = wkb_type()
                # Check string representation or enum name
                name = getattr(type_val, "name", str(type_val))
                if "MultiPolygon" in name:
                    return cls.MULTIPOLYGON
                if "Polygon" in name:
                    return cls.POLYGON
                if "MultiLineString" in name or "MultiLine" in name:
                    return cls.MULTILINESTRING
                if "LineString" in name or "Line" in name:
                    return cls.LINESTRING
                if "MultiPoint" in name:
                    return cls.MULTIPOINT
                if "Point" in name:
                    return cls.POINT
            except Exception:
                pass

        # Fallback to geometryType()
        geom_type = getattr(layer, "geometryType", None)
        if callable(geom_type):
            try:
                gt = geom_type()
                gt_str = str(gt).lower()
                if "polygon" in gt_str or gt == 2:
                    return cls.POLYGON
                if "line" in gt_str or gt == 1:
                    return cls.LINESTRING
                if "point" in gt_str or gt == 0:
                    return cls.POINT
            except Exception:
                pass

        # Direct string matching
        s = str(layer).strip().lower()
        if "multipolygon" in s:
            return cls.MULTIPOLYGON
        if "polygon" in s:
            return cls.POLYGON
        if "multilinestring" in s or "multiline" in s:
            return cls.MULTILINESTRING
        if "linestring" in s or "line" in s:
            return cls.LINESTRING
        if "multipoint" in s:
            return cls.MULTIPOINT
        if "point" in s:
            return cls.POINT

        return cls.UNKNOWN

    @property
    def is_polygon(self) -> bool:
        return self in (LayerGeometryKind.POLYGON, LayerGeometryKind.MULTIPOLYGON)

    @property
    def is_line(self) -> bool:
        return self in (LayerGeometryKind.LINESTRING, LayerGeometryKind.MULTILINESTRING)

    @property
    def is_supported(self) -> bool:
        return self.is_polygon or self.is_line


class TraceMode(str, Enum):
    """The active tracing mode derived from target layer."""
    POLYGON = "polygon"
    LINESTRING = "linestring"

    @classmethod
    def from_layer_kind(cls, kind: LayerGeometryKind) -> Optional["TraceMode"]:
        if kind.is_polygon:
            return cls.POLYGON
        if kind.is_line:
            return cls.LINESTRING
        return None


class GeometryProfile(str, Enum):
    """Geometry profile used for post-trace refinement."""
    AUTO = "auto"
    ANGULAR = "angular"          # Parcels, cadastral boundaries, buildings, orthogonal features
    CURVILINEAR = "curvilinear"  # Contours, rivers, roads, geological lines, historical linework


class TraceSegmentMethod(str, Enum):
    """Method by which a path segment was generated."""
    LIVEWIRE = "livewire"
    DIRECTIONAL_LIVEWIRE = "directional_livewire"
    HYBRID_NEURAL = "hybrid_neural"
    MANUAL_STRAIGHT = "manual_straight"
    EXISTING_SNAP = "existing_snap"


class ConfidenceLevel(str, Enum):
    """User-facing confidence level."""
    HIGH = "high"
    AMBIGUOUS = "ambiguous"
    LOW = "low"
    VERY_LOW = "very_low"


@dataclass(frozen=True)
class TracePoint:
    """A point in map coordinate space and optionally pixel space."""
    x: float
    y: float
    px: float = 0.0
    py: float = 0.0

    def as_tuple(self) -> Tuple[float, float]:
        return (self.x, self.y)

    def as_pixel_tuple(self) -> Tuple[float, float]:
        return (self.px, self.py)

    def distance_to(self, other: "TracePoint") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def pixel_distance_to(self, other: "TracePoint") -> float:
        return math.hypot(self.px - other.px, self.py - other.py)


@dataclass
class TraceSegment:
    """A continuous traced path between two user anchors."""
    start_point: TracePoint
    end_point: TracePoint
    points: List[TracePoint]
    method: TraceSegmentMethod = TraceSegmentMethod.DIRECTIONAL_LIVEWIRE
    cost: float = 0.0
    confidence: float = 1.0
    is_manual: bool = False

    @property
    def vertex_count(self) -> int:
        return len(self.points)

    @property
    def length_map(self) -> float:
        if len(self.points) < 2:
            return 0.0
        total = 0.0
        for i in range(len(self.points) - 1):
            total += self.points[i].distance_to(self.points[i + 1])
        return total

    @property
    def length_px(self) -> float:
        if len(self.points) < 2:
            return 0.0
        total = 0.0
        for i in range(len(self.points) - 1):
            total += self.points[i].pixel_distance_to(self.points[i + 1])
        return total


@dataclass
class JunctionCandidate:
    """An alternative continuation branch at a detected junction."""
    candidate_id: int
    branch_direction: Tuple[float, float]  # Unit direction (dx, dy)
    confidence: float                     # Score between 0.0 and 1.0
    path_points: List[TracePoint]
    description: str = ""
    rank: int = 1


@dataclass
class TracePath:
    """A full multi-segment trace path being drawn by the user."""
    mode: TraceMode
    segments: List[TraceSegment] = field(default_factory=list)
    anchors: List[TracePoint] = field(default_factory=list)
    active_preview: Optional[TraceSegment] = None
    detected_profile: GeometryProfile = GeometryProfile.AUTO
    is_closed: bool = False

    def all_points(self) -> List[TracePoint]:
        """Flatten all committed points in sequence."""
        pts: List[TracePoint] = []
        for seg in self.segments:
            if not pts:
                pts.extend(seg.points)
            else:
                # Avoid duplicating shared anchor points
                pts.extend(seg.points[1:] if len(seg.points) > 1 else [])
        if self.active_preview and self.active_preview.points:
            if not pts:
                pts.extend(self.active_preview.points)
            else:
                pts.extend(self.active_preview.points[1:] if len(self.active_preview.points) > 1 else [])
        return pts

    def coordinates_list(self) -> List[Tuple[float, float]]:
        """Return list of (x, y) coordinates."""
        return [pt.as_tuple() for pt in self.all_points()]


@dataclass
class TraceOptions:
    """Configurable weights and thresholds for the hybrid trace engine."""
    # Classical edge costs
    weight_ink: float = 0.30
    weight_gradient: float = 0.25
    weight_laplacian: float = 0.10
    
    # Directional & orientation costs
    weight_direction: float = 0.20
    weight_orientation: float = 0.15
    weight_curvature: float = 0.10
    
    # Neural model cost weight
    weight_neural: float = 0.35
    
    # Search & performance tuning
    search_window_padding_px: int = 256
    max_search_radius_px: int = 600
    subpixel_interpolation: bool = True
    
    # Auto-closure & snapping
    auto_close_threshold_px: float = 18.0
    snap_tolerance_px: float = 10.0
    
    # Simplification & Refinement
    angular_corner_threshold_deg: float = 25.0
    orthogonal_snap_tolerance_deg: float = 6.0
    simplification_tolerance_px: float = 0.8
    contour_smoothing_factor: float = 0.6


@dataclass
class TraceMetrics:
    """Metrics measuring trace accuracy, vertex reduction, and latency."""
    wrong_branch_rate: float = 0.0
    path_deviation_rmse_px: float = 0.0
    hausdorff_distance_px: float = 0.0
    clicks_per_1000px: float = 0.0
    vertex_reduction_ratio: float = 0.0
    corner_angle_error_deg: float = 0.0
    invalid_geometry_rate: float = 0.0
    preview_latency_ms: float = 0.0
    graph_init_latency_ms: float = 0.0
    total_scenarios_tested: int = 0
    passed_scenarios: int = 0


@dataclass
class BenchmarkScenario:
    """Specification of a test scenario in the trace benchmark suite."""
    scenario_id: str
    category: str
    title: str
    expected_profile: GeometryProfile
    start_pt: Tuple[float, float]
    end_pt: Tuple[float, float]
    ground_truth_path: List[Tuple[float, float]]
    raster_width: int = 256
    raster_height: int = 256
    difficulty: str = "medium"
    metadata: dict[str, Any] = field(default_factory=dict)
