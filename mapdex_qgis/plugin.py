from __future__ import annotations

import os
import json
import tempfile

from qgis.PyQt.QtCore import QLocale, QSettings, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction, QComboBox, QDockWidget, QFileDialog, QLabel, QPushButton, QVBoxLayout, QWidget
from qgis.core import QgsApplication, QgsProject, QgsTask, QgsVectorFileWriter

from .api_client import MapdexAPI, MapdexAPIError
from .generated_contracts import BatchKind


WORKFLOWS = (
    ("Georeference maps", BatchKind.GEOREFERENCE),
    ("Digitize parcels", BatchKind.DIGITIZE_PARCELS),
    ("Validate & deliver", BatchKind.VALIDATE_DELIVER),
    ("Full pipeline", BatchKind.FULL_PIPELINE),
)


class MapdexPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.api = MapdexAPI(QSettings().value("mapdex/base_url", "https://api.mapdex.ai"), QSettings().value("mapdex/token", ""))
        self.device_code = ""
        self.batch_id = ""
        self.project_id = ""
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_token)
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self._poll_batch)
        self.progress_pending = False

    def initGui(self):
        self.action = QAction(QIcon(), "Mapdex", self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu("Mapdex", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.poll_timer.stop()
        self.progress_timer.stop()
        if self.action:
            self.iface.removePluginMenu("Mapdex", self.action)
            self.iface.removeToolBarIcon(self.action)

    def show(self):
        if self.dock is None:
            self.dock = QDockWidget("Mapdex Companion Beta", self.iface.mainWindow())
            root = QWidget(); layout = QVBoxLayout(root)
            self.status = QLabel("Connect Mapdex to send the active layer.")
            self.connect_button = QPushButton("Connect in browser"); self.connect_button.clicked.connect(self.connect)
            self.project_box = QComboBox(); self.workflow_box = QComboBox(); self.input_box = QComboBox()
            for title, key in WORKFLOWS: self.workflow_box.addItem(title, key)
            self.input_box.addItem("Active layer", "active_layer")
            self.input_box.addItem("Current map extent", "extent")
            self.input_box.addItem("Choose a file…", "file")
            self.run_button = QPushButton("Send to Mapdex"); self.run_button.clicked.connect(self.run_input)
            self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(self.cancel_batch)
            self.retry_button = QPushButton("Retry failed items"); self.retry_button.clicked.connect(self.retry_failed)
            self.review_button = QPushButton("Open Review in browser"); self.review_button.clicked.connect(self.open_review)
            for widget in (self.status, self.connect_button, self.project_box, self.workflow_box, self.input_box, self.run_button, self.cancel_button, self.retry_button, self.review_button): layout.addWidget(widget)
            self.dock.setWidget(root); self.iface.addDockWidget(2, self.dock)
            if self.api.token: self._load_projects()
        self.dock.show(); self.dock.raise_()

    def _task(self, description, work, done):
        def runner(task): return work()
        def finished(exception, result=None): done(exception, result)
        QgsApplication.taskManager().addTask(QgsTask.fromFunction(description, runner, on_finished=finished))

    def connect(self):
        self._task("Create Mapdex device authorization", self.api.authorize_device, self._authorization_created)

    def _authorization_created(self, exception, response):
        if exception: self.status.setText(str(exception)); return
        self.device_code = response["device_code"]
        self.status.setText(f"Enter code {response['user_code']} in the browser.")
        QDesktopServices.openUrl(QUrl(response["verification_uri"] + "?code=" + response["user_code"]))
        self.poll_timer.start(max(5, response.get("interval", 5)) * 1000)

    def _poll_token(self):
        if not self.device_code: return
        try:
            response = self.api.poll_device_token(self.device_code)
        except MapdexAPIError as exc:
            if exc.status == 400: return
            self.poll_timer.stop(); self.status.setText(str(exc)); return
        self.poll_timer.stop(); self.api.token = response["access_token"]; self.project_id = response.get("project_id", "")
        QSettings().setValue("mapdex/token", self.api.token); self.status.setText("Connected to Mapdex."); self._load_projects()

    def _load_projects(self):
        self._task("Load Mapdex projects", self.api.projects, self._projects_loaded)

    def _projects_loaded(self, exception, projects):
        if exception: self.status.setText(str(exception)); return
        self.project_box.clear()
        for project in projects: self.project_box.addItem(project.get("name", "Project"), project["id"])
        if self.project_id:
            index = self.project_box.findData(self.project_id)
            if index >= 0: self.project_box.setCurrentIndex(index)

    def run_input(self):
        project_id = self.project_box.currentData() or self.project_id
        if not project_id: self.status.setText("Select a Mapdex project."); return
        mode = self.input_box.currentData()
        temp_dir = tempfile.mkdtemp(prefix="mapdex-qgis-")
        if mode == "file":
            path, _ = QFileDialog.getOpenFileName(self.iface.mainWindow(), "Choose spatial input")
            if not path: return
        elif mode == "extent":
            extent = self.iface.mapCanvas().extent()
            path = os.path.join(temp_dir, "map-extent.geojson")
            ring = [[extent.xMinimum(), extent.yMinimum()], [extent.xMaximum(), extent.yMinimum()], [extent.xMaximum(), extent.yMaximum()], [extent.xMinimum(), extent.yMaximum()], [extent.xMinimum(), extent.yMinimum()]]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"mapdex_input": "extent", "crs_authid": self.iface.mapCanvas().mapSettings().destinationCrs().authid()}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}, handle)
        else:
            layer = self.iface.activeLayer()
            if layer is None or not layer.isValid(): self.status.setText("Select a valid vector layer first."); return
            path = os.path.join(temp_dir, "active-layer.gpkg")
            options = QgsVectorFileWriter.SaveVectorOptions(); options.driverName = "GPKG"; options.layerName = "active_layer"
            result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsProject.instance().transformContext(), options)
            if result[0] != QgsVectorFileWriter.NoError: self.status.setText("Could not prepare the active layer."); return
        kind = self.workflow_box.currentData(); self.status.setText("Uploading active layer…")
        self._task("Send layer to Mapdex", lambda: self._upload_and_run(path, project_id, kind), self._run_started)

    def _upload_and_run(self, path, project_id, kind):
        uploaded = self.api.upload_file(path, project_id); file_id = uploaded.get("id") or uploaded.get("source_file_id")
        return self.api.start_batch(project_id, file_id, kind)

    def _run_started(self, exception, response):
        if exception: self.status.setText(str(exception)); return
        self.batch_id = response["id"]; self.status.setText("Mapdex batch started. Work continues in the background."); self.progress_timer.start(3000)

    def _poll_batch(self):
        if self.progress_pending or not self.batch_id: return
        self.progress_pending = True
        self._task("Refresh Mapdex batch", lambda: self.api.batch(self.project_box.currentData() or self.project_id, self.batch_id), self._batch_updated)

    def _batch_updated(self, exception, response):
        self.progress_pending = False
        if exception: self.status.setText(str(exception)); return
        state = response.get("state") or response.get("status") or "running"
        counts = response.get("counts") or {}
        self.status.setText(f"Mapdex: {state} · {counts.get('succeeded', 0)} completed · {counts.get('failed', 0)} failed")
        if state in ("completed", "failed", "cancelled", "partial"):
            self.progress_timer.stop()

    def cancel_batch(self):
        if self.batch_id: self._task("Cancel Mapdex batch", lambda: self.api.cancel_batch(self.project_box.currentData() or self.project_id, self.batch_id), self._batch_updated)

    def retry_failed(self):
        if self.batch_id: self._task("Retry failed Mapdex items", lambda: self.api.retry_failed(self.project_box.currentData() or self.project_id, self.batch_id), self._run_started)

    def open_review(self):
        if not self.batch_id: return
        locale = QLocale.system().name().split("_")[0]
        prefix = "" if locale == "en" else f"/{locale}"
        QDesktopServices.openUrl(QUrl(f"https://mapdex.ai{prefix}/workspace/review?batch={self.batch_id}"))
