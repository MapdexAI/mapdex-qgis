"""Compact, QGIS-native Mapdex task panel (layout only)."""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import QEvent, QSize, Qt
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
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
    READING_WIDTH,
    panel_layout_mode,
)
from .panel_state import (
    CONNECT_PROMISE,
    FIRST_OPEN_PROMPT,
    FIRST_OPEN_TITLE,
    JOBS_LOCKED_NOTICE,
    OWN_MODEL_LABEL,
    OWN_MODEL_PROMISE,
    PROVIDER_CHOICES,
    TASK_LOCKED_NOTICE,
)
from .branding import ACTION_ICONS, CONTROL_ICONS, surface_asset_path
from .qt_compat import enum_member


NIVO_AVATAR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "assistant", "nivo.png"
)


class ActionRow(QFrame):
    """The panel's one repeated shape: icon, label, optional second line.

    A real widget rather than a QToolButton, because a QToolButton does not
    wrap its text: given a sentence as its second line it reports a sizeHint
    of the whole sentence on one line, which pushed the transcript to 720 px
    inside a 396 px dock and shifted every message off the right edge. Rendered
    and measured, not reasoned about.

    Every choice in this panel is this row. Before it a choice was sometimes a
    chip, sometimes a full-width button, sometimes bare text - two chips in one
    strip came out 364 px and 280 px wide at different heights - and that is
    most of what made the panel read as unfinished.
    """

    def __init__(self, label, sublabel="", icon=None, tone="normal", parent=None):
        super().__init__(parent)
        self.setObjectName({
            "primary": "mapdexRowPrimary",
            "quiet": "mapdexRowQuiet",
        }.get(tone, "mapdexRow"))
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(
            enum_member(QSizePolicy, "Policy", "Preferred"),
            enum_member(QSizePolicy, "Policy", "Minimum"),
        )
        self._callbacks = []

        row = QHBoxLayout(self)
        row.setContentsMargins(11, 9, 11, 9)
        row.setSpacing(9)
        if icon is not None:
            glyph = QLabel()
            glyph.setObjectName("mapdexRowGlyph")
            glyph.setFixedSize(18, 18)
            glyph.setPixmap(icon.pixmap(18, 18))
            row.addWidget(glyph, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        title = QLabel(label)
        title.setObjectName("mapdexRowLabel")
        title.setWordWrap(True)
        title.setMinimumWidth(1)
        text.addWidget(title)
        if sublabel:
            detail = QLabel(sublabel)
            detail.setObjectName("mapdexRowSub")
            detail.setWordWrap(True)
            detail.setMinimumWidth(1)
            text.addWidget(detail)
        row.addLayout(text, 1)

    def set_tone(self, tone):
        """Re-rank the row without rebuilding it.

        A row is not always the same rank: Connect is the one filled action
        while nothing has been chosen, and a quiet upgrade for somebody already
        answering from their own model. Rebuilding the widget to say that would
        drop its signal connections.
        """
        self.setObjectName({
            "primary": "mapdexRowPrimary",
            "quiet": "mapdexRowQuiet",
        }.get(tone, "mapdexRow"))
        for child in self.findChildren(QLabel):
            child.style().unpolish(child)
            child.style().polish(child)
        self.style().unpolish(self)
        self.style().polish(self)

    def clicked_connect(self, callback):
        """Keep the QToolButton-ish API the panel already reads as `clicked`."""
        self._callbacks.append(callback)

    @property
    def clicked(self):
        return self

    def connect(self, callback):  # noqa: A003 - mirrors the Qt signal shape
        self._callbacks.append(callback)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.rect().contains(event.position().toPoint()):
            for callback in list(self._callbacks):
                callback()

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.setProperty("disabled", not enabled)


def action_row(label, sublabel="", icon=None, tone="normal"):
    return ActionRow(label, sublabel, icon, tone)


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
        # Set by the plugin. The Mapdex mark on the toolbar and the menus is
        # chosen from the interface palette, and a theme sampled once at build
        # time is not theme-aware: a user who switches QGIS to Night Mapping
        # would keep an ink mark on an ink toolbar, which on screen looks
        # exactly like an icon that failed to load. This panel is a widget, so
        # it receives the application palette change; the actions are not its
        # children, so the plugin re-icons them from here.
        self.on_palette_change = None

    def changeEvent(self, event):
        super().changeEvent(event)
        kind = enum_member(QEvent, "Type", "PaletteChange")
        if event.type() == kind and callable(self.on_palette_change):
            self.on_palette_change()

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


def allow_narrow(widget):
    """Let a wrapping label be as narrow as the dock is.

    A label with `setWordWrap(True)` has already said it can be any width, and
    then reports a minimum as wide as its longest line anyway. Every one of
    those is a floor under the whole column, and together they made the panel
    un-narrowable: measured, a 300 px dock rendered a 614 px column, so half of
    every row - the Settings button included - was off the screen.

    Called at build time AND after the transcript draws. The first call alone
    was not enough and that is the instructive half: turn widgets are created
    later, so the rule applied to the panel and not to the conversation inside
    it, which is where most of the text lives.
    """
    for label in widget.findChildren(QLabel):
        if label.wordWrap():
            label.setMinimumWidth(1)
    # A resizable scroll area adopts its content's minimum width as its own,
    # so a wide transcript makes the whole panel un-narrowable even though the
    # scroll area is the one widget that could simply scroll. Measured: with a
    # real conversation in it the column would not go below 614 px whatever the
    # dock did. Let both ends of that pair shrink.
    for area in widget.findChildren(QScrollArea):
        area.setMinimumWidth(1)
        inner = area.widget()
        if inner is not None:
            inner.setMinimumWidth(1)
    if isinstance(widget, QScrollArea):
        widget.setMinimumWidth(1)
    widget.setMinimumWidth(1)


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
        /* Four control tiers, and the point of them is that only ONE thing on
           screen is filled. Seven controls used to claim "primary" and the
           style was three lines with no colour in it, so primary meant "a
           slightly bolder default button" and seven of those means none. */
        QPushButton#mapdexPrimaryButton {
            min-height: 32px;
            padding: 4px 12px;
            font-weight: 600;
            color: #FFFFFF;
            background: #4F46E5;
            border: 0;
            border-radius: 6px;
        }
        QPushButton#mapdexPrimaryButton:hover { background: #6366F1; }
        QPushButton#mapdexPrimaryButton:pressed { background: #4338CA; }
        QPushButton#mapdexPrimaryButton:disabled {
            color: #6B6B6B;
            background: #2A2A2A;
        }
        QPushButton#mapdexSecondaryButton {
            min-height: 32px;
            padding: 4px 11px;
            font-weight: 600;
            color: #C9CDD8;
            background: transparent;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 6px;
        }
        QPushButton#mapdexSecondaryButton:hover {
            color: #F7F7F5;
            border-color: #6366F1;
        }
        QPushButton#mapdexSecondaryButton:disabled {
            color: #6B6B6B;
            border-color: rgba(230, 233, 242, 0.12);
        }
        /* Fields. The panel painted its own #191919 surface and then left ten
           combos wearing Qt's default, which is what reads as unfinished. One
           treatment, the same one the composer already had. */
        QComboBox, QLineEdit {
            background: #1c1c1c;
            color: #F7F7F5;
            selection-background-color: #4F46E5;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 6px;
            padding: 6px 9px;
            min-height: 20px;
        }
        QComboBox:focus, QLineEdit:focus { border-color: #6366F1; }
        QComboBox:disabled, QLineEdit:disabled {
            color: #6B6B6B;
            background: #1a1a1a;
            border-color: rgba(230, 233, 242, 0.10);
        }
        QComboBox::drop-down { border: 0; width: 18px; }
        QComboBox QAbstractItemView {
            background: #212121;
            color: #F7F7F5;
            border: 1px solid rgba(230, 233, 242, 0.22);
            selection-background-color: #4F46E5;
            selection-color: #FFFFFF;
            outline: 0;
        }
        QFrame#mapdexSegmentBar { border-bottom: 1px solid rgba(230, 233, 242, 0.14); }
        QFrame#mapdexNivoSurface {
            background: transparent;
            color: #F7F7F5;
            border: 0;
        }
        QLabel#mapdexNivoTitleIcon { background: transparent; border: 0; }
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
        /* Which engine answers the next turn. Accent-coloured because it is a
           statement about where the user's data and money go, not chrome. */
        QLabel#mapdexNivoRuntime {
            color: #A5A2F5;
            font-size: 11px;
        }
        /* A turn. Styled by object name from HERE, never with an inline
           setStyleSheet on the card: an unscoped sheet on a container applies
           to every descendant, so `border: 0` on the card was erasing the
           border of every chip inside it. */
        QWidget#mapdexTurn { background: transparent; border: 0; }
        QWidget#mapdexOpeningTurn {
            background: #212121;
            border: 1px solid rgba(230, 233, 242, 0.14);
            border-radius: 8px;
        }
        QWidget#mapdexTurnUser { background: #4F46E5; border-radius: 8px; }
        QLabel#mapdexTurnWho { color: #8F96A8; font-weight: 600; }
        QLabel#mapdexTurnWhoUser { color: #FFFFFF; font-weight: 600; }
        QLabel#mapdexTurnBody { color: #F7F7F5; }
        QLabel#mapdexTurnBodyUser { color: #FFFFFF; }
        QLabel#mapdexTurnThinking { color: #8F96A8; }
        /* The measured line a turn opens with. Mono, because a file name, a
           pixel size and a CRS code are data and read as data. */
        QLabel#mapdexTurnFact {
            color: #8F96A8;
            font-family: "Geist Mono", "JetBrains Mono", monospace;
            font-size: 11px;
        }
        /* Free work and Mapdex work must be told apart before they are pressed.
           The account action is the only filled button in a finding. */
        QToolButton#mapdexFreeAction {
            color: #C9CDD8;
            background: #2A2A2A;
            border: 1px solid rgba(230, 233, 242, 0.20);
            border-radius: 6px;
            padding: 6px 11px;
            font-size: 11px;
        }
        QToolButton#mapdexFreeAction:hover { color: #F7F7F5; border-color: #6366F1; }
        QToolButton#mapdexAccountAction {
            color: #A5A2F5;
            background: transparent;
            border: 1px solid rgba(165, 162, 245, 0.5);
            border-radius: 6px;
            padding: 6px 11px;
            font-size: 11px;
        }
        QToolButton#mapdexAccountAction:hover {
            color: #C7C5FA;
            border-color: #A5A2F5;
        }
        QLabel#mapdexKeyState { color: #C9CDD8; font-size: 11px; }
        /* The reason under a control, not a separate announcement. A button
           that asks for a decision and supplies none reads as configuration. */
        QLabel#mapdexPromise { color: #8F96A8; font-size: 11px; }
        /* The one repeated shape. Same height, same radius, same padding, so a
           screen of choices reads as one list rather than as four products. */
        QFrame#mapdexRow, QFrame#mapdexRowPrimary, QFrame#mapdexRowQuiet {
            border-radius: 8px;
        }
        QFrame#mapdexRow {
            background: #212121;
            border: 1px solid rgba(230, 233, 242, 0.14);
        }
        QFrame#mapdexRow:hover { background: #2A2A2A; border-color: #6366F1; }
        QFrame#mapdexRowPrimary { background: #4F46E5; border: 0; }
        QFrame#mapdexRowPrimary:hover { background: #6366F1; }
        QFrame#mapdexRowQuiet { background: transparent; border: 0; }
        QLabel#mapdexRowLabel { color: #F7F7F5; font-size: 12px; }
        QLabel#mapdexRowSub { color: #8F96A8; font-size: 11px; }
        QLabel#mapdexRowGlyph { background: transparent; border: 0; }
        QFrame#mapdexRowPrimary QLabel#mapdexRowLabel { color: #FFFFFF; font-weight: 600; }
        QFrame#mapdexRowPrimary QLabel#mapdexRowSub { color: rgba(255, 255, 255, 0.78); }
        QFrame#mapdexRowQuiet QLabel#mapdexRowLabel { color: #A5A2F5; }
        QFrame#mapdexRowQuiet:hover QLabel#mapdexRowLabel { color: #C7C5FA; }
        /* A state is a dot and a word, top left, and it is the only place the
           panel says what it is doing. */
        QToolButton#mapdexHeaderButton {
            color: #C9CDD8;
            background: transparent;
            border: 1px solid rgba(230, 233, 242, 0.20);
            border-radius: 6px;
            padding: 5px 11px;
            font-size: 11px;
        }
        QToolButton#mapdexHeaderButton:hover { color: #F7F7F5; border-color: #6366F1; }
        QToolButton#mapdexHeaderButton:checked {
            color: #F7F7F5;
            background: #2A2A2A;
            border-color: #6366F1;
        }
        QToolButton#mapdexHeaderIcon {
            background: transparent;
            border: 1px solid transparent;
            border-radius: 6px;
            padding: 4px;
        }
        QToolButton#mapdexHeaderIcon:hover {
            background: #2A2A2A;
            border-color: rgba(230, 233, 242, 0.18);
        }
        QToolButton#mapdexHeaderIcon:disabled { opacity: 0.4; }
        QLabel#mapdexStateDot { font-size: 15px; }
        QLabel#mapdexStateWord { color: #C9CDD8; font-size: 12px; }
        QLabel#mapdexFirstOpenTitle {
            color: #F7F7F5;
            font-size: 18px;
            font-weight: 600;
        }
        QLabel#mapdexLockedNotice {
            padding: 12px;
            color: #C9CDD8;
            border: 1px solid rgba(230, 233, 242, 0.16);
            border-radius: 6px;
        }
        QScrollArea#mapdexChatTranscript {
            background: transparent;
            border: 0;
            padding: 0;
        }
        QWidget#mapdexChatTranscriptContent { background: transparent; }
        QFrame#mapdexComposer {
            background: #1c1c1c;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 8px;
        }
        QFrame#mapdexComposer:focus-within { border-color: #6366F1; }
        QLineEdit#mapdexNivoInput {
            background: transparent;
            color: #F7F7F5;
            selection-background-color: #4F46E5;
            border: 0;
            padding: 6px 6px;
            min-height: 22px;
        }
        QToolButton#mapdexSendButton {
            background: #4F46E5;
            border: 0;
            border-radius: 6px;
            padding: 5px 7px;
        }
        QToolButton#mapdexSendButton:hover { background: #6366F1; }
        QToolButton#mapdexSendButton:disabled { background: #2A2A2A; }
        QToolButton#mapdexStopButton {
            color: #C9CDD8;
            background: transparent;
            border: 1px solid rgba(230, 233, 242, 0.22);
            border-radius: 6px;
            padding: 4px 9px;
            font-size: 11px;
        }
        QToolButton#mapdexSegment {
            color: #8F96A8;
            background: transparent;
            border: 0;
            border-bottom: 2px solid transparent;
            border-radius: 0;
            padding: 9px 13px 8px;
        }
        QToolButton#mapdexSegment:checked {
            color: #F7F7F5;
            background: transparent;
            border-bottom-color: #6366F1;
            font-weight: 600;
        }
        QStackedWidget#mapdexPages { background: transparent; }
        QWidget#mapdexPage { background: #191919; }
        QToolButton#mapdexSettingsButton { padding: 3px 6px; }
        QToolButton#mapdexNivoHeaderButton {
            color: #C9CDD8;
            background: #212121;
            border: 1px solid rgba(230, 233, 242, 0.18);
            border-radius: 6px;
            padding: 4px 9px;
            font-size: 11px;
        }
        QToolButton#mapdexNivoHeaderButton:hover {
            color: #F7F7F5;
            border-color: #6366F1;
        }
        QToolButton#mapdexNivoHeaderButton:disabled { color: #6B6B6B; }
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
    body_row = QHBoxLayout(body)
    body_row.setContentsMargins(0, 0, 0, 0)
    body_row.setSpacing(0)
    column = QWidget()
    column.setObjectName("mapdexColumn")
    column.setMaximumWidth(READING_WIDTH)
    # A capped column pinned to the left edge is what "there is a huge gap on
    # the right" actually is: measured at a 1,540 px dock the column sat at
    # x=0, 720 wide, with 820 px of nothing beside it. Centred, the same cap
    # keeps the line length readable and the empty width reads as margin
    # instead of as a column that failed to fill.
    #
    # Equal stretch on both sides, and the column takes NO stretch of its own:
    # with stretch on the column the spacers collapse and it goes back to the
    # left edge.
    # The column takes the larger share and the two spacers divide what its cap
    # leaves over. With the spacers on equal footing (or ahead of it) they ate
    # the width and the column stayed at its minimum - 347 px inside a 1,540 px
    # dock - so centring it only moved the emptiness from one side to both.
    # The ratio is what makes one rule serve both docks. The column asks for
    # essentially all of the width, so in a narrow dock the spacers round down
    # to a few pixels and it fills - at 4:1:1 a 420 px dock kept 140 px empty,
    # which is the same waste at the other end of the scale. In a wide dock the
    # cap stops it and the spacers divide the remainder evenly, so the emptiness
    # becomes a margin rather than a column that failed to fill.
    body_row.addStretch(1)
    body_row.addWidget(column, 100)
    body_row.addStretch(1)
    column.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Preferred"),
    )
    # Below the cap the column has to actually shrink. It would not: its own
    # minimum came from descendants that refuse to be narrower than their
    # text, so a 420 px dock got a 512 px column and 92 px of it was simply
    # off-screen. `_shrinkable` is applied to every wrapping label as it is
    # built; this is the container's half of the same rule.
    column.setMinimumWidth(1)
    layout = QVBoxLayout(column)
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
    settings_button.setObjectName("mapdexHeaderButton")
    settings_button.setCheckable(True)
    settings_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    # Disconnect belongs with the connection state it acts on, immediately left
    # of Settings, rather than stranded at the bottom of the panel under
    # unrelated controls. The two sit in one widget so the responsive pair
    # keeps working: on a narrow dock the label stacks above both actions.
    disconnect_button = QToolButton()
    disconnect_button.setObjectName("mapdexHeaderButton")
    disconnect_button.setText("Disconnect")
    disconnect_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    nivo_new_button = QToolButton()
    nivo_new_button.setObjectName("mapdexNivoHeaderButton")
    nivo_new_button.setText("New chat")
    nivo_new_button.setToolTip(
        "Start a new conversation. The current one is kept in History; "
        "nothing is deleted from Mapdex."
    )
    nivo_new_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    nivo_history_button = QToolButton()
    nivo_history_button.setObjectName("mapdexNivoHeaderButton")
    nivo_history_button.setText("History")
    nivo_history_button.setToolTip(
        "Open an earlier Nivo conversation in this project and continue it"
    )
    nivo_history_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    header_actions = QWidget()
    header_actions_row = QHBoxLayout(header_actions)
    header_actions_row.setContentsMargins(0, 0, 0, 0)
    header_actions_row.setSpacing(6)
    # Icon-only, with tooltips, because a label here costs width the map is
    # paying for and these two are reached rarely.
    for control, key in ((nivo_new_button, "new_chat"),
                         (nivo_history_button, "history")):
        control.setObjectName("mapdexHeaderIcon")
        control.setToolButtonStyle(
            enum_member(Qt, "ToolButtonStyle", "ToolButtonIconOnly"))
        control.setIcon(QIcon(surface_asset_path(CONTROL_ICONS[key])))
        control.setIconSize(QSize(16, 16))
        control.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        header_actions_row.addWidget(control)
    header_actions_row.addWidget(disconnect_button)
    header_actions_row.addWidget(settings_button)
    layout.addLayout(root.register_pair(connection_label, header_actions))

    status = QLabel("Connect Mapdex to start a task.")
    status.setObjectName("mapdexStatus")
    status.setWordWrap(True)
    layout.addWidget(status)

    connection_panel = QFrame()
    connection_panel.setVisible(False)
    connection_layout = QVBoxLayout(connection_panel)
    connection_layout.setContentsMargins(0, 0, 0, 0)
    # The endpoint fields are the part a released build pins; the assistant
    # settings below them stay available either way, because choosing your own
    # model provider is a user decision, not a deployment one.
    endpoint_frame = QFrame()
    endpoint_layout = QVBoxLayout(endpoint_frame)
    endpoint_layout.setContentsMargins(0, 0, 0, 0)
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
    endpoint_layout.addLayout(connection_form)
    connection_layout.addWidget(endpoint_frame)

    # Nivo assistant runtime. With no provider the turn goes through Mapdex on
    # the user's plan; with one configured it goes straight to that provider and
    # never reaches Mapdex. The privacy label below is rendered from the SAME
    # resolution the turn uses (`plugin.assistant_runtime`), so it states the
    # live answer rather than an intention - reading it from a second place is
    # how the panel came to promise a path the turn never took.
    connection_layout.addWidget(_section_label("Nivo assistant"))
    assistant_form = root.register_form(QFormLayout())
    assistant_form.setFieldGrowthPolicy(
        enum_member(QFormLayout, "FieldGrowthPolicy", "AllNonFixedFieldsGrow")
    )
    provider_box = _elastic(QComboBox())
    # One list, in panel_state, so the sentence that names the running engine
    # and the menu the user picked it from cannot disagree about its name.
    for label, value in PROVIDER_CHOICES:
        provider_box.addItem(label, value)
    model_input = QLineEdit()
    model_input.setPlaceholderText("Provider default")
    base_url_input = QLineEdit()
    base_url_input.setPlaceholderText("https://… (required for compatible/local endpoints)")
    api_key_input = QLineEdit()
    # Never echo a secret, and never prefill it back from storage: the stored
    # key is readable only by the provider transport.
    api_key_input.setEchoMode(enum_member(QLineEdit, "EchoMode", "Password"))
    api_key_input.setPlaceholderText("Paste a key to use your own provider")
    assistant_form.addRow("Provider", provider_box)
    assistant_form.addRow("Model", model_input)
    assistant_form.addRow("Endpoint", base_url_input)
    # The clear action belongs on the field it clears, not as a loose button
    # further down the panel where it reads as a general "remove" of something.
    clear_key_button = QToolButton()
    clear_key_button.setText("Remove")
    clear_key_button.setToolTip("Remove the stored provider key and go back to your Mapdex plan")
    clear_key_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly")
    )
    # A plain container widget, NOT a registered responsive pair. A pair's
    # QBoxLayout has its direction and alignment mutated on every resize; nesting
    # one inside a QFormLayout row means mutating a layout the form owns, which
    # is the ownership hand-off this file already warns about under PyQt6. The
    # row is short enough that it never needs to stack anyway.
    api_key_row = QWidget()
    api_key_row_layout = QHBoxLayout(api_key_row)
    api_key_row_layout.setContentsMargins(0, 0, 0, 0)
    api_key_row_layout.setSpacing(6)
    api_key_row_layout.addWidget(api_key_input, 1)
    api_key_row_layout.addWidget(clear_key_button, 0)
    assistant_form.addRow("API key", api_key_row)
    connection_layout.addLayout(assistant_form)
    # Stored settings, as text rather than as a hint. The key showed only in
    # the field's PLACEHOLDER, and a placeholder is grey and reads as "this is
    # empty, type here" - which is how a user with a working stored OpenAI key
    # came to report that no provider was configured at all.
    assistant_key_state = QLabel("No provider key stored. Nivo runs on your Mapdex plan.")
    assistant_key_state.setObjectName("mapdexKeyState")
    assistant_key_state.setWordWrap(True)
    connection_layout.addWidget(assistant_key_state)
    assistant_privacy = QLabel("Mapdex-hosted assistant: bounded map context is sent to Mapdex.")
    assistant_privacy.setWordWrap(True)
    assistant_privacy.setStyleSheet("color: #8F96A8; font-size: 11px;")
    connection_layout.addWidget(assistant_privacy)

    save_settings_button = QPushButton("Save settings")
    save_settings_button.setObjectName("mapdexSecondaryButton")
    connection_layout.addWidget(save_settings_button)
    settings_button.toggled.connect(connection_panel.setVisible)
    # Added to the column further down, under the choice rather than above it.
    # Opening it here pushed the two cards below a nine-field form, so picking
    # "use my own model" answered the question by burying it: the decision the
    # form belongs to scrolled off while the form filled the screen.
    if not endpoint_settings:
        # A released build talks to the hosted Mapdex, so nobody can repoint it
        # by accident - but the assistant provider settings remain reachable.
        endpoint_frame.setVisible(False)
        settings_button.setToolTip("Nivo assistant settings")

    # Connect stays a full-width primary action: it is the one thing to do when
    # nothing is connected yet. Disconnect now lives in the header row above.
    # One row each, in the panel's one shape. The promise is the row's second
    # line rather than a caption under it: a button that asks for a decision
    # and keeps its reason outside itself stops being one object the moment the
    # layout gets tight.
    connect_button = action_row("Connect Mapdex", CONNECT_PROMISE, tone="primary")
    connect_promise = QLabel("")
    connect_promise.setObjectName("mapdexPromise")
    connect_promise.setVisible(False)
    first_open_prompt = QLabel(FIRST_OPEN_PROMPT)
    first_open_prompt.setObjectName("mapdexPromise")
    first_open_prompt.setWordWrap(True)
    own_model_button = action_row(OWN_MODEL_LABEL, OWN_MODEL_PROMISE)
    own_model_promise = QLabel("")
    own_model_promise.setObjectName("mapdexPromise")
    own_model_promise.setVisible(False)
    own_model = QWidget()
    own_model_layout = QVBoxLayout(own_model)
    own_model_layout.setContentsMargins(0, 0, 0, 0)
    own_model_layout.setSpacing(0)
    own_model_layout.addWidget(own_model_button)

    # The choice, and it is placed AFTER the pages below so the reading in the
    # Nivo transcript has already said something true about the open file
    # before the panel asks anybody to pick a side. Showing the choice alone
    # would be a wall in front of a product nobody has seen yet, and it asks a
    # billing-and-privacy question the reader cannot answer at that moment.
    sign_in = QWidget()
    sign_in_layout = QVBoxLayout(sign_in)
    sign_in_layout.setContentsMargins(0, 8, 0, 0)
    sign_in_layout.setSpacing(10)
    sign_in_layout.addWidget(first_open_prompt)
    sign_in_layout.addWidget(connect_button)
    sign_in_layout.addWidget(connect_promise)
    sign_in_layout.addWidget(own_model)

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
    first_open_title = QLabel(FIRST_OPEN_TITLE)
    first_open_title.setObjectName("mapdexFirstOpenTitle")
    first_open_title.setWordWrap(True)
    first_open_title.setVisible(False)
    layout.addWidget(first_open_title)
    # The choice comes BEFORE the transcript, and that reverses an earlier
    # decision on purpose. The reasoning was that a reading of the open project
    # should say something true before the panel asks anyone to pick a side;
    # what shipped was the choice at y=602 on a first-open screen whose title
    # ended at y=119, with 234 px of empty transcript between them, so the one
    # decision a stranger has to make was the last thing they found.
    #
    # Nivo introduces itself, offers the two ways it can think, and the reading
    # follows as the argument for them. The reading is still there and still
    # first-hand; it is no longer in front of the door.
    layout.addWidget(sign_in)
    # The settings form belongs to the choice above it, so it opens underneath
    # it. When nothing is connected the reader sees the two cards, presses one,
    # and its fields appear where they were looking.
    layout.addWidget(connection_panel)
    layout.addWidget(segment_bar)
    layout.addWidget(pages, 1)
    # First open has no conversation to fill a tall dock, so the pages stop
    # grabbing the spare height and this takes it instead - which puts the
    # choice directly under the reading rather than 300 px below it.
    tail = QWidget()
    tail.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    tail.setVisible(False)
    layout.addWidget(tail)

    workspace = QWidget()
    workspace.setObjectName("mapdexPage")
    workspace_page_layout = QVBoxLayout(workspace)
    workspace_page_layout.setContentsMargins(12, 12, 12, 12)
    workspace_page_layout.setSpacing(0)
    # Everything on this page is Mapdex session state. Disconnected, it kept
    # showing the previous session's project, workflow and selected source in
    # greyed-out controls, which reads as a product that is broken rather than
    # as one that is signed out. The body and the notice swap; the page itself
    # stays mounted so the segment button never becomes a dead end.
    workspace_locked = QLabel(TASK_LOCKED_NOTICE)
    workspace_locked.setObjectName("mapdexLockedNotice")
    workspace_locked.setWordWrap(True)
    workspace_locked.setVisible(False)
    workspace_page_layout.addWidget(workspace_locked)
    workspace_body = QWidget()
    workspace_page_layout.addWidget(workspace_body)
    workspace_page_layout.addStretch(1)
    workspace_layout = QVBoxLayout(workspace_body)
    workspace_layout.setContentsMargins(0, 0, 0, 0)
    workspace_layout.setSpacing(8)
    workspace_layout.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignTop"))
    new_task_label = _section_label("New chat")
    open_project_button = QPushButton("Open in Mapdex")
    open_project_button.setToolTip("Continue this project in the Mapdex workspace")
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
    source_summary.setStyleSheet("color: #8F96A8; font-size: 11px;")
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
    surface_layout.setContentsMargins(0, 0, 0, 0)
    surface_layout.setSpacing(10)
    nivo_header = QWidget()
    nivo_header.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Fixed"),
    )
    nivo_header_layout = QHBoxLayout(nivo_header)
    nivo_header_layout.setContentsMargins(0, 0, 0, 0)
    nivo_header_layout.setSpacing(8)
    nivo_icon = QLabel()
    nivo_icon.setObjectName("mapdexNivoTitleIcon")
    nivo_icon.setFixedSize(34, 34)
    if os.path.isfile(NIVO_AVATAR_PATH):
        nivo_icon.setPixmap(
            QPixmap(NIVO_AVATAR_PATH).scaled(
                34,
                34,
                enum_member(Qt, "AspectRatioMode", "KeepAspectRatio"),
                enum_member(Qt, "TransformationMode", "SmoothTransformation"),
            )
        )
    nivo_title = QLabel("Nivo")
    nivo_title.setObjectName("mapdexNivoTitle")
    nivo_context = QLabel("No active QGIS layer")
    nivo_context.setObjectName("mapdexNivoContext")
    nivo_context.setWordWrap(True)
    # Which engine answers the next turn, where the user actually is. The only
    # sentence that named it used to live in the settings panel, which is
    # collapsed by default - so a disconnected session answering from the
    # user's own key read as a leak rather than as the design it is.
    nivo_runtime = QLabel("")
    nivo_runtime.setObjectName("mapdexNivoRuntime")
    nivo_runtime.setWordWrap(True)
    nivo_header_text = QWidget()
    nivo_header_text.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Fixed"),
    )
    nivo_header_text_layout = QVBoxLayout(nivo_header_text)
    nivo_header_text_layout.setContentsMargins(0, 0, 0, 0)
    nivo_header_text_layout.setSpacing(1)
    nivo_header_text_layout.addWidget(nivo_title)
    nivo_header_text_layout.addWidget(nivo_context)
    nivo_header_text_layout.addWidget(nivo_runtime)
    # Two compact conversation controls, beside the title rather than near the
    # composer: they act on the transcript as a whole, and putting them by the
    # input would read as something the next message does.
    nivo_header_layout.addWidget(nivo_icon, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
    nivo_header_layout.addWidget(nivo_header_text, 1)
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
    nivo_send_button = QToolButton()
    nivo_send_button.setObjectName("mapdexSendButton")
    nivo_send_button.setToolTip("Ask Nivo")
    nivo_send_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonIconOnly"))
    nivo_send_button.setIconSize(QSize(16, 16))
    # The glyph belongs to the control. Set by the plugin instead, a panel
    # built any other way had a blank button with no label to fall back on -
    # and no palette is read here, because this surface is always dark, so
    # the choice is a constant rather than a theme decision.
    nivo_send_button.setIcon(QIcon(surface_asset_path(ACTION_ICONS["send"])))
    nivo_stop_button = QToolButton()
    nivo_stop_button.setObjectName("mapdexStopButton")
    nivo_stop_button.setText("Stop")
    nivo_stop_button.setToolTip("Stop the current Nivo request")
    nivo_stop_button.setToolButtonStyle(
        enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly"))
    nivo_stop_button.setVisible(False)
    nivo_stop_button.setEnabled(False)
    composer = QFrame()
    composer.setObjectName("mapdexComposer")
    composer_row = QHBoxLayout(composer)
    composer_row.setContentsMargins(4, 3, 4, 3)
    composer_row.setSpacing(4)
    composer_row.addWidget(nivo_input, 1)
    composer_row.addWidget(nivo_stop_button, 0)
    composer_row.addWidget(nivo_send_button, 0)
    surface_layout.addWidget(nivo_header)
    surface_layout.addWidget(nivo_reply, 1)
    surface_layout.addWidget(nivo_status)
    surface_layout.addWidget(composer)
    nivo_layout.addWidget(nivo_surface, 1)

    jobs = QWidget()
    jobs.setObjectName("mapdexPage")
    jobs_layout = QVBoxLayout(jobs)
    jobs_layout.setContentsMargins(12, 12, 12, 12)
    jobs_layout.setSpacing(10)
    jobs_layout.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignTop"))

    # The recent-task list is read from QSettings and survived disconnect, so
    # this page kept offering Resume on batches the next token cannot read -
    # and after connecting a different account, on another workspace's tasks.
    jobs_locked = QLabel(JOBS_LOCKED_NOTICE)
    jobs_locked.setObjectName("mapdexLockedNotice")
    jobs_locked.setWordWrap(True)
    jobs_locked.setVisible(False)
    jobs_layout.addWidget(jobs_locked)

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
    guidance_label.setStyleSheet("color: #8F96A8; font-size: 11px;")
    batch_layout.addWidget(guidance_label)
    retry_button = QPushButton("Retry failed item")
    # Jobs shows Retry and Get result together, so exactly one of them can
    # be filled. Get result is the thing the user came for.
    retry_button.setObjectName("mapdexSecondaryButton")
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

    segment_buttons = []

    def switch_page(target):
        pages.setCurrentIndex(target)
        for index, page in enumerate(page_order):
            page.setVisible(index == target)
        # The buttons are auto-exclusive, which handles a CLICK. It does not
        # handle the panel moving the page itself, and a tab bar showing one
        # page while the stack shows another is worse than not moving at all.
        for index, button in enumerate(segment_buttons):
            button.setChecked(index == target)

    for index, title in enumerate(("Nivo AI", "Task", "Jobs")):
        button = QToolButton()
        button.setObjectName("mapdexSegment")
        button.setText(title)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.clicked.connect(lambda _checked=False, target=index: switch_page(target))
        segment_buttons.append(button)
        segment_layout.addWidget(button)
        if index == 0:
            button.setChecked(True)
    switch_page(0)
    segment_layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)

    allow_narrow(body)

    # Lay out for the width the dock opens at; resizeEvent takes over after.
    root.apply_layout_mode(panel_layout_mode(PREFERRED_WIDTH))
    return root, {
        "status": status,
        "connection_label": connection_label,
        "api_url_input": api_url_input,
        "web_url_input": web_url_input,
        "save_settings_button": save_settings_button,
        "settings_button": settings_button,
        "provider_box": provider_box,
        "model_input": model_input,
        "base_url_input": base_url_input,
        "api_key_input": api_key_input,
        "assistant_privacy": assistant_privacy,
        "clear_key_button": clear_key_button,
        "connect_button": connect_button,
        "connect_promise": connect_promise,
        "sign_in": sign_in,
        "column": column,
        "tail": tail,
        "body_layout": layout,
        "first_open_prompt": first_open_prompt,
        "first_open_title": first_open_title,
        "own_model": own_model,
        "own_model_button": own_model_button,
        "segment_bar": segment_bar,
        "disconnect_button": disconnect_button,
        "workspace": workspace,
        "tabs": pages,
        "switch_page": switch_page,
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
        "nivo_runtime": nivo_runtime,
        "assistant_key_state": assistant_key_state,
        "workspace_body": workspace_body,
        "workspace_locked": workspace_locked,
        "jobs_locked": jobs_locked,
        "nivo_new_button": nivo_new_button,
        "nivo_history_button": nivo_history_button,
        "nivo_reply": nivo_reply,
        "nivo_status": nivo_status,
        "nivo_input": nivo_input,
        "nivo_send_button": nivo_send_button,
        "composer": composer,
        "nivo_stop_button": nivo_stop_button,
        "cancel_button": cancel_button,
        "retry_button": retry_button,
        "import_button": import_button,
        "review_button": review_button,
        "recent": recent,
        "recent_box": recent_box,
        "resume_button": resume_button,
    }


def build_thread_history_dialog(parent=None):
    """The Nivo History dialog: layout and every state it can be in.

    Loading, empty, failed and populated each get a widget here. A dialog that
    shows an empty box while the request is in flight, and the same empty box
    when the request failed, has told the user that this project has no earlier
    conversations - which may be false and is not recoverable from.

    Rows are plain `QListWidget` text, never rich text: a conversation title is
    the user's own words coming back from the server, and this panel renders
    such text as data everywhere else too.
    """
    dialog = QDialog(parent)
    dialog.setObjectName("mapdexHistoryDialog")
    dialog.setWindowTitle("Nivo conversations")
    dialog.setMinimumWidth(430)
    dialog.setMinimumHeight(360)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(9)

    intro = QLabel("Earlier Nivo conversations in this Mapdex project. Opening one continues it.")
    intro.setWordWrap(True)
    layout.addWidget(intro)

    state_label = QLabel("Loading conversations…")
    state_label.setObjectName("mapdexHistoryState")
    state_label.setWordWrap(True)
    layout.addWidget(state_label)

    listing = QListWidget()
    listing.setObjectName("mapdexHistoryList")
    listing.setVisible(False)
    layout.addWidget(listing, 1)

    actions = QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(8)
    delete_button = QPushButton("Delete")
    delete_button.setToolTip("Delete this conversation from Mapdex. This cannot be undone.")
    delete_button.setEnabled(False)
    retry_button = QPushButton("Try again")
    retry_button.setVisible(False)
    open_button = QPushButton("Open")
    open_button.setObjectName("mapdexPrimaryButton")
    open_button.setEnabled(False)
    open_button.setDefault(True)
    close_button = QPushButton("Close")
    actions.addWidget(delete_button)
    actions.addStretch(1)
    actions.addWidget(retry_button)
    actions.addWidget(open_button)
    actions.addWidget(close_button)
    layout.addLayout(actions)

    return dialog, {
        "dialog": dialog,
        "state_label": state_label,
        "list": listing,
        "retry_button": retry_button,
        "open_button": open_button,
        "delete_button": delete_button,
        "close_button": close_button,
    }
