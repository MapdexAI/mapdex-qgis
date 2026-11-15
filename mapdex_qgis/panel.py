"""The Mapdex for QGIS panel (layout only).

Redesigned to match the Mapdex web app (0.14.0). The shape, top to bottom:

* a slim header: the "Mapdex" wordmark, a divider, the WORKSPACE name, and on
  the right only the controls that act on the page in view (New chat and
  History on Nivo) plus Settings;
* three tabs, Nivo · Task · Jobs, ink and a 2 px underline when chosen, with an
  amber count on Jobs when something waits on the user;
* the page, its content capped and centred so a wide dock gains margin instead
  of 1,400 px lines.

There is no permanent status strip. A status appears only when it says
something, inline under the tabs, and goes away on its own when it was only
information.

Settings is not a tab: it opens from the header as its own view with a Back
control. The first open, before anything is connected, is its own view too:
one sentence of value, three promises and one button.

Colours and sizes live in `theme`, glyphs in `icons`, and every repeated shape
in `ui`. This file only arranges them, and hands the plugin a dictionary of
named handles it already knows how to drive.
"""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import QEvent, QObject, QPointF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from . import ui
from .build_profile import endpoint_choices
from .build_version import PLUGIN_VERSION
from .job_items import item_action, item_status, item_title, items_summary, order_items
from .layout_rules import (
    MINIMUM_WIDTH,
    PREFERRED_HEIGHT,
    PREFERRED_WIDTH,
    READING_WIDTH,
    panel_layout_mode,
)
from .panel_state import (
    CONNECT_POINTS,
    CONNECT_PROMISE,
    FIRST_OPEN_PROMPT,
    FIRST_OPEN_TITLE,
    JOBS_LOCKED_NOTICE,
    KEY_PROVIDER_CHOICES,
    LOCAL_PROVIDER,
    MODEL_CHOICES,
    MODEL_FIELDS_HINT,
    PROVIDER_CHOICES,
    TASK_LOCKED_NOTICE,
    model_choice_for,
    model_fields_for,
)
from .qt_compat import enum_member
from .task_options import concurrency_choices

# Which page Settings is. Named because the header control, the first-open
# route and the plugin agree on it, and three copies of a 3 is how they come
# to disagree. Pages 0-2 are tabs; 3 is the Settings view.
SETTINGS_PAGE = 3

# The tabs, in order. Settings is deliberately not one: it is configuration,
# reached from the header, and a fourth tab beside three pieces of work read as
# a fourth piece of work.
TAB_TITLES = ("Nivo", "Task", "Jobs")

# The assistant avatar is kept on disk for the listing and documentation; the
# panel itself no longer draws an avatar (no robot, no mascot in the chrome).
NIVO_AVATAR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "assistant", "nivo.png"
)

# What each workflow does, in the web app's words, keyed by the BatchKind
# value. The TITLE comes from the plugin's WORKFLOWS table; the sentence and
# the marker are presentation and live here.
WORKFLOW_COPY = {
    "georeference": ("Find where each scan belongs and georeference it.", ""),
    "digitize_parcels": ("Trace parcel outlines from placed cadastral maps.", "Beta"),
    "validate_deliver": ("Validate a dataset and deliver it in another format.", ""),
    "full_pipeline": ("Place each scan, then trace its parcels.", "Beta"),
}

# The glyph on the Start control, per workflow.
WORKFLOW_ICONS = {
    "georeference": "crosshair",
    "digitize_parcels": "layers-2",
    "validate_deliver": "shield-check",
    "full_pipeline": "crosshair",
}


def _kind_value(key):
    return str(getattr(key, "value", key) or "")


class _ArrowComboBox(QComboBox):
    """A combo with a guaranteed, SVG-free down chevron in the theme colour."""

    def paintEvent(self, event):  # noqa: N802 - Qt virtual name
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(enum_member(QPainter, "RenderHint", "Antialiasing"), True)
        tokens = ui.tokens()
        colour = QColor(tokens["slate"] if self.isEnabled() else tokens["faint"])
        painter.setPen(QPen(colour, 1.6))
        x = self.width() - 15.0
        y = self.height() / 2.0 - 1.0
        painter.drawLine(QPointF(x - 4.0, y - 2.0), QPointF(x, y + 2.0))
        painter.drawLine(QPointF(x, y + 2.0), QPointF(x + 4.0, y - 2.0))
        painter.end()


class ActionRow(ui.PromptCard):
    """A pressable row with a sentence. Kept under its old name for callers.

    It used to be the panel's one repeated shape in three tones; the redesign
    has exactly one filled control per screen, so a row is now always a quiet
    card and the tone methods only change its second line.
    """

    def __init__(self, label, sublabel="", icon=None, tone="normal", parent=None):
        super().__init__(label, "chevron-right" if icon is None else "message-circle", sublabel, parent)
        self._callbacks = []
        super().clicked.connect(self._fire)

    def _fire(self):
        for callback in list(self._callbacks):
            callback()

    def set_sublabel(self, sublabel):
        self.hint.setText(sublabel or "")
        self.hint.setVisible(bool(sublabel))

    def set_label(self, label):
        self.text_label.setText(label or "")

    def set_tone(self, tone):
        self.setProperty("tone", tone)

    @property
    def clicked(self):
        return self

    def connect(self, callback):  # noqa: A003 - mirrors the Qt signal shape
        self._callbacks.append(callback)


def action_row(label, sublabel="", icon=None, tone="normal"):
    return ActionRow(label, sublabel, icon, tone)


def _section_label(text):
    return ui.label(text, "mapdexSectionLabel")


# Kept under its old name: tests and the dialogs import it from here.
_ElidedLabel = ui.ElidedLabel


