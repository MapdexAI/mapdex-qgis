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

from . import analytics, presentation, spatial
from .capabilities import CapabilityError
from .guard import log_debug

# Attribute analytics stream this many features at most. Above the cap the
# answer states that it was truncated.
MAX_ANALYTIC_FEATURES = 200_000
MAX_UNDO_DEPTH = 20


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

        indexes = [self._field_index(layer, name) for name in fields]
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes(indexes)
        # Geometry is the expensive part of a feature and no attribute
        # statistic needs it.
        request.setFlags(QgsFeatureRequest.NoGeometry)
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
        else:
            return {"kind": "no_map_change", "reason": "unsupported_visualization"}
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        entries = spec.get("classes") or spec.get("categories") or []
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

    def _graduated_renderer(self, layer: Any, spec: Mapping[str, Any]) -> Any:
        from qgis.core import QgsGraduatedSymbolRenderer, QgsRendererRange  # noqa: PLC0415

        ranges = []
        for entry in spec.get("classes") or []:
            symbol = self._symbol_for(layer, str(entry.get("color") or "#888888"))
            ranges.append(QgsRendererRange(
                float(entry.get("lower")), float(entry.get("upper")), symbol, str(entry.get("label") or "")
            ))
        return QgsGraduatedSymbolRenderer(str(spec.get("field") or ""), ranges)

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
        from . import expressions

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
        "measure.distance@1": lambda p: runtime.measure_distance(
            (p["from_lon"], p["from_lat"]), (p["to_lon"], p["to_lat"])),
        "export.layer@1": lambda p: runtime.export_layer(p["layer_id"], p.get("format", "gpkg")),
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
        "analytics.group@1": lambda p: runtime.group(
            p["layer_id"], p["group_field"], p.get("value_field"), p.get("statistic", "count"),
        ),
        "analytics.compare@1": lambda p: runtime.compare(
            p["layer_id"], p["field"], p.get("scope", "selection_vs_all"),
        ),
        "analytics.profile@1": lambda p: {
            "layer": runtime.profile_layer(p["layer_id"]),
            "fields": runtime.field_profile(p["layer_id"]),
        },
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


def bound_capability_ids() -> frozenset:
    """The capability ids this build implements.

    Built with no runtime because only the handler *keys* are wanted; the
    lambdas close over the argument and are never called here. That keeps the
    advertised set derivable without a live QGIS, which is what lets the parity
    test run in CI.
    """
    return build_executor(None).capabilities


def _active_layer_id(runtime: QGISRuntime) -> str:
    layer = runtime.iface.activeLayer()
    _require(layer is not None, "no layer is active")
    return layer.id()
