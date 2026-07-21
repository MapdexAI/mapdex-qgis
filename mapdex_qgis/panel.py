"""Mapdex for QGIS dock panel — layout only (no network)."""
from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
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


def build_companion_panel(workflows):
    """Return (root_widget, refs) for the Mapdex for QGIS dock body."""
    root = QWidget()
    root.setObjectName("mapdexPluginRoot")
    root.setAutoFillBackground(True)
    root.setMinimumWidth(300)
    root.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    root.setStyleSheet(
        """
        QWidget#mapdexPluginRoot {
            background-color: palette(window);
        }
        QFrame#mapdexCard {
            background-color: palette(base);
            border: 1px solid palette(mid);
            border-radius: 4px;
        }
        QLabel#mapdexStatus {
            color: palette(window-text);
            padding: 8px;
            background-color: palette(base);
            border: 1px solid palette(mid);
            border-radius: 4px;
        }
        QGroupBox {
            font-weight: 600;
            margin-top: 10px;
            padding-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }
        """
    )

    outer = QVBoxLayout(root)
    outer.setContentsMargins(10, 10, 10, 10)
    outer.setSpacing(10)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(enum_member(QFrame, "Shape", "NoFrame"))
    scroll.setHorizontalScrollBarPolicy(
        enum_member(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")
    )

    body = QWidget()
    body.setObjectName("mapdexPluginRoot")
    body.setAutoFillBackground(True)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)

    heading = QLabel("Mapdex for QGIS")
    heading.setStyleSheet("font-size: 14px; font-weight: 600;")
    subtitle = QLabel("Send layers from QGIS to Mapdex. Bring verified vector results back.")
    subtitle.setWordWrap(True)
    subtitle.setStyleSheet("color: palette(placeholder-text);")

    status = QLabel("Not connected.")
    status.setObjectName("mapdexStatus")
    status.setWordWrap(True)
    status.setMinimumHeight(48)

    # --- Account ---
    account = QGroupBox("Account")
    account_layout = QVBoxLayout(account)
    account_layout.setSpacing(8)

    advanced_button = QToolButton()
    advanced_button.setText("Advanced connection settings")
    advanced_button.setCheckable(True)
    advanced_button.setChecked(False)
    advanced_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextBesideIcon")
    )
    advanced_button.setArrowType(enum_member(Qt, "ArrowType", "RightArrow"))
    account_layout.addWidget(advanced_button)

    advanced = QWidget()
    advanced.setVisible(False)
    advanced_layout = QVBoxLayout(advanced)
    advanced_layout.setContentsMargins(0, 0, 0, 0)
    advanced_layout.setSpacing(6)
    settings_form = QFormLayout()
    settings_form.setLabelAlignment(enum_member(Qt, "AlignmentFlag", "AlignLeft"))
    settings_form.setSpacing(6)
    api_url_input = QComboBox()
    api_url_input.setEditable(True)
    api_url_input.setInsertPolicy(enum_member(QComboBox, "InsertPolicy", "NoInsert"))
    for suggestion in (
        "https://api.mapdex.ai",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ):
        api_url_input.addItem(suggestion)
    web_url_input = QComboBox()
    web_url_input.setEditable(True)
    web_url_input.setInsertPolicy(enum_member(QComboBox, "InsertPolicy", "NoInsert"))
    for suggestion in (
        "https://mapdex.ai",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ):
        web_url_input.addItem(suggestion)
    settings_form.addRow("API URL", api_url_input)
    settings_form.addRow("Web URL", web_url_input)
    advanced_layout.addLayout(settings_form)
    save_settings_button = QPushButton("Save connection settings")
    advanced_layout.addWidget(save_settings_button)
    account_layout.addWidget(advanced)

    def toggle_advanced(checked):
        advanced.setVisible(checked)
        advanced_button.setArrowType(
            enum_member(Qt, "ArrowType", "DownArrow" if checked else "RightArrow")
        )

    advanced_button.toggled.connect(toggle_advanced)

    connect_button = QPushButton("Connect in browser")
    connect_button.setDefault(True)
    disconnect_button = QPushButton("Disconnect")
    account_row = QHBoxLayout()
    account_row.addWidget(connect_button)
    account_row.addWidget(disconnect_button)
    account_layout.addLayout(account_row)
    # --- Workspace (hidden until connected) ---
    workspace = QGroupBox("Workspace")
    workspace_layout = QVBoxLayout(workspace)
    workspace_layout.setSpacing(8)

    form = QFormLayout()
    form.setLabelAlignment(enum_member(Qt, "AlignmentFlag", "AlignLeft"))
    form.setSpacing(8)
    project_box = QComboBox()
    project_box.setMinimumHeight(28)
    workflow_box = QComboBox()
    workflow_box.setMinimumHeight(28)
    for title, key in workflows:
        workflow_box.addItem(title, key)
    input_box = QComboBox()
    input_box.setMinimumHeight(28)
    input_box.addItem("Active vector layer", "active_layer")
    input_box.addItem("Current map extent", "extent")
    input_box.addItem("Choose a file…", "file")
    form.addRow("Project", project_box)
    form.addRow("Workflow", workflow_box)
    form.addRow("Input", input_box)
    workspace_layout.addLayout(form)

    run_button = QPushButton("Send to Mapdex")
    run_button.setMinimumHeight(34)
    workspace_layout.addWidget(run_button)

    # --- Batch (hidden until a batch exists) ---
    batch = QGroupBox("Batch")
    batch_layout = QVBoxLayout(batch)
    batch_layout.setSpacing(8)
    cancel_button = QPushButton("Cancel batch")
    retry_button = QPushButton("Retry failed items")
    import_button = QPushButton("Add results to QGIS")
    review_button = QPushButton("Open Review in browser")
    batch_layout.addWidget(cancel_button)
    batch_layout.addWidget(retry_button)
    batch_layout.addWidget(import_button)
    batch_layout.addWidget(review_button)

    layout.addWidget(heading)
    layout.addWidget(subtitle)
    layout.addWidget(status)
    layout.addWidget(account)
    layout.addWidget(workspace)
    layout.addWidget(batch)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)

    refs = {
        "status": status,
        "api_url_input": api_url_input,
        "web_url_input": web_url_input,
        "save_settings_button": save_settings_button,
        "connect_button": connect_button,
        "disconnect_button": disconnect_button,
        "workspace": workspace,
        "batch": batch,
        "project_box": project_box,
        "workflow_box": workflow_box,
        "input_box": input_box,
        "run_button": run_button,
        "cancel_button": cancel_button,
        "retry_button": retry_button,
        "import_button": import_button,
        "review_button": review_button,
    }
    return root, refs
