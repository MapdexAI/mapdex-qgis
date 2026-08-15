"""Compact, QGIS-native Mapdex task panel (layout only)."""
from __future__ import annotations

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .layout_rules import (
    MINIMUM_WIDTH,
    PREFERRED_HEIGHT,
    PREFERRED_WIDTH,
    panel_layout_mode,
)
from .qt_compat import enum_member


def _section_label(text):
    label = QLabel(text)
    label.setObjectName("mapdexSectionLabel")
    return label


def _elastic(combo):
    """Let a combo shrink with the dock instead of demanding its widest item.

    A long project or file name otherwise pins a minimum width on the whole
    panel, which is what clipped every field until the dock was dragged wide.
    """
    combo.setSizeAdjustPolicy(
        enum_member(QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon")
    )
    combo.setMinimumContentsLength(8)
    combo.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Fixed"),
    )
    return combo


class _CompanionPanel(QWidget):
    """Panel root that re-lays itself out for the current dock width.

    QGIS docks are user-resizable and are often left narrow. Two-column
    forms and side-by-side control pairs clip their fields there, so below
    the compact breakpoint labels stack above their field and paired rows
    become one control per row.
    """

    def __init__(self):
        super().__init__()
        self._forms = []
        self._pairs = []
        self._mode = ""

    def register_form(self, form):
        self._forms.append(form)
        return form

    def register_pair(self, first, second):
        """Build a two-widget row that stacks when the dock is narrow.

        The row is a QBoxLayout whose direction is flipped on resize. Moving
        widgets between layout cells at runtime (removeWidget/addWidget) hands
        ownership back and forth between C++ and Python, and under PyQt6 that
        cost us a deleted QComboBox during plugin start-up. Adding each widget
        exactly once removes that whole class of problem.
        """
        row = QBoxLayout(enum_member(QBoxLayout, "Direction", "LeftToRight"))
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(first, 1)
        row.addWidget(second, 0)
        self._pairs.append((row, first, second))
        return row

    def sizeHint(self):
        return QSize(PREFERRED_WIDTH, PREFERRED_HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.apply_layout_mode(panel_layout_mode(self.width()))

    def apply_layout_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        compact = mode == "compact"
        wrap = enum_member(
            QFormLayout,
            "RowWrapPolicy",
            "WrapAllRows" if compact else "DontWrapRows",
        )
        for form in self._forms:
            form.setRowWrapPolicy(wrap)
        direction = enum_member(
            QBoxLayout, "Direction", "TopToBottom" if compact else "LeftToRight"
        )
        left = enum_member(Qt, "AlignmentFlag", "AlignLeft")
        right = enum_member(Qt, "AlignmentFlag", "AlignRight") | enum_member(
            Qt, "AlignmentFlag", "AlignVCenter"
        )
        for row, first, second in self._pairs:
            row.setDirection(direction)
            # Stacked, the trailing control starts at the left edge instead of
            # floating in the middle of the row.
            row.setAlignment(second, left if compact else right)
            row.setAlignment(first, left if compact else enum_member(Qt, "AlignmentFlag", "AlignVCenter"))


def build_companion_panel(workflows, endpoint_settings=True):
    """Build the panel.

    endpoint_settings=False is a released build talking to the hosted
    Mapdex: the API/web address controls are not shown at all, so nobody can
    point a production install somewhere else by accident.
    """
    root = _CompanionPanel()
    root.setObjectName("mapdexPluginRoot")
    root.setMinimumWidth(MINIMUM_WIDTH)
    root.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    root.setStyleSheet(
        """
        QWidget#mapdexPluginRoot { background: #191919; color: #F7F7F5; }
        QLabel#mapdexSectionLabel {
            color: #8F96A8;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        QLabel#mapdexStatus {
            padding: 9px 10px;
            background: #212121;
            color: #F7F7F5;
            border-left: 3px solid #6366F1;
        }
        QFrame#mapdexResultCard {
            background: transparent;
            border: 0;
        }
        QPushButton#mapdexPrimaryButton {
            min-height: 32px;
            padding: 4px 10px;
            font-weight: 600;
        }
        QFrame#mapdexSegmentBar { border-bottom: 1px solid palette(mid); }
        QFrame#mapdexNivoSurface {
            background: #212121;
            color: #F7F7F5;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 8px;
        }
        QLabel#mapdexNivoTitle {
            color: #F7F7F5;
            font-size: 13px;
            font-weight: 600;
        }
        QLabel#mapdexNivoContext,
        QLabel#mapdexNivoStatus {
            color: #8F96A8;
            font-size: 11px;
        }
        QScrollArea#mapdexChatTranscript {
            background: #191919;
            border: 1px solid rgba(230, 233, 242, 0.12);
            border-radius: 6px;
            padding: 6px;
        }
        QWidget#mapdexChatTranscriptContent { background: #191919; }
        QLineEdit#mapdexNivoInput {
            background: #1c1c1c;
            color: #F7F7F5;
            selection-background-color: #4F46E5;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 7px;
            padding: 8px 10px;
            min-height: 20px;
        }
        QLineEdit#mapdexNivoInput:focus { border-color: #6366F1; }
        QToolButton#mapdexSegment {
            color: #8F96A8;
            background: transparent;
            border: 0;
            border-bottom: 2px solid transparent;
            border-radius: 0;
            padding: 9px 13px 8px;
        }
        QToolButton#mapdexSegment:checked {
            color: palette(text);
            background: transparent;
            border-bottom-color: palette(highlight);
            font-weight: 600;
        }
        QStackedWidget#mapdexPages { background: transparent; }
        QWidget#mapdexPage { background: palette(window); }
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

    connection_label = QLabel("Not connected")
    connection_label.setWordWrap(True)
    connection_label.setStyleSheet("font-weight: 600;")
    settings_button = QToolButton()
    settings_button.setObjectName("mapdexSettingsButton")
    # Short label: paired with the connection state on one row, the long form
    # squeezed the state text into two lines at the dock's normal width.
    settings_button.setText("Settings")
    settings_button.setToolTip("Connection settings (API and web addresses)")
    settings_button.setCheckable(True)
    settings_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    layout.addLayout(root.register_pair(connection_label, settings_button))

    status = QLabel("Connect Mapdex to start a task.")
    status.setObjectName("mapdexStatus")
    status.setWordWrap(True)
    layout.addWidget(status)

    connection_panel = QFrame()
    connection_panel.setVisible(False)
    connection_layout = QVBoxLayout(connection_panel)
    connection_layout.setContentsMargins(0, 0, 0, 0)
    connection_form = root.register_form(QFormLayout())
    connection_form.setFieldGrowthPolicy(
        enum_member(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow")
    )
    api_url_input = _elastic(QComboBox())
    api_url_input.setEditable(True)
    web_url_input = _elastic(QComboBox())
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
    if not endpoint_settings:
        settings_button.setVisible(False)
        settings_button.setChecked(False)
        connection_panel.setVisible(False)

    connect_button = QPushButton("Connect Mapdex")
    connect_button.setObjectName("mapdexPrimaryButton")
    disconnect_button = QToolButton()
    disconnect_button.setText("Disconnect")
    disconnect_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    layout.addWidget(connect_button)
    layout.addWidget(disconnect_button, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))

    # Deliberately avoid QTabWidget: QGIS' themed tab pane allowed sibling
    # page widgets to bleed into the active page on narrow docks. This compact
    # segmented navigation controls one clipped stacked page at a time.
    segment_bar = QFrame()
    segment_bar.setObjectName("mapdexSegmentBar")
    segment_layout = QBoxLayout(enum_member(QBoxLayout, "Direction", "LeftToRight"))
    segment_bar.setLayout(segment_layout)
    segment_layout.setContentsMargins(0, 0, 0, 0)
    segment_layout.setSpacing(5)
    pages = QStackedWidget()
    pages.setObjectName("mapdexPages")
    # QGIS' stylesheet can leave non-current pages painted. Force StackOne so
    # only the selected page remains visible and receives layout geometry.
    pages.layout().setStackingMode(enum_member(QStackedLayout, "StackingMode", "StackOne"))
    layout.addWidget(segment_bar)
    layout.addWidget(pages, 1)

    workspace = QWidget()
    workspace.setObjectName("mapdexPage")
    workspace_layout = QVBoxLayout(workspace)
    workspace_layout.setContentsMargins(12, 12, 12, 12)
    workspace_layout.setSpacing(8)
    workspace_layout.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignTop"))
    new_task_label = _section_label("New task")
    open_project_button = QPushButton("Open project")
    open_project_button.setToolTip("Open this project in Mapdex web workspace")
    workspace_layout.addLayout(root.register_pair(new_task_label, open_project_button))
    form = root.register_form(QFormLayout())
    form.setFieldGrowthPolicy(enum_member(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow"))
    form.setSpacing(7)
    project_box = _elastic(QComboBox())
    workflow_box = _elastic(QComboBox())
    input_box = _elastic(QComboBox())
    for title, key in workflows:
        workflow_box.addItem(title, key)
    input_box.addItem("Select source…", "")
    input_box.addItem("Choose a file…", "file")
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

    # Nivo is a separate, map-aware companion surface. It calls the same
    # server compose path as Studio; it is not a local chatbot or Python console.
    nivo = QWidget()
    nivo.setObjectName("mapdexPage")
    nivo_layout = QVBoxLayout(nivo)
    nivo_layout.setContentsMargins(14, 14, 14, 14)
    nivo_layout.setSpacing(9)
    nivo_surface = QFrame()
    nivo_surface.setObjectName("mapdexNivoSurface")
    surface_layout = QVBoxLayout(nivo_surface)
    surface_layout.setContentsMargins(12, 12, 12, 12)
    surface_layout.setSpacing(9)
    nivo_title = QLabel("Nivo")
    nivo_title.setObjectName("mapdexNivoTitle")
    nivo_context = QLabel("No active QGIS layer")
    nivo_context.setObjectName("mapdexNivoContext")
    nivo_context.setWordWrap(True)
    # A widget transcript keeps messages as native Qt widgets. It intentionally
    # is not HTML: assistant text is data, never markup.
    nivo_reply = QScrollArea()
    nivo_reply.setObjectName("mapdexChatTranscript")
    nivo_reply.setWidgetResizable(True)
    nivo_reply.setHorizontalScrollBarPolicy(
        enum_member(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff")
    )
    transcript = QWidget()
    transcript.setObjectName("mapdexChatTranscriptContent")
    transcript_layout = QVBoxLayout(transcript)
    transcript_layout.setContentsMargins(8, 8, 8, 8)
    transcript_layout.setSpacing(8)
    transcript_layout.addStretch(1)
    nivo_reply.setWidget(transcript)
    nivo_reply.setMinimumHeight(170)
    nivo_reply.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    nivo_status = QLabel("Ready")
    nivo_status.setObjectName("mapdexNivoStatus")
    nivo_input = QLineEdit()
    nivo_input.setObjectName("mapdexNivoInput")
    nivo_input.setPlaceholderText("Ask Nivo about this layer or map view…")
    nivo_send_button = QPushButton("Ask Nivo")
    nivo_send_button.setObjectName("mapdexPrimaryButton")
    surface_layout.addWidget(nivo_title)
    surface_layout.addWidget(nivo_context)
    surface_layout.addWidget(nivo_reply, 1)
    surface_layout.addWidget(nivo_status)
    surface_layout.addWidget(nivo_input)
    surface_layout.addWidget(nivo_send_button)
    nivo_layout.addWidget(nivo_surface, 1)

    jobs = QWidget()
    jobs.setObjectName("mapdexPage")
    jobs_layout = QVBoxLayout(jobs)
    jobs_layout.setContentsMargins(12, 12, 12, 12)
    jobs_layout.setSpacing(10)
    jobs_layout.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignTop"))

    batch = QFrame()
    batch.setObjectName("mapdexResultCard")
    batch_layout = QVBoxLayout(batch)
    batch_layout.setContentsMargins(10, 10, 10, 10)
    batch_layout.setSpacing(7)
    batch_title = QLabel("Current task")
    batch_title.setStyleSheet("font-weight: 600;")
    batch_layout.addWidget(batch_title)
    phase_label = QLabel("Preparing task")
    phase_label.setStyleSheet("font-weight: 600;")
    batch_layout.addWidget(phase_label)
    progress_bar = QProgressBar()
    progress_bar.setRange(0, 100)
    progress_bar.setValue(0)
    batch_layout.addWidget(progress_bar)
    guidance_label = QLabel("Mapdex will show the next action here.")
    guidance_label.setWordWrap(True)
    guidance_label.setStyleSheet("color: palette(placeholder-text); font-size: 11px;")
    batch_layout.addWidget(guidance_label)
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
    jobs_layout.addWidget(batch)

    recent = QWidget()
    recent_layout = QVBoxLayout(recent)
    recent_layout.setContentsMargins(0, 4, 0, 0)
    recent_layout.setSpacing(6)
    recent_layout.addWidget(_section_label("Recent tasks"))
    recent_box = _elastic(QComboBox())
    resume_button = QPushButton("Resume")
    recent_layout.addLayout(root.register_pair(recent_box, resume_button))
    jobs_layout.addWidget(recent)
    jobs_layout.addStretch(1)
    # Nivo is the primary companion surface. Explicitly hide sibling pages on
    # every switch as a QGIS theme safety net against stacked-page bleed.
    page_order = (nivo, workspace, jobs)
    for page in page_order:
        pages.addWidget(page)

    def switch_page(target):
        pages.setCurrentIndex(target)
        for index, page in enumerate(page_order):
            page.setVisible(index == target)

    for index, title in enumerate(("Nivo AI", "Task", "Jobs")):
        button = QToolButton()
        button.setObjectName("mapdexSegment")
        button.setText(title)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.clicked.connect(lambda _checked=False, target=index: switch_page(target))
        segment_layout.addWidget(button)
        if index == 0:
            button.setChecked(True)
    switch_page(0)
    segment_layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)
    # Lay out for the width the dock opens at; resizeEvent takes over after.
    root.apply_layout_mode(panel_layout_mode(PREFERRED_WIDTH))
    return root, {
        "status": status,
        "connection_label": connection_label,
        "api_url_input": api_url_input,
        "web_url_input": web_url_input,
        "save_settings_button": save_settings_button,
        "connect_button": connect_button,
        "disconnect_button": disconnect_button,
        "workspace": workspace,
        "tabs": pages,
        "batch": batch,
        "batch_title": batch_title,
        "phase_label": phase_label,
        "progress_bar": progress_bar,
        "guidance_label": guidance_label,
        "project_box": project_box,
        "open_project_button": open_project_button,
        "workflow_box": workflow_box,
        "input_box": input_box,
        "source_summary": source_summary,
        "run_button": run_button,
        "nivo_context": nivo_context,
        "nivo_reply": nivo_reply,
        "nivo_status": nivo_status,
        "nivo_input": nivo_input,
        "nivo_send_button": nivo_send_button,
        "cancel_button": cancel_button,
        "retry_button": retry_button,
        "import_button": import_button,
        "review_button": review_button,
        "recent": recent,
        "recent_box": recent_box,
        "resume_button": resume_button,
    }