class Notice(QWidget):
    """An inline status: shown only when it says something, then gone.

    Information fades after a few seconds; a warning or an error stays until
    the next status replaces it. Never a permanent strip.
    """

    INFO_MS = 9000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexNotice")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.icon = ui.StatusIcon("info", 16)
        self.text_label = ui.label("", "mapdexNoticeText")
        row.addWidget(self.icon, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        row.addWidget(self.text_label, 1, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        ui.align_icon(self.icon, self.text_label, size=16)
        self._tone = "info"
        self.setVisible(False)

    def show_text(self, text, tone="info"):
        self._tone = tone or "info"
        self.text_label.setText(text or "")
        has_icon = self._tone in ("ok", "warn", "err")
        self.icon.setVisible(has_icon)
        self.retheme()
        self.setVisible(bool(text))

    def retheme(self):
        if self._tone in ("ok", "warn", "err"):
            self.icon.set_tone(self._tone)


class NoticeHub(QObject):
    """The plugin's `status`: one text, shown in whichever view is on screen.

    It keeps the `setText`/`text` surface the plugin and the verification
    harness already use, so a hundred call sites did not have to learn a new
    API to stop being a permanent strip.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._notices = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)

    def add(self, notice):
        self._notices.append(notice)
        return notice

    def setText(self, text, tone="info", quiet=False):  # noqa: N802 - mirrors QLabel
        self._text = str(text or "")
        shown = "" if quiet else self._text
        for notice in self._notices:
            try:
                notice.show_text(shown, tone)
            except RuntimeError:
                continue
        self._timer.stop()
        if shown and tone in ("info", "ok"):
            self._timer.start(Notice.INFO_MS)

    def text(self):
        return self._text

    def clear(self):
        self.setText("", quiet=True)

    def _expire(self):
        for notice in self._notices:
            try:
                notice.show_text("", "info")
            except RuntimeError:
                continue

    def setVisible(self, _visible):  # noqa: N802 - the hub has no geometry
        return None

    def isVisible(self):  # noqa: N802
        return any(notice.isVisible() for notice in self._notices)


class JobItemList(QWidget):
    """The sheets in a batch, one row each, attention first.

    Each row: a status line (solid icon, bold word, the reason), the sheet's
    own name, and at most one action as a link. Ordering and wording are
    decided in `job_items`, so this class only mounts rows.

    Rows are rebuilt on each poll rather than diffed: the list is bounded and
    a stale row that survived a diff would be the worse failure.
    """

    # Above this many rows the list says what it is not showing. Attention
    # sorts first, so whatever needs the user is always inside the limit.
    VISIBLE_ROWS = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexJobItems")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self._summary = ui.label("", "mapdexMeta")
        self._summary.setVisible(False)
        outer.addWidget(self._summary)
        self._frame = QFrame()
        self._frame.setObjectName("mapdexList")
        self._body_layout = QVBoxLayout(self._frame)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        outer.addWidget(self._frame)
        self._overflow = ui.label("", "mapdexMeta")
        self._overflow.setVisible(False)
        outer.addWidget(self._overflow)

    def set_items(self, items, names=None, on_action=None):
        """Replace the rows. Returns the number of items."""
        while self._body_layout.count():
            entry = self._body_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        rows = order_items(items or [])
        self._summary.setText(items_summary(rows))
        shown = rows[: self.VISIBLE_ROWS]
        for position, item in enumerate(shown):
            self._body_layout.addWidget(
                self._build_row(item, names or {}, on_action, position == len(shown) - 1))
        hidden = len(rows) - len(shown)
        self._overflow.setText("and {} more in Mapdex".format(hidden) if hidden > 0 else "")
        self._overflow.setVisible(hidden > 0)
        self.setVisible(bool(rows))
        return len(rows)

    def _build_row(self, item, names, on_action, last):
        status = item_status(item)
        row = QFrame()
        row.setObjectName("mapdexJobItemRow")
        row.setProperty("itemState", status["state"])
        row.setProperty("last", "true" if last else "false")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        line = ui.StatusLine(status.get("tone", "neutral"), status["label"], status["detail"])
        line.setObjectName("mapdexJobItemStatus")
        layout.addWidget(line)
        # The file name is the user's own, so it is data: plain text, elided
        # rather than allowed to widen the dock.
        title = ui.ElidedLabel()
        title.setObjectName("mapdexJobItemTitle")
        title.setText(item_title(item, names))
        layout.addWidget(title)
        action = item_action(item)
        if action["key"] and on_action is not None:
            icon = "layers-2" if action["key"] == "import" else "external-link"
            button = ui.LinkButton(action["label"], "link", icon)
            button.setObjectName("mapdexJobItemAction")
            key = action["key"]
            captured = dict(item)
            button.clicked.connect(lambda _checked=False, k=key, i=captured: on_action(k, i))
            layout.addWidget(button, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))
        return row


class WorkflowChooser(ui.RadioCardGroup):
    """The workflows as radio cards, presenting the combo surface the plugin reads.

    Each card states the work and what it does, which a combo hid behind a
    click. Keyboard: Tab focuses a card, the arrow keys move, Space chooses.
    """

    def addItem(self, title, data, icon=None, description=None, badge=None):  # noqa: N802
        copy_description, copy_badge = WORKFLOW_COPY.get(_kind_value(data), ("", ""))
        card = super().addItem(
            str(title),
            data,
            description if description is not None else copy_description,
            badge if badge is not None else copy_badge,
        )
        card.setObjectName("mapdexRadioCard")
        return card


class SourceDropZone(QFrame):
    """The Maps area: where maps come in, and the list of what came in.

    Two equal ways in, first the one a QGIS user already has open: "From QGIS
    layers", then "Choose files". Under them one muted line says files can be
    dropped. While the list is empty the whole area is a dashed drop zone with
    the two buttons in its centre; once it holds maps the dashes go and the
    list sits under the buttons. The whole area takes drops either way.
    Drops are accepted for local files only, so a drag carrying a QGIS layer is
    never read as an upload (docs/UX.md section 4).
    """

    HINT = "or drop files here"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexDropZone")
        self.setAcceptDrops(True)
        self.on_files = None
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(8)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self.layers_button = ui.secondary_button("From QGIS layers", "layers")
        self.layers_button.setObjectName("mapdexSecondaryButton")
        self.choose_button = ui.secondary_button("Choose files", "upload")
        self.choose_button.setObjectName("mapdexSecondaryButton")
        for button in (self.layers_button, self.choose_button):
            button.setSizePolicy(
                enum_member(QSizePolicy, "Policy", "Expanding"),
                enum_member(QSizePolicy, "Policy", "Fixed"),
            )
            buttons.addWidget(button, 1)
        self._layout.addLayout(buttons)
        self.hint = ui.label(self.HINT, "mapdexMetaCentred")
        self.hint.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignHCenter"))
        self._layout.addWidget(self.hint)
        self._list_slot = QVBoxLayout()
        self._list_slot.setContentsMargins(0, 4, 0, 0)
        self._layout.addLayout(self._list_slot)
        self.set_empty(True)

    def set_list(self, widget):
        self._list_slot.addWidget(widget)

    def set_empty(self, empty):
        """Dashed and roomy while empty; plain and flush once it holds maps."""
        self.setProperty("empty", "true" if empty else "false")
        if empty:
            self._layout.setContentsMargins(16, 24, 16, 24)
        else:
            self._layout.setContentsMargins(0, 0, 0, 0)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_hint(self, _text=""):
        """Kept for callers; the line under the buttons always says the same."""
        self.hint.setText(self.HINT)

    def retheme(self):
        return None

    def _paths(self, event):
        data = event.mimeData()
        if data is None or not data.hasUrls():
            return []
        paths = []
        for url in data.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                paths.append(path)
        return paths

    def _set_active(self, active):
        self.setProperty("dropActive", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt virtual name
        if self._paths(event):
            self._set_active(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt virtual name
        self._set_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # noqa: N802 - Qt virtual name
        self._set_active(False)
        paths = self._paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        if callable(self.on_files):
            self.on_files(paths)


class SourceList(QWidget):
    """The sheets chosen so far, one row each, each removable.

    A count answers whether something is selected and never which twelve, so
    a wrong file in a batch is unfindable without starting over.
    """

    VISIBLE_LIMIT = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexSourceList")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._body = None

    def set_sources(self, sources, on_remove=None):
        """Replace the whole list. The old container is detached and deleted,
        never re-added anywhere, so no widget changes owner between layouts."""
        if self._body is not None:
            previous = self._body
            self._body = None
            previous.hide()
            previous.setParent(None)
            previous.deleteLater()
        body = QFrame(self)
        body.setObjectName("mapdexList")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        shown = list(sources)[: self.VISIBLE_LIMIT]
        hidden = len(sources) - len(shown)
        for position, source in enumerate(shown):
            body_layout.addWidget(self._build_row(
                body, source, on_remove, position == len(shown) - 1 and hidden <= 0))
        if hidden > 0:
            overflow = ui.label("and {} more".format(hidden), "mapdexMeta")
            overflow.setObjectName("mapdexSourceOverflow")
            overflow.setContentsMargins(12, 8, 12, 8)
            body_layout.addWidget(overflow)
        self._body = body
        self._layout.addWidget(body)
        self.setVisible(bool(sources))

    @staticmethod
    def _icon_for(source):
        """image for a raster (file or layer), file-text for a PDF, layers for
        a vector layer, file for anything else."""
        name = str(source.label or "").lower()
        path = str(getattr(source, "path", "") or "").lower()
        if name.endswith(".pdf") or path.endswith(".pdf"):
            return "file-text"
        data_kind = str(getattr(source, "data_kind", "") or "")
        if data_kind == "vector":
            return "layers"
        if data_kind == "raster":
            return "image"
        return "file"

    def _build_row(self, parent, source, on_remove, last):
        row = QFrame(parent)
        # Rows share the list's hairlines, so they wear the list row's style.
        row.setObjectName("mapdexListRow")
        row.setProperty("sourceKind", source.kind)
        row.setProperty("last", "true" if last else "false")
        row.setAccessibleName(source.label)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 8, 10)
        layout.setSpacing(10)
        middle = enum_member(Qt, "AlignmentFlag", "AlignVCenter")
        top = enum_member(Qt, "AlignmentFlag", "AlignTop")
        glyph = ui.IconLabel(self._icon_for(source), "slate", 16)
        layout.addWidget(glyph, 0, top)
        # The name on its own line, the detail under it: side by side, a long
        # layer name was cut to a few letters on a narrow dock.
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        title = ui.ElidedLabel(source.label)
        title.setObjectName("mapdexSourceName")
        text.addWidget(title)
        # Which route the sheet took, as a word: a file is uploaded as it is, a
        # layer may be exported first. Not a colour and not an icon alone.
        kind_word = "QGIS layer" if source.kind == "layer" else ""
        detail_text = " · ".join(part for part in (kind_word, source.detail) if part)
        if detail_text:
            detail = ui.ElidedLabel(detail_text, mode="ElideRight")
            detail.setObjectName("mapdexSourceDetail")
            text.addWidget(detail)
        layout.addLayout(text, 1)
        if on_remove is not None:
            remove = ui.IconButton("x", "Remove {} from this task".format(source.label),
                                   "muted", 24, 14, "mapdexRowRemove")
            remove.setObjectName("mapdexSourceRemove")
            kind, key = source.kind, source.key
            remove.clicked.connect(lambda _checked=False, k=kind, i=key: on_remove(k, i))
            layout.addWidget(remove, 0, middle)
        ui.align_icon(glyph, title, size=16)
        return row


class OptionsDisclosure(QWidget):
    """Options, closed by default; what they say sits at the row's right edge.

    "default" while nothing was changed, otherwise the changes in a few words.
    Closed is right because the defaults are the behaviour that was already
    shipping, and the right-edge summary is what makes closed safe.
    """

    def __init__(self, title="Options", parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexOptions")
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        middle = enum_member(Qt, "AlignmentFlag", "AlignVCenter")
        self.header = ui.LinkButton(title, "muted", "chevron-right")
        self.header.setObjectName("mapdexOptionsHeader")
        self.header.setCheckable(True)
        self.chip = ui.label("default", "mapdexMeta", wrap=False)
        self.chip.setObjectName("mapdexOptionsChip")
        header_row.addWidget(self.header, 0, middle)
        header_row.addStretch(1)
        header_row.addWidget(self.chip, 0, middle)
        layout.addLayout(header_row)
        self.body = QFrame()
        self.body.setObjectName("mapdexOptionsBody")
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self.header.toggled.connect(self._toggled)

    def _toggled(self, opened):
        self.body.setVisible(bool(opened))
        self.header.set_icon_name("chevron-down" if opened else "chevron-right")

    def set_summary(self, text):
        text = str(text or "")
        self.chip.setText("default" if not text or text.lower() == "default" else text)


def _elastic(combo):
    """Let a combo shrink with the dock instead of demanding its widest item."""
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
    """Panel root: follows the QGIS theme and the dock width.

    The theme is read from the APPLICATION palette whenever it changes, so a
    user who switches QGIS to Night Mapping (or the system goes dark) gets the
    dark panel without reopening it. `on_palette_change` is the plugin's hook
    for the toolbar marks, which are not children of this panel.
    """

    def __init__(self):
        super().__init__()
        self._forms = []
        self._pairs = []
        self._mode = ""
        self.on_palette_change = None
        self.on_theme_change = None
        self._applying = False

    def changeEvent(self, event):
        super().changeEvent(event)
        kind = enum_member(QEvent, "Type", "PaletteChange")
        if event.type() != kind or self._applying:
            return
        self._applying = True
        try:
            wanted = ui.tokens_for_interface()
            if wanted is not ui.tokens() or not self.property("mapdexThemed"):
                ui.apply_theme(self, wanted)
                if callable(self.on_theme_change):
                    self.on_theme_change()
        finally:
            self._applying = False
        if callable(self.on_palette_change):
            self.on_palette_change()

    def register_form(self, form):
        self._forms.append(form)
        return form

    def register_pair(self, first, second):
        """A two-widget row that stacks when the dock is narrow.

        The row is a QBoxLayout whose direction is flipped on resize; each
        widget is added exactly once, never moved between layout cells.
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
            QFormLayout, "RowWrapPolicy", "WrapAllRows" if compact else "DontWrapRows"
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
            row.setAlignment(second, left if compact else right)
            row.setAlignment(first, left if compact else enum_member(Qt, "AlignmentFlag", "AlignVCenter"))


def allow_narrow(widget):
    """Let wrapping labels and scroll areas be as narrow as the dock is.

    A label with `setWordWrap(True)` still reports a minimum as wide as its
    longest line, and a resizable scroll area adopts its content's minimum, so
    together they made the panel un-narrowable. Applied at build time and
    again after the transcript draws, because turns are created later.
    """
    for child in widget.findChildren(QLabel):
        if child.wordWrap():
            child.setMinimumWidth(1)
    for area in widget.findChildren(QScrollArea):
        area.setMinimumWidth(1)
        inner = area.widget()
        if inner is not None:
            inner.setMinimumWidth(1)
    if isinstance(widget, QScrollArea):
        widget.setMinimumWidth(1)
    widget.setMinimumWidth(1)


def _field_row(caption, field):
    """A label over its field: the shape that survives a narrow dock."""
    holder = QWidget()
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(6)
    column.addWidget(ui.label(caption, "mapdexSectionLabel", wrap=False))
    column.addWidget(field)
    return holder


def _view(name):
    view = QWidget()
    view.setObjectName("mapdexView")
    view.setProperty("view", name)
    layout = QVBoxLayout(view)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    return view, layout


def _notice_band(hub):
    band = ui.Band("mapdexPlainBand", READING_WIDTH, (16, 10, 16, 0))
    notice = hub.add(Notice())
    band.layout_.addWidget(notice)
    band.setVisible(True)
    return band, notice


def build_companion_panel(workflows, endpoint_settings=True):
    """Build the panel and return (root, handles).

    endpoint_settings=False is a released build talking to the hosted Mapdex:
    the API/web address controls are not shown at all, so nobody can point a
    production install somewhere else by accident.
    """
    root = _CompanionPanel()
    root.setObjectName("mapdexPluginRoot")
    root.setMinimumWidth(MINIMUM_WIDTH)
    root.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Preferred"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    # The theme is applied once the tree exists (end of this function), so
    # every widget with a `retheme` is reached by the first pass.
    ui.set_tokens(ui.tokens_for_interface())

    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    views = QStackedWidget()
    views.setObjectName("mapdexViews")
    views.layout().setStackingMode(enum_member(QStackedLayout, "StackingMode", "StackOne"))
    outer.addWidget(views, 1)
    hub = NoticeHub(root)

    # Widgets the redesign retired from view but the plugin still drives (the
    # project combo, the old phase line). Parented and hidden, never orphaned:
    # an orphan shown later becomes a top-level window.
    attic = QWidget()
    attic.setObjectName("mapdexAttic")
    attic.setVisible(False)
    attic_layout = QVBoxLayout(attic)
    outer.addWidget(attic, 0)

    # ------------------------------------------------------------------
    # Connect view (first open, nothing connected)
    # ------------------------------------------------------------------
    connect_view, connect_layout = _view("connect")
    connect_header = ui.Band("mapdexBand", READING_WIDTH, (16, 14, 16, 14))
    connect_wordmark = ui.label("Mapdex", "mapdexWordmark", wrap=False)
    connect_header.layout_.addWidget(connect_wordmark)
    connect_layout.addWidget(connect_header)
    connect_scroll, sign_in_layout, sign_in = ui.scroll_column(
        READING_WIDTH, (16, 24, 16, 24), 24)
    connect_layout.addWidget(connect_scroll, 1)
    sign_in_layout.addStretch(1)
    intro = QVBoxLayout()
    intro.setSpacing(10)
    first_open_title = ui.label(FIRST_OPEN_TITLE, "mapdexConnectTitle")
    first_open_prompt = ui.label(FIRST_OPEN_PROMPT, "mapdexConnectBody")
    intro.addWidget(first_open_title)
    intro.addWidget(first_open_prompt)
    sign_in_layout.addLayout(intro)
    points = QVBoxLayout()
    points.setSpacing(10)
    for promise in CONNECT_POINTS:
        point = QHBoxLayout()
        point.setSpacing(10)
        tick = ui.IconLabel("check", "ok", 16, 2.25)
        sentence = ui.label(promise, "mapdexBody")
        point.addWidget(tick, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        point.addWidget(sentence, 1, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        ui.align_icon(tick, sentence, size=16)
        points.addLayout(point)
    sign_in_layout.addLayout(points)
    connect_actions = QVBoxLayout()
    connect_actions.setSpacing(10)
    connect_button = _ConnectButton("Connect Mapdex", icon_name="external-link", height=52)
    connect_promise = ui.label(CONNECT_PROMISE, "mapdexMetaCentred")
    connect_promise.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignHCenter"))
    connect_button.promise_label = connect_promise
    connect_actions.addWidget(connect_button)
    connect_actions.addWidget(connect_promise)
    connect_notice = hub.add(Notice())
    connect_actions.addWidget(connect_notice)
    sign_in_layout.addLayout(connect_actions)
    sign_in_layout.addStretch(1)
    first_open_title.setVisible(True)

    # ------------------------------------------------------------------
    # Main view: header, tabs, notice, pages
    # ------------------------------------------------------------------
    main_view, main_layout = _view("main")
    header = ui.Band("mapdexBand", READING_WIDTH, (16, 8, 9, 8))
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(10)
    wordmark = ui.label("Mapdex", "mapdexWordmark", wrap=False)
    header_row.addWidget(wordmark, 0)
    header_row.addWidget(ui.vline(16), 0)
    # The workspace, as a label. With one membership it is a static name, and
    # the tasks go to that workspace's default project - the web app removed
    # project selection, so the panel does not reintroduce it.
    connection_label = ui.ElidedLabel("", mode="ElideRight")
    connection_label.setObjectName("mapdexWorkspace")
    header_row.addWidget(connection_label, 1)
    nivo_new_button = ui.IconButton(
        "message-square-plus",
        "New chat. The current one stays in History; nothing is deleted from Mapdex.")
    nivo_new_button.setText("New chat")
    nivo_history_button = ui.IconButton(
        "history", "History: open an earlier Nivo conversation and continue it")
    nivo_history_button.setText("History")
    open_settings_button = ui.IconButton("sliders", "Settings")
    for control in (nivo_new_button, nivo_history_button, open_settings_button):
        header_row.addWidget(control, 0)
    header.layout_.addLayout(header_row)
    main_layout.addWidget(header)

    segment_bar = ui.Band("mapdexBand", READING_WIDTH, (16, 0, 16, 0))
    segment_bar.setObjectName("mapdexSegmentBar")
    tab_bar = ui.TabBar(TAB_TITLES)
    segment_bar.layout_.addWidget(tab_bar)
    main_layout.addWidget(segment_bar)

    main_notice_band, main_notice = _notice_band(hub)
    main_layout.addWidget(main_notice_band)

    pages = QStackedWidget()
    pages.setObjectName("mapdexPages")
    pages.layout().setStackingMode(enum_member(QStackedLayout, "StackingMode", "StackOne"))
    main_layout.addWidget(pages, 1)

    # -- Nivo ----------------------------------------------------------
    nivo = QWidget()
    nivo.setObjectName("mapdexPage")
    nivo_layout = QVBoxLayout(nivo)
    nivo_layout.setContentsMargins(0, 0, 0, 0)
    nivo_layout.setSpacing(0)
    nivo_reply = QScrollArea()
    nivo_reply.setObjectName("mapdexChatTranscript")
    nivo_reply.setWidgetResizable(True)
    nivo_reply.setFrameShape(enum_member(QFrame, "Shape", "NoFrame"))
    nivo_reply.setHorizontalScrollBarPolicy(
        enum_member(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
    transcript_body = QWidget()
    transcript_body.setObjectName("mapdexScrollBody")
    transcript_row = QHBoxLayout(transcript_body)
    transcript_row.setContentsMargins(0, 0, 0, 0)
    transcript_row.setSpacing(0)
    # The transcript is the capped, centred column; the plugin draws turns into
    # ITS layout. Kept on the scroll area as `transcript` so the plugin finds
    # it without knowing about the centring wrapper.
    transcript = QWidget()
    transcript.setObjectName("mapdexChatTranscriptContent")
    transcript.setMaximumWidth(READING_WIDTH)
    transcript.setMinimumWidth(1)
    transcript.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Preferred"))
    transcript_layout = QVBoxLayout(transcript)
    transcript_layout.setContentsMargins(16, 16, 16, 16)
    transcript_layout.setSpacing(16)
    transcript_layout.addStretch(1)
    transcript_row.addStretch(1)
    transcript_row.addWidget(transcript, 100)
    transcript_row.addStretch(1)
    nivo_reply.setWidget(transcript_body)
    ui.balance_scrollbar(nivo_reply, transcript_row)
    nivo_reply.transcript = transcript
    nivo_reply.setMinimumHeight(120)
    nivo_reply.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Expanding"),
    )
    nivo_layout.addWidget(nivo_reply, 1)

    nivo_footer = ui.Band("mapdexFooterBand", READING_WIDTH, (16, 12, 16, 16))
    nivo_footer.setObjectName("mapdexFooterBand")
    context_row = QHBoxLayout()
    context_row.setContentsMargins(0, 0, 0, 0)
    context_row.setSpacing(6)
    context_icon = ui.IconLabel("layers", "muted", 14, 2)
    nivo_context = ui.ElidedLabel("No active QGIS layer", mode="ElideRight")
    nivo_context.setObjectName("mapdexMeta")
    context_row.addWidget(context_icon, 0, enum_member(Qt, "AlignmentFlag", "AlignVCenter"))
    context_row.addWidget(nivo_context, 1, enum_member(Qt, "AlignmentFlag", "AlignVCenter"))
    ui.align_icon(context_icon, nivo_context, size=14)
    nivo_footer.layout_.addLayout(context_row)
    # Which engine answers, shown only when it is not the plain hosted default
    # (the plugin decides): a BYOK turn never reaches Mapdex, and that has to
    # be said where the question is typed.
    nivo_runtime = ui.label("", "mapdexMeta")
    nivo_runtime.setVisible(False)
    nivo_footer.layout_.addWidget(nivo_runtime)
    nivo_status = _TurnStatusLabel("Ready")
    nivo_footer.layout_.addWidget(nivo_status)
    composer = QFrame()
    composer.setObjectName("mapdexComposer")
    composer.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Expanding"),
        enum_member(QSizePolicy, "Policy", "Fixed"),
    )
    composer_row = QHBoxLayout(composer)
    composer_row.setContentsMargins(12, 8, 8, 8)
    composer_row.setSpacing(8)
    nivo_input = QLineEdit()
    nivo_input.setObjectName("mapdexNivoInput")
    nivo_input.setPlaceholderText("Ask about this layer or the map view")
    nivo_input.setAccessibleName("Ask Nivo")
    nivo_stop_button = QToolButton()
    nivo_stop_button.setObjectName("mapdexStopButton")
    nivo_stop_button.setText("Stop")
    nivo_stop_button.setToolTip("Stop the current Nivo request")
    nivo_stop_button.setToolButtonStyle(enum_member(Qt, "ToolButtonStyle", "ToolButtonTextOnly"))
    nivo_stop_button.setFixedHeight(36)
    nivo_stop_button.setVisible(False)
    nivo_stop_button.setEnabled(False)
    nivo_send_button = ui.IconButton("arrow-up", "Ask Nivo", "primary_text", 36, 18, "mapdexSendButton")
    composer_row.addWidget(nivo_input, 1)
    composer_row.addWidget(nivo_stop_button, 0, enum_member(Qt, "AlignmentFlag", "AlignBottom"))
    composer_row.addWidget(nivo_send_button, 0, enum_member(Qt, "AlignmentFlag", "AlignBottom"))
    nivo_footer.layout_.addWidget(composer)
    nivo_layout.addWidget(nivo_footer, 0)

    # -- Task ----------------------------------------------------------
    workspace = QWidget()
    workspace.setObjectName("mapdexPage")
    workspace_page_layout = QVBoxLayout(workspace)
    workspace_page_layout.setContentsMargins(0, 0, 0, 0)
    workspace_page_layout.setSpacing(0)
    task_scroll, workspace_layout, workspace_body = ui.scroll_column(
        READING_WIDTH, (16, 20, 16, 20), 20)
    workspace_page_layout.addWidget(task_scroll, 1)
    workspace_locked = ui.label(TASK_LOCKED_NOTICE, "mapdexBody")
    workspace_locked.setObjectName("mapdexLockedNotice")
    workspace_locked.setVisible(False)
    workspace_layout.addWidget(workspace_locked)

    title_block = QVBoxLayout()
    title_block.setSpacing(6)
    new_task_title = ui.label("Send maps to Mapdex", "mapdexPageTitle")
    new_task_subtitle = ui.label("Results come back here as QGIS layers.", "mapdexBody")
    title_block.addWidget(new_task_title)
    title_block.addWidget(new_task_subtitle)
    workspace_layout.addLayout(title_block)

    workflow_block = QVBoxLayout()
    workflow_block.setSpacing(8)
    workflow_block.addWidget(_section_label("What should Mapdex do?"))
    workflow_box = WorkflowChooser()
    workflow_box.setObjectName("mapdexWorkflowChooser")
    for title, key in workflows:
        workflow_box.addItem(title, key)
    workflow_block.addWidget(workflow_box)
    workspace_layout.addLayout(workflow_block)

    maps_block = QVBoxLayout()
    maps_block.setSpacing(8)
    maps_block.addWidget(_section_label("Maps"))
    drop_zone = SourceDropZone()
    source_list = SourceList()
    source_list.setVisible(False)
    drop_zone.set_list(source_list)
    maps_block.addWidget(drop_zone)
    # A status line here only for a problem that blocks the run; the list and
    # the button already say what is ready.
    maps_problem = ui.StatusLine("warn", "", "")
    maps_problem.setObjectName("mapdexMapsProblem")
    maps_problem.setVisible(False)
    maps_block.addWidget(maps_problem)
    # The count and size the list already shows: kept for the code that writes
    # it, never on screen.
    source_summary = ui.label("No sources selected", "mapdexMeta")
    source_summary.setObjectName("mapdexSourceSummary")
    attic_layout.addWidget(source_summary)
    workspace_layout.addLayout(maps_block)

    # Three real fields, every one read by POST /v1/batches.
    options = OptionsDisclosure("Options")
    options_layout = QVBoxLayout(options.body)
    options_layout.setContentsMargins(12, 12, 12, 12)
    options_layout.setSpacing(10)
    expand_pages_check = QCheckBox("Treat every PDF page as its own sheet")
    expand_pages_check.setToolTip(
        "A multi-page PDF arrives as one sheet unless this is on. With it on, "
        "each page becomes its own item with its own result and review."
    )
    skip_completed_check = QCheckBox("Skip sheets already completed in this project")
    skip_completed_check.setToolTip(
        "Re-running an archive after a partial failure otherwise runs, and "
        "charges for, the sheets that already succeeded."
    )
    options_layout.addWidget(expand_pages_check)
    options_layout.addWidget(skip_completed_check)
    concurrency_box = _elastic(_ArrowComboBox())
    for label_text, value in concurrency_choices():
        concurrency_box.addItem(label_text, value)
    options_layout.addWidget(_field_row("Parallel sheets", concurrency_box))
    workspace_layout.addWidget(options)
    workspace_layout.addStretch(1)

    task_footer = ui.Band("mapdexFooterBand", READING_WIDTH, (16, 16, 16, 16))
    balance_row = QWidget()
    balance_layout = QHBoxLayout(balance_row)
    balance_layout.setContentsMargins(0, 0, 0, 0)
    balance_layout.setSpacing(8)
    balance_icon = ui.StatusIcon("warn", 16)
    balance_notice = ui.label("", "mapdexBody")
    balance_notice.setObjectName("mapdexBalanceNotice")
    top_up_button = ui.LinkButton("Add balance", "link", "external-link")
    balance_layout.addWidget(balance_icon, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
    balance_layout.addWidget(balance_notice, 1, enum_member(Qt, "AlignmentFlag", "AlignTop"))
    balance_layout.addWidget(top_up_button, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
    ui.align_icon(balance_icon, balance_notice, size=16)
    balance_row.setVisible(False)
    task_footer.layout_.addWidget(balance_row)
    run_button = ui.PrimaryButton("Add maps to start", "", "crosshair", height=52)
    task_footer.layout_.addWidget(run_button)
    price_label = ui.label("", "mapdexMeta")
    price_label.setObjectName("mapdexPriceLine")
    price_label.setVisible(False)
    task_footer.layout_.addWidget(price_label)
    workspace_page_layout.addWidget(task_footer, 0)

    project_box = _elastic(_ArrowComboBox())
    attic_layout.addWidget(project_box)
    open_project_button = ui.secondary_button("Open in Mapdex")
    attic_layout.addWidget(open_project_button)
    run_footnote = ui.label("", "mapdexMeta")
    attic_layout.addWidget(run_footnote)

    # -- Jobs ----------------------------------------------------------
    jobs = QWidget()
    jobs.setObjectName("mapdexPage")
    jobs_page_layout = QVBoxLayout(jobs)
    jobs_page_layout.setContentsMargins(0, 0, 0, 0)
    jobs_scroll, jobs_layout, _jobs_column = ui.scroll_column(READING_WIDTH, (4, 20, 4, 20), 16)
    jobs_page_layout.addWidget(jobs_scroll, 1)
    jobs_locked = ui.label(JOBS_LOCKED_NOTICE, "mapdexBody")
    jobs_locked.setContentsMargins(12, 0, 12, 0)
    jobs_locked.setObjectName("mapdexLockedNotice")
    jobs_locked.setVisible(False)
    jobs_layout.addWidget(jobs_locked)

    jobs_empty = QWidget()
    empty_layout = QVBoxLayout(jobs_empty)
    empty_layout.setContentsMargins(12, 0, 12, 0)
    empty_layout.setSpacing(8)
    empty_layout.addWidget(ui.label("No jobs yet", "mapdexPageTitle"))
    empty_layout.addWidget(ui.label("Maps you send appear here.", "mapdexBody"))
    jobs_empty_link = ui.LinkButton("Send maps to Mapdex", "link")
    empty_layout.addWidget(jobs_empty_link, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))
    jobs_layout.addWidget(jobs_empty)

    batch = QWidget()
    batch.setObjectName("mapdexResultCard")
    batch.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_StyledBackground"), True)
    batch_layout = QVBoxLayout(batch)
    batch_layout.setContentsMargins(12, 12, 12, 12)
    batch_layout.setSpacing(12)
    heading = QVBoxLayout()
    heading.setSpacing(4)
    batch_title = ui.label("Latest task", "mapdexPageTitle")
    guidance_label = ui.label("", "mapdexBody")
    heading.addWidget(batch_title)
    heading.addWidget(guidance_label)
    batch_layout.addLayout(heading)
    phase_label = ui.StatusLine("working", "", "")
    phase_label.setObjectName("mapdexPhaseLine")
    batch_layout.addWidget(phase_label)
    # A bar only for a real fraction (sheets done of sheets sent), with the
    # numbers beside it. Never a percentage made from steps: it sits still and
    # reads as stuck.
    progress_bar = QProgressBar()
    progress_bar.setRange(0, 100)
    progress_bar.setValue(0)
    progress_bar.setTextVisible(False)
    progress_text = ui.label("", "mapdexMeta", wrap=False)
    progress_text.setObjectName("mapdexProgressText")
    progress_row = QHBoxLayout()
    progress_row.setContentsMargins(0, 0, 0, 0)
    progress_row.setSpacing(10)
    progress_row.addWidget(progress_bar, 1, enum_member(Qt, "AlignmentFlag", "AlignVCenter"))
    progress_row.addWidget(progress_text, 0, enum_member(Qt, "AlignmentFlag", "AlignVCenter"))
    batch_layout.addLayout(progress_row)
    item_list = JobItemList()
    item_list.setVisible(False)
    batch_layout.addWidget(item_list)
    import_button = ui.PrimaryButton("Add result to QGIS", "", "layers-2", height=40)
    import_button.setSizePolicy(
        enum_member(QSizePolicy, "Policy", "Maximum"),
        enum_member(QSizePolicy, "Policy", "Fixed"))
    batch_layout.addWidget(import_button, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))
    retry_button = ui.secondary_button("Retry failed item")
    review_button = ui.secondary_button("Review in Mapdex")
    secondary_row = QHBoxLayout()
    secondary_row.setContentsMargins(0, 0, 0, 0)
    secondary_row.setSpacing(8)
    secondary_row.addWidget(review_button, 0)
    secondary_row.addWidget(retry_button, 0)
    secondary_row.addStretch(1)
    batch_layout.addLayout(secondary_row)
    quiet_row = QHBoxLayout()
    quiet_row.setContentsMargins(0, 0, 0, 0)
    quiet_row.setSpacing(16)
    open_button = ui.LinkButton("Open task in Mapdex", "link", "external-link")
    open_button.setObjectName("mapdexQuietButton")
    cancel_button = ui.LinkButton("Cancel task", "danger", "x")
    cancel_button.setObjectName("mapdexDangerLink")
    quiet_row.addWidget(open_button, 0)
    quiet_row.addWidget(cancel_button, 0)
    quiet_row.addStretch(1)
    batch_layout.addLayout(quiet_row)
    jobs_layout.addWidget(batch)

    recent = ui.Disclosure("Earlier jobs")
    recent.setContentsMargins(12, 0, 12, 0)
    recent_body = QVBoxLayout(recent.body)
    recent_body.setContentsMargins(0, 0, 0, 0)
    recent_body.setSpacing(8)
    recent_box = _elastic(_ArrowComboBox())
    resume_button = ui.secondary_button("Open")
    recent_body.addLayout(root.register_pair(recent_box, resume_button))
    jobs_layout.addWidget(recent)
    jobs_layout.addStretch(1)

    page_order = (nivo, workspace, jobs)
    for page in page_order:
        pages.addWidget(page)

    # ------------------------------------------------------------------
    # Settings view
    # ------------------------------------------------------------------
    settings_page, settings_layout = _view("settings")
    settings_header = ui.Band("mapdexBand", READING_WIDTH, (9, 8, 16, 8))
    settings_header_row = QHBoxLayout()
    settings_header_row.setContentsMargins(0, 0, 0, 0)
    settings_header_row.setSpacing(8)
    settings_back = ui.IconButton("arrow-left", "Back")
    settings_title = ui.label("Settings", "mapdexViewTitle", wrap=False)
    settings_title.setObjectName("mapdexViewTitle")
    settings_header_row.addWidget(settings_back, 0)
    settings_header_row.addWidget(settings_title, 1)
    settings_header.layout_.addLayout(settings_header_row)
    settings_layout.addWidget(settings_header)
    settings_notice_band, settings_notice = _notice_band(hub)
    settings_layout.addWidget(settings_notice_band)
    settings_scroll, connection_layout, connection_panel = ui.scroll_column(
        READING_WIDTH, (16, 20, 16, 20), 24)
    connection_panel.setObjectName("mapdexSettingsPanel")
    settings_layout.addWidget(settings_scroll, 1)

    account_section = QVBoxLayout()
    account_section.setSpacing(10)
    account_section.addWidget(_section_label("Account"))
    account_card = QFrame()
    account_card.setObjectName("mapdexCard")
    account_row = QHBoxLayout(account_card)
    account_row.setContentsMargins(12, 12, 12, 12)
    account_row.setSpacing(12)
    avatar = QLabel()
    avatar.setObjectName("mapdexAvatar")
    avatar.setFixedSize(32, 32)
    avatar_layout = QHBoxLayout(avatar)
    avatar_layout.setContentsMargins(8, 8, 8, 8)
    avatar_layout.addWidget(ui.IconLabel("user", "slate", 16, 2))
    account_row.addWidget(avatar, 0)
    account_text = QVBoxLayout()
    account_text.setSpacing(2)
    account_name = ui.ElidedLabel("Connected to Mapdex", mode="ElideRight")
    account_name.setObjectName("mapdexInk")
    account_detail = ui.label("", "mapdexMeta")
    account_text.addWidget(account_name)
    account_text.addWidget(account_detail)
    account_row.addLayout(account_text, 1)
    plan_link = ui.LinkButton("Plan", "link")
    account_row.addWidget(plan_link, 0)
    account_section.addWidget(account_card)
    connection_layout.addLayout(account_section)

    model_section = QVBoxLayout()
    model_section.setSpacing(8)
    model_section.addWidget(_section_label("Which model answers in Nivo"))
    model_group = ui.RadioCardGroup()
    model_group.setObjectName("mapdexModelChoice")
    for value, title, description in MODEL_CHOICES:
        model_group.addItem(title, value, description)
    model_section.addWidget(model_group)
    # Retired: the fields appearing under the chosen card says it.
    model_hint = ui.label(MODEL_FIELDS_HINT, "mapdexMeta")
    attic_layout.addWidget(model_hint)

    # The stored provider, the single source of truth the plugin reads and
    # writes. Hidden: the cards and the vendor menu drive it.
    provider_box = _elastic(_ArrowComboBox())
    for label_text, value in PROVIDER_CHOICES:
        provider_box.addItem(label_text, value)
    attic_layout.addWidget(provider_box)
    provider_fields = QWidget()
    fields_layout = QVBoxLayout(provider_fields)
    fields_layout.setContentsMargins(0, 8, 0, 0)
    fields_layout.setSpacing(12)
    vendor_box = _elastic(_ArrowComboBox())
    for label_text, value in KEY_PROVIDER_CHOICES:
        vendor_box.addItem(label_text, value)
    vendor_row = _field_row("Provider", vendor_box)
    model_input = QLineEdit()
    model_input.setPlaceholderText("Provider default")
    model_row = _field_row("Model", model_input)
    base_url_input = QLineEdit()
    base_url_input.setPlaceholderText("https://… (required for compatible/local endpoints)")
    endpoint_row = _field_row("Endpoint", base_url_input)
    api_key_input = QLineEdit()
    # Never echo a secret, and never prefill it back from storage.
    api_key_input.setEchoMode(enum_member(QLineEdit, "EchoMode", "Password"))
    api_key_input.setPlaceholderText("Paste a key to use your own provider")
    clear_key_button = ui.LinkButton("Remove", "danger")
    clear_key_button.setToolTip("Remove the stored provider key and go back to your Mapdex plan")
    # A plain container widget, not a responsive pair: nothing here is ever
    # moved between layout cells.
    api_key_row = QWidget()
    api_key_row_layout = QHBoxLayout(api_key_row)
    api_key_row_layout.setContentsMargins(0, 0, 0, 0)
    api_key_row_layout.setSpacing(10)
    api_key_row_layout.addWidget(api_key_input, 1)
    api_key_row_layout.addWidget(clear_key_button, 0)
    key_row = _field_row("API key", api_key_row)
    for row in (vendor_row, model_row, endpoint_row, key_row):
        fields_layout.addWidget(row)
    # Stored settings as TEXT, never only a placeholder: grey placeholder text
    # reads as "empty, type here".
    assistant_key_state = ui.label("No provider key stored. Nivo runs on your Mapdex plan.", "mapdexMeta")
    assistant_key_state.setObjectName("mapdexKeyState")
    fields_layout.addWidget(assistant_key_state)
    # Where the question goes, said with the fields that change it. Under
    # Mapdex the card's own sentence already says it.
    # The card's own sentence says where questions go; this line is kept for
    # the code that writes it, never on screen.
    assistant_privacy = ui.label("Mapdex-hosted assistant: bounded map context is sent to Mapdex.", "mapdexMeta")
    attic_layout.addWidget(assistant_privacy)
    model_section.addWidget(provider_fields)
    save_settings_button = ui.PrimaryButton("Save settings", height=40)
    save_settings_button.set_centred(True)
    model_section.addWidget(save_settings_button)
    connection_layout.addLayout(model_section)

    endpoint_frame = QFrame()
    endpoint_layout = QVBoxLayout(endpoint_frame)
    endpoint_layout.setContentsMargins(0, 0, 0, 0)
    endpoint_layout.setSpacing(10)
    endpoint_layout.addWidget(_section_label("Endpoints (development build)"))
    api_url_input = _elastic(_ArrowComboBox())
    api_url_input.setEditable(True)
    web_url_input = _elastic(_ArrowComboBox())
    web_url_input.setEditable(True)
    api_choices, web_choices = endpoint_choices()
    for value in api_choices:
        api_url_input.addItem(value)
    for value in web_choices:
        web_url_input.addItem(value)
    endpoint_layout.addWidget(_field_row("API", api_url_input))
    endpoint_layout.addWidget(_field_row("Web", web_url_input))
    connection_layout.addWidget(endpoint_frame)
    if not endpoint_settings:
        endpoint_frame.setVisible(False)

    connection_layout.addStretch(1)
    settings_footer = QVBoxLayout()
    settings_footer.setSpacing(12)
    settings_footer.addWidget(ui.hline())
    disconnect_button = ui.LinkButton("Disconnect from Mapdex", "danger", "log-out")
    settings_footer.addWidget(disconnect_button, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))
    meta_row = QHBoxLayout()
    meta_row.setContentsMargins(0, 0, 0, 0)
    meta_row.setSpacing(6)
    meta_row.addWidget(ui.label("Mapdex for QGIS {} ·".format(PLUGIN_VERSION), "mapdexMeta", wrap=False))
    help_link = ui.LinkButton("Help", "link")
    contact_link = ui.LinkButton("Contact us", "link")
    copy_details_link = ui.LinkButton("Copy details", "muted")
    copy_details_link.setToolTip(
        "Copy the plugin, QGIS, Qt and system versions to paste into a message")
    meta_row.addWidget(help_link)
    meta_row.addWidget(ui.label("·", "mapdexMeta", wrap=False))
    meta_row.addWidget(contact_link)
    meta_row.addWidget(ui.label("·", "mapdexMeta", wrap=False))
    meta_row.addWidget(copy_details_link)
    meta_row.addStretch(1)
    settings_footer.addLayout(meta_row)
    connection_layout.addLayout(settings_footer)

    # -- the model choice drives the stored provider, and follows it --------
    syncing = {"busy": False}

    def _provider_value():
        return str(provider_box.currentData() or "")

    def sync_settings(*_args):
        """Show the cards, vendor and fields for the stored provider value."""
        if syncing["busy"]:
            return
        syncing["busy"] = True
        try:
            provider = _provider_value()
            choice = model_choice_for(provider)
            model_group.blockSignals(True)
            model_group.setCurrentIndex(model_group.findData(choice))
            model_group.blockSignals(False)
            if choice == "own":
                index = vendor_box.findData(provider)
                if index >= 0:
                    vendor_box.blockSignals(True)
                    vendor_box.setCurrentIndex(index)
                    vendor_box.blockSignals(False)
            shown = model_fields_for(provider)
            vendor_row.setVisible(shown["vendor"])
            model_row.setVisible(shown["model"])
            endpoint_row.setVisible(shown["endpoint"])
            key_row.setVisible(shown["key"])
            provider_fields.setVisible(choice != "mapdex")
            stored = str(provider_box.property("storedProvider") or "")
            save_settings_button.setVisible(choice != "mapdex" or provider != stored)
        finally:
            syncing["busy"] = False

    def _choose_model(index):
        choice = model_group.itemData(index)
        if choice == "mapdex":
            wanted = ""
        elif choice == "local":
            wanted = LOCAL_PROVIDER
        else:
            wanted = str(vendor_box.currentData() or "openai")
        target = provider_box.findData(wanted)
        if target >= 0 and target != provider_box.currentIndex():
            provider_box.setCurrentIndex(target)
        else:
            sync_settings()

    def _choose_vendor(_index):
        if model_group.currentData() == "own":
            target = provider_box.findData(str(vendor_box.currentData() or ""))
            if target >= 0:
                provider_box.setCurrentIndex(target)

    model_group.currentIndexChanged.connect(_choose_model)
    vendor_box.currentIndexChanged.connect(_choose_vendor)
    provider_box.currentIndexChanged.connect(sync_settings)

    # ------------------------------------------------------------------
    # Views and page switching
    # ------------------------------------------------------------------
    for view in (connect_view, main_view, settings_page):
        views.addWidget(view)

    current = {"page": 0}

    def _show_page(target):
        # A status belongs to the page it was said on; moving away clears it.
        if pages.currentIndex() != target:
            hub.clear()
        pages.setCurrentIndex(target)
        for index, page in enumerate(page_order):
            page.setVisible(index == target)
        tab_bar.set_current(target)
        # New chat and History act on the conversation, so they are offered
        # only where the conversation is.
        on_nivo = target == 0
        nivo_new_button.setVisible(on_nivo and nivo_new_button.property("allowed") is not False)
        nivo_history_button.setVisible(on_nivo and nivo_history_button.property("allowed") is not False)

    def switch_page(target):
        """Show a tab (0-2) or the Settings view (SETTINGS_PAGE)."""
        if target == SETTINGS_PAGE:
            if views.currentWidget() is not settings_page:
                hub.clear()
            views.setCurrentWidget(settings_page)
            sync_settings()
            return
        if views.currentWidget() is not main_view and views.currentWidget() is not connect_view:
            views.setCurrentWidget(main_view)
        if views.currentWidget() is connect_view:
            current["page"] = target
            _show_page(target)
            return
        current["page"] = target
        _show_page(target)

    def show_connected(connected):
        """The connect view while nothing is connected, otherwise the work."""
        if not connected:
            views.setCurrentWidget(connect_view)
            return
        if views.currentWidget() is connect_view:
            views.setCurrentWidget(main_view)
            _show_page(current["page"])

    def _back():
        views.setCurrentWidget(main_view)
        _show_page(current["page"])

    tab_bar.selected.connect(switch_page)
    open_settings_button.clicked.connect(lambda _checked=False: switch_page(SETTINGS_PAGE))
    settings_back.clicked.connect(lambda _checked=False: _back())
    jobs_empty_link.clicked.connect(lambda _checked=False: switch_page(1))
    views.setCurrentWidget(connect_view)
    _show_page(0)

    tail = QWidget()
    attic_layout.addWidget(tail)

    allow_narrow(root)
    ui.apply_theme(root, ui.tokens())
    sync_settings()
    root.apply_layout_mode(panel_layout_mode(PREFERRED_WIDTH))
    return root, {
        "status": hub,
        "connection_label": connection_label,
        "api_url_input": api_url_input,
        "web_url_input": web_url_input,
        "save_settings_button": save_settings_button,
        "provider_box": provider_box,
        "vendor_box": vendor_box,
        "model_group": model_group,
        "sync_settings": sync_settings,
        "model_input": model_input,
        "base_url_input": base_url_input,
        "api_key_input": api_key_input,
        "assistant_privacy": assistant_privacy,
        "clear_key_button": clear_key_button,
        "connect_button": connect_button,
        "connect_promise": connect_promise,
        "sign_in": sign_in,
        "column": transcript,
        "tail": tail,
        "body_layout": outer,
        "first_open_prompt": first_open_prompt,
        "first_open_title": first_open_title,
        "segment_bar": segment_bar,
        "tab_bar": tab_bar,
        "settings_page": settings_page,
        "connection_panel": connection_panel,
        "disconnect_button": disconnect_button,
        "workspace": workspace,
        "tabs": pages,
        "views": views,
        "switch_page": switch_page,
        "show_connected": show_connected,
        "open_settings_button": open_settings_button,
        "account_name": account_name,
        "account_detail": account_detail,
        "plan_link": plan_link,
        "help_link": help_link,
        "contact_link": contact_link,
        "copy_details_link": copy_details_link,
        "progress_text": progress_text,
        "maps_problem": maps_problem,
        "batch": batch,
        "batch_title": batch_title,
        "phase_label": phase_label,
        "progress_bar": progress_bar,
        "guidance_label": guidance_label,
        "jobs_empty": jobs_empty,
        "project_box": project_box,
        "open_project_button": open_project_button,
        "workflow_box": workflow_box,
        "drop_zone": drop_zone,
        "source_list": source_list,
        "source_summary": source_summary,
        "options": options,
        "price_label": price_label,
        "balance_row": balance_row,
        "balance_notice": balance_notice,
        "top_up_button": top_up_button,
        "expand_pages_check": expand_pages_check,
        "skip_completed_check": skip_completed_check,
        "concurrency_box": concurrency_box,
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
        "open_batch_button": open_button,
        "retry_button": retry_button,
        "import_button": import_button,
        "review_button": review_button,
        "item_list": item_list,
        "recent": recent,
        "recent_box": recent_box,
        "resume_button": resume_button,
    }


class _ConnectButton(ui.PrimaryButton):
    """The one button on the connect view, keeping the row API the plugin uses.

    `set_sublabel` writes the line UNDER the button, which is where the
    consequence of pressing it is said ("Opens your browser...").
    """

    def __init__(self, text, icon_name="", height=52):
        super().__init__(text, "", "", height)
        self.set_centred(True)
        self._trailing = ui.IconLabel(icon_name, "primary_text", 16, 2) if icon_name else None
        if self._trailing is not None:
            self._trailing.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
            self.layout().addWidget(self._trailing, 0)
        self.promise_label = None

    def set_label(self, label):
        self.setText(label)

    def set_sublabel(self, sublabel):
        if self.promise_label is not None:
            self.promise_label.setText(sublabel or "")
            self.promise_label.setVisible(bool(sublabel))

    def set_tone(self, tone):
        self.setProperty("tone", tone)


class _TurnStatusLabel(QLabel):
    """Nivo's turn status, shown only while it says something.

    "Ready" is the absence of news, so it hides the line rather than printing a
    word under every answer.
    """

    _SILENT = {"", "Ready"}

    def __init__(self, text=""):
        super().__init__(text)
        self.setObjectName("mapdexMeta")
        self.setWordWrap(True)
        self.setMinimumWidth(1)
        self.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self.setVisible(text not in self._SILENT)

    def setText(self, text):  # noqa: N802 - Qt virtual name
        super().setText(text or "")
        super().setVisible((text or "") not in self._SILENT)

    def setVisible(self, visible):  # noqa: N802 - Qt virtual name
        super().setVisible(bool(visible) and self.text() not in self._SILENT)


def _style_dialog(dialog):
    """Dialogs are not children of the panel, so they take the theme here."""
    tokens = ui.tokens()
    dialog.setStyleSheet(
        theme.stylesheet(tokens, ui.panel_font_family())
        + "QDialog {{ background: {s}; color: {i}; }} "
          "QListWidget {{ background: {f}; color: {i}; border: 1px solid {b}; border-radius: 6px; }}".format(
              s=tokens["surface"], i=tokens["ink"], f=tokens["field"], b=tokens["border_strong"])
    )


def build_balance_dialog(notice, workflow_title="", parent=None, headline=None, reassurance=None):
    """The batch did not start, and here is the one thing that changes that.

    It carries the refusal SENTENCE it was handed rather than composing one:
    the numbers behind it are the server's. `exec()` is truthy when the user
    chose to go to billing.
    """
    dialog = QDialog(parent)
    dialog.setObjectName("mapdexBalanceDialog")
    dialog.setWindowTitle("Not enough balance")
    dialog.setMinimumWidth(420)
    _style_dialog(dialog)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)
    title = ui.label(
        headline if headline
        else "{} did not start.".format(workflow_title) if workflow_title
        else "This task did not start.",
        "mapdexCardTitle",
    )
    title.setObjectName("mapdexSectionTitle")
    layout.addWidget(title)
    body = ui.label(str(notice or ""), "mapdexBody")
    layout.addWidget(body)
    footnote = ui.label(
        reassurance or (
            "Nothing was uploaded and nothing was charged. Your sources are still "
            "in the list."
        ),
        "mapdexMeta",
    )
    footnote.setObjectName("mapdexPageFootnote")
    layout.addWidget(footnote)

    actions = QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(8)
    billing_button = ui.PrimaryButton("Open billing", height=40)
    billing_button.set_centred(True)
    billing_button.setDefault(True)
    close_button = ui.secondary_button("Not now")
    actions.addWidget(billing_button, 1)
    actions.addWidget(close_button, 0)
    layout.addLayout(actions)

    billing_button.clicked.connect(dialog.accept)
    close_button.clicked.connect(dialog.reject)
    return dialog


def build_layer_picker_dialog(entries, workflow_title="", parent=None):
    """Tick the open layers to send. Explicit selection, never "the project".

    Nothing is ticked when it opens, every row is named, and what leaves is
    exactly what was ticked. An incompatible layer is SHOWN and disabled with
    its reason rather than filtered out.
    """
    dialog = QDialog(parent)
    dialog.setObjectName("mapdexLayerPicker")
    dialog.setWindowTitle("Add QGIS layers")
    dialog.setMinimumWidth(440)
    dialog.setMinimumHeight(340)
    _style_dialog(dialog)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    intro = ui.label(
        "Choose the layers to send{}. Each ticked layer becomes its own sheet.".format(
            " to {}".format(workflow_title) if workflow_title else ""
        ),
        "mapdexBody",
    )
    layout.addWidget(intro)

    listing = QListWidget()
    listing.setObjectName("mapdexLayerPickerList")
    layout.addWidget(listing, 1)

    eligible_total = 0
    for entry in entries:
        item = QListWidgetItem(listing)
        name = str(entry.get("name") or entry.get("id") or "")
        detail = str(entry.get("detail") or "")
        item.setText("{}  ·  {}".format(name, detail) if detail else name)
        item.setData(enum_member(Qt, "ItemDataRole", "UserRole"), str(entry.get("id") or ""))
        checkable = enum_member(Qt, "ItemFlag", "ItemIsUserCheckable")
        enabled = enum_member(Qt, "ItemFlag", "ItemIsEnabled")
        if entry.get("eligible"):
            eligible_total += 1
            item.setFlags(item.flags() | checkable | enabled)
            item.setCheckState(enum_member(Qt, "CheckState", "Unchecked"))
        else:
            item.setFlags(item.flags() & ~enabled & ~checkable)
            item.setCheckState(enum_member(Qt, "CheckState", "Unchecked"))
            reason = str(entry.get("reason") or "")
            if reason:
                item.setText("{}  ·  {}".format(item.text(), reason))

    state_label = ui.label("", "mapdexMeta")
    state_label.setObjectName("mapdexLayerPickerState")
    if not entries:
        state_label.setText("This QGIS project has no layers to send.")
    elif eligible_total == 0:
        state_label.setText(
            "None of the open layers suit this workflow. The reason is beside each one."
        )
    layout.addWidget(state_label)

    actions = QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(8)
    select_all = ui.LinkButton("Select all compatible", "link")
    select_all.setEnabled(eligible_total > 0)
    add_button = ui.PrimaryButton("Add", height=40)
    add_button.set_centred(True)
    add_button.setFixedWidth(150)
    add_button.setDefault(True)
    add_button.setEnabled(False)
    cancel_button = ui.secondary_button("Cancel")
    actions.addWidget(select_all)
    actions.addStretch(1)
    actions.addWidget(cancel_button)
    actions.addWidget(add_button)
    layout.addLayout(actions)

    checked = enum_member(Qt, "CheckState", "Checked")
    role = enum_member(Qt, "ItemDataRole", "UserRole")

    def selected_ids():
        ids = []
        for index in range(listing.count()):
            item = listing.item(index)
            if item.checkState() == checked:
                ids.append(str(item.data(role) or ""))
        return [value for value in ids if value]

    def _sync(*_args):
        count = len(selected_ids())
        add_button.setEnabled(count > 0)
        add_button.setText("Add {} layers".format(count) if count > 1 else "Add")

    def _select_all():
        for index in range(listing.count()):
            item = listing.item(index)
            if item.flags() & enum_member(Qt, "ItemFlag", "ItemIsUserCheckable"):
                item.setCheckState(checked)
        _sync()

    listing.itemChanged.connect(_sync)
    select_all.clicked.connect(_select_all)
    add_button.clicked.connect(dialog.accept)
    cancel_button.clicked.connect(dialog.reject)

    return dialog, {
        "listing": listing,
        "state_label": state_label,
        "add_button": add_button,
        "cancel_button": cancel_button,
        "select_all_button": select_all,
        "selected_ids": selected_ids,
    }


def build_thread_history_dialog(parent=None):
    """The Nivo History dialog: layout and every state it can be in.

    Loading, empty, failed and populated each get a widget here. Rows are plain
    text: a conversation title is the user's own words coming back from the
    server.
    """
    dialog = QDialog(parent)
    dialog.setObjectName("mapdexHistoryDialog")
    dialog.setWindowTitle("Nivo conversations")
    dialog.setMinimumWidth(430)
    dialog.setMinimumHeight(360)
    _style_dialog(dialog)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    intro = ui.label("Earlier Nivo conversations in this workspace. Opening one continues it.", "mapdexBody")
    layout.addWidget(intro)
    state_label = ui.label("Loading conversations…", "mapdexMeta")
    state_label.setObjectName("mapdexHistoryState")
    layout.addWidget(state_label)
    listing = QListWidget()
    listing.setObjectName("mapdexHistoryList")
    listing.setVisible(False)
    layout.addWidget(listing, 1)

    actions = QHBoxLayout()
    actions.setContentsMargins(0, 0, 0, 0)
    actions.setSpacing(8)
    delete_button = ui.LinkButton("Delete", "danger")
    delete_button.setToolTip("Delete this conversation from Mapdex. This cannot be undone.")
    delete_button.setEnabled(False)
    retry_button = ui.secondary_button("Try again")
    retry_button.setVisible(False)
    open_button = ui.PrimaryButton("Open", height=40)
    open_button.set_centred(True)
    open_button.setFixedWidth(120)
    open_button.setEnabled(False)
    open_button.setDefault(True)
    close_button = ui.secondary_button("Close")
    actions.addWidget(delete_button)
    actions.addStretch(1)
    actions.addWidget(retry_button)
    actions.addWidget(close_button)
    actions.addWidget(open_button)
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
