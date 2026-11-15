"""The panel's reusable widgets: one shape per kind of thing on screen.

Before this file a choice in the panel was sometimes a chip, sometimes a
full-width button and sometimes bare text, a status was a coloured stripe or an
ASCII mark, and the one filled colour (indigo) was on seven controls at once.
Each widget here is one rule from the web app (`docs/DESIGN.md`):

* `PrimaryButton`  - the ONE filled blue action on a screen, optionally with its
                     price inside it on the right.
* `SecondaryButton`- a raised surface with a border.
* `LinkButton`     - a quiet action as a text link; red text when destructive.
* `StatusLine`     - a solid status icon, a bold coloured word, a muted detail.
* `RadioCard(Group)` - a choice that states what it does, not a combo entry.
* `PromptCard`     - a starting point you can press.
* `TabBar`         - ink text and a 2 px ink underline on the chosen tab.

Colours come from `theme`, icons from `icons`. A widget that draws an icon has a
`retheme()` method, and `apply_theme` calls every one of them, so switching the
QGIS theme recolours the panel without rebuilding it.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import QByteArray, QEvent, QObject, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
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

from . import icons as icon_files
from . import theme
from .qt_compat import enum_member, event_point

try:  # QtSvg ships with every QGIS build, but the wrapper module name has moved.
    from qgis.PyQt.QtSvg import QSvgRenderer
except Exception:  # noqa: BLE001 - fall back to the image plugin
    QSvgRenderer = None

_CURRENT = {"tokens": theme.DARK}


def tokens():
    """The token set the panel is currently drawn with."""
    return _CURRENT["tokens"]


def set_tokens(value):
    _CURRENT["tokens"] = value


def window_lightness():
    """HSL lightness of the QGIS window colour, or None without an application."""
    try:
        from qgis.PyQt.QtGui import QPalette  # noqa: PLC0415

        application = QApplication.instance()
        if application is None:
            return None
        return application.palette().color(enum_member(QPalette, "ColorRole", "Window")).lightness()
    except Exception:  # noqa: BLE001 - a wrong theme beats a broken panel
        return None


def tokens_for_interface():
    """The token set matching the QGIS theme on screen right now."""
    lightness = window_lightness()
    return theme.DARK if lightness is None else theme.theme_for_lightness(lightness)


def apply_theme(root, value=None):
    """Restyle `root` and re-colour every icon under it.

    Returns True when the token set changed. Reading the APPLICATION palette,
    never the root's own: the root's palette is what our stylesheet painted,
    so asking it which theme QGIS is wearing would always answer "ours".
    """
    value = value or tokens_for_interface()
    changed = value is not tokens() or not root.property("mapdexThemed")
    set_tokens(value)
    root.setStyleSheet(theme.stylesheet(value))
    root.setProperty("mapdexThemed", True)
    for widget in [root] + list(root.findChildren(QWidget)):
        retheme = getattr(widget, "retheme", None)
        if callable(retheme):
            try:
                retheme()
            except RuntimeError:  # a wrapper whose C++ side is already gone
                pass
    return changed


def _ratio():
    application = QApplication.instance()
    try:
        return max(2.0, float(application.devicePixelRatio())) if application else 2.0
    except Exception:  # noqa: BLE001
        return 2.0


def svg_pixmap(data, size):
    """Render SVG bytes to a crisp pixmap of `size` logical pixels."""
    ratio = _ratio()
    edge = max(1, int(round(size * ratio)))
    pixmap = QPixmap(edge, edge)
    pixmap.fill(QColor(0, 0, 0, 0))
    rendered = False
    if QSvgRenderer is not None:
        renderer = QSvgRenderer(QByteArray(data))
        if renderer.isValid():
            painter = QPainter(pixmap)
            painter.setRenderHint(enum_member(QPainter, "RenderHint", "Antialiasing"), True)
            renderer.render(painter)
            painter.end()
            rendered = True
    if not rendered:
        loaded = QPixmap()
        if loaded.loadFromData(QByteArray(data), "SVG"):
            pixmap = loaded.scaled(
                edge, edge,
                enum_member(Qt, "AspectRatioMode", "KeepAspectRatio"),
                enum_member(Qt, "TransformationMode", "SmoothTransformation"),
            )
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def lucide_icon(name, colour_key="slate", size=18, stroke=1.75, disabled_key="faint"):
    """A Lucide icon in a theme colour, with a muted variant for disabled."""
    tokens_now = tokens()
    normal = svg_pixmap(icon_files.lucide_svg(name, tokens_now[colour_key], stroke), size)
    off = svg_pixmap(icon_files.lucide_svg(name, tokens_now[disabled_key], stroke), size)
    icon = QIcon()
    icon.addPixmap(normal, enum_member(QIcon, "Mode", "Normal"))
    icon.addPixmap(off, enum_member(QIcon, "Mode", "Disabled"))
    return icon


def status_pixmap(tone, size=18):
    return svg_pixmap(icon_files.status_svg(tone, theme.tone_colour(tokens(), tone)), size)


def _repolish(widget):
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _policy(horizontal, vertical):
    return QSizePolicy(
        enum_member(QSizePolicy, "Policy", horizontal),
        enum_member(QSizePolicy, "Policy", vertical),
    )


def label(text="", name="mapdexBody", wrap=True, plain=True):
    """A themed label. Plain text by default: user and model text is data."""
    widget = QLabel(text)
    widget.setObjectName(name)
    if plain:
        widget.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
    widget.setWordWrap(wrap)
    if wrap:
        widget.setMinimumWidth(1)
    return widget


class ElidedLabel(QLabel):
    """One line that shrinks with the dock instead of pinning it open.

    A plain QLabel reports its whole text as its minimum width, so one long
    file name puts a floor under the entire panel. The text is elided at paint
    time and the full string stays in the tooltip.
    """

    def __init__(self, text="", parent=None, mode="ElideMiddle"):
        super().__init__(text, parent)
        self._full = text or ""
        self._mode = mode
        self.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self.setMinimumWidth(1)
        self.setSizePolicy(_policy("Ignored", "Preferred"))
        self.setToolTip(self._full)

    def setText(self, text):  # noqa: N802 - Qt virtual name
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)

    def text(self):  # noqa: D401 - the full text, not the elided paint
        return self._full

    def paintEvent(self, event):  # noqa: N802 - Qt virtual name
        elided = self.fontMetrics().elidedText(
            self._full, enum_member(Qt, "TextElideMode", self._mode), max(1, self.width()))
        if elided != super().text():
            super().setText(elided)
        super().paintEvent(event)


class IconLabel(QLabel):
    """A Lucide glyph as a label, recoloured with the theme."""

    def __init__(self, name, colour_key="slate", size=16, stroke=1.75, parent=None):
        super().__init__(parent)
        self._spec = (name, colour_key, size, stroke)
        self.setFixedSize(size, size)
        self.setObjectName("mapdexIcon")
        self.retheme()

    def set_icon(self, name, colour_key=None):
        _old, key, size, stroke = self._spec
        self._spec = (name, colour_key or key, size, stroke)
        self.retheme()

    def retheme(self):
        name, key, size, stroke = self._spec
        self.setPixmap(svg_pixmap(icon_files.lucide_svg(name, tokens()[key], stroke), size))


class StatusLine(QWidget):
    """A solid status icon, a bold coloured word, and a muted detail.

    Never a tinted pill and never colour alone: the icon's SHAPE and the word
    carry the state, the colour is the third carrier.
    """

    def __init__(self, tone="neutral", word="", detail="", parent=None, icon_size=18):
        super().__init__(parent)
        self.setObjectName("mapdexStatusLine")
        self._tone = tone
        self._icon_size = icon_size
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.icon = QLabel()
        self.icon.setObjectName("mapdexStatusIcon")
        self.icon.setFixedSize(icon_size, icon_size)
        self.word = QLabel()
        self.word.setObjectName("mapdexStatusWord")
        self.word.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self.detail = label("", "mapdexStatusDetail")
        row.addWidget(self.icon, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        row.addWidget(self.word, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        row.addWidget(self.detail, 1, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        self.set_status(tone, word, detail)

    def set_status(self, tone, word, detail=""):
        self._tone = tone if tone in theme.TONES else "neutral"
        self.word.setText(word or "")
        self.word.setVisible(bool(word))
        self.word.setProperty("tone", self._tone)
        _repolish(self.word)
        self.detail.setText(detail or "")
        self.detail.setVisible(bool(detail))
        self.retheme()

    def tone(self):
        return self._tone

    def setText(self, text):  # noqa: N802 - the QLabel surface callers already use
        self.set_status(self._tone, self.word.text(), text)

    def text(self):
        return " ".join(part for part in (self.word.text(), self.detail.text()) if part)

    def retheme(self):
        self.icon.setPixmap(status_pixmap(self._tone, self._icon_size))


class IconButton(QToolButton):
    """An icon-only control with a tooltip and an accessible name."""

    def __init__(self, icon_name, tooltip, colour_key="slate", size=32, icon_size=18,
                 object_name="mapdexIconButton", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self._spec = (icon_name, colour_key, icon_size)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setText(tooltip)
        self.setToolButtonStyle(enum_member(Qt, "ToolButtonStyle", "ToolButtonIconOnly"))
        self.setFixedSize(size, size)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.retheme()

    def set_icon_name(self, name):
        _old, key, size = self._spec
        self._spec = (name, key, size)
        self.retheme()

    def retheme(self):
        name, key, size = self._spec
        self.setIcon(lucide_icon(name, key, size, stroke=2 if key == "primary_text" else 1.75))


class PrimaryButton(QPushButton):
    """The one filled blue action: its label on the left, its price on the right.

    The price sits INSIDE the control that spends it (the web app's rule): a
    person who presses without reading the line under it has still been told.
    An empty price shows nothing, which is how an unknown price is rendered -
    never as "$0".
    """

    def __init__(self, text="", price="", icon_name="", height=44, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexPrimaryButton")
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(_policy("Expanding", "Fixed"))
        self.setFixedHeight(height)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(10)
        self._glyph = IconLabel(icon_name, "primary_text", 18, 2) if icon_name else None
        self._label = QLabel()
        self._label.setObjectName("mapdexPrimaryLabel")
        self._label.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self._label.setMinimumWidth(1)
        self._label.setSizePolicy(_policy("Ignored", "Preferred"))
        self._price = QLabel()
        self._price.setObjectName("mapdexPrimaryPrice")
        self._price.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        for child in (self._glyph, self._label, self._price):
            if child is not None:
                child.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
                row.addWidget(child, 1 if child is self._label else 0)
        self._centred = False
        self.setText(text)
        self.set_price(price)

    def set_icon_name(self, name):
        if self._glyph is not None and name:
            self._glyph.set_icon(name)

    def sizeHint(self):  # noqa: N802 - Qt virtual name
        """The width the label, the price and the glyph need, so a button that
        is not stretched across its row is exactly as wide as what it says."""
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing()
        width = margins.left() + margins.right()
        parts = [self._label.fontMetrics().boundingRect(self._label.text()).width() + 8]
        if self._glyph is not None:
            parts.append(self._glyph.width())
        if self._price.isVisibleTo(self) and self._price.text():
            parts.append(self._price.fontMetrics().boundingRect(self._price.text()).width() + 6)
        width += sum(parts) + spacing * (len(parts) - 1)
        return QSize(max(width, 64), self.height() or 40)

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual name
        return QSize(64, self.height() or 40)

    def set_centred(self, centred):
        """A button with no price reads better centred (Connect, Save)."""
        self._centred = bool(centred)
        self._label.setAlignment(
            enum_member(Qt, "AlignmentFlag", "AlignCenter") if centred
            else enum_member(Qt, "AlignmentFlag", "AlignLeft") | enum_member(Qt, "AlignmentFlag", "AlignVCenter"))
        self._label.setSizePolicy(_policy("Expanding" if centred else "Ignored", "Preferred"))

    def setText(self, text):  # noqa: N802 - Qt virtual name
        self._label.setText(str(text or ""))
        self.setAccessibleName(self.text())
        self.setToolTip(self.text() if len(self.text()) > 40 else "")

    def text(self):  # noqa: D401 - the visible label, price included when set
        price = self._price.text() if self._price.isVisibleTo(self) else ""
        label_text = self._label.text()
        return "{} · {}".format(label_text, price) if price else label_text

    def label_text(self):
        return self._label.text()

    def set_price(self, price):
        self._price.setText(str(price or ""))
        self._price.setVisible(bool(price))

    def price(self):
        return self._price.text()

    def changeEvent(self, event):  # noqa: N802 - Qt virtual name
        super().changeEvent(event)
        from qgis.PyQt.QtCore import QEvent  # noqa: PLC0415

        if event.type() == enum_member(QEvent, "Type", "EnabledChange"):
            off = "true" if not self.isEnabled() else "false"
            for child in (self._label, self._price):
                child.setProperty("off", off)
                _repolish(child)


def secondary_button(text):
    button = QPushButton(text)
    button.setObjectName("mapdexSecondaryButton")
    button.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
    return button


class LinkButton(QPushButton):
    """A quiet action, as a text link. `kind` is link, muted or danger."""

    _NAMES = {"link": "mapdexLink", "muted": "mapdexMutedLink", "danger": "mapdexDangerLink"}
    _COLOURS = {"link": "link", "muted": "muted", "danger": "err_text"}

    def __init__(self, text, kind="link", icon_name="", parent=None):
        super().__init__(text, parent)
        self._kind = kind
        self._icon_name = icon_name
        self.setObjectName(self._NAMES.get(kind, "mapdexLink"))
        self.setFlat(True)
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(_policy("Maximum", "Fixed"))
        self.retheme()

    def set_icon_name(self, name):
        self._icon_name = name
        self.retheme()

    def retheme(self):
        if self._icon_name:
            size = 14 if self._kind != "danger" else 16
            self.setIcon(lucide_icon(self._icon_name, self._COLOURS.get(self._kind, "link"), size, 2))
            self.setIconSize(QSize(size, size))
        else:
            self.setIcon(QIcon())


class RadioCard(QFrame):
    """One choice, with a title, an optional marker and a sentence.

    A combo hides every option but the chosen one, so the offer was a closed
    list a first-time user had to open to discover. A card states the choice
    and what it does at once.
    """

    clicked = pyqtSignal()

    def __init__(self, title, description="", badge="", parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexRadioCard")
        self.setFocusPolicy(enum_member(Qt, "FocusPolicy", "StrongFocus"))
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(_policy("Preferred", "Minimum"))
        self._checked = False
        self._title_text = str(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)
        self._radio = QLabel()
        self._radio.setFixedSize(16, 16)
        indicator = QVBoxLayout()
        indicator.setContentsMargins(0, 2, 0, 0)
        indicator.addWidget(self._radio)
        indicator.addStretch(1)
        row.addLayout(indicator)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)
        # Not wrapped: a title is a few words, and a wrapping label beside a
        # marker is squeezed to one word per line by the row it sits in.
        self.title = label(str(title), "mapdexRadioTitle", wrap=False)
        heading.addWidget(self.title, 0)
        self.badge = QLabel(badge)
        self.badge.setObjectName("mapdexBadgeWord")
        self.badge.setVisible(bool(badge))
        heading.addWidget(self.badge, 0)
        heading.addStretch(1)
        text.addLayout(heading)
        self.description = label(description, "mapdexRadioDescription")
        self.description.setVisible(bool(description))
        text.addWidget(self.description)
        row.addLayout(text, 1)
        self.setAccessibleName(str(title))
        self.retheme()

    def text(self):
        return self._title_text

    def isChecked(self):  # noqa: N802 - mirrors QAbstractButton
        return self._checked

    def setChecked(self, checked):  # noqa: N802 - mirrors QAbstractButton
        self._checked = bool(checked)
        self.setProperty("checked", "true" if self._checked else "false")
        _repolish(self)
        self.retheme()

    def setEnabled(self, enabled):  # noqa: N802 - Qt virtual name
        super().setEnabled(enabled)
        self.setProperty("disabled", "false" if enabled else "true")
        _repolish(self)

    def click(self):
        if self.isEnabled():
            self.clicked.emit()

    def retheme(self):
        colour = tokens()["radio_on"] if self._checked else tokens()["muted"]
        self._radio.setPixmap(svg_pixmap(icon_files.radio_svg(self._checked, colour), 16))

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual name
        super().mouseReleaseEvent(event)
        if self.rect().contains(event_point(event)):
            self.click()

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual name
        key = event.key()
        if key in (enum_member(Qt, "Key", "Key_Space"), enum_member(Qt, "Key", "Key_Return"),
                   enum_member(Qt, "Key", "Key_Enter")):
            self.click()
            return
        group = self.parentWidget()
        step = 0
        if key in (enum_member(Qt, "Key", "Key_Down"), enum_member(Qt, "Key", "Key_Right")):
            step = 1
        elif key in (enum_member(Qt, "Key", "Key_Up"), enum_member(Qt, "Key", "Key_Left")):
            step = -1
        if step and isinstance(group, RadioCardGroup):
            group.step(step)
            return
        super().keyPressEvent(event)


def hold_height_for_width(widget):
    """Keep a column of wrapping text at the height its width needs.

    Measured in QGIS 4.0.2: a vertical box that holds a widget whose cards wrap
    gave it its plain sizeHint, not its height-for-width, so with a larger UI
    font the last lines of each description were painted past the card's own
    bottom edge. The widget states its own floor from its layout instead.
    """
    layout = widget.layout()
    if layout is None or not layout.hasHeightForWidth():
        return
    needed = layout.totalHeightForWidth(max(1, widget.width()))
    if needed > 0 and widget.minimumHeight() != needed:
        widget.setMinimumHeight(needed)


class RadioCardGroup(QWidget):
    """Radio cards that present the QComboBox surface the plugin already reads.

    `plugin.py` reads the workflow chooser through `currentData`,
    `currentIndex`, `count`, `itemData`, `itemText`, `findData`,
    `setCurrentIndex` and `currentIndexChanged`. Keeping that surface makes the
    cards a presentation change and nothing downstream of it had to move.
    """

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._data = []
        self._current = -1
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self.setSizePolicy(_policy("Preferred", "Minimum"))

    def resizeEvent(self, event):  # noqa: N802 - Qt virtual name
        super().resizeEvent(event)
        hold_height_for_width(self)

    def addItem(self, title, data=None, description="", badge=""):  # noqa: N802
        card = RadioCard(title, description, badge, self)
        index = len(self._cards)
        card.clicked.connect(lambda target=index: self.setCurrentIndex(target))
        self._cards.append(card)
        self._data.append(data)
        self._layout.addWidget(card)
        if self._current < 0:
            self.setCurrentIndex(0)
        return card

    def cards(self):
        return list(self._cards)

    def count(self):
        return len(self._cards)

    def currentIndex(self):  # noqa: N802
        return self._current

    def currentData(self):  # noqa: N802
        return self._data[self._current] if 0 <= self._current < len(self._data) else None

    def itemData(self, index):  # noqa: N802
        return self._data[index] if 0 <= index < len(self._data) else None

    def itemText(self, index):  # noqa: N802
        return self._cards[index].text() if 0 <= index < len(self._cards) else ""

    def findData(self, data):  # noqa: N802
        for index, value in enumerate(self._data):
            if value == data:
                return index
        return -1

    def setCurrentIndex(self, index):  # noqa: N802
        if not 0 <= index < len(self._cards):
            return
        # A repeat press must not re-emit: a listener may clear state on change.
        if index == self._current:
            self._cards[index].setChecked(True)
            return
        self._current = index
        for position, card in enumerate(self._cards):
            card.setChecked(position == index)
        if not self.signalsBlocked():
            self.currentIndexChanged.emit(index)

    def step(self, delta):
        if not self._cards:
            return
        target = (self._current + delta) % len(self._cards)
        self.setCurrentIndex(target)
        self._cards[target].setFocus()


class PromptCard(QFrame):
    """A starting point: an icon and a sentence, the whole card pressable."""

    clicked = pyqtSignal()

    def __init__(self, text, icon_name="message-circle", hint="", parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexPromptCard")
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setFocusPolicy(enum_member(Qt, "FocusPolicy", "StrongFocus"))
        self.setSizePolicy(_policy("Preferred", "Minimum"))
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(10)
        self.glyph = IconLabel(icon_name or "message-circle", "slate", 16)
        row.addWidget(self.glyph, 0, enum_member(Qt, "AlignmentFlag", "AlignTop"))
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self.text_label = label(text, "mapdexInk")
        column.addWidget(self.text_label)
        self.hint = label(hint, "mapdexMeta")
        self.hint.setVisible(bool(hint))
        column.addWidget(self.hint)
        row.addLayout(column, 1)
        self.setAccessibleName(text)
        if hint:
            self.setToolTip(hint)

    def text(self):
        return self.text_label.text()

    def mouseReleaseEvent(self, event):  # noqa: N802
        super().mouseReleaseEvent(event)
        if self.isEnabled() and self.rect().contains(event_point(event)):
            self.clicked.emit()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (enum_member(Qt, "Key", "Key_Space"), enum_member(Qt, "Key", "Key_Return"),
                           enum_member(Qt, "Key", "Key_Enter")):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class TabBar(QWidget):
    """Nivo · Task · Jobs, with an amber count on a tab that needs the user."""

    selected = pyqtSignal(int)

    def __init__(self, titles, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexTabBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(24)
        self.buttons = []
        self.badges = []
        for index, title in enumerate(titles):
            holder = QHBoxLayout()
            holder.setContentsMargins(0, 0, 0, 0)
            holder.setSpacing(6)
            button = QToolButton()
            button.setObjectName("mapdexTab")
            button.setText(title)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
            button.clicked.connect(lambda _checked=False, target=index: self.selected.emit(target))
            badge = QLabel("")
            badge.setObjectName("mapdexTabBadge")
            badge.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignCenter"))
            badge.setFixedHeight(18)
            badge.setMinimumWidth(18)
            badge.setVisible(False)
            holder.addWidget(button)
            holder.addWidget(badge)
            row.addLayout(holder)
            self.buttons.append(button)
            self.badges.append(badge)
        row.addStretch(1)

    def set_current(self, index):
        for position, button in enumerate(self.buttons):
            button.setChecked(position == index)

    def set_badge(self, index, count):
        if not 0 <= index < len(self.badges):
            return
        badge = self.badges[index]
        badge.setText(str(count) if count else "")
        badge.setVisible(bool(count))
        badge.setAccessibleName("{} need you".format(count) if count else "")


class Disclosure(QWidget):
    """A muted link with a chevron that opens a body under it."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.header = LinkButton(title, "muted", "chevron-right")
        self.header.setCheckable(True)
        layout.addWidget(self.header, 0, enum_member(Qt, "AlignmentFlag", "AlignLeft"))
        self.body = QWidget()
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self.header.toggled.connect(self._toggled)

    def _toggled(self, opened):
        self.body.setVisible(bool(opened))
        self.header.set_icon_name("chevron-down" if opened else "chevron-right")

    def set_title(self, title):
        self.header.setText(title)


def hline():
    line = QFrame()
    line.setObjectName("mapdexDivider")
    line.setFixedHeight(1)
    return line


def vline(height=16):
    line = QFrame()
    line.setObjectName("mapdexDivider")
    line.setFixedSize(1, height)
    return line


class Band(QFrame):
    """A full-width strip whose content is capped and centred.

    The hairline spans the dock; the content does not. On a 1,540 px dock a
    header pinned to the left edge and a body centred under it put the two out
    of line, which is exactly the misalignment this redesign is for, so every
    band and every page centres the same capped column.
    """

    def __init__(self, kind="mapdexBand", max_width=720, margins=(16, 12, 16, 12), parent=None):
        super().__init__(parent)
        self.setObjectName(kind)
        self.setSizePolicy(_policy("Preferred", "Fixed"))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.inner = QWidget()
        self.inner.setObjectName("mapdexColumn")
        self.inner.setMaximumWidth(max_width)
        self.inner.setMinimumWidth(1)
        self.inner.setSizePolicy(_policy("Expanding", "Preferred"))
        outer.addStretch(1)
        outer.addWidget(self.inner, 100)
        outer.addStretch(1)
        self.layout_ = QVBoxLayout(self.inner)
        self.layout_.setContentsMargins(*margins)
        self.layout_.setSpacing(8)


def scroll_column(max_width=720, margins=(16, 20, 16, 20), spacing=20, name="mapdexScroll"):
    """A vertical scroll area holding one capped, centred column.

    Returns (scroll_area, column_layout, column_widget).
    """
    area = QScrollArea()
    area.setObjectName(name)
    area.setWidgetResizable(True)
    area.setFrameShape(enum_member(QFrame, "Shape", "NoFrame"))
    area.setHorizontalScrollBarPolicy(enum_member(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
    area.setMinimumWidth(1)
    body = QWidget()
    body.setObjectName("mapdexScrollBody")
    body.setMinimumWidth(1)
    row = QHBoxLayout(body)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    column = QWidget()
    column.setObjectName("mapdexColumn")
    column.setMaximumWidth(max_width)
    column.setMinimumWidth(1)
    column.setSizePolicy(_policy("Expanding", "Preferred"))
    row.addStretch(1)
    row.addWidget(column, 100)
    row.addStretch(1)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    area.setWidget(body)
    balance_scrollbar(area, row)
    return area, layout, column


def balance_scrollbar(area, row, column=None):
    """Keep a capped scrolling column on the same centre line as the bands.

    A vertical scrollbar takes its width out of the viewport, so a CAPPED
    column centred in the viewport sits half a scrollbar left of the header and
    the footer, which are centred in the full width. Measured at 1540 px: a
    5 px step between the page and its footer. The row gives that back, but
    only while the column is capped: at dock width the column already starts
    at the same left edge as the bands, and a margin there would push it in.
    """
    bar = area.verticalScrollBar()
    if column is None and row.count():
        widgets = [row.itemAt(i).widget() for i in range(row.count())]
        column = next((w for w in widgets if w is not None), None)

    def _update(*_args):
        width = bar.sizeHint().width() if bar.maximum() > bar.minimum() else 0
        capped = column is not None and area.width() - width >= column.maximumWidth() + width
        shift = width if capped else 0
        if row.contentsMargins().left() != shift:
            row.setContentsMargins(shift, 0, 0, 0)

    class _Watcher(QObject):
        def eventFilter(self, _watched, event):  # noqa: N802 - Qt virtual name
            if event.type() == enum_member(QEvent, "Type", "Resize"):
                _update()
            return False

    watcher = _Watcher(area)
    area.installEventFilter(watcher)
    area._mapdex_scroll_balance = watcher
    bar.rangeChanged.connect(_update)
    _update()
