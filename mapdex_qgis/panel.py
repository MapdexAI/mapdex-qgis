"""Compact, QGIS-native Mapdex task panel (layout only)."""
from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .qt_compat import enum_member


def _section_label(text):
    label = QLabel(text)
    label.setObjectName("mapdexSectionLabel")
    return label


def build_companion_panel(workflows):
    root = QWidget()
    root.setObjectName("mapdexPluginRoot")
    root.setMinimumWidth(300)
    root.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    root.setStyleSheet(
        """
        QWidget#mapdexPluginRoot { background: palette(window); }
        QLabel#mapdexSectionLabel {
            color: palette(placeholder-text);
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        QLabel#mapdexStatus {
            padding: 9px 10px;
            background: palette(base);
            border-left: 3px solid palette(highlight);
        }
        QFrame#mapdexResultCard {
            background: palette(base);
            border: 1px solid palette(mid);
            border-radius: 3px;
        }
        QPushButton#mapdexPrimaryButton {
            min-height: 34px;
            font-weight: 600;
        }
        QToolButton#mapdexSettingsButton { padding: 3px 6px; }
        """
    )

    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(enum_member(QFrame, "Shape", "NoFrame"))
    scroll.setHorizontalScrollBarPolicy(
        enum_member(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")
    )
    body = QWidget()
    body.setObjectName("mapdexPluginRoot")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)

    connection_row = QHBoxLayout()
    connection_label = QLabel("Not connected")
    connection_label.setStyleSheet("font-weight: 600;")
    settings_button = QToolButton()
    settings_button.setObjectName("mapdexSettingsButton")
    settings_button.setText("Connection settings")
    settings_button.setCheckable(True)
    settings_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    connection_row.addWidget(connection_label)
    connection_row.addStretch(1)
    connection_row.addWidget(settings_button)
    layout.addLayout(connection_row)

    status = QLabel("Connect Mapdex to start a task.")
    status.setObjectName("mapdexStatus")
    status.setWordWrap(True)
    layout.addWidget(status)

    connection_panel = QFrame()
    connection_panel.setVisible(False)
    connection_layout = QVBoxLayout(connection_panel)
    connection_layout.setContentsMargins(0, 0, 0, 0)
    connection_form = QFormLayout()
    api_url_input = QComboBox()
    api_url_input.setEditable(True)
    web_url_input = QComboBox()
    web_url_input.setEditable(True)
    for value in ("https://api.mapdex.ai", "http://127.0.0.1:8080"):
        api_url_input.addItem(value)
    for value in ("https://mapdex.ai", "http://127.0.0.1:3000"):
        web_url_input.addItem(value)
    connection_form.addRow("API", api_url_input)
    connection_form.addRow("Web", web_url_input)
    connection_layout.addLayout(connection_form)
    save_settings_button = QPushButton("Save settings")
    connection_layout.addWidget(save_settings_button)
    settings_button.toggled.connect(connection_panel.setVisible)
    layout.addWidget(connection_panel)

    connect_button = QPushButton("Connect Mapdex")
    connect_button.setObjectName("mapdexPrimaryButton")
    disconnect_button = QToolButton()
    disconnect_button.setText("Disconnect")
    disconnect_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    layout.addWidget(connect_button)
    layout.addWidget(disconnect_button, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))

    workspace = QWidget()
    workspace_layout = QVBoxLayout(workspace)
    workspace_layout.setContentsMargins(0, 4, 0, 0)
    workspace_layout.setSpacing(8)
    workspace_layout.addWidget(_section_label("New task"))
    form = QFormLayout()
    form.setFieldGrowthPolicy(enum_member(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow"))
    form.setSpacing(7)
    project_box = QComboBox()
    workflow_box = QComboBox()
    input_box = QComboBox()
    for title, key in workflows:
        workflow_box.addItem(title, key)
    input_box.addItem("Select source…", "")
    input_box.addItem("Choose a file…", "file")
    input_box.addItem("Choose multiple files…", "files")
    form.addRow("Project", project_box)
    form.addRow("Workflow", workflow_box)
    form.addRow("Source", input_box)
    workspace_layout.addLayout(form)
    source_summary = QLabel("No source selected")
    source_summary.setWordWrap(True)
    source_summary.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")
    workspace_layout.addWidget(source_summary)
    run_button = QPushButton("Start task")
    run_button.setObjectName("mapdexPrimaryButton")
    workspace_layout.addWidget(run_button)
    layout.addWidget(workspace)

    batch = QFrame()
    batch.setObjectName("mapdexResultCard")
    batch_layout = QVBoxLayout(batch)
    batch_layout.setContentsMargins(10, 10, 10, 10)
    batch_layout.setSpacing(7)
    batch_title = QLabel("Current task")
    batch_title.setStyleSheet("font-weight: 600;")
    batch_layout.addWidget(batch_title)
    retry_button = QPushButton("Retry failed item")
    retry_button.setObjectName("mapdexPrimaryButton")
    import_button = QPushButton("Add result to QGIS")
    import_button.setObjectName("mapdexPrimaryButton")
    review_button = QPushButton("Review in Mapdex")
    cancel_button = QToolButton()
    cancel_button.setText("Cancel task")
    cancel_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    for widget in (retry_button, import_button, review_button, cancel_button):
        batch_layout.addWidget(widget)
    layout.addWidget(batch)

    recent = QWidget()
    recent_layout = QVBoxLayout(recent)
    recent_layout.setContentsMargins(0, 4, 0, 0)
    recent_layout.setSpacing(6)
    recent_layout.addWidget(_section_label("Recent tasks"))
    recent_row = QHBoxLayout()
    recent_box = QComboBox()
    resume_button = QPushButton("Resume")
    recent_row.addWidget(recent_box, 1)
    recent_row.addWidget(resume_button)
    recent_layout.addLayout(recent_row)
    layout.addWidget(recent)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)
    return root, {
        "status": status,
        "connection_label": connection_label,
        "api_url_input": api_url_input,
        "web_url_input": web_url_input,
        "save_settings_button": save_settings_button,
        "connect_button": connect_button,
        "disconnect_button": disconnect_button,
        "workspace": workspace,
        "batch": batch,
        "batch_title": batch_title,
        "project_box": project_box,
        "workflow_box": workflow_box,
        "input_box": input_box,
        "source_summary": source_summary,
        "run_button": run_button,
        "cancel_button": cancel_button,
        "retry_button": retry_button,
        "import_button": import_button,
        "review_button": review_button,
        "recent": recent,
        "recent_box": recent_box,
        "resume_button": resume_button,
    }
