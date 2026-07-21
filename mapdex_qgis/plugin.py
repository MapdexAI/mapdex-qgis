from __future__ import annotations

import json
import os
import tempfile
import traceback
from typing import Callable, Optional

from qgis.PyQt.QtCore import Qt, QLocale, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QDockWidget, QFileDialog, QMessageBox
from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .api_client import MapdexAPI, MapdexAPIError, normalize_api_base
from .generated_contracts import BatchKind
from .panel import build_companion_panel
from .qt_compat import enum_member
from .results import (
    batch_is_terminal,
    batch_state,
    collect_geojson_artifact_urls,
    collect_vector_layer_imports,
    review_run_ids,
    succeeded_run_ids,
)
from .token_store import LEGACY_TOKEN_SETTING, qgis_token_store


WORKFLOWS = (
    ("Georeference maps", BatchKind.GEOREFERENCE),
    ("Digitize parcels", BatchKind.DIGITIZE_PARCELS),
    ("Validate & deliver", BatchKind.VALIDATE_DELIVER),
    ("Full pipeline", BatchKind.FULL_PIPELINE),
)

DEFAULT_API = "https://api.mapdex.ai"
DEFAULT_WEB = "https://mapdex.ai"


class _WorkTask(QgsTask):
    """Small QgsTask wrapper — more reliable than QgsTask.fromFunction across builds."""

    def __init__(self, description: str, work: Callable):
        flags = enum_member(QgsTask, "Flag", "CanCancel")
        super().__init__(description, flags)
        self._work = work
        self.result = None
        self.error = None  # type: Optional[BaseException]

    def run(self):
        try:
            self.result = self._work()
            return True
        except BaseException as exc:  # noqa: BLE001 — surface to UI callback
            self.error = exc
            return False


class MapdexPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        settings = QSettings()
        self.token_store = qgis_token_store(settings)
        persisted_token = self.token_store.load() if self.token_store is not None else ""
        # Never continue using the historical plaintext setting. Existing users
        # reconnect once and receive encrypted QGIS Authentication DB storage.
        settings.remove(LEGACY_TOKEN_SETTING)
        self.api = MapdexAPI(
            settings.value("mapdex/base_url", DEFAULT_API),
            persisted_token,
        )
        self.web_base = str(settings.value("mapdex/web_base", DEFAULT_WEB)).rstrip("/")
        self.device_code = ""
        self.batch_id = ""
        self.project_id = str(settings.value("mapdex/project_id", "") or "")
        self.imported_layer_ids = set()  # type: set[str]
        self._last_batch = None  # type: Optional[dict]
        self._busy = False
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_token)
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self._poll_batch)
        self.progress_pending = False
        # Widget refs filled in _ensure_dock
        self.status = None
        self.api_url_input = None
        self.web_url_input = None
        self.save_settings_button = None
        self.connect_button = None
        self.disconnect_button = None
        self.workspace = None
        self.batch_group = None
        self.project_box = None
        self.workflow_box = None
        self.input_box = None
        self.run_button = None
        self.cancel_button = None
        self.retry_button = None
        self.import_button = None
        self.review_button = None

    def initGui(self):
        self.action = QAction(QIcon(), "Mapdex", self.iface.mainWindow())
        self.action.setToolTip("Open Mapdex for QGIS")
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu("&Mapdex", self.action)
        self.iface.addToolBarIcon(self.action)
        # Register the dock immediately so QGIS places it in the right rail,
        # not as a floating overlay over the menu bar.
        self._ensure_dock()
        self.dock.hide()

    def unload(self):
        self.poll_timer.stop()
        self.progress_timer.stop()
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action:
            self.iface.removePluginMenu("&Mapdex", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def _ensure_dock(self):
        if self.dock is not None:
            return
        self.dock = QDockWidget("Mapdex for QGIS", self.iface.mainWindow())
        self.dock.setObjectName("MapdexForQgisDock")
        left = enum_member(Qt, "DockWidgetArea", "LeftDockWidgetArea")
        right = enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea")
        self.dock.setAllowedAreas(left | right)
        self.dock.setMinimumWidth(320)
        self.dock.setFeatures(
            enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetMovable")
            | enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetFloatable")
            | enum_member(QDockWidget, "DockWidgetFeature", "DockWidgetClosable")
        )

        root, refs = build_companion_panel(WORKFLOWS)
        for key, value in refs.items():
            setattr(self, key if key != "batch" else "batch_group", value)

        self._load_connection_fields()
        self.connect_button.clicked.connect(self.connect)
        self.disconnect_button.clicked.connect(self.disconnect)
        self.save_settings_button.clicked.connect(self.save_connection_settings)
        self.run_button.clicked.connect(self.run_input)
        self.cancel_button.clicked.connect(self.cancel_batch)
        self.retry_button.clicked.connect(self.retry_failed)
        self.import_button.clicked.connect(self.import_results)
        self.review_button.clicked.connect(self.open_review)

        self.dock.setWidget(root)
        self.iface.addDockWidget(
            enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea"),
            self.dock,
        )
        if self.api.token:
            self._set_status("Connected. Loading projects…")
            self._refresh_ui()
            self._load_projects()
        else:
            self._set_status("Not connected. Click Connect in browser to link this QGIS session.")
            self._refresh_ui()

    def show(self):
        self._ensure_dock()
        self.dock.show()
        self.dock.raise_()
        # If QGIS restored it as floating over the chrome, re-dock on the right.
        if self.dock.isFloating():
            self.dock.setFloating(False)
            self.iface.addDockWidget(
                enum_member(Qt, "DockWidgetArea", "RightDockWidgetArea"),
                self.dock,
            )

    def _set_status(self, text: str, toast: bool = False):
        if self.status is not None:
            self.status.setText(text)
        if toast:
            try:
                self.iface.messageBar().pushMessage("Mapdex", text, level=0, duration=4)
            except Exception:
                pass

    def _load_connection_fields(self):
        if self.api_url_input is None:
            return
        self.api_url_input.setEditText(self.api.base_url or DEFAULT_API)
        self.web_url_input.setEditText(self.web_base or DEFAULT_WEB)

    def save_connection_settings(self):
        api_url = normalize_api_base(self.api_url_input.currentText())
        web_url = (self.web_url_input.currentText() or DEFAULT_WEB).strip().rstrip("/")
        if not api_url:
            QMessageBox.warning(self.iface.mainWindow(), "Mapdex", "API URL is required.")
            return
        self.api.base_url = api_url
        self.web_base = web_url
        settings = QSettings()
        settings.setValue("mapdex/base_url", api_url)
        settings.setValue("mapdex/web_base", web_url)
        self._set_status("Saved. API = {api} · Web = {web}".format(api=api_url, web=web_url))

    def _apply_connection_settings_from_fields(self):
        """Read current URL fields before connect (even if Save was not clicked)."""
        api_url = normalize_api_base(self.api_url_input.currentText())
        web_url = (self.web_url_input.currentText() or DEFAULT_WEB).strip().rstrip("/")
        if api_url:
            self.api.base_url = api_url
            QSettings().setValue("mapdex/base_url", api_url)
        if web_url:
            self.web_base = web_url
            QSettings().setValue("mapdex/web_base", web_url)

    def _show_error(self, title: str, exc: BaseException):
        detail = str(exc)
        if isinstance(exc, MapdexAPIError):
            bits = [detail]
            if exc.url:
                bits.append("\n\n{method} {url}".format(method=exc.method or "HTTP", url=exc.url))
            if exc.correlation_id:
                bits.append("\ncorrelation_id: {cid}".format(cid=exc.correlation_id))
            if exc.status == 404 and "api.mapdex.ai" in (exc.url or ""):
                bits.append(
                    "\n\nProduction may not expose device auth yet. "
                    "Set API URL to http://127.0.0.1:8080 while the local API is running."
                )
            detail = "".join(bits)
        self._set_status(str(exc).split("\n")[0])
        QMessageBox.warning(self.iface.mainWindow(), title, detail)

    def _refresh_ui(self):
        if self.connect_button is None:
            return
        connected = bool(self.api.token)
        has_batch = bool(self.batch_id)

        self.connect_button.setVisible(not connected)
        self.connect_button.setEnabled(not self._busy)
        self.disconnect_button.setVisible(connected)
        self.disconnect_button.setEnabled(connected and not self._busy)

        self.workspace.setVisible(connected)
        self.workspace.setEnabled(connected and not self._busy)
        self.run_button.setEnabled(connected and not self._busy)

        self.batch_group.setVisible(connected and has_batch)
        self.cancel_button.setEnabled(has_batch and not self._busy)
        self.retry_button.setEnabled(has_batch and not self._busy)
        self.import_button.setEnabled(has_batch and not self._busy)
        self.review_button.setEnabled(has_batch)

    def _task(self, description: str, work: Callable, done: Callable, busy: bool = True):
        if busy and self._busy:
            self._set_status("Please wait — Mapdex is still working…")
            return
        if busy:
            self._busy = True
            self._refresh_ui()
        task = _WorkTask(description, work)
        finished_once = {"done": False}

        def finished():
            if finished_once["done"]:
                return
            finished_once["done"] = True
            if busy:
                self._busy = False
            try:
                if task.error is not None:
                    done(task.error, None)
                else:
                    done(None, task.result)
            finally:
                if busy:
                    self._refresh_ui()

        task.taskCompleted.connect(finished)
        task.taskTerminated.connect(finished)
        QgsApplication.taskManager().addTask(task)

    def _active_project_id(self) -> str:
        data = self.project_box.currentData() if self.project_box is not None else None
        return str(data or self.project_id or "")

    def connect(self):
        self._apply_connection_settings_from_fields()
        self._set_status("Starting browser connection via {url}…".format(url=self.api.base_url))
        self._task("Mapdex device authorization", self.api.authorize_device, self._authorization_created)

    def disconnect(self):
        self.poll_timer.stop()
        self.progress_timer.stop()
        self.api.token = ""
        self.device_code = ""
        self.batch_id = ""
        self.project_id = ""
        self._last_batch = None
        self.imported_layer_ids.clear()
        settings = QSettings()
        if self.token_store is not None:
            self.token_store.clear()
        else:
            settings.remove(LEGACY_TOKEN_SETTING)
        settings.remove("mapdex/project_id")
        self.project_box.clear()
        self._set_status("Disconnected.")
        self._refresh_ui()

    def _authorization_created(self, exception, response):
        if exception:
            self._show_error("Mapdex connection failed", exception)
            return
        if not isinstance(response, dict):
            self._show_error("Mapdex connection failed", RuntimeError("Empty authorization response"))
            return
        self.device_code = response.get("device_code") or ""
        user_code = response.get("user_code") or ""
        if not self.device_code or not user_code:
            self._show_error("Mapdex connection failed", RuntimeError("Authorization response missing codes"))
            return
        self._set_status(f"Approve code {user_code} in your browser, then return to QGIS.")
        verify = response.get("verification_uri") or f"{self.web_base}/device"
        QDesktopServices.openUrl(QUrl(f"{verify}?code={user_code}"))
        interval = max(5, int(response.get("interval") or 5))
        self.poll_timer.start(interval * 1000)

    def _poll_token(self):
        if not self.device_code:
            return
        try:
            response = self.api.poll_device_token(self.device_code)
        except MapdexAPIError as exc:
            if exc.status in (400, 428):
                # Still pending user approval.
                return
            self.poll_timer.stop()
            self._show_error("Mapdex connection failed", exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.poll_timer.stop()
            self._show_error("Mapdex connection failed", exc)
            return
        self.poll_timer.stop()
        token = (response or {}).get("access_token")
        if not token:
            self._show_error("Mapdex connection failed", RuntimeError("No access token returned"))
            return
        self.api.token = token
        self.project_id = str((response or {}).get("project_id") or self.project_id or "")
        settings = QSettings()
        persisted = self.token_store is not None and self.token_store.save(self.api.token)
        if self.project_id:
            settings.setValue("mapdex/project_id", self.project_id)
        if persisted:
            self._set_status("Connected securely to Mapdex.")
        else:
            self._set_status(
                "Connected for this QGIS session. Unlock the QGIS Authentication Database "
                "to keep the connection after restart."
            )
        self._refresh_ui()
        self._load_projects()

    def _load_projects(self):
        self._task("Load Mapdex projects", self.api.projects, self._projects_loaded)

    def _projects_loaded(self, exception, projects):
        if exception:
            self._show_error("Could not load projects", exception)
            return
        self.project_box.clear()
        items = projects if isinstance(projects, list) else []
        for project in items:
            if not isinstance(project, dict):
                continue
            name = project.get("name") or project.get("title") or "Project"
            project_id = project.get("id") or ""
            if project_id:
                self.project_box.addItem(str(name), project_id)
        if self.project_id:
            index = self.project_box.findData(self.project_id)
            if index >= 0:
                self.project_box.setCurrentIndex(index)
        if self.project_box.count() == 0:
            self._set_status(
                "Connected, but no projects were returned. "
                "If the API requires admin scope for /v1/projects, use the project_id from the token."
            )
        else:
            self._set_status("Connected. Choose a project and send work.")

    def run_input(self):
        project_id = self._active_project_id()
        if not project_id:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Mapdex",
                "Select a Mapdex project first.",
            )
            return
        mode = self.input_box.currentData()
        temp_dir = tempfile.mkdtemp(prefix="mapdex-qgis-")
        try:
            if mode == "file":
                path, _ = QFileDialog.getOpenFileName(
                    self.iface.mainWindow(),
                    "Choose spatial input",
                    "",
                    "Spatial files (*.gpkg *.geojson *.json *.shp *.tif *.tiff *.pdf);;All files (*.*)",
                )
                if not path:
                    return
            elif mode == "extent":
                extent = self.iface.mapCanvas().extent()
                path = os.path.join(temp_dir, "map-extent.geojson")
                ring = [
                    [extent.xMinimum(), extent.yMinimum()],
                    [extent.xMaximum(), extent.yMinimum()],
                    [extent.xMaximum(), extent.yMaximum()],
                    [extent.xMinimum(), extent.yMaximum()],
                    [extent.xMinimum(), extent.yMinimum()],
                ]
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "type": "FeatureCollection",
                            "features": [
                                {
                                    "type": "Feature",
                                    "properties": {
                                        "mapdex_input": "extent",
                                        "crs_authid": self.iface.mapCanvas()
                                        .mapSettings()
                                        .destinationCrs()
                                        .authid(),
                                    },
                                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                                }
                            ],
                        },
                        handle,
                    )
            else:
                layer = self.iface.activeLayer()
                if layer is None or not layer.isValid():
                    QMessageBox.information(
                        self.iface.mainWindow(),
                        "Mapdex",
                        "Select a valid vector layer in the Layers panel first.",
                    )
                    return
                path = os.path.join(temp_dir, "active-layer.gpkg")
                options = QgsVectorFileWriter.SaveVectorOptions()
                options.driverName = "GPKG"
                options.layerName = "active_layer"
                result = QgsVectorFileWriter.writeAsVectorFormatV3(
                    layer, path, QgsProject.instance().transformContext(), options
                )
                if result[0] != QgsVectorFileWriter.NoError:
                    raise RuntimeError("Could not export the active layer to GeoPackage.")
        except Exception as exc:  # noqa: BLE001
            self._show_error("Could not prepare input", exc)
            return

        kind = self.workflow_box.currentData()
        self.imported_layer_ids.clear()
        self._set_status("Uploading to Mapdex…")
        self._task(
            "Send layer to Mapdex",
            lambda: self._upload_and_run(path, project_id, kind),
            self._run_started,
        )

    def _upload_and_run(self, path, project_id, kind):
        uploaded = self.api.upload_file(path, project_id)
        file_id = uploaded.get("id") or uploaded.get("source_file_id")
        if not file_id:
            raise RuntimeError("Upload succeeded but no file id was returned.")
        return self.api.start_batch(project_id, file_id, kind)

    def _run_started(self, exception, response):
        if exception:
            self._show_error("Send to Mapdex failed", exception)
            return
        self.batch_id = (response or {}).get("id") or ""
        if not self.batch_id:
            self._show_error("Send to Mapdex failed", RuntimeError("No batch id returned"))
            return
        self.project_id = self._active_project_id()
        QSettings().setValue("mapdex/project_id", self.project_id)
        self._set_status("Batch started. Mapdex is working in the background…")
        self._refresh_ui()
        self.progress_timer.start(3000)

    def _poll_batch(self):
        if self.progress_pending or not self.batch_id or self._busy:
            return
        self.progress_pending = True
        project_id = self._active_project_id()
        self._task(
            "Refresh Mapdex batch",
            lambda: self.api.batch(project_id, self.batch_id),
            self._batch_updated,
            busy=False,
        )

    def _batch_updated(self, exception, response):
        self.progress_pending = False
        if exception:
            self._show_error("Batch status failed", exception)
            return
        self._last_batch = response
        state = batch_state(response)
        counts = (response or {}).get("counts") or {}
        self._set_status(
            "Mapdex: {state} · {ok} completed · {review} need review · {failed} failed".format(
                state=state or "unknown",
                ok=counts.get("succeeded", 0),
                review=counts.get("needs_review", 0),
                failed=counts.get("failed", 0),
            )
        )
        self._refresh_ui()
        if batch_is_terminal(response or {}):
            self.progress_timer.stop()
            if succeeded_run_ids(response or {}):
                self._task(
                    "Import Mapdex results into QGIS",
                    lambda: self._fetch_result_files(response),
                    self._results_imported,
                )
            elif review_run_ids(response or {}):
                self._set_status(
                    "Batch needs review in the browser before vector results can be imported."
                )

    def import_results(self):
        if not self.batch_id:
            return
        project_id = self._active_project_id()

        def work():
            detail = self._last_batch or self.api.batch(project_id, self.batch_id)
            self._last_batch = detail
            return self._fetch_result_files(detail)

        self._set_status("Fetching Mapdex results…")
        self._task("Import Mapdex results into QGIS", work, self._results_imported)

    def _fetch_result_files(self, detail: dict):
        project_id = self._active_project_id()
        prepared = []
        for run_id in succeeded_run_ids(detail):
            run = self.api.run(run_id, project_id)
            for layer in collect_vector_layer_imports(run):
                layer_id = layer["layer_id"]
                if layer_id in self.imported_layer_ids:
                    continue
                geojson = self.api.layer_geojson(layer_id, project_id)
                path = os.path.join(
                    tempfile.mkdtemp(prefix="mapdex-qgis-result-"),
                    f"{layer_id}.geojson",
                )
                with open(path, "wb") as handle:
                    handle.write(geojson)
                prepared.append(
                    {
                        "path": path,
                        "name": "Mapdex · {}".format(layer["name"]),
                        "layer_id": layer_id,
                    }
                )
            for artifact in collect_geojson_artifact_urls(run):
                key = artifact["url"]
                if key in self.imported_layer_ids:
                    continue
                raw = self.api.download_bytes(artifact["url"], project_id)
                path = os.path.join(
                    tempfile.mkdtemp(prefix="mapdex-qgis-result-"),
                    "artifact.geojson",
                )
                with open(path, "wb") as handle:
                    handle.write(raw)
                prepared.append(
                    {
                        "path": path,
                        "name": "Mapdex · {}".format(artifact["name"]),
                        "layer_id": key,
                    }
                )
        return {"files": prepared, "batch": detail}

    def _results_imported(self, exception, payload):
        if exception:
            self._show_error("Could not import results", exception)
            return
        files = (payload or {}).get("files") or []
        detail = (payload or {}).get("batch") or self._last_batch or {}
        added = 0
        for item in files:
            layer = QgsVectorLayer(item["path"], item["name"], "ogr")
            if not layer.isValid():
                continue
            QgsProject.instance().addMapLayer(layer)
            self.imported_layer_ids.add(item["layer_id"])
            added += 1
        review_n = len(review_run_ids(detail))
        if added:
            msg = "Added {} Mapdex result layer(s) to the project.".format(added)
            if review_n:
                msg += " {} item(s) still need browser Review.".format(review_n)
            self._set_status(msg)
            self.iface.mapCanvas().refresh()
        elif review_n:
            self._set_status("Open Review — no approved vector layers are ready to import yet.")
        else:
            self._set_status("Batch finished, but no vector GeoJSON layers were available to add.")

    def cancel_batch(self):
        if not self.batch_id:
            return
        project_id = self._active_project_id()

        def done(exception, _response):
            if exception:
                self._show_error("Cancel failed", exception)
                return
            self._set_status("Cancel requested. Refreshing batch…")
            self.progress_timer.start(2000)

        self._task(
            "Cancel Mapdex batch",
            lambda: self.api.cancel_batch(project_id, self.batch_id),
            done,
        )

    def retry_failed(self):
        if not self.batch_id:
            return
        project_id = self._active_project_id()

        def done(exception, response):
            if exception:
                self._show_error("Retry failed", exception)
                return
            retried = (response or {}).get("retried", 0)
            self._set_status("Retried {} failed item(s). Watching progress…".format(retried))
            self.imported_layer_ids.clear()
            self.progress_timer.start(3000)

        self._task(
            "Retry failed Mapdex items",
            lambda: self.api.retry_failed(project_id, self.batch_id),
            done,
        )

    def open_review(self):
        if not self.batch_id:
            return
        locale = QLocale.system().name().split("_")[0]
        prefix = "" if locale == "en" else "/{}".format(locale)
        QDesktopServices.openUrl(
            QUrl("{}{}/workspace/review?batch={}".format(self.web_base, prefix, self.batch_id))
        )


# Keep optional debug import available without binding into runtime paths.
_ = traceback
