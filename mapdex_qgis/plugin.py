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
    QgsRasterLayer,
)

from .api_client import MapdexAPI, MapdexAPIError, device_verification_url, normalize_api_base
from .generated_contracts import BatchKind
from .guidance import ACTIVE_STATES, task_guidance
from .panel import build_companion_panel
from .qt_compat import enum_member
from .results import (
    batch_is_terminal,
    batch_state,
    collect_geojson_artifact_urls,
    collect_vector_layer_imports,
    first_batch_error,
    review_run_ids,
    succeeded_run_ids,
)
from .token_store import LEGACY_TOKEN_SETTING, qgis_token_store
from .source_info import inspect_paths
from .workspace import task_workspace_path


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
        self.selected_paths = []  # type: list[str]
        self._last_batch = None  # type: Optional[dict]
        self._source_label = ""
        self._pending_is_batch = False
        self._busy = False
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_token)
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self._poll_batch)
        self.progress_pending = False
        # Widget refs filled in _ensure_dock
        self.status = None
        self.connection_label = None
        self.api_url_input = None
        self.web_url_input = None
        self.save_settings_button = None
        self.connect_button = None
        self.disconnect_button = None
        self.workspace = None
        self.batch_group = None
        self.batch_title = None
        self.phase_label = None
        self.progress_bar = None
        self.guidance_label = None
        self.project_box = None
        self.workflow_box = None
        self.input_box = None
        self.source_summary = None
        self.run_button = None
        self.cancel_button = None
        self.retry_button = None
        self.import_button = None
        self.review_button = None
        self.recent = None
        self.recent_box = None
        self.resume_button = None

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
        self.input_box.currentIndexChanged.connect(self._source_changed)
        self.workflow_box.currentIndexChanged.connect(self._workflow_changed)
        self.cancel_button.clicked.connect(self.cancel_batch)
        self.retry_button.clicked.connect(self.retry_failed)
        self.import_button.clicked.connect(self.import_results)
        self.review_button.clicked.connect(self.open_review)
        self.resume_button.clicked.connect(self.resume_recent)
        self._workflow_changed(self.workflow_box.currentIndex())
        self._load_recent_tasks()
        try:
            self.iface.currentLayerChanged.connect(
                lambda _layer: self._summarize_active_layer()
                if self.input_box.currentData() == "active_layer"
                else None
            )
        except AttributeError:
            pass

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
            if exc.status == 401:
                self.api.token = ""
                if self.token_store is not None:
                    self.token_store.clear()
                detail += "\n\nYour Mapdex session expired. Connect again to continue."
                self._refresh_ui()
        self._set_status(str(exc).split("\n")[0])
        QMessageBox.warning(self.iface.mainWindow(), title, detail)

    def _refresh_ui(self):
        if self.connect_button is None:
            return
        connected = bool(self.api.token)
        has_batch = bool(self.batch_id)
        state = batch_state(self._last_batch or {}) if has_batch else ""
        counts = (self._last_batch or {}).get("counts") or {}
        active = state in ACTIVE_STATES
        failed = int(counts.get("failed", 0) or 0)
        succeeded = int(counts.get("succeeded", 0) or 0)
        needs_review = int(counts.get("needs_review", 0) or 0)
        total = int(counts.get("total", 0) or 0)
        completed = succeeded + needs_review + failed + int(counts.get("cancelled", 0) or 0)

        if self.connection_label is not None:
            self.connection_label.setText("Connected to Mapdex" if connected else "Not connected")

        self.connect_button.setVisible(not connected)
        self.connect_button.setEnabled(not self._busy)
        self.disconnect_button.setVisible(connected)
        self.disconnect_button.setEnabled(connected and not self._busy)

        self.workspace.setVisible(connected)
        self.workspace.setEnabled(connected and not self._busy)
        self.run_button.setEnabled(connected and not self._busy and not active)

        self.batch_group.setVisible(connected and has_batch)
        self.cancel_button.setVisible(active)
        self.cancel_button.setEnabled(active and not self._busy)
        self.retry_button.setVisible(has_batch and failed > 0 and not active)
        self.retry_button.setEnabled(not self._busy)
        self.retry_button.setText(
            "Retry failed item" if failed == 1 else "Retry {} failed items".format(failed)
        )
        self.import_button.setVisible(has_batch and succeeded > 0 and not active)
        self.import_button.setEnabled(not self._busy)
        self.import_button.setText(
            "Add result to QGIS" if succeeded == 1 else "Add {} results to QGIS".format(succeeded)
        )
        self.review_button.setVisible(has_batch)
        self.review_button.setEnabled(not self._busy)
        if has_batch:
            guidance = task_guidance(state, counts)
            self.review_button.setText(guidance["action"])
            self.phase_label.setText(guidance["phase"])
            self.guidance_label.setText(guidance["hint"])
            if guidance["busy"]:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(guidance["progress"])
        if self.batch_title is not None:
            if active and total > 1:
                self.batch_title.setText("Processing {} of {} files".format(completed, total))
            else:
                self.batch_title.setText("Task in progress" if active else "Latest task")

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

    def _source_changed(self, _index):
        mode = self.input_box.currentData()
        if mode != "file":
            self.selected_paths = []
            if mode == "active_layer":
                self._summarize_active_layer()
            elif self.source_summary is not None:
                self.source_summary.setText("No source selected")
            return
        # Let the combo popup close before opening the native Windows dialog.
        # Opening it synchronously from currentIndexChanged can leave it behind
        # the QGIS window on some Qt5 builds.
        QTimer.singleShot(0, lambda selected_mode=mode: self._choose_source(selected_mode))

    def _choose_source(self, mode):
        if self.input_box.currentData() != mode:
            return
        file_filter = (
            "Spatial files (*.gpkg *.geojson *.json *.shp *.tif *.tiff *.pdf);;"
            "All files (*.*)"
        )
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(), "Choose a spatial file", "", file_filter
        )
        paths = [path] if path else []
        if not paths:
            self.input_box.blockSignals(True)
            self.input_box.setCurrentIndex(0)
            self.input_box.blockSignals(False)
            self.selected_paths = []
            return
        self.selected_paths = list(paths)
        label = (
            os.path.basename(paths[0])
            if len(paths) == 1
            else "{} files selected".format(len(paths))
        )
        self.input_box.setItemText(self.input_box.currentIndex(), label)
        self._source_label = label
        report = inspect_paths(self.selected_paths, str(self.workflow_box.currentData() or ""))
        self.source_summary.setText(
            report["summary"] if report["valid"] else "{} · {}".format(report["summary"], report["error"])
        )
        self._set_status(
            "Ready to start a task with {}.".format(label)
            if len(paths) == 1
            else "Ready to start a batch with {} files.".format(len(paths))
        )

    def _summarize_active_layer(self):
        layer = self.iface.activeLayer()
        if layer is None or not layer.isValid():
            self.source_summary.setText("No valid active QGIS layer")
            return
        kind = "Raster" if isinstance(layer, QgsRasterLayer) else "Vector" if isinstance(layer, QgsVectorLayer) else "Unsupported"
        crs = layer.crs().authid() if hasattr(layer, "crs") and layer.crs().isValid() else "No CRS"
        self._source_label = layer.name()
        self.source_summary.setText("{} · {} · {}".format(layer.name(), kind, crs))

    def _workflow_changed(self, _index):
        kind = self.workflow_box.currentData()
        current = self.input_box.currentData()
        self.input_box.blockSignals(True)
        self.input_box.clear()
        self.input_box.addItem("Select source…", "")
        self.input_box.addItem("Active QGIS layer", "active_layer")
        self.input_box.addItem("Choose a file…", "file")
        target = self.input_box.findData(current)
        self.input_box.setCurrentIndex(target if target >= 0 else 0)
        self.input_box.blockSignals(False)
        self.selected_paths = []
        self._source_label = ""
        self.source_summary.setText("No source selected")
        if kind == BatchKind.VALIDATE_DELIVER:
            self._set_status("Use the active vector layer or choose one file.")
        else:
            self._set_status("Use the active raster layer or choose one file.")

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
        verify = device_verification_url(
            response.get("verification_uri") or "", self.web_base, self.api.base_url
        )
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
            if exc.status == 429:
                delay = max(5, exc.retry_after or 30)
                self.poll_timer.setInterval(delay * 1000)
                self._set_status("Mapdex asked QGIS to slow down. Retrying in {} seconds…".format(delay))
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
        if not mode:
            QMessageBox.information(
                self.iface.mainWindow(), "Mapdex", "Choose a source file first."
            )
            return
        temp_dir = tempfile.mkdtemp(prefix="mapdex-qgis-")
        paths = []
        try:
            if mode == "file":
                if not self.selected_paths:
                    self._choose_source(mode)
                if not self.selected_paths:
                    return
                paths = list(self.selected_paths)
            else:
                layer = self.iface.activeLayer()
                if layer is None or not layer.isValid():
                    QMessageBox.information(
                        self.iface.mainWindow(),
                        "Mapdex",
                        "Select a valid layer in the QGIS Layers panel first.",
                    )
                    return
                kind = self.workflow_box.currentData()
                if isinstance(layer, QgsVectorLayer):
                    if kind != BatchKind.VALIDATE_DELIVER:
                        raise RuntimeError(
                            "This workflow needs a raster image. Select an open raster layer "
                            "or choose an image file."
                        )
                    path = os.path.join(temp_dir, "active-layer.gpkg")
                    options = QgsVectorFileWriter.SaveVectorOptions()
                    options.driverName = "GPKG"
                    options.layerName = "active_layer"
                    result = QgsVectorFileWriter.writeAsVectorFormatV3(
                        layer, path, QgsProject.instance().transformContext(), options
                    )
                    if result[0] != QgsVectorFileWriter.NoError:
                        raise RuntimeError("Could not export the active layer to GeoPackage.")
                elif isinstance(layer, QgsRasterLayer):
                    if kind == BatchKind.VALIDATE_DELIVER:
                        raise RuntimeError(
                            "Validate & deliver needs a vector layer. Select an open vector layer."
                        )
                    source = str(layer.source() or "").split("|", 1)[0]
                    if source.startswith("file:"):
                        source = QUrl(source).toLocalFile()
                    path = os.path.normpath(source)
                    if not os.path.isfile(path):
                        raise RuntimeError(
                            "The active raster is remote or has no local source file. "
                            "Save it locally first, then choose that file."
                        )
                else:
                    raise RuntimeError("The active QGIS layer type is not supported.")
                paths = [path]
        except Exception as exc:  # noqa: BLE001
            self._show_error("Could not prepare input", exc)
            return

        kind = self.workflow_box.currentData()
        report = inspect_paths(paths, str(kind or ""))
        self.source_summary.setText(report["summary"])
        if not report["valid"]:
            QMessageBox.warning(self.iface.mainWindow(), "Source is not compatible", report["error"])
            return
        self.imported_layer_ids.clear()
        self._pending_is_batch = len(paths) > 1
        self._set_status("Uploading to Mapdex…")
        self.batch_id = "uploading"
        self._last_batch = {"status": "created", "counts": {"total": 1}}
        self._refresh_ui()
        self._task(
            "Send layer to Mapdex",
            lambda: self._upload_and_run(paths, project_id, kind),
            self._run_started,
        )

    def _upload_and_run(self, paths, project_id, kind):
        file_ids = []
        for path in paths:
            uploaded = self.api.upload_file(path, project_id)
            file_id = uploaded.get("id") or uploaded.get("source_file_id")
            if not file_id:
                raise RuntimeError("Upload succeeded but no file id was returned.")
            file_ids.append(file_id)
        return {"batch": self.api.start_batch(project_id, file_ids, kind), "file_ids": file_ids}

    def _run_started(self, exception, response):
        if exception:
            self.batch_id = ""
            self._last_batch = None
            self._show_error("Send to Mapdex failed", exception)
            return
        payload = response or {}
        batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else payload
        file_ids = payload.get("file_ids") or []
        self.batch_id = (batch or {}).get("id") or ""
        if not self.batch_id:
            self._show_error("Send to Mapdex failed", RuntimeError("No batch id returned"))
            return
        self.project_id = self._active_project_id()
        self._remember_task(
            self.batch_id,
            self.project_id,
            str(self.workflow_box.currentData() or ""),
            self._source_label or "QGIS source",
            file_ids[0] if file_ids else "",
        )
        QSettings().setValue("mapdex/project_id", self.project_id)
        self._set_status(
            "Task started. Mapdex is working in the background…"
        )
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
            if isinstance(exception, MapdexAPIError) and exception.status == 429:
                delay = max(3, exception.retry_after or 15)
                self.progress_timer.setInterval(delay * 1000)
                self._set_status("Rate limit reached. Retrying task status in {} seconds…".format(delay))
                return
            self._show_error("Batch status failed", exception)
            return
        self._last_batch = response
        state = batch_state(response)
        counts = (response or {}).get("counts") or {}
        ok = int(counts.get("succeeded", 0) or 0)
        review = int(counts.get("needs_review", 0) or 0)
        failed = int(counts.get("failed", 0) or 0)
        if state in ("created", "queued", "pending"):
            message = "Task queued. Mapdex will start processing shortly."
        elif state == "running":
            message = "Processing in Mapdex…"
        elif failed and not ok and not review:
            message = first_batch_error(response or {}) or "Task failed. Retry it, or open Mapdex for details."
        elif failed:
            message = "Task finished with {} failed item(s). {} result(s) are ready.".format(failed, ok)
        elif review:
            message = "{} item(s) need review before they can be added to QGIS.".format(review)
        else:
            message = "Task complete. {} result(s) are ready to add to QGIS.".format(ok)
        self._set_status(message)
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
                    "This task needs review in the browser before vector results can be imported."
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
            self.iface.mapCanvas().setExtent(layer.extent())
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
            self._set_status("Task finished, but no vector GeoJSON layers were available to add.")

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
        project_id = self._active_project_id()
        workflow = str(self.workflow_box.currentData() or "") if self.workflow_box else ""
        fallback_file_id = ""
        for task in self._recent_tasks():
            if task.get("batch_id") == self.batch_id:
                workflow = str(task.get("workflow") or workflow)
                fallback_file_id = str(task.get("file_id") or "")
                break
        detail = self._last_batch
        if not detail and fallback_file_id:
            detail = {"items": [{"file_id": fallback_file_id, "state": "running"}]}
        path = task_workspace_path(project_id, workflow, detail)
        QDesktopServices.openUrl(
            QUrl("{}{}{}".format(self.web_base, prefix, path))
        )

    def _recent_tasks(self):
        raw = str(QSettings().value("mapdex/recent_tasks", "[]") or "[]")
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    def _remember_task(self, batch_id, project_id, workflow, source, file_id=""):
        tasks = [item for item in self._recent_tasks() if item.get("batch_id") != batch_id]
        tasks.insert(0, {"batch_id": batch_id, "project_id": project_id, "workflow": workflow, "source": source, "file_id": file_id})
        QSettings().setValue("mapdex/recent_tasks", json.dumps(tasks[:5]))
        self._load_recent_tasks()

    def _load_recent_tasks(self):
        if self.recent_box is None:
            return
        self.recent_box.clear()
        for item in self._recent_tasks():
            label = "{} · {}".format(item.get("workflow") or "Task", item.get("source") or "Source")
            self.recent_box.addItem(label, item)
        self.recent.setVisible(self.recent_box.count() > 0)

    def resume_recent(self):
        item = self.recent_box.currentData()
        if not isinstance(item, dict):
            return
        self.batch_id = str(item.get("batch_id") or "")
        self.project_id = str(item.get("project_id") or self.project_id)
        project_index = self.project_box.findData(self.project_id)
        if project_index >= 0:
            self.project_box.setCurrentIndex(project_index)
        self._last_batch = None
        self._set_status("Refreshing the selected Mapdex task…")
        self._refresh_ui()
        self._poll_batch()
        self.progress_timer.start(3000)


# Keep optional debug import available without binding into runtime paths.
_ = traceback
