"""Executes validated capability requests against the live QGIS project.

This is the only module that turns a decision into a real QGIS effect. It
receives requests that the registry has already validated, so it never parses
model output and never sees a raw string as a target: every layer is resolved by
its stable project id.

Reading is deliberately frugal. Attribute analytics fetch only the columns the
analysis needs and skip geometry entirely (``NoGeometry``), because pulling full
features off a large PostGIS or GeoPackage layer to compute one mean is what
makes an assistant unusable on a real project. Row counts are capped and the cap
is reported, so a partial answer says it is partial instead of quietly
describing a sample as the whole dataset.

Reversible presentation changes push their previous state onto an undo stack
before they apply, so "geri al" restores what was actually there rather than an
approximation the model remembers.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from ._vendor.nivo import (
    analytics,
    coordinates,
    intersect,
    postgis,
    presentation,
    spatial,
    survey,
)
from ._vendor.nivo.capabilities import CapabilityError
from .guard import log_debug

# Attribute analytics stream this many features at most. Above the cap the
# answer states that it was truncated.
MAX_ANALYTIC_FEATURES = 200_000
MAX_UNDO_DEPTH = 20
# A pair operation compares every candidate against every reference geometry.
# Bounding the PRODUCT rather than either side is what stops two individually
# reasonable layers from multiplying into a frozen host application; the result
# reports the truncation instead of describing a partial answer as the whole.
MAX_PAIR_TESTS = 5_000_000


class RuntimeUnavailable(Exception):
    """A QGIS object the request needs is missing or no longer valid."""


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeUnavailable(message)


class QGISRuntime:
    """Binds validated capability requests to QGIS objects.

    ``iface`` and ``project`` are injected rather than imported so the class can
    be constructed in a test with fakes; the QGIS-only symbols are imported
    lazily inside the methods that need them.
    """

    def __init__(self, iface: Any, project: Any, on_message: Callable[[str], None] | None = None):
        self.iface = iface
        self.project = project
        self.on_message = on_message or (lambda _text: None)
        self._undo: list[dict[str, Any]] = []

    # -- resolution --------------------------------------------------------

    def layer(self, layer_id: str) -> Any:
        """Resolve a layer by its stable project id, never by display name."""
        found = self.project.mapLayer(str(layer_id or ""))
        _require(found is not None, "that layer is no longer in the project")
        _require(found.isValid(), "that layer is not valid")
        return found

    def _is_vector(self, layer: Any) -> bool:
        return hasattr(layer, "fields") and hasattr(layer, "getFeatures")

    def vector(self, layer_id: str) -> Any:
        layer = self.layer(layer_id)
        _require(self._is_vector(layer), "that operation needs a vector layer")
        return layer

    def field_names(self, layer: Any) -> list[str]:
        return [field.name() for field in layer.fields()]

    def _field_index(self, layer: Any, field: str) -> int:
        index = layer.fields().indexOf(field)
        _require(index >= 0, "the field '{}' is not in that layer".format(field))
        return index

    # -- bounded reading ---------------------------------------------------

    def records(self, layer: Any, fields: list[str], scope: str = "all") -> dict[str, Any]:
        """Fetch only the needed columns, honouring the requested scope.

        ``selection`` and ``viewport`` are the two scopes that make follow-up
        questions work ("now just the ones I selected", "only what I'm looking
        at") without the user restating the whole request.
        """
        from qgis.core import QgsFeatureRequest  # noqa: PLC0415 - QGIS-only import

        from .qt_compat import enum_member

        indexes = [self._field_index(layer, name) for name in fields]
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes(indexes)
        # Geometry is the expensive part of a feature and no attribute
        # statistic needs it. PyQt6 requires the scoped ``Flag`` path; PyQt5
        # accepts the flat form, so this is resolved rather than hardcoded.
        request.setFlags(enum_member(QgsFeatureRequest, "Flag", "NoGeometry"))
        if scope == "viewport":
            extent = self._viewport_in_layer_crs(layer)
            if extent is not None:
                request.setFilterRect(extent)
        selected_ids = None
        if scope == "selection":
            selected_ids = set(layer.selectedFeatureIds())
            if not selected_ids:
                raise RuntimeUnavailable("nothing is selected on that layer")
        rows: list[dict[str, Any]] = []
        truncated = False
        for feature in layer.getFeatures(request):
            if selected_ids is not None and feature.id() not in selected_ids:
                continue
            if len(rows) >= MAX_ANALYTIC_FEATURES:
                truncated = True
                break
            row: dict[str, Any] = {"id": feature.id()}
            for name in fields:
                row[name] = feature[name]
            rows.append(row)
        return {"rows": rows, "truncated": truncated, "scope": scope}

    def _viewport_in_layer_crs(self, layer: Any) -> Any:
        from qgis.core import QgsCoordinateTransform, QgsCsException  # noqa: PLC0415

        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        source = canvas.mapSettings().destinationCrs()
        target = layer.crs()
        if not source.isValid() or not target.isValid() or source == target:
            return extent
        try:
            return QgsCoordinateTransform(source, target, self.project).transformBoundingBox(extent)
        except QgsCsException:
            # An untransformable viewport must not silently become the whole
            # layer: that would answer a "in this view" question with every row.
            raise RuntimeUnavailable("the map view cannot be transformed into that layer's CRS")

    # -- analytics ---------------------------------------------------------

    def profile_layer(self, layer_id: str) -> dict[str, Any]:
        layer = self.layer(layer_id)
        crs = layer.crs()
        info: dict[str, Any] = {
            "kind": "layer_profile",
            "name": layer.name(),
            "crs": crs.authid() if crs.isValid() else "",
            "crs_is_geographic": bool(crs.isGeographic()) if crs.isValid() else None,
        }
        extent = layer.extent()
        info["extent"] = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        if self._is_vector(layer):
            info["type"] = "vector"
            info["feature_count"] = layer.featureCount()
            info["selected"] = layer.selectedFeatureCount()
            info["fields"] = [
                {"name": field.name(), "type": field.typeName()} for field in layer.fields()
            ]
        else:
            info["type"] = "raster"
            info["width"] = getattr(layer, "width", lambda: 0)()
            info["height"] = getattr(layer, "height", lambda: 0)()
            info["bands"] = getattr(layer, "bandCount", lambda: 0)()
            # A raster with no valid CRS is the georeferencing prerequisite the
            # extraction workflow checks, so it is reported here explicitly.
            info["georeferenced"] = bool(crs.isValid()) and not extent.isEmpty()
        return info

    def numeric(self, layer_id: str, field: str, scope: str = "all") -> dict[str, Any]:
        layer = self.vector(layer_id)
        data = self.records(layer, [field], scope)
        result = analytics.numeric_summary(row[field] for row in data["rows"])
        result["truncated"] = data["truncated"]
        result["scope"] = scope
        return result

    def categories(self, layer_id: str, field: str, limit: int = 25, scope: str = "all") -> dict[str, Any]:
        layer = self.vector(layer_id)
        data = self.records(layer, [field], scope)
        result = analytics.categorical_summary((row[field] for row in data["rows"]), limit)
        result["truncated_read"] = data["truncated"]
        return result

    def histogram(self, layer_id: str, field: str, bins: int = 12) -> dict[str, Any]:
        layer = self.vector(layer_id)
        data = self.records(layer, [field], "all")
        return analytics.histogram((row[field] for row in data["rows"]), bins)

    def top_n(self, layer_id: str, field: str, limit: int = 10, ascending: bool = False) -> dict[str, Any]:
        layer = self.vector(layer_id)
        data = self.records(layer, [field], "all")
        return analytics.top_n(data["rows"], field, limit, ascending)

    def outliers(self, layer_id: str, field: str, method: str = "iqr") -> dict[str, Any]:
        layer = self.vector(layer_id)
        data = self.records(layer, [field], "all")
        return analytics.outliers(data["rows"], field, method)

    def group(
        self,
        layer_id: str,
        group_field: str,
        value_field: str | None = None,
        statistic: str = "count",
    ) -> dict[str, Any]:
        layer = self.vector(layer_id)
        needed = [group_field] + ([value_field] if value_field else [])
        data = self.records(layer, needed, "all")
        return analytics.group_aggregate(data["rows"], group_field, value_field, statistic)

    def compare(self, layer_id: str, field: str, scope: str = "selection_vs_all") -> dict[str, Any]:
        layer = self.vector(layer_id)
        whole = self.records(layer, [field], "all")
        left_scope = "selection" if scope == "selection_vs_all" else "viewport"
        subset = self.records(layer, [field], left_scope)
        return analytics.compare_distributions(
            (row[field] for row in subset["rows"]),
            (row[field] for row in whole["rows"]),
        )

    def field_profile(self, layer_id: str) -> dict[str, Any]:
        """Field list with per-column null counts - what to analyse next."""
        layer = self.vector(layer_id)
        names = self.field_names(layer)
        data = self.records(layer, names, "all")
        columns = []
        for name in names:
            values = [row[name] for row in data["rows"]]
            numbers, nulls = analytics.numeric_values(values)
            distinct = len({str(value) for value in values if value is not None})
            columns.append({
                "name": name,
                "nulls": nulls,
                "distinct": distinct,
                # A column is treated as numeric only when most of its values
                # actually parse; a mostly-empty numeric column is not a
                # candidate for a graduated renderer.
                "numeric": bool(numbers) and len(numbers) >= max(1, len(values) // 2),
            })
        return {"kind": "field_profile", "fields": columns, "features": len(data["rows"])}

    # -- measuring the geometry -------------------------------------------

    def _crs_map_units(self, crs: Any) -> str:
        """The layer's map unit as a plain string spatial.py can reason about."""
        try:
            from qgis.core import QgsUnitTypes  # noqa: PLC0415 - QGIS-only import

            return QgsUnitTypes.toString(crs.mapUnits())
        except Exception:  # noqa: BLE001 - a missing accessor must not lose the answer
            return "degrees" if crs.isGeographic() else "m"

    def _extent_in_wgs84(self, layer: Any) -> list[float] | None:
        """The layer extent as WGS84 bounds, used only to choose a projection."""
        extent = layer.extent()
        if extent is None or extent.isEmpty():
            return None
        bounds = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        crs = layer.crs()
        if crs.isValid() and not crs.isGeographic():
            return None
        return bounds

    def _geometry_dimension(self, geometry: Any) -> int:
        """0 for a point, 1 for a line, 2 for a polygon.

        Read from the abstract geometry rather than from a GeometryType enum
        member: that enum moved between QGIS 3 and QGIS 4 and the flat spelling
        raises AttributeError on PyQt6, in a path no unit test would open.
        """
        try:
            inner = geometry.get()
            if inner is not None:
                return int(inner.dimension())
        except Exception:  # noqa: BLE001  # nosec B110
            # Deliberately silent: this runs once per feature while filtering a
            # layer, and the second route below is the expected answer on builds
            # where the abstract geometry is not reachable. Logging a normal
            # fallback per feature would bury the log it belongs in.
            pass
        try:
            return int(geometry.type())
        except Exception:  # noqa: BLE001
            return -1

    def measure_geometry(
        self,
        layer_id: str,
        metric: str = "area",
        statistic: str = "sum",
        limit: int = 5,
        scope: str = "all",
    ) -> dict[str, Any]:
        """Measure the shapes, and say in what frame.

        Every number here comes off the geometry. Nothing consults a column,
        which is the whole point: a layer's attribute named `real` or `alan_m2`
        may or may not be its area, and the geometry always is.

        Features whose geometry cannot carry the measure - a point has no area -
        are counted as skipped rather than measured as zero, so a mixed layer
        reports "3 of 10 measured" instead of a total that quietly treats seven
        points as polygons.
        """
        metric = spatial.normalize_measure(metric)
        if not metric:
            raise CapabilityError("I can measure area, length or perimeter.")
        layer = self.vector(layer_id)
        crs = layer.crs()
        plan = spatial.plan_geometry_measurement(
            bool(crs.isGeographic()) if crs.isValid() else False,
            self._crs_map_units(crs) if crs.isValid() else "",
            metric,
            self._extent_in_wgs84(layer),
        )
        if plan.get("strategy") == "unsupported":
            # Refuse and say why. A figure in an unknown scale is worse than no
            # figure, because it cannot be recognised as wrong.
            raise CapabilityError(
                "I can't state a {} for this layer: its CRS units are {}, which I cannot "
                "convert to metres.".format(metric, plan.get("crs_unit") or "unknown"))

        transform, calculator = self._measurement_frame(layer, plan)
        wanted = spatial.MEASURE_DIMENSION[metric]
        label_field = self._display_field(layer)

        measurements: list[dict[str, Any]] = []
        truncated = False
        for feature in self._geometry_features(layer, label_field, scope):
            if len(measurements) >= MAX_ANALYTIC_FEATURES:
                truncated = True
                break
            geometry = feature.geometry()
            value = None
            if geometry is not None and not geometry.isEmpty() \
                    and self._geometry_dimension(geometry) == wanted:
                value = self._measure_one(geometry, metric, plan, transform, calculator)
            measurements.append({
                "id": feature.id(),
                "name": str(feature[label_field]) if label_field else "",
                "value": value,
            })

        result = spatial.summarize_geometry(measurements, statistic, limit)
        result["metric"] = metric
        result["unit"] = plan["unit"]
        result["layer"] = layer.name()
        result["scope"] = scope
        result["truncated"] = truncated
        result["method"] = "ellipsoidal" if plan["strategy"] == "ellipsoidal" else "planar"
        result["measured_in"] = plan.get("crs") or (crs.authid() if crs.isValid() else "")
        result["frame"] = self._frame_note(plan, crs)
        if result["skipped"]:
            result["skipped_reason"] = "no_{}".format(metric)
        return result

    def _frame_note(self, plan: Mapping[str, Any], crs: Any) -> str:
        """One sentence naming how the figure was obtained.

        Not decoration. The same polygon measured planar in UTM and on the
        ellipsoid differs by about a tenth of a percent, and a reader cannot
        tell the two apart from the number alone.
        """
        authid = crs.authid() if crs.isValid() else "an unknown CRS"
        if plan["strategy"] == "project":
            return ("measured in {} because {} is a geographic CRS, where the figure would "
                    "otherwise be in degrees".format(plan["crs"], authid))
        if plan["strategy"] == "ellipsoidal":
            return ("measured on the ellipsoid because {} is a geographic CRS and the layer is "
                    "too wide for one projection".format(authid))
        return "measured in the layer's own projection, {}".format(authid)

    def _measurement_frame(self, layer: Any, plan: Mapping[str, Any]):
        """The transform and/or calculator the chosen strategy needs."""
        if plan["strategy"] == "project":
            from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform  # noqa: PLC0415

            target = QgsCoordinateReferenceSystem(plan["crs"])
            _require(target.isValid(), "this build cannot use {}".format(plan["crs"]))
            return QgsCoordinateTransform(layer.crs(), target, self.project), None
        if plan["strategy"] == "ellipsoidal":
            from qgis.core import QgsDistanceArea  # noqa: PLC0415

            calculator = QgsDistanceArea()
            calculator.setSourceCrs(layer.crs(), self.project.transformContext())
            calculator.setEllipsoid(self.project.ellipsoid() or "WGS84")
            return None, calculator
        return None, None

    def _measure_one(
        self,
        geometry: Any,
        metric: str,
        plan: Mapping[str, Any],
        transform: Any,
        calculator: Any,
    ) -> float | None:
        if plan["strategy"] == "ellipsoidal":
            if metric == "area":
                return float(calculator.measureArea(geometry))
            if metric == "perimeter":
                return float(calculator.measurePerimeter(geometry))
            return float(calculator.measureLength(geometry))
        if transform is not None:
            # Clone before transforming: QgsGeometry.transform mutates in place
            # and the feature's own geometry must not be left reprojected.
            from qgis.core import QgsCsException, QgsGeometry  # noqa: PLC0415

            geometry = QgsGeometry(geometry)
            try:
                if geometry.transform(transform) != 0:
                    return None
            except QgsCsException:
                # One untransformable feature is skipped and counted, not
                # allowed to abandon the whole measurement.
                return None
        raw = geometry.area() if metric == "area" else geometry.length()
        return float(raw) * float(plan.get("factor", 1.0))

    def _display_field(self, layer: Any) -> str:
        """The layer's own display field, so a ranked feature has a name.

        Taken from the layer rather than chosen here or supplied by a model:
        naming the largest polygon "BIT Systems" instead of "feature 1" is worth
        having, and guessing which column is the name is not.
        """
        try:
            name = str(layer.displayField() or "")
        except Exception:  # noqa: BLE001
            return ""
        return name if name in self.field_names(layer) else ""

    def _geometry_features(self, layer: Any, label_field: str, scope: str):
        """Features WITH geometry, bounded and scoped like records()."""
        from qgis.core import QgsFeatureRequest  # noqa: PLC0415

        request = QgsFeatureRequest()
        if label_field:
            request.setSubsetOfAttributes([self._field_index(layer, label_field)])
        elif hasattr(request, "setNoAttributes"):
            request.setNoAttributes()
        if scope == "viewport":
            extent = self._viewport_in_layer_crs(layer)
            if extent is not None:
                request.setFilterRect(extent)
        selected_ids = None
        if scope == "selection":
            selected_ids = set(layer.selectedFeatureIds())
            if not selected_ids:
                raise RuntimeUnavailable("nothing is selected on that layer")
        for feature in layer.getFeatures(request):
            if selected_ids is not None and feature.id() not in selected_ids:
                continue
            yield feature

    # -- distance correctness ---------------------------------------------

    def distance_plan(self, layer_id: str, distance: float, unit: str = "m") -> dict[str, Any]:
        """Resolve a real-world distance against the layer's own CRS."""
        layer = self.layer(layer_id)
        crs = layer.crs()
        units = ""
        try:
            from qgis.core import QgsUnitTypes  # noqa: PLC0415

            units = QgsUnitTypes.toString(crs.mapUnits())
        except Exception:
            units = "degrees" if crs.isGeographic() else "m"
        return spatial.plan_distance(bool(crs.isGeographic()), units, distance, unit)

    # -- spatial relationships between two layers --------------------------
    #
    # Two engines, on purpose. GEOS (through QgsGeometry) decides whether two
    # shapes intersect, because a hand-written predicate would be a second and
    # worse implementation of a solved problem. `spatial.py` decides what a
    # distance or an area MEANS, because that is where the mistakes actually
    # live: a 2 km search radius on a layer in degrees is not a small error, it
    # is a number with no meaning, and QGIS will compute it without complaint.
    #
    # Two layers in one project rarely share a CRS, so every pair operation
    # transforms the second into the first and reports which frame it used. The
    # transform is applied to the geometries being compared rather than to the
    # layer being scanned, so a feature request can still be pre-filtered.

    _PREDICATES = ("intersects", "within", "contains", "overlaps", "touches", "crosses", "disjoint")

    def _crs_transform(self, source: Any, target: Any) -> Any:
        """Transform from `source` to `target`, or None when they already agree."""
        if source is None or target is None:
            return None
        if not source.isValid() or not target.isValid() or source == target:
            return None
        from qgis.core import QgsCoordinateTransform  # noqa: PLC0415

        return QgsCoordinateTransform(source, target, self.project)

    def _geometries_in_crs(self, layer: Any, target_crs: Any, scope: str = "all") -> list[Any]:
        """Every geometry of `layer`, reprojected into `target_crs`.

        Bounded by the same cap as the attribute analytics. An unbounded pair
        operation over two large layers is not slow, it is a hung QGIS, and this
        runs inside the host application.
        """
        from qgis.core import QgsCsException, QgsGeometry  # noqa: PLC0415

        transform = self._crs_transform(layer.crs(), target_crs)
        out: list[Any] = []
        for feature in self._geometry_features(layer, "", scope):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if transform is not None:
                geometry = QgsGeometry(geometry)
                try:
                    if geometry.transform(transform) != 0:
                        continue
                except QgsCsException:
                    # One untransformable feature is dropped; a whole layer that
                    # cannot be transformed produces an empty list, which the
                    # callers below refuse rather than answer.
                    continue
            out.append(geometry)
            if len(out) >= MAX_ANALYTIC_FEATURES:
                break
        return out

    @staticmethod
    def _combined_bbox(geometries: list[Any]) -> Any:
        from qgis.core import QgsRectangle  # noqa: PLC0415

        box = QgsRectangle()
        box.setMinimal()
        for geometry in geometries:
            box.combineExtentWith(geometry.boundingBox())
        return box

    def _relate_pair(self, geometry: Any, others: list[Any], predicate: str) -> bool:
        """Does `geometry` stand in `predicate` to ANY of `others`?

        Any, not all: "the parcels that intersect the flood layer" means any
        polygon of it, which is also what QGIS's own select-by-location means.
        `disjoint` is the negation of that and so is the one predicate that must
        test every candidate before it can answer.
        """
        box = geometry.boundingBox()
        if predicate == "disjoint":
            for other in others:
                if box.intersects(other.boundingBox()) and geometry.intersects(other):
                    return False
            return True
        for other in others:
            if not box.intersects(other.boundingBox()):
                continue
            if getattr(geometry, predicate)(other):
                return True
        return False

    def relate(
        self,
        layer_id: str,
        other_layer_id: str,
        predicate: str = "intersects",
        use_selection: bool = False,
    ) -> dict[str, Any]:
        """Select features of one layer by their relationship to another."""
        layer = self.vector(layer_id)
        other = self.vector(other_layer_id)
        _require(layer.id() != other.id(), "a spatial relationship needs two different layers")
        name = str(predicate or "intersects").strip().lower()
        if name not in self._PREDICATES:
            raise CapabilityError("{} is not a spatial relationship this build tests".format(name))
        others = self._geometries_in_crs(other, layer.crs(), "selection" if use_selection else "all")
        _require(
            others,
            "{} has no geometry that can be compared in {}'s coordinate system".format(other.name(), layer.name()),
        )
        matched: list[Any] = []
        tested = 0
        truncated = False
        for feature in self._geometry_features(layer, "", "all"):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            tested += 1
            if tested * len(others) > MAX_PAIR_TESTS:
                truncated = True
                break
            if self._relate_pair(geometry, others, name):
                matched.append(feature.id())
        applied = self.select_features(layer_id, matched, zoom=bool(matched))
        return {
            "kind": "spatial_selection",
            "predicate": name,
            "layer": layer.name(),
            "other_layer": other.name(),
            "compared_in": layer.crs().authid(),
            "reference_features": len(others),
            "tested": tested,
            "matched": len(matched),
            "truncated": truncated,
            "selection": applied,
        }

    def near(
        self,
        layer_id: str,
        other_layer_id: str,
        distance: float,
        unit: str = "m",
    ) -> dict[str, Any]:
        """Select features within a real-world distance of another layer."""
        layer = self.vector(layer_id)
        other = self.vector(other_layer_id)
        _require(layer.id() != other.id(), "a distance search needs two different layers")
        plan = self.distance_plan(layer_id, distance, unit)
        strategy = str(plan.get("strategy") or "")
        if strategy == "reproject":
            # Deliberately a refusal rather than a degree approximation. There
            # is no single degrees-per-metre factor: it is latitude-dependent
            # and anisotropic, so any answer here would be wrong everywhere
            # except one parallel, and would look exactly like a right one.
            raise RuntimeUnavailable(
                "{} is in degrees, so {} {} cannot be measured on it. Reproject it to a projected "
                "CRS and ask again.".format(layer.name(), distance, spatial.normalize_unit(unit))
            )
        if strategy != "map_units":
            raise RuntimeUnavailable(
                "that distance cannot be applied to {}: {}".format(layer.name(), plan.get("reason") or "unknown")
            )
        radius = float(plan["map_units"])
        others = self._geometries_in_crs(other, layer.crs(), "all")
        _require(
            others,
            "{} has no geometry that can be compared in {}'s coordinate system".format(other.name(), layer.name()),
        )
        search = self._combined_bbox(others)
        search.grow(radius)
        matched: list[Any] = []
        tested = 0
        truncated = False
        for feature in self._geometry_features(layer, "", "all"):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if not search.intersects(geometry.boundingBox()):
                continue
            tested += 1
            if tested * len(others) > MAX_PAIR_TESTS:
                truncated = True
                break
            box = geometry.boundingBox()
            for reference in others:
                probe = reference.boundingBox()
                probe.grow(radius)
                if not probe.intersects(box):
                    continue
                if geometry.distance(reference) <= radius:
                    matched.append(feature.id())
                    break
        applied = self.select_features(layer_id, matched, zoom=bool(matched))
        return {
            "kind": "spatial_selection",
            "predicate": "within_distance",
            "layer": layer.name(),
            "other_layer": other.name(),
            "distance": plan.get("requested"),
            "unit": plan.get("unit"),
            "metres": plan.get("metres"),
            "map_units": radius,
            "crs_unit": plan.get("crs_unit"),
            "compared_in": layer.crs().authid(),
            "reference_features": len(others),
            "tested": tested,
            "matched": len(matched),
            "truncated": truncated,
            "selection": applied,
        }

    def _nearest_reference(self, layer: Any) -> tuple[Any, str]:
        """What "nearest" is measured from, and a phrase naming it.

        A selection on exactly one other layer is unambiguous and is preferred.
        Otherwise the map centre is used, and the result says so, because an
        answer whose origin the reader cannot see is not an answer.
        """
        from qgis.core import QgsGeometry  # noqa: PLC0415

        candidates = []
        for other in self.project.mapLayers().values():
            if other.id() == layer.id() or not self._is_vector(other):
                continue
            if not other.selectedFeatureIds():
                continue
            candidates.append(other)
        if len(candidates) == 1:
            source = candidates[0]
            geometries = self._geometries_in_crs(source, layer.crs(), "selection")
            if geometries:
                return (
                    QgsGeometry.collectGeometry(geometries),
                    "the {} feature(s) selected on {}".format(len(geometries), source.name()),
                )
        # `center()` already returns a QgsPointXY; re-wrapping it would rely on
        # a copy constructor for no gain.
        point = QgsGeometry.fromPointXY(self.iface.mapCanvas().center())
        transform = self._crs_transform(self.iface.mapCanvas().mapSettings().destinationCrs(), layer.crs())
        if transform is not None:
            point.transform(transform)
        return point, "the centre of the current map view"

    def nearest(self, layer_id: str, limit: int = 10) -> dict[str, Any]:
        """Find and select the N nearest features to a stated reference."""
        layer = self.vector(layer_id)
        reference, described = self._nearest_reference(layer)
        _require(reference is not None and not reference.isEmpty(), "there is nothing to measure distance from")
        count = max(1, min(500, int(limit or 10)))
        ranked: list[tuple[float, Any]] = []
        for feature in self._geometry_features(layer, "", "all"):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            ranked.append((float(geometry.distance(reference)), feature.id()))
        _require(ranked, "{} has no geometry to rank".format(layer.name()))
        ranked.sort(key=lambda item: item[0])
        chosen = ranked[:count]
        applied = self.select_features(layer_id, [identifier for _distance, identifier in chosen], zoom=True)
        units = self._crs_map_units(layer.crs())
        return {
            "kind": "nearest",
            "layer": layer.name(),
            "reference": described,
            "measured_in": units,
            "requested": count,
            "returned": len(chosen),
            "considered": len(ranked),
            "nearest_distance": chosen[0][0] if chosen else None,
            "farthest_distance": chosen[-1][0] if chosen else None,
            "selection": applied,
        }

    # -- counting one layer into another -----------------------------------

    def _polygon_parts(
        self, layer: Any, label_field: str,
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, float]]:
        """Polygon parts as ring lists, with their label and their area.

        One entry per PART rather than per feature, because flattening a
        multipolygon's rings into one list destroys the shell/hole distinction
        and would count points that fall in a hole. Parts are aggregated back to
        their feature's label by the caller.
        """
        parts: list[dict[str, Any]] = []
        labels: dict[str, str] = {}
        areas: dict[str, float] = {}
        for feature in self._geometry_features(layer, label_field, "all"):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            label = str(feature[label_field]) if label_field else "feature {}".format(feature.id())
            try:
                polygons = geometry.asMultiPolygon() if geometry.isMultipart() else [geometry.asPolygon()]
            except Exception as exc:  # noqa: BLE001 - a non-polygon geometry is skipped, not fatal
                # Skipping is right: a layer can legitimately mix geometry
                # types and one line among polygons is not a failure. Skipping
                # SILENTLY is not - it swallows a real defect just as quietly
                # as it swallows the expected case, which is what the bare
                # except/continue here used to do.
                log_debug(
                    "skipped feature {}: geometry is not polygonal ({})".format(
                        feature.id(), exc.__class__.__name__
                    )
                )
                continue
            for index, polygon in enumerate(polygons):
                rings = [[(float(point.x()), float(point.y())) for point in ring] for ring in polygon if ring]
                if not rings:
                    continue
                part_id = "{}#{}".format(feature.id(), index)
                parts.append({"id": part_id, "rings": rings})
                labels[part_id] = label
                areas[part_id] = spatial.polygon_area(rings)
            if len(parts) >= MAX_ANALYTIC_FEATURES:
                break
        return parts, labels, areas

    def _representative_points(self, layer: Any, target_crs: Any) -> tuple[list[dict[str, Any]], bool]:
        """One point per feature, in `target_crs`, and whether any was derived.

        A non-point layer is not refused: "how many parcels are in each
        district" is the same question as "how many buildings", and a point on
        the surface is the honest reduction. The result says it happened.
        """
        from qgis.core import QgsCsException, QgsGeometry  # noqa: PLC0415

        transform = self._crs_transform(layer.crs(), target_crs)
        records: list[dict[str, Any]] = []
        derived = False
        for feature in self._geometry_features(layer, "", "all"):
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            if transform is not None:
                geometry = QgsGeometry(geometry)
                try:
                    if geometry.transform(transform) != 0:
                        continue
                except QgsCsException:
                    continue
            if geometry.type() == 0 and not geometry.isMultipart():
                point = geometry.asPoint()
            else:
                derived = True
                surface = geometry.pointOnSurface()
                if surface is None or surface.isEmpty():
                    continue
                point = surface.asPoint()
            records.append({"id": feature.id(), "point": (float(point.x()), float(point.y()))})
            if len(records) >= MAX_ANALYTIC_FEATURES:
                break
        return records, derived

    def _counted_into_polygons(
        self,
        polygon_layer_id: str,
        point_layer_id: str,
        group_field: str = "",
    ) -> dict[str, Any]:
        polygons = self.vector(polygon_layer_id)
        points = self.vector(point_layer_id)
        _require(polygons.id() != points.id(), "counting one layer into another needs two different layers")
        label_field = group_field or self._display_field(polygons)
        parts, labels, part_areas = self._polygon_parts(polygons, label_field)
        _require(parts, "{} has no polygons to count into".format(polygons.name()))
        records, derived = self._representative_points(points, polygons.crs())
        _require(
            records,
            "{} has no features that can be placed in {}'s coordinate system".format(points.name(), polygons.name()),
        )
        raw = spatial.count_points_in_polygons(parts, records)
        counts: dict[str, int] = {}
        areas: dict[str, float] = {}
        for part_id, value in (raw.get("counts") or {}).items():
            label = labels.get(part_id, part_id)
            counts[label] = counts.get(label, 0) + int(value)
            areas[label] = areas.get(label, 0.0) + float(part_areas.get(part_id, 0.0))
        return {
            "counts": counts,
            "areas": areas,
            "raw": raw,
            "polygons": polygons,
            "points": points,
            "label_field": label_field,
            "derived_points": derived,
        }

    def count_in_polygons(
        self,
        polygon_layer_id: str,
        point_layer_id: str,
        group_field: str = "",
    ) -> dict[str, Any]:
        """Count features of one layer inside each polygon of another."""
        computed = self._counted_into_polygons(polygon_layer_id, point_layer_id, group_field)
        raw = computed["raw"]
        return {
            "kind": "group_aggregate",
            "measured": "points_in_polygons",
            "group_field": computed["label_field"],
            "statistic": "count",
            "polygon_layer": computed["polygons"].name(),
            "point_layer": computed["points"].name(),
            "compared_in": computed["polygons"].crs().authid(),
            "features": raw.get("points"),
            "matched": raw.get("matched"),
            "unmatched": raw.get("unmatched"),
            "used_representative_point": computed["derived_points"],
            "groups": [
                {"group": label, "value": value}
                for label, value in sorted(computed["counts"].items(), key=lambda item: -item[1])
            ],
        }

    def density(
        self,
        polygon_layer_id: str,
        point_layer_id: str,
        group_field: str = "",
    ) -> dict[str, Any]:
        """Normalise those counts by polygon area, stating the unit."""
        computed = self._counted_into_polygons(polygon_layer_id, point_layer_id, group_field)
        polygons = computed["polygons"]
        result = spatial.density_per_area(
            computed["counts"], computed["areas"], self._crs_map_units(polygons.crs())
        )
        if result.get("reason") == "unknown_crs_unit":
            raise RuntimeUnavailable(
                "{} is not in a linear coordinate system, so an area, and therefore a density, cannot be "
                "measured on it. Reproject it and ask again.".format(polygons.name())
            )
        result = dict(result)
        result.update({
            "group_field": computed["label_field"],
            "polygon_layer": polygons.name(),
            "point_layer": computed["points"].name(),
            "compared_in": polygons.crs().authid(),
            "counts": computed["counts"],
            "used_representative_point": computed["derived_points"],
            "unmatched": computed["raw"].get("unmatched"),
        })
        return result

    # -- reading a PostGIS connection --------------------------------------
    #
    # The credentials for a QGIS saved connection never leave the machine:
    # `connections.py` sends the server an id and a user-authored label and has
    # no field that could carry a host, a user or a password. So the server
    # cannot answer a question about a QGIS-local database however good its own
    # PostGIS implementation is, and this is the local half of that design
    # rather than a duplicate of it.
    #
    # `postgis.py` builds the statement; nothing here composes SQL. QGIS's
    # connection API executes a statement string and accepts no parameter list,
    # so the numeric parameters are bound by `bind_numeric_parameters`, which
    # refuses anything that is not a finite number and re-guards the bound text.

    def _pg_connection(self, connection_id: str) -> Any:
        """Resolve a saved PostgreSQL connection by the name QGIS stored it under.

        Addressed by name, which is what `connections.py` reported as the id.
        The credential, the auth config and the host stay inside QGIS's own
        connection store and are never read here.
        """
        from qgis.core import QgsProviderRegistry  # noqa: PLC0415

        metadata = QgsProviderRegistry.instance().providerMetadata("postgres")
        _require(metadata is not None, "this QGIS build has no PostgreSQL provider")
        name = str(connection_id or "")
        try:
            connections = metadata.connections(False)
        except Exception as error:  # noqa: BLE001 - an unreadable store is a refusal
            raise RuntimeUnavailable("QGIS could not read its saved connections: {}".format(error))
        connection = connections.get(name) if hasattr(connections, "get") else None
        _require(connection is not None, "'{}' is not a saved PostgreSQL connection in this QGIS".format(name))
        return connection

    def _pg_catalog(self, connection: Any, connection_id: str, schema: str, table: str) -> Any:
        """Discover the table's real shape, so a forged name cannot resolve."""
        try:
            fields = connection.fields(schema, table)
        except Exception as error:  # noqa: BLE001
            raise RuntimeUnavailable(
                "{}.{} could not be read on '{}': {}".format(schema, table, connection_id, error)
            )
        columns = [{"name": field.name(), "type": field.typeName()} for field in fields]
        _require(columns, "{}.{} has no readable columns".format(schema, table))
        geometry_column, srid = "", 0
        try:
            for candidate in connection.tables(schema):
                if candidate.tableName() != table:
                    continue
                geometry_column = str(candidate.geometryColumn() or "")
                crs_list = candidate.crsList() if hasattr(candidate, "crsList") else []
                if crs_list and crs_list[0].isValid():
                    srid = int(str(crs_list[0].authid() or "EPSG:0").split(":")[-1] or 0)
                break
        except Exception as error:  # noqa: BLE001 - geometry metadata is optional
            log_debug("reading PostGIS table properties", error)
        return postgis.catalog_from_columns(
            schema, table, columns, geometry_column, srid, connection_id,
        )

    def _pg_execute(self, connection: Any, connection_id: str, built: tuple) -> dict[str, Any]:
        """Run one built statement inside a read-only transaction where possible.

        The transaction is attempted rather than assumed. QGIS pools its own
        libpq connections and does not promise that two `executeSql` calls share
        a session, so the result reports whether the server-side READ ONLY
        guarantee was actually established. The two construction-side layers -
        builders that emit only SELECT, identifiers validated against this live
        connection - hold either way; saying which ones were in force is the
        difference between a guarantee and a hope.
        """
        sql, params = built
        statement = postgis.bind_numeric_parameters(sql, params)
        enforced = True
        for setup in postgis.session_setup():
            try:
                connection.executeSql(setup)
            except Exception as error:  # noqa: BLE001
                enforced = False
                log_debug("establishing a read-only PostGIS transaction", error)
                break
        try:
            rows = connection.executeSql(statement)
        except Exception as error:  # noqa: BLE001
            raise RuntimeUnavailable("that query could not be run on '{}': {}".format(connection_id, error))
        finally:
            if enforced:
                try:
                    connection.executeSql("COMMIT")
                except Exception as error:  # noqa: BLE001
                    log_debug("closing the read-only PostGIS transaction", error)
        return {
            "rows": [list(row) for row in (rows or [])],
            "sql": statement,
            "enforced_read_only": enforced,
        }

    def _postgis_answer(self, connection_id: str, schema: str, table: str, built: tuple) -> dict[str, Any]:
        connection = self._pg_connection(connection_id)
        outcome = self._pg_execute(connection, connection_id, built)
        return {
            "connection": connection_id,
            "schema": schema,
            "table": table,
            # The statement travels back on purpose. The claim is that the SQL
            # selects what it says it selects, and a claim nobody can inspect is
            # not a claim.
            "sql": outcome["sql"],
            "enforced_read_only": outcome["enforced_read_only"],
            "rows": outcome["rows"],
        }

    def postgis_profile(self, connection_id: str, schema: str, table: str) -> dict[str, Any]:
        connection = self._pg_connection(connection_id)
        catalog = self._pg_catalog(connection, connection_id, schema, table)
        outcome = self._pg_execute(connection, connection_id, postgis.build_profile(catalog))
        return {
            "kind": "postgis_profile",
            "connection": connection_id,
            "schema": schema,
            "table": table,
            "columns": dict(catalog.columns),
            "geometry_column": catalog.geometry_column,
            "srid": catalog.srid,
            "sql": outcome["sql"],
            "enforced_read_only": outcome["enforced_read_only"],
            "rows": outcome["rows"],
        }

    def postgis_analyze(
        self,
        connection_id: str,
        schema: str,
        table: str,
        operation: str,
        field: str = "",
        group_field: str = "",
        statistic: str = "count",
        limit: int = 0,
        bbox: Any = None,
        id_field: str = "",
        ascending: bool = False,
        x: Any = None,
        y: Any = None,
        srid: int = 4326,
    ) -> dict[str, Any]:
        connection = self._pg_connection(connection_id)
        catalog = self._pg_catalog(connection, connection_id, schema, table)
        name = str(operation or "").strip().lower()
        try:
            if name == "numeric":
                built = postgis.build_numeric_stats(catalog, field, bbox)
            elif name == "categories":
                built = postgis.build_categorical(catalog, field, limit or 25, bbox)
            elif name == "group":
                built = postgis.build_group_aggregate(
                    catalog, group_field, statistic, field or None, limit or 50, bbox,
                )
            elif name == "top_n":
                _require(id_field, "ranking needs the column that identifies a feature")
                built = postgis.build_top_n(catalog, field, id_field, limit or 10, bool(ascending), bbox)
            elif name == "bbox_count":
                _require(bbox, "a bounding box is needed to count features in an area")
                built = postgis.build_bbox_count(catalog, bbox, int(srid or 4326))
            elif name == "nearest":
                _require(id_field, "a nearest search needs the column that identifies a feature")
                _require(x is not None and y is not None, "a nearest search needs a point to measure from")
                built = postgis.build_nearest(
                    catalog, id_field, float(x), float(y), int(srid or 4326), limit or 10,
                )
            else:
                raise CapabilityError("{} is not an analysis this build performs".format(operation))
        except postgis.ReadOnlyViolation as error:
            # An unknown column reaches here. Naming it is the answer: the user
            # asked about a field this table does not have.
            raise RuntimeUnavailable("that question cannot be asked of {}.{}: {}".format(schema, table, error))
        outcome = self._pg_execute(connection, connection_id, built)
        return {
            "kind": "postgis_analysis",
            "operation": name,
            "connection": connection_id,
            "schema": schema,
            "table": table,
            "field": field,
            "group_field": group_field,
            "sql": outcome["sql"],
            "enforced_read_only": outcome["enforced_read_only"],
            "rows": outcome["rows"],
        }

    def postgis_spatial(
        self,
        connection_id: str,
        schema: str,
        table: str,
        other_table: str,
        operation: str = "relationship_count",
        other_schema: str = "",
        predicate: str = "intersects",
        limit: int = 0,
        group_field: str = "",
    ) -> dict[str, Any]:
        connection = self._pg_connection(connection_id)
        left = self._pg_catalog(connection, connection_id, schema, table)
        right = self._pg_catalog(connection, connection_id, other_schema or schema, other_table)
        name = str(operation or "relationship_count").strip().lower()
        try:
            if name == "join_counts":
                _require(group_field, "counting one table into another needs the column that names each area")
                built = postgis.build_spatial_join_counts(left, right, group_field, limit or 50)
            else:
                built = postgis.build_spatial_relationship_count(left, right, predicate)
        except postgis.ReadOnlyViolation as error:
            raise RuntimeUnavailable("that spatial question cannot be asked here: {}".format(error))
        outcome = self._pg_execute(connection, connection_id, built)
        return {
            "kind": "postgis_spatial",
            "operation": name,
            "predicate": predicate,
            "connection": connection_id,
            "schema": schema,
            "table": table,
            "other_table": other_table,
            "sql": outcome["sql"],
            "enforced_read_only": outcome["enforced_read_only"],
            "rows": outcome["rows"],
        }

    # -- map effects (reversible) -----------------------------------------

    def _push_undo(self, entry: Mapping[str, Any]) -> None:
        self._undo.append(dict(entry))
        del self._undo[:-MAX_UNDO_DEPTH]

    def select_features(self, layer_id: str, feature_ids: list[Any], zoom: bool = True) -> dict[str, Any]:
        """Make an analytical result visible as a real map selection."""
        layer = self.vector(layer_id)
        previous = list(layer.selectedFeatureIds())
        identifiers = []
        for value in feature_ids:
            try:
                identifiers.append(int(value))
            except (TypeError, ValueError):
                continue
        layer.selectByIds(identifiers)
        self._push_undo({"kind": "selection", "layer_id": layer_id, "ids": previous})
        if zoom and identifiers:
            self.zoom_to_selection(layer_id)
        return {"kind": "selection_applied", "selected": len(identifiers), "requested": len(feature_ids)}

    def set_visibility(self, layer_id: str, visible: bool) -> dict[str, Any]:
        layer = self.layer(layer_id)
        node = self.project.layerTreeRoot().findLayer(layer.id())
        _require(node is not None, "that layer is not in the layer tree")
        self._push_undo({"kind": "visibility", "layer_id": layer_id, "visible": node.itemVisibilityChecked()})
        node.setItemVisibilityChecked(bool(visible))
        self.iface.mapCanvas().refresh()
        return {"kind": "visibility_applied", "visible": bool(visible)}

    def set_opacity(self, layer_id: str, opacity: float) -> dict[str, Any]:
        layer = self.layer(layer_id)
        _require(hasattr(layer, "setOpacity"), "that layer does not support opacity")
        self._push_undo({"kind": "opacity", "layer_id": layer_id, "opacity": layer.opacity() * 100.0})
        layer.setOpacity(max(0.0, min(100.0, float(opacity))) / 100.0)
        layer.triggerRepaint()
        return {"kind": "opacity_applied", "opacity": float(opacity)}

    def apply_visualization(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a renderer specification produced from a measured analysis."""
        kind = str(spec.get("kind") or "")
        if kind == "none":
            return {"kind": "no_map_change", "reason": spec.get("reason")}
        if kind == "selection":
            return self.select_features(
                str(spec.get("layer_id")), list(spec.get("feature_ids") or []),
                bool(spec.get("zoom_to_selection")),
            )
        layer = self.vector(str(spec.get("layer_id")))
        self._push_undo({"kind": "renderer", "layer_id": layer.id(), "renderer": layer.renderer().clone()})
        if kind == "categorized":
            renderer = self._categorized_renderer(layer, spec)
        elif kind == "graduated":
            renderer = self._graduated_renderer(layer, spec)
        elif kind == "group_choropleth":
            renderer = self._choropleth_renderer(layer, spec)
        else:
            return {"kind": "no_map_change", "reason": "unsupported_visualization"}
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        entries = spec.get("classes") or spec.get("categories") or spec.get("groups") or []
        return {"kind": "style_applied", "style": kind, "classes": len(entries)}

    def _symbol_for(self, layer: Any, color: str) -> Any:
        from qgis.core import QgsSymbol  # noqa: PLC0415
        from qgis.PyQt.QtGui import QColor  # noqa: PLC0415

        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        symbol.setColor(QColor(color))
        return symbol

    def _categorized_renderer(self, layer: Any, spec: Mapping[str, Any]) -> Any:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory  # noqa: PLC0415

        categories = []
        for entry in spec.get("categories") or []:
            symbol = self._symbol_for(layer, str(entry.get("color") or "#888888"))
            categories.append(QgsRendererCategory(entry.get("value"), symbol, str(entry.get("label") or "")))
        return QgsCategorizedSymbolRenderer(str(spec.get("field") or ""), categories)

    def _choropleth_renderer(self, layer: Any, spec: Mapping[str, Any]) -> Any:
        """Colour each group by its measured value.

        A group-by and a density both produce one number per named group, and
        the number lives in the analysis rather than in a column on the layer.
        A graduated renderer cannot read it, so the map is drawn as categories
        over the grouping field with each category's colour taken from where its
        value sits in the measured range. Groups are ordered by value so the
        legend reads as a scale rather than as an alphabet.
        """
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory  # noqa: PLC0415

        entries = [
            (str(entry.get("group")), float(entry.get("value")))
            for entry in spec.get("groups") or []
            if entry.get("group") is not None and entry.get("value") is not None
        ]
        entries.sort(key=lambda item: item[1])
        colors = presentation.ramp_colors(presentation.DEFAULT_SEQUENTIAL, max(len(entries), 2))
        unit = str(spec.get("unit") or "")
        categories = []
        for index, (group, value) in enumerate(entries):
            symbol = self._symbol_for(layer, colors[min(index, len(colors) - 1)])
            label = "{} ({:g}{})".format(group, value, " " + unit if unit else "")
            categories.append(QgsRendererCategory(group, symbol, label))
        return QgsCategorizedSymbolRenderer(str(spec.get("field") or ""), categories)

    def _graduated_renderer(self, layer: Any, spec: Mapping[str, Any]) -> Any:
        from qgis.core import QgsGraduatedSymbolRenderer, QgsRendererRange  # noqa: PLC0415

        ranges = []
        for entry in spec.get("classes") or []:
            symbol = self._symbol_for(layer, str(entry.get("color") or "#888888"))
            ranges.append(QgsRendererRange(
                float(entry.get("lower")), float(entry.get("upper")), symbol, str(entry.get("label") or "")
            ))
        return QgsGraduatedSymbolRenderer(str(spec.get("field") or ""), ranges)

    def style_single(
        self,
        layer_id: str,
        color: str = "",
        stroke_width: Any = None,
        size: Any = None,
        opacity: Any = None,
    ) -> dict[str, Any]:
        """One symbol for the whole layer: the "make these stand out" request.

        Registered and unbound, so the most ordinary styling ask in GIS - a flat
        colour on one layer - fell through to a refusal while categorised and
        graduated both worked. Every property is optional and an omitted one is
        left alone rather than reset to a default, because "make them red"
        should not also change the outline width the user set by hand.
        """
        from qgis.core import QgsSingleSymbolRenderer  # noqa: PLC0415

        layer = self.vector(layer_id)
        self._push_undo({"kind": "renderer", "layer_id": layer.id(), "renderer": layer.renderer().clone()})
        symbol = self._symbol_for(layer, str(color) if color else "#5B5BD6")
        applied = {"color": color or "#5B5BD6"}
        if stroke_width is not None and hasattr(symbol, "symbolLayer"):
            try:
                symbol.symbolLayer(0).setStrokeWidth(float(stroke_width))
                applied["stroke_width"] = float(stroke_width)
            except (AttributeError, TypeError, ValueError):
                # A symbol layer without a stroke (a fill, a marker) is not an
                # error: the rest of the request still applies.
                log_debug("setting a stroke width on a symbol that has none", None)
        if size is not None and hasattr(symbol, "setSize"):
            try:
                symbol.setSize(float(size))
                applied["size"] = float(size)
            except (AttributeError, TypeError, ValueError):
                log_debug("setting a size on a symbol that has none", None)
        if opacity is not None and hasattr(symbol, "setOpacity"):
            symbol.setOpacity(max(0.0, min(100.0, float(opacity))) / 100.0)
            applied["opacity"] = float(opacity)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"kind": "style_applied", "style": "single", "layer": layer.name(), "applied": applied}

    def style_raster(
        self,
        layer_id: str,
        band: Any = None,
        ramp: str = "",
        opacity: Any = None,
    ) -> dict[str, Any]:
        """Band, stretch and opacity on a raster.

        Deliberately narrow. Rendering a raster well is a large surface and this
        covers the three things a person asks for out loud: show me a different
        band, colour it, and let me see through it.
        """
        from qgis.core import (  # noqa: PLC0415
            QgsColorRampShader,
            QgsRasterShader,
            QgsSingleBandPseudoColorRenderer,
        )

        layer = self.layer(layer_id)
        _require(hasattr(layer, "dataProvider") and hasattr(layer, "renderer"),
                 "that layer is not a raster")
        provider = layer.dataProvider()
        count = provider.bandCount() if hasattr(provider, "bandCount") else 1
        selected = int(band or 1)
        _require(1 <= selected <= count, "that raster has {} band(s)".format(count))
        self._push_undo({"kind": "renderer", "layer_id": layer.id(), "renderer": layer.renderer().clone()})
        applied: dict[str, Any] = {"band": selected}
        if ramp:
            statistics = provider.bandStatistics(selected)
            shader = QgsRasterShader()
            colors = presentation.ramp_colors(ramp, 5)
            ramp_shader = QgsColorRampShader(statistics.minimumValue, statistics.maximumValue)
            ramp_shader.setColorRampItemList([
                self._ramp_item(statistics, index, len(colors), colors[index])
                for index in range(len(colors))
            ])
            shader.setRasterShaderFunction(ramp_shader)
            layer.setRenderer(QgsSingleBandPseudoColorRenderer(provider, selected, shader))
            applied["ramp"] = ramp
            applied["range"] = [statistics.minimumValue, statistics.maximumValue]
        if opacity is not None:
            layer.setOpacity(max(0.0, min(100.0, float(opacity))) / 100.0)
            applied["opacity"] = float(opacity)
        layer.triggerRepaint()
        return {"kind": "style_applied", "style": "raster", "layer": layer.name(), "applied": applied}

    @staticmethod
    def _ramp_item(statistics: Any, index: int, count: int, color: str) -> Any:
        from qgis.core import QgsColorRampShader  # noqa: PLC0415
        from qgis.PyQt.QtGui import QColor  # noqa: PLC0415

        low, high = statistics.minimumValue, statistics.maximumValue
        value = low + (high - low) * (index / float(max(count - 1, 1)))
        return QgsColorRampShader.ColorRampItem(value, QColor(color), "{:g}".format(value))

    def set_labels(self, layer_id: str, field: str, size: float = 9.0, enabled: bool = True) -> dict[str, Any]:
        from qgis.core import QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling  # noqa: PLC0415

        layer = self.vector(layer_id)
        # Only an existing field may label a layer; a made-up field name would
        # silently render nothing and look like a broken map.
        _require(field in self.field_names(layer), "the field '{}' is not in that layer".format(field))
        self._push_undo({"kind": "labels", "layer_id": layer.id(),
                         "labeling": layer.labeling().clone() if layer.labeling() else None,
                         "enabled": layer.labelsEnabled()})
        settings = QgsPalLayerSettings()
        settings.fieldName = field
        text_format = QgsTextFormat()
        font = text_format.font()
        font.setPointSizeF(max(4.0, min(48.0, float(size))))
        text_format.setFont(font)
        text_format.setSize(max(4.0, min(48.0, float(size))))
        settings.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(bool(enabled))
        layer.triggerRepaint()
        return {"kind": "labels_applied", "field": field, "enabled": bool(enabled)}

    def preview_filter(self, layer_id: str, field: str, operator: str, value: str = "") -> dict[str, Any]:
        """Apply a validated subset filter built from typed parts, not free text.

        The expression is assembled here from a checked field name, a closed
        operator set and a quoted literal, so a filter can never carry an
        arbitrary QGIS expression from the model.
        """
        layer = self.vector(layer_id)
        _require(field in self.field_names(layer), "the field '{}' is not in that layer".format(field))
        quoted = '"{}"'.format(field.replace('"', '""'))
        if operator in {"is null", "is not null"}:
            expression = "{} {}".format(quoted, operator.upper())
        elif operator == "in":
            parts = [item.strip() for item in str(value).split(",") if item.strip()]
            _require(parts, "that filter needs at least one value")
            expression = "{} IN ({})".format(quoted, ", ".join(_literal(part) for part in parts))
        else:
            expression = "{} {} {}".format(quoted, operator, _literal(value))
        self._push_undo({"kind": "filter", "layer_id": layer.id(), "subset": layer.subsetString()})
        if not layer.setSubsetString(expression):
            raise RuntimeUnavailable("QGIS rejected that filter for this layer")
        self.iface.mapCanvas().refresh()
        return {"kind": "filter_applied", "expression": expression, "matched": layer.featureCount()}

    def clear_filter(self, layer_id: str) -> dict[str, Any]:
        layer = self.vector(layer_id)
        self._push_undo({"kind": "filter", "layer_id": layer.id(), "subset": layer.subsetString()})
        layer.setSubsetString("")
        self.iface.mapCanvas().refresh()
        return {"kind": "filter_cleared"}

    # -- navigation --------------------------------------------------------

    def zoom_to_layer(self, layer_id: str) -> dict[str, Any]:
        layer = self.layer(layer_id)
        canvas = self.iface.mapCanvas()
        extent = self._extent_in_canvas_crs(layer)
        _require(extent is not None and not extent.isEmpty(), "that layer has no extent to zoom to")
        canvas.setExtent(extent)
        canvas.refresh()
        return {"kind": "zoomed", "layer": layer.name()}

    def _extent_in_canvas_crs(self, layer: Any) -> Any:
        from qgis.core import QgsCoordinateTransform, QgsCsException  # noqa: PLC0415

        canvas = self.iface.mapCanvas()
        extent = layer.extent()
        source = layer.crs()
        target = canvas.mapSettings().destinationCrs()
        if not source.isValid() or not target.isValid() or source == target:
            return extent
        try:
            return QgsCoordinateTransform(source, target, self.project).transformBoundingBox(extent)
        except QgsCsException:
            return None

    def zoom_to_selection(self, layer_id: str) -> dict[str, Any]:
        layer = self.vector(layer_id)
        _require(layer.selectedFeatureCount() > 0, "nothing is selected on that layer")
        from qgis.core import QgsCoordinateTransform, QgsCsException  # noqa: PLC0415

        canvas = self.iface.mapCanvas()
        extent = layer.boundingBoxOfSelected()
        source = layer.crs()
        target = canvas.mapSettings().destinationCrs()
        if source.isValid() and target.isValid() and source != target:
            try:
                extent = QgsCoordinateTransform(source, target, self.project).transformBoundingBox(extent)
            except QgsCsException:
                raise RuntimeUnavailable("that selection cannot be transformed into the map CRS")
        canvas.setExtent(extent)
        canvas.refresh()
        return {"kind": "zoomed_to_selection", "features": layer.selectedFeatureCount()}

    # -- undo --------------------------------------------------------------

    def measure_distance(self, point_a, point_b) -> dict[str, Any]:
        """Distance between two WGS84 positions.

        QGIS is asked first because QgsDistanceArea measures on the ellipsoid,
        which is the better answer. The pure spherical fallback exists so the
        capability still works when no ellipsoid is configured, and the result
        always names which one produced it: the two differ by enough to matter
        on a cadastral boundary and not at all on a site plan, and a reader
        cannot tell them apart from the number.
        """
        try:
            from qgis.core import QgsCoordinateReferenceSystem, QgsDistanceArea, QgsPointXY

            calculator = QgsDistanceArea()
            crs = QgsCoordinateReferenceSystem("EPSG:4326")
            calculator.setSourceCrs(crs, self.project.transformContext())
            ellipsoid = self.project.ellipsoid() or "WGS84"
            calculator.setEllipsoid(ellipsoid)
            metres = calculator.measureLine(
                QgsPointXY(float(point_a[0]), float(point_a[1])),
                QgsPointXY(float(point_b[0]), float(point_b[1])),
            )
            if metres and metres > 0:
                return {
                    "kind": "measurement", "metres": float(metres),
                    "method": "ellipsoidal", "ellipsoid": ellipsoid,
                }
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail the turn
            # Worth recording: the fallback below measures on the sphere rather
            # than the project ellipsoid, so the answer the user receives is not
            # the one they would have got. Silence here makes that undetectable.
            log_debug("ellipsoidal distance unavailable, measuring spherically", exc)
        result = spatial.measure_distance(point_a, point_b, True, "degrees")
        result["kind"] = "measurement"
        return result

    def export_layer(self, layer_id: str, output_format: str = "gpkg") -> dict[str, Any]:
        """Write a layer to a file and report where it went.

        The path is derived here rather than taken as a parameter. A model that
        can name the output path can be steered into overwriting something, and
        no phrasing of the prompt makes an arbitrary filesystem write safe.
        """
        import os
        import tempfile

        # Validate the format before importing anything or resolving a layer.
        # Refusing an unwritable format is a decision this function can make on
        # its own, and making it first means the refusal does not depend on
        # QGIS being importable.
        drivers = {"geojson": ("GeoJSON", "geojson"), "gpkg": ("GPKG", "gpkg"),
                   "shp": ("ESRI Shapefile", "shp"), "csv": ("CSV", "csv")}
        chosen = drivers.get(str(output_format).lower())
        if chosen is None:
            raise CapabilityError("{} is not a format this build can write".format(output_format))
        driver, extension = chosen

        from qgis.core import QgsVectorFileWriter

        from .qt_compat import enum_member

        # self.vector, not a _require_layer that was never written. Export is a
        # vector operation and the raster path has no writer here.
        layer = self.vector(layer_id)

        directory = os.path.join(tempfile.gettempdir(), "mapdex-exports")
        os.makedirs(directory, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer.name())[:60] or "layer"
        path = os.path.join(directory, "{}.{}".format(safe_name, extension))

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        error = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, self.project.transformContext(), options)
        # The API returns a tuple whose first element is the error code.
        # Reporting a path without checking it is how a user is handed a
        # filename that was never written.
        #
        # The success constant is resolved through enum_member: PyQt6 requires
        # the scoped path and the flat form raises AttributeError at runtime, in
        # a code path no unit test opens.
        code = error[0] if isinstance(error, (tuple, list)) else error
        no_error = enum_member(QgsVectorFileWriter, "WriterError", "NoError")
        if code != no_error:
            raise CapabilityError("QGIS could not write the {} file".format(driver))
        if not os.path.exists(path):
            raise CapabilityError("the export reported success but no file was written")
        return {
            "kind": "export", "path": path, "format": output_format,
            "features": layer.featureCount(), "bytes": os.path.getsize(path),
        }

    def diagnose_layer_crs(self, layer_id: str) -> dict[str, Any]:
        """Whether a layer's declared reference system can hold its coordinates.

        Contradictions only. Guessing what the system SHOULD be is a different
        question that needs a hint - a place, an expected extent - and without
        one a ranked list of candidates is a list of coincidences.

        QGIS supplies the facts, `crs_diagnose` decides. Keeping the rules in a
        module with no QGIS import is what lets them be tested at all: the
        interesting cases are coordinates nobody has a project for.
        """
        from qgis.core import (  # noqa: PLC0415
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsProject,
        )

        from ._vendor.nivo import crs_diagnose

        layer = self.vector(layer_id)
        crs = layer.crs()
        extent = layer.extent()
        if extent.isEmpty():
            raise CapabilityError(
                "that layer has no extent to check: it may be empty")
        bounds = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())

        declared = crs.authid() if crs.isValid() else ""
        kind = None
        if crs.isValid():
            kind = "geographic" if crs.isGeographic() else "projected"

        area = None
        wgs84_bounds = None
        if crs.isValid():
            try:
                box = crs.bounds()
                if not box.isEmpty():
                    area = (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())
            except Exception:  # noqa: BLE001 - a CRS without bounds is not fatal
                area = None
            try:
                wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
                transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
                projected = transform.transformBoundingBox(extent)
                if not projected.isEmpty():
                    wgs84_bounds = (projected.xMinimum(), projected.yMinimum(),
                                    projected.xMaximum(), projected.yMaximum())
            except Exception:  # noqa: BLE001 - an untransformable extent is a finding, not a crash
                wgs84_bounds = None

        findings = crs_diagnose.contradictions(bounds, kind, area, wgs84_bounds)
        return {
            "kind": "crs_diagnosis",
            "layer_id": layer.id(),
            "declared_crs": declared,
            "crs_kind": kind,
            "bounds": list(bounds),
            "findings": findings,
            "verdict": crs_diagnose.verdict(declared, findings),
        }

    def datum_transformations(self, source_crs: str, target_crs: str,
                             lat: float = 0.0, lon: float = 0.0) -> dict[str, Any]:
        """Which paths exist between two systems, and how good each one is.

        A reprojection between datums is not one operation with one answer: PROJ
        usually knows several, they differ by metres, and which one runs depends
        on whether a grid file is installed. Applying one silently and reporting
        success is how a survey moves by two metres between two people who both
        think they used "the same" transformation.

        So this reports the CANDIDATES with their accuracies and says which are
        actually available here. An operation whose grid is missing is listed as
        unavailable rather than omitted: knowing that the good path exists and
        is not installed is what tells a surveyor to go and fetch it, and
        hiding it leaves them accepting a worse answer without knowing there
        was a better one.
        """
        from qgis.core import (  # noqa: PLC0415
            QgsCoordinateReferenceSystem,
            QgsDatumTransform,
        )

        source = QgsCoordinateReferenceSystem(str(source_crs or "").strip())
        target = QgsCoordinateReferenceSystem(str(target_crs or "").strip())
        if not source.isValid():
            raise CapabilityError("{} is not a coordinate reference system QGIS knows".format(source_crs))
        if not target.isValid():
            raise CapabilityError("{} is not a coordinate reference system QGIS knows".format(target_crs))

        operations = []
        try:
            candidates = QgsDatumTransform.operations(source, target)
        except Exception as error:  # noqa: BLE001 - PROJ may raise anything
            raise CapabilityError("PROJ could not list the transformations: {}".format(error))
        for candidate in candidates:
            accuracy = getattr(candidate, "accuracy", -1.0)
            missing = [
                {"name": getattr(grid, "shortName", ""), "url": getattr(grid, "url", ""),
                 "available": bool(getattr(grid, "isAvailable", False))}
                for grid in (getattr(candidate, "grids", None) or [])
                if not getattr(grid, "isAvailable", False)
            ]
            operations.append({
                "name": getattr(candidate, "name", ""),
                "proj": getattr(candidate, "proj", ""),
                # PROJ reports -1 when it does not know, and that is NOT zero
                # metres. Reporting an unknown accuracy as perfect is the
                # specific lie this capability exists to prevent.
                "accuracy_m": accuracy if accuracy is not None and accuracy >= 0 else None,
                "available": bool(getattr(candidate, "isAvailable", True)),
                "missing_grids": missing,
            })

        # PROJ orders by preference, so the first available one is what a
        # reprojection would actually use.
        chosen = next((op for op in operations if op["available"]), None)
        return {
            "kind": "datum_transformations",
            "source_crs": source.authid() or str(source_crs),
            "target_crs": target.authid() or str(target_crs),
            "operations": operations,
            "would_use": chosen,
            "count": len(operations),
        }

    def show_layer_legend(self, layer_id: str, visible: bool = True) -> dict[str, Any]:
        """Expand or collapse a layer's classes in the legend.

        QGIS has no separate legend to open: the layer tree IS the legend, and
        it is always on screen. What a person asking to "show the legend" wants
        is the CLASSES under the layer - the colours and their labels - which
        are collapsed by default on a categorized or graduated layer and are
        the part that explains the map.

        Returning the entries as well as setting the state means the assistant
        can read the legend back rather than only toggling it, which is what a
        question like "what do these colours mean" actually needs.
        """
        from qgis.core import QgsProject  # noqa: PLC0415

        layer = self.vector(layer_id)
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is None:
            raise CapabilityError("that layer is not in the project's layer tree")
        node.setExpanded(bool(visible))

        entries = []
        try:
            renderer = layer.renderer()
            for item in renderer.legendSymbolItems():
                entries.append({"label": item.label(), "key": item.ruleKey()})
        except Exception:  # noqa: BLE001 - a renderer without legend items is not fatal
            entries = []
        return {
            "kind": "legend",
            "layer_id": layer.id(),
            "name": layer.name(),
            "expanded": bool(visible),
            "entries": entries,
        }

    def import_field_points(
        self,
        path: str,
        crs: str,
        easting_field: str,
        northing_field: str,
        name: str = "",
        elevation_field: str = "",
        delimiter: str = "",
    ) -> dict[str, Any]:
        """A total station's point list, as a layer, with nothing guessed.

        The reference system is REQUIRED. A field point list is eastings and
        northings in some projected system, and the one thing that cannot be
        recovered from the numbers is which. QGIS's delimited-text provider is
        happy to be told EPSG:4326 and will place a survey in the Gulf of
        Guinea; refusing is the only honest answer to a file that does not say.

        The columns are named by the caller for the same reason. `X` and `Y`,
        `E` and `N`, `Sag` and `Yukari` - the header is whatever the instrument
        wrote, and picking one by position puts northings in the easting.
        """
        import os  # noqa: PLC0415

        from qgis.core import (  # noqa: PLC0415
            QgsCoordinateReferenceSystem,
            QgsProject,
            QgsVectorLayer,
        )

        source = str(path or "").strip()
        if not source or not os.path.exists(source):
            raise CapabilityError("no file at {}".format(source or "(no path given)"))
        reference = str(crs or "").strip()
        if not reference:
            raise CapabilityError(
                "a coordinate reference system is required: a point list's numbers "
                "cannot say which system they are in")
        system = QgsCoordinateReferenceSystem(reference)
        if not system.isValid():
            raise CapabilityError("{} is not a coordinate reference system QGIS knows".format(reference))
        easting = str(easting_field or "").strip()
        northing = str(northing_field or "").strip()
        if not easting or not northing:
            # Naming one and not the other is refused rather than half-applied:
            # a guess for the second one is a guess about which axis is which.
            raise CapabilityError("both the easting and the northing column must be named")

        options = [
            "type=csv",
            "xField={}".format(easting),
            "yField={}".format(northing),
            "crs={}".format(system.authid() or reference),
            "spatialIndex=no",
            "subsetIndex=no",
            "watchFile=no",
        ]
        if delimiter:
            options.insert(1, "delimiter={}".format(delimiter))
        uri = "file://{}?{}".format(os.path.abspath(source), "&".join(options))

        label = str(name or "").strip() or os.path.splitext(os.path.basename(source))[0]
        layer = QgsVectorLayer(uri, label, "delimitedtext")
        if not layer.isValid():
            raise CapabilityError(
                "QGIS could not read {} as a point list. Check the delimiter and that "
                "{} and {} are column names in it.".format(
                    os.path.basename(source), easting, northing))
        count = layer.featureCount()
        if count == 0:
            # A layer with no points is not an import; reporting success would
            # leave the user looking for features that were never read.
            raise CapabilityError(
                "{} produced no points: the columns may name the wrong fields".format(
                    os.path.basename(source)))
        QgsProject.instance().addMapLayer(layer)
        return {
            "kind": "field_points_imported",
            "layer_id": layer.id(),
            "name": label,
            "feature_count": count,
            "crs": system.authid() or reference,
            "easting_field": easting,
            "northing_field": northing,
            "elevation_field": str(elevation_field or "").strip(),
        }

    def calculate_field(
        self,
        layer_id: str,
        field: str,
        expression: str,
        field_type: str = "number",
        preview: bool = False,
    ) -> dict[str, Any]:
        """Add a field computed from the layer's own fields.

        Two properties this has to hold. The expression is parsed by the bounded
        grammar in expressions.py before a single row is touched, so a bad
        expression is a refusal rather than a layer left half-modified. And an
        existing field is never overwritten: silently replacing a column the user
        already had is the one mistake here that destroys data rather than merely
        adding a wrong number.
        """
        from ._vendor.nivo import expressions

        layer = self.vector(layer_id)
        name = str(field or "").strip()
        # CapabilityError rather than _require: these are things the caller got
        # wrong and can fix, not the runtime being unavailable, and the two are
        # shown to the user in different words.
        if not name:
            raise CapabilityError("the new field needs a name")
        if len(name) > 60:
            raise CapabilityError("that field name is too long")

        existing = self.field_names(layer)
        if any(name.lower() == present.lower() for present in existing):
            raise CapabilityError(
                "{} already exists on this layer. Pick a different name - I will not "
                "overwrite a column you already have.".format(name))

        try:
            compiled = expressions.compile_expression(expression, existing)
        except expressions.ExpressionError as error:
            raise CapabilityError(str(error)) from None

        referenced = expressions.referenced_fields(compiled)
        missing = sorted(referenced - set(existing))
        if missing:
            raise CapabilityError("this layer has no field called {}".format(", ".join(missing)))

        # Evaluate first, write second. A preview and a real run compute exactly
        # the same values, so what the user approves is what lands.
        values: dict[int, Any] = {}
        failures: list[str] = []
        sample: list[Any] = []
        for feature in layer.getFeatures():
            row = {key: feature[key] for key in existing}
            try:
                value = expressions.evaluate(compiled, row)
            except expressions.ExpressionError as error:
                # One unusable row must not abandon the layer, but the count is
                # reported rather than hidden.
                value = None
                if len(failures) < 3:
                    failures.append(str(error))
            values[feature.id()] = value
            if len(sample) < 5 and value is not None:
                sample.append(value)

        if preview:
            return {
                "kind": "field_preview", "field": name, "rows": len(values),
                "sample": sample, "nulls": sum(1 for v in values.values() if v is None),
                "problems": failures,
            }

        # Imported here rather than at module scope: everything above this line
        # is validation, and it must be able to refuse a bad expression without
        # QGIS being importable at all.
        from qgis.core import QgsField

        from .qt_compat import field_type as resolve_field_type

        kind = str(field_type).lower()
        if kind not in ("number", "integer", "text"):
            kind = "number"
        provider = layer.dataProvider()
        if not provider.addAttributes([QgsField(name, resolve_field_type(kind))]):
            raise CapabilityError("this layer's storage does not accept a new field")
        layer.updateFields()

        index = layer.fields().indexOf(name)
        if index < 0:
            raise CapabilityError("the field was accepted but did not appear on the layer")

        # Coerced from the requested kind rather than from the resolved Qt enum,
        # because that enum's identity differs between Qt5 and Qt6 and comparing
        # against it would quietly write floats into an integer column on one of
        # the two bindings.
        coerce = {"integer": int, "text": str}.get(kind, float)
        changes = {}
        for feature_id, value in values.items():
            changes[feature_id] = {index: None if value is None else coerce(value)}
        provider.changeAttributeValues(changes)
        layer.updateFields()
        layer.triggerRepaint()
        return {
            "kind": "field_calculated", "field": name, "rows": len(values),
            "nulls": sum(1 for v in values.values() if v is None),
            "sample": sample, "problems": failures,
        }

    def reorder_layer(self, layer_id: str, position: str = "top") -> dict[str, Any]:
        """Move a layer in the drawing order.

        Drawing order is the difference between a map that reads and one where
        the polygons cover the labels, and it is the one cartographic complaint a
        user cannot phrase as a style change.
        """
        layer = self.layer(layer_id)
        root = self.project.layerTreeRoot()
        node = root.findLayer(layer.id())
        _require(node is not None, "that layer is not in the layer tree")

        parent = node.parent() or root
        children = list(parent.children())
        current = children.index(node)
        target = {"top": 0, "bottom": len(children) - 1,
                  "up": max(0, current - 1), "down": min(len(children) - 1, current + 1)}.get(
                      str(position).lower(), 0)
        if target == current:
            return {"kind": "reorder_unchanged", "position": position, "index": current}

        # A layer tree node cannot be moved, only cloned into place and the
        # original removed. Insert first so a failure leaves the tree intact
        # rather than short one layer.
        clone = node.clone()
        parent.insertChildNode(target, clone)
        parent.removeChildNode(node)
        self._push_undo({"kind": "layer_order", "layer_id": layer_id, "index": current})
        self.iface.mapCanvas().refresh()
        return {"kind": "reorder_applied", "position": position, "from": current, "to": target}

    def undo(self) -> dict[str, Any]:
        """Restore the previous state of the last reversible change."""
        if not self._undo:
            return {"kind": "nothing_to_undo"}
        entry = self._undo.pop()
        kind = entry.get("kind")
        try:
            layer = self.layer(str(entry.get("layer_id"))) if entry.get("layer_id") else None
        except RuntimeUnavailable:
            return {"kind": "undo_failed", "reason": "the layer is no longer in the project"}
        if kind == "selection" and layer is not None:
            layer.selectByIds(list(entry.get("ids") or []))
        elif kind == "visibility" and layer is not None:
            node = self.project.layerTreeRoot().findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(bool(entry.get("visible")))
        elif kind == "opacity" and layer is not None:
            layer.setOpacity(float(entry.get("opacity") or 100.0) / 100.0)
        elif kind == "renderer" and layer is not None and entry.get("renderer") is not None:
            layer.setRenderer(entry["renderer"])
            layer.triggerRepaint()
        elif kind == "filter" and layer is not None:
            layer.setSubsetString(str(entry.get("subset") or ""))
        elif kind == "labels" and layer is not None:
            layer.setLabeling(entry.get("labeling"))
            layer.setLabelsEnabled(bool(entry.get("enabled")))
            layer.triggerRepaint()
        elif kind == "layer_order" and layer is not None:
            # Declaring a capability reversible and then having undo say "that
            # change cannot be reversed" is worse than not offering undo at all,
            # so the branch exists for every kind _push_undo can record.
            node = self.project.layerTreeRoot().findLayer(layer.id())
            if node is None:
                return {"kind": "undo_failed", "reason": "the layer is no longer in the layer tree"}
            parent = node.parent() or self.project.layerTreeRoot()
            index = max(0, min(len(parent.children()) - 1, int(entry.get("index") or 0)))
            clone = node.clone()
            parent.insertChildNode(index, clone)
            parent.removeChildNode(node)
        else:
            return {"kind": "undo_failed", "reason": "that change cannot be reversed"}
        self.iface.mapCanvas().refresh()
        return {"kind": "undone", "change": kind}

    @property
    def undo_depth(self) -> int:
        return len(self._undo)


def _literal(value: Any) -> str:
    """Quote a filter literal: numbers bare, everything else escaped."""
    text = str(value).strip()
    try:
        float(text)
        return text
    except ValueError:
        return "'{}'".format(text.replace("'", "''"))


# --------------------------------------------------------------------------
# Capability dispatch
# --------------------------------------------------------------------------

def _measured_geometry(runtime: QGISRuntime, analysis: Mapping[str, Any], layer_id: str) -> dict[str, Any]:
    """Show a geometry ranking on the map; leave an aggregate alone.

    Kept here rather than in presentation.visualization_for because the spec
    builder keys off analytical result kinds and a ranking by shape is already
    a list of feature ids - there is nothing to infer.
    """
    ranked = analysis.get("ranked") or []
    if not ranked:
        return {"analysis": dict(analysis), "map": {"kind": "no_map_change"}}
    applied = runtime.select_features(layer_id, [entry["id"] for entry in ranked], zoom=False)
    return {"analysis": dict(analysis), "map": applied}


def _visualized_groups(runtime: QGISRuntime, analysis: Mapping[str, Any], layer_id: str) -> dict[str, Any]:
    """Draw a per-group measurement on the polygons it was measured over.

    Separate from the executor's generic `visualize` because the grouping field
    lives in the analysis, not in the request: the caller named two layers, not
    a column, and passing the wrong field here silently produces a legend of
    one grey class.
    """
    field = str(analysis.get("group_field") or "")
    spec = presentation.visualization_for(analysis, layer_id, field)
    applied = runtime.apply_visualization(spec) if spec.get("kind") != "none" else {"kind": "no_map_change"}
    return {"analysis": analysis, "map": applied}


def build_executor(runtime: QGISRuntime) -> Callable[[Mapping[str, Any]], Any]:
    """Map validated capability requests onto runtime methods.

    A dictionary rather than an if/elif chain: adding a capability is a registry
    entry plus one line here, which is what keeps the surface contributable.
    An analysis that can be shown on the map applies its visualization in the
    same step, so "analyse it" and "show me" are never two separate asks.
    """

    def visualize(result: Mapping[str, Any], layer_id: str, field: str = "") -> dict[str, Any]:
        spec = presentation.visualization_for(result, layer_id, field)
        applied = runtime.apply_visualization(spec) if spec.get("kind") != "none" else {"kind": "no_map_change"}
        return {"analysis": result, "map": applied}

    handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {
        # Survey computation. These take no runtime argument at all: they are
        # arithmetic over numbers in the request, so they answer with the
        # project closed and the network down, and they can be tested without
        # a QGIS at all.
        "coordinate.write@1": lambda p: {
            "lat": p["lat"], "lon": p["lon"],
            "dms": {"latitude": coordinates.format_dms(p["lat"], True),
                    "longitude": coordinates.format_dms(p["lon"], False)},
            "utm": coordinates.to_utm(
                p["lat"], p["lon"], survey.ellipsoid_by_name(p.get("ellipsoid"))),
            "mgrs": coordinates.to_mgrs(
                p["lat"], p["lon"], int(p.get("mgrs_digits", 5)),
                survey.ellipsoid_by_name(p.get("ellipsoid"))),
        },
        "coordinate.read@1": lambda p: {
            "lat": coordinates.parse_dms(p["latitude"]),
            "lon": coordinates.parse_dms(p["longitude"]),
        },
        "survey.scale_factor@1": lambda p: coordinates.combined_scale(
            p["lat"], p["lon"], float(p.get("height_m", 0)),
            str(p.get("height_reference", "orthometric")),
            float(p.get("geoid_separation_m", 0)),
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.inverse@1": lambda p: survey.inverse(
            p["from_lat"], p["from_lon"], p["to_lat"], p["to_lon"],
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.forward@1": lambda p: survey.forward(
            p["from_lat"], p["from_lon"], p["azimuth_deg"], p["distance_m"],
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.traverse@1": lambda p: survey.traverse(
            p["from_lat"], p["from_lon"], p["legs"], bool(p.get("closed")),
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.intersect@1": lambda p: intersect.intersect_bearings(
            p["first_lat"], p["first_lon"], p["first_azimuth_deg"],
            p["second_lat"], p["second_lon"], p["second_azimuth_deg"],
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.station_offset@1": lambda p: intersect.project_onto_line(
            p["start_lat"], p["start_lon"], p["end_lat"], p["end_lon"],
            p["point_lat"], p["point_lon"],
            survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "survey.closure@1": lambda p: survey.closure(
            p["legs"], survey.ellipsoid_by_name(p.get("ellipsoid"))),
        "measure.distance@1": lambda p: runtime.measure_distance(
            (p["from_lon"], p["from_lat"]), (p["to_lon"], p["to_lat"])),
        "export.layer@1": lambda p: runtime.export_layer(
            p["layer_id"], p.get("target_format", "gpkg"),
        ),
        "crs.diagnose@1": lambda p: runtime.diagnose_layer_crs(p["layer_id"]),
        "crs.transformations@1": lambda p: runtime.datum_transformations(
            p["source_crs"], p["target_crs"]),
        "map.legend@1": lambda p: runtime.show_layer_legend(
            p["layer_id"], bool(p.get("visible", True))),
        "field.import_points@1": lambda p: runtime.import_field_points(
            p["path"], p["crs"], p["easting_field"], p["northing_field"],
            str(p.get("name", "")), str(p.get("elevation_field", "")),
            str(p.get("delimiter", ""))),
        "field.calculate@1": lambda p: runtime.calculate_field(
            p["layer_id"], p["field"], p["expression"],
            p.get("field_type", "number"), bool(p.get("preview"))),
        "layer.reorder@1": lambda p: runtime.reorder_layer(p["layer_id"], p.get("position", "top")),
        "inspect.layer@1": lambda p: runtime.profile_layer(p["layer_id"]),
        "inspect.raster@1": lambda p: runtime.profile_layer(p["layer_id"]),
        "inspect.fields@1": lambda p: runtime.field_profile(p["layer_id"]),
        "inspect.project@1": lambda p: {
            "kind": "project_profile",
            "layers": [
                {"id": layer.id(), "name": layer.name(),
                 "crs": layer.crs().authid() if layer.crs().isValid() else ""}
                for layer in runtime.project.mapLayers().values()
            ],
        },
        "analytics.numeric@1": lambda p: runtime.numeric(p["layer_id"], p["field"], p.get("scope", "all")),
        "analytics.categories@1": lambda p: visualize(
            runtime.categories(p["layer_id"], p["field"], p.get("limit", 25), p.get("scope", "all")),
            p["layer_id"], p["field"],
        ),
        "analytics.histogram@1": lambda p: runtime.histogram(p["layer_id"], p["field"], p.get("bins", 12)),
        "analytics.top_n@1": lambda p: visualize(
            runtime.top_n(p["layer_id"], p["field"], p.get("limit", 10), p.get("ascending", False)),
            p["layer_id"], p["field"],
        ),
        "analytics.outliers@1": lambda p: visualize(
            runtime.outliers(p["layer_id"], p["field"], p.get("method", "iqr")), p["layer_id"], p["field"],
        ),
        # A ranking by geometry lands on the map as a selection, the same way a
        # ranking by a field does: "which polygon is the largest" is a question
        # whose answer the user wants highlighted, not just named. The
        # aggregates (sum, mean, min, max, count) change nothing on the canvas.
        "analytics.geometry@1": lambda p: _measured_geometry(
            runtime,
            runtime.measure_geometry(
                p["layer_id"], p["metric"], p.get("statistic", "sum"),
                p.get("limit", 5), p.get("scope", "all"),
            ),
            p["layer_id"],
        ),
        # The wire names are the server's; the runtime keeps its own signature,
        # so the adaptation lives here where the boundary is rather than being
        # spread through the analytics kernel.
        "analytics.group@1": lambda p: runtime.group(
            p["layer_id"], p["group_by"], p.get("field"), p.get("stat", "count"),
        ),
        "analytics.compare@1": lambda p: runtime.compare(
            p["layer_id"], p["field"], p.get("scope", "selection_vs_all"),
        ),
        "analytics.profile@1": lambda p: {
            "layer": runtime.profile_layer(p["layer_id"]),
            "fields": runtime.field_profile(p["layer_id"]),
        },
        # Spatial questions about two layers. Each one that can be shown on the
        # map shows itself in the same step: "how many buildings per district"
        # and "draw me that" are one ask, not two.
        "spatial.relate@1": lambda p: runtime.relate(
            p["layer_id"], p["other_layer_id"], p.get("predicate", "intersects"),
            bool(p.get("use_selection")),
        ),
        "spatial.near@1": lambda p: runtime.near(
            p["layer_id"], p["other_layer_id"], p["distance"], p.get("unit", "m"),
        ),
        "spatial.nearest@1": lambda p: runtime.nearest(p["layer_id"], p.get("k", 10)),
        "spatial.count_in_polygons@1": lambda p: _visualized_groups(
            runtime,
            runtime.count_in_polygons(p["polygon_layer_id"], p["point_layer_id"], p.get("group_field", "")),
            p["polygon_layer_id"],
        ),
        "spatial.density@1": lambda p: _visualized_groups(
            runtime,
            runtime.density(p["polygon_layer_id"], p["point_layer_id"], p.get("group_field", "")),
            p["polygon_layer_id"],
        ),
        # Asking a database a question it can answer. The request names a table,
        # a column and an operation; there is no field that can carry SQL.
        "postgis.profile@1": lambda p: runtime.postgis_profile(
            p["connection_id"], p["schema"], p["table"],
        ),
        "postgis.analyze@1": lambda p: runtime.postgis_analyze(
            p["connection_id"], p["schema"], p["table"], p["operation"],
            p.get("field", ""), p.get("group_field", ""), p.get("statistic", "count"),
            p.get("limit", 0), p.get("bbox"), p.get("id_field", ""),
            bool(p.get("ascending")), p.get("x"), p.get("y"), p.get("srid", 4326),
        ),
        "postgis.spatial@1": lambda p: runtime.postgis_spatial(
            p["connection_id"], p["schema"], p["table"], p["other_table"],
            p.get("operation", "relationship_count"), p.get("other_schema", ""),
            p.get("predicate", "intersects"), p.get("limit", 0), p.get("group_field", ""),
        ),
        "map.zoom_layer@1": lambda p: runtime.zoom_to_layer(p["layer_id"]),
        "map.zoom_selection@1": lambda p: runtime.zoom_to_selection(_active_layer_id(runtime)),
        "map.refresh@1": lambda p: (runtime.iface.mapCanvas().refresh(), {"kind": "refreshed"})[1],
        "map.previous_extent@1": lambda p: (
            runtime.iface.mapCanvas().zoomToPreviousExtent(), {"kind": "previous_extent"}
        )[1],
        "layer.visibility@1": lambda p: runtime.set_visibility(p["layer_id"], p["visible"]),
        "layer.opacity@1": lambda p: runtime.set_opacity(p["layer_id"], p["opacity"]),
        "layer.attribute_table@1": lambda p: (
            runtime.iface.showAttributeTable(runtime.vector(p["layer_id"])), {"kind": "table_opened"}
        )[1],
        "layer.activate@1": lambda p: (
            runtime.iface.setActiveLayer(runtime.layer(p["layer_id"])), {"kind": "layer_activated"}
        )[1],
        "selection.all@1": lambda p: (runtime.vector(p["layer_id"]).selectAll(), {"kind": "selected_all"})[1],
        "selection.clear@1": lambda p: (
            runtime.vector(p["layer_id"]).removeSelection(), {"kind": "selection_cleared"}
        )[1],
        "selection.invert@1": lambda p: (
            runtime.vector(p["layer_id"]).invertSelection(), {"kind": "selection_inverted"}
        )[1],
        "selection.by_ids@1": lambda p: runtime.select_features(p["layer_id"], p["feature_ids"]),
        "filter.preview@1": lambda p: runtime.preview_filter(
            p["layer_id"], p["field"], p["operator"], p.get("value", ""),
        ),
        "filter.clear@1": lambda p: runtime.clear_filter(p["layer_id"]),
        "style.categorized@1": lambda p: visualize(
            runtime.categories(p["layer_id"], p["field"]), p["layer_id"], p["field"],
        ),
        "style.graduated@1": lambda p: visualize(
            analytics.classify_breaks(
                [row[p["field"]] for row in runtime.records(
                    runtime.vector(p["layer_id"]), [p["field"]], "all")["rows"]],
                p.get("classes", 5), p.get("method", "quantile"),
            ),
            p["layer_id"], p["field"],
        ),
        "style.labels@1": lambda p: runtime.set_labels(
            p["layer_id"], p["field"], p.get("size", 9), p.get("enabled", True),
        ),
        "style.single@1": lambda p: runtime.style_single(
            p["layer_id"], p.get("color", ""), p.get("stroke_width"), p.get("size"), p.get("opacity"),
        ),
        "style.raster@1": lambda p: runtime.style_raster(
            p["layer_id"], p.get("band"), p.get("ramp", ""), p.get("opacity"),
        ),
        "style.undo@1": lambda p: runtime.undo(),
    }

    def execute(request: Mapping[str, Any]) -> Any:
        capability = str(request.get("capability") or "")
        handler = handlers.get(capability)
        if handler is None:
            # Registered but not bound to a desktop implementation: an honest
            # gap, not a silent no-op that looks like success.
            raise CapabilityError("{} is not available in this QGIS build yet".format(capability))
        return handler(dict(request.get("params") or {}))

    # What this build can actually carry out. Advertising more than this is how
    # a user gets "Nivo prepared a QGIS action" and a canvas that never moves.
    execute.capabilities = frozenset(handlers)
    return execute


# Capabilities the PLUGIN binds rather than the runtime, because they need the
# plugin's own async task runner, its viewport resolution or its layer loading.
# Declared here so the advertised set is one list with two consumers: the plugin
# builds its handler table from it, and `bound_capability_ids` includes it. Kept
# apart from the runtime table and not implemented here, because a runtime that
# reached back into the plugin would be the coupling this separation avoids.
PLUGIN_BOUND_CAPABILITIES = frozenset({
    "map.basemap@1",
    "map.zoom_extent@1",
    "processing.run@1",
    "processing.discover@1",
    # The ten named operations. They run through the same Processing task as
    # the generic bridge, so they belong to the plugin for the same reason it
    # does: the runtime has no async task runner and no layer loading.
    "geoprocessing.buffer@1",
    "geoprocessing.clip@1",
    "geoprocessing.intersection@1",
    "geoprocessing.union@1",
    "geoprocessing.difference@1",
    "geoprocessing.dissolve@1",
    "geoprocessing.merge@1",
    "geoprocessing.centroid@1",
    "geoprocessing.convex_hull@1",
    "geoprocessing.reproject@1",
    "geoprocessing.spatial_join@1",
    "geoprocessing.simplify@1",
    "geoprocessing.repair@1",
    "geoprocessing.validate@1",
    "geoprocessing.split@1",
    "geoprocessing.zonal_statistics@1",
    # The two server-side surfaces a desktop user otherwise had to leave QGIS to
    # reach: which runs exist and why one failed, and the review of a run that is
    # waiting on a person. Both need the plugin's API client, its project scope
    # and its task runner, none of which the runtime has.
    "mapdex.jobs@1",
    "mapdex.open_review@1",
    # Drawing needs the canvas map tool, the project the new layer joins and the
    # assistant context that has to learn about it - all of them the plugin's,
    # none of them the runtime's. The runtime could add a memory layer; it could
    # not arm a tool and wait for a person to click.
    "draw.geometry@1",
})


def bound_capability_ids() -> frozenset:
    """Every capability id this build implements, from either half.

    The runtime table is built with no runtime because only the handler *keys*
    are wanted; the lambdas close over the argument and are never called here.
    That keeps the advertised set derivable without a live QGIS, which is what
    lets the parity test run in CI.

    The plugin-bound ids are unioned in rather than listed separately, because
    the one question every caller is asking is "can this build perform it", and
    answering that with two sets is how `processing.run@1` ended up refused
    while `qgis:processing_operation@1`, the same operation, ran.
    """
    return build_executor(None).capabilities | PLUGIN_BOUND_CAPABILITIES


def _active_layer_id(runtime: QGISRuntime) -> str:
    layer = runtime.iface.activeLayer()
    _require(layer is not None, "no layer is active")
    return layer.id()
