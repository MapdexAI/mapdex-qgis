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

import html
import os

from qgis.PyQt.QtCore import QByteArray, QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPixmap
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
from .panel_state import plain_tooltip
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


FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
FONT_FILES = ("Geist-Regular.ttf", "Geist-Medium.ttf", "Geist-SemiBold.ttf")
_FONT = {"family": None}


def panel_font_family():
    """Geist, registered once for THIS panel, or "" to keep the system font.

    Registered with QFontDatabase.addApplicationFont and named only in the
    panel's own stylesheet: QApplication.setFont is never called, so QGIS's
    own interface keeps its font. A file that fails to load falls back to the
    system font rather than to a broken glyph set.
    """
    if _FONT["family"] is None:
        family = ""
        try:
            from qgis.PyQt.QtGui import QFontDatabase  # noqa: PLC0415

            for name in FONT_FILES:
                font_id = QFontDatabase.addApplicationFont(os.path.join(FONT_DIR, name))
                if font_id < 0:
                    continue
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families and not family:
                    family = str(families[0])
        except Exception:  # noqa: BLE001 - the system font is the fallback
            family = ""
        _FONT["family"] = family
    return _FONT["family"]


def apply_theme(root, value=None):
    """Restyle `root` and re-colour every icon under it.

    Returns True when the token set changed. Reading the APPLICATION palette,
    never the root's own: the root's palette is what our stylesheet painted,
    so asking it which theme QGIS is wearing would always answer "ours".
    """
    value = value or tokens_for_interface()
    changed = value is not tokens() or not root.property("mapdexThemed")
    set_tokens(value)
    root.setStyleSheet(theme.stylesheet(value, panel_font_family()))
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
        self.setToolTip(plain_tooltip(self._full))

    def setText(self, text):  # noqa: N802 - Qt virtual name
        self._full = text or ""
        self.setToolTip(plain_tooltip(self._full))
        super().setText(self._full)

    def text(self):  # noqa: D401 - the full text, not the elided paint
        return self._full

    def paintEvent(self, event):  # noqa: N802 - Qt virtual name
        elided = self.fontMetrics().elidedText(
            self._full, enum_member(Qt, "TextElideMode", self._mode),
            max(1, self.contentsRect().width()))
        if elided != super().text():
            super().setText(elided)
        super().paintEvent(event)


def _align_flag(*names):
    flag = enum_member(Qt, "AlignmentFlag", names[0])
    for name in names[1:]:
        flag = flag | enum_member(Qt, "AlignmentFlag", name)
    return flag


def line_height(label):
    """The height of one line of `label`'s text, in its polished font."""
    label.ensurePolished()
    return QFontMetrics(label.font()).height()


class _LineSync(QObject):
    """Keeps an icon on the centre of its labels' first text line.

    The icon's box is as tall as the tallest first line (or the icon, if that
    is taller); each label gets a top and bottom margin that puts its first
    line on the same centre. Re-run whenever a font arrives from the
    stylesheet, because that is when the line height becomes true.
    """

    _EVENTS = ("Polish", "StyleChange", "FontChange", "Show", "ShowToParent")

    def __init__(self, icon, labels, size):
        super().__init__(icon)
        self._icon = icon
        self._labels = list(labels)
        self._size = size
        self._busy = False
        self._watched = tuple(enum_member(QEvent, "Type", name) for name in self._EVENTS)
        icon.installEventFilter(self)
        for label_widget in self._labels:
            label_widget.installEventFilter(self)

    def eventFilter(self, _watched, event):  # noqa: N802 - Qt virtual name
        if event.type() in self._watched:
            self.apply()
        return False

    def apply(self):
        if self._busy:
            return
        self._busy = True
        try:
            heights = [line_height(label_widget) for label_widget in self._labels]
            box = max([self._size] + heights)
            if self._icon.height() != box or self._icon.minimumHeight() != box:
                self._icon.setFixedHeight(box)
            for label_widget, height in zip(self._labels, heights):
                top = (box - height) // 2
                bottom = box - height - top
                margins = label_widget.contentsMargins()
                if margins.top() != top or margins.bottom() != bottom:
                    label_widget.setContentsMargins(margins.left(), top, margins.right(), bottom)
                horizontal = label_widget.alignment() & enum_member(Qt, "AlignmentFlag", "AlignHorizontal_Mask")
                wanted = horizontal | enum_member(Qt, "AlignmentFlag", "AlignTop")
                if label_widget.alignment() != wanted:
                    label_widget.setAlignment(wanted)
        except RuntimeError:  # a widget already deleted
            pass
        finally:
            self._busy = False


def register_pair(icon, *labels):
    """Record that `icon` belongs to `labels` so `icon_alignment` can measure it."""
    icon._mapdex_line_partners = [label_widget for label_widget in labels if label_widget is not None]


def align_icon(icon, *labels, size=None):
    """Centre `icon` on the first text line of `labels`, and keep it there.

    Put the icon and the labels in their row with AlignTop (or AlignVCenter
    for one-line rows): both then share the same box.
    """
    labels = [label_widget for label_widget in labels if label_widget is not None]
    register_pair(icon, *labels)
    icon.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignCenter"))
    sync = _LineSync(icon, labels, int(size or icon.width() or 16))
    icon._mapdex_line_sync = sync
    sync.apply()
    return sync


def _text_centre(label_widget, root):
    """Where the centre of `label_widget`'s first line sits, in root coordinates."""
    rect = label_widget.contentsRect()
    top = label_widget.mapTo(root, QPoint(0, rect.top())).y()
    height = line_height(label_widget)
    alignment = label_widget.alignment()
    if alignment & enum_member(Qt, "AlignmentFlag", "AlignVCenter"):
        lines = max(1, round(label_widget.heightForWidth(rect.width()) / max(1, height))
                    if label_widget.wordWrap() else 1)
        block = height * lines
        return top + (rect.height() - block) / 2.0 + height / 2.0
    if alignment & enum_member(Qt, "AlignmentFlag", "AlignBottom"):
        return top + rect.height() - height / 2.0
    return top + height / 2.0


def icon_alignment(root):
    """[(icon, label, offset_px)] for every visible icon paired with text.

    `offset_px` is the icon's vertical centre minus the label's first-line
    centre; the redesign holds it within one pixel.
    """
    rows = []
    for icon in root.findChildren(QLabel):
        partners = getattr(icon, "_mapdex_line_partners", None)
        if not partners or not icon.isVisibleTo(root) or icon.height() <= 0:
            continue
        icon_rect = icon.contentsRect()
        centre = icon.mapTo(root, QPoint(0, icon_rect.top())).y() + icon_rect.height() / 2.0
        for label_widget in partners:
            try:
                if not label_widget.isVisibleTo(root) or not label_widget.text():
                    continue
                rows.append((icon, label_widget, centre - _text_centre(label_widget, root)))
            except RuntimeError:
                continue
    return rows


class IconLabel(QLabel):
    """A Lucide glyph as a label, recoloured with the theme.

    Fixed width; its height follows the text line it is paired with
    (`align_icon`), and the glyph is drawn in the vertical centre of that box.
    """

    def __init__(self, name, colour_key="slate", size=16, stroke=1.75, parent=None):
        super().__init__(parent)
        self._spec = (name, colour_key, size, stroke)
        self.setFixedSize(size, size)
        self.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignCenter"))
        self.setObjectName("mapdexIcon")
        self.retheme()

    def set_icon(self, name, colour_key=None):
        _old, key, size, stroke = self._spec
        self._spec = (name, colour_key or key, size, stroke)
        self.retheme()

    def retheme(self):
        name, key, size, stroke = self._spec
        self.setPixmap(svg_pixmap(icon_files.lucide_svg(name, tokens()[key], stroke), size))


class StatusIcon(QLabel):
    """A solid status icon; the working ring turns while it is on screen.

    The timer runs only while the icon is shown AND working, so a hidden page or
    a finished job costs nothing.
    """

    FRAME_MS = 80
    STEP_DEGREES = 30

    def __init__(self, tone="neutral", size=18, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexStatusIcon")
        self._tone = tone
        self._size = size
        self._angle = 0
        self._base = None
        self.setFixedSize(size, size)
        self.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignCenter"))
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self.retheme()

    def set_tone(self, tone):
        self._tone = tone if tone in theme.TONES else "neutral"
        self.retheme()

    def tone(self):
        return self._tone

    def spinning(self):
        return self._timer.isActive()

    def angle(self):
        return self._angle

    def retheme(self):
        self._base = status_pixmap(self._tone, self._size)
        self._draw()
        self._update_timer()

    def _draw(self):
        if self._tone != "working" or self._base is None or not self._angle:
            self.setPixmap(self._base)
            return
        frame = QPixmap(self._base.size())
        frame.fill(QColor(0, 0, 0, 0))
        frame.setDevicePixelRatio(self._base.devicePixelRatio())
        painter = QPainter(frame)
        painter.setRenderHint(enum_member(QPainter, "RenderHint", "SmoothPixmapTransform"), True)
        half = self._size / 2.0
        painter.translate(half, half)
        painter.rotate(self._angle)
        painter.translate(-half, -half)
        painter.drawPixmap(0, 0, self._base)
        painter.end()
        self.setPixmap(frame)

    def _tick(self):
        self._angle = (self._angle + self.STEP_DEGREES) % 360
        self._draw()

    def _update_timer(self):
        wanted = self._tone == "working" and self.isVisible()
        if wanted and not self._timer.isActive():
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event):  # noqa: N802 - Qt virtual name
        super().showEvent(event)
        self._update_timer()

    def hideEvent(self, event):  # noqa: N802 - Qt virtual name
        super().hideEvent(event)
        self._update_timer()


class StatusLine(QWidget):
    """A solid status icon, a bold coloured word, a muted detail, and a clock.

    Never a tinted pill and never colour alone: the icon's SHAPE and the word
    carry the state, the colour is the third carrier. The icon sits on the
    centre of the word's line, and a working ring turns.
    """

    def __init__(self, tone="neutral", word="", detail="", parent=None, icon_size=18):
        super().__init__(parent)
        self.setObjectName("mapdexStatusLine")
        self._tone = tone
        self._icon_size = icon_size
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.icon = StatusIcon(tone, icon_size)
        self.word = QLabel()
        self.word.setObjectName("mapdexStatusWord")
        self.word.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self.detail = label("", "mapdexStatusDetail")
        self.clock = QLabel()
        self.clock.setObjectName("mapdexClock")
        self.clock.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self.clock.setVisible(False)
        top = _align_flag("AlignTop")
        row.addWidget(self.icon, 0, top)
        row.addWidget(self.word, 0, top)
        row.addWidget(self.detail, 100, top)
        row.addStretch(1)
        row.addWidget(self.clock, 0, top)
        align_icon(self.icon, self.word, self.detail, self.clock, size=icon_size)
        self.set_status(tone, word, detail)

    def set_status(self, tone, word, detail=""):
        self._tone = tone if tone in theme.TONES else "neutral"
        self.word.setText(word or "")
        self.word.setVisible(bool(word))
        self.word.setProperty("tone", self._tone)
        _repolish(self.word)
        self.detail.setText(detail or "")
        self.detail.setVisible(bool(detail))
        self.icon.set_tone(self._tone)

    def set_clock(self, text):
        """A live elapsed time at the right of the line, or "" for none."""
        self.clock.setText(text or "")
        self.clock.setVisible(bool(text))

    def tone(self):
        return self._tone

    def setText(self, text):  # noqa: N802 - the QLabel surface callers already use
        self.set_status(self._tone, self.word.text(), text)

    def text(self):
        return " ".join(part for part in (self.word.text(), self.detail.text()) if part)

    def retheme(self):
        self.icon.retheme()


class IconButton(QToolButton):
    """An icon-only control with a tooltip and an accessible name."""

    def __init__(self, icon_name, tooltip, colour_key="slate", size=32, icon_size=18,
                 object_name="mapdexIconButton", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self._spec = (icon_name, colour_key, icon_size)
        # Some of these name a file or a layer ("Remove <file> from this task").
        self.setToolTip(plain_tooltip(tooltip))
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
        middle = _align_flag("AlignVCenter")
        for child in (self._glyph, self._label, self._price):
            if child is not None:
                child.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
                row.addWidget(child, 1 if child is self._label else 0, middle)
        if self._glyph is not None:
            align_icon(self._glyph, self._label, size=18)
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
        self.setToolTip(plain_tooltip(self.text()) if len(self.text()) > 40 else "")

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


class _ContentButton(QPushButton):
    """A button drawn as a row of widgets: an optional icon, then its text.

    QPushButton paints its icon and its text through the style, which under a
    stylesheet does not keep them on one centre line. Here both are widgets in
    one row and the icon is centred on the text's line (`align_icon`), which a
    check measures. `text()`/`setText()` keep the QPushButton surface.
    """

    TEXT_NAME = "mapdexButtonText"
    ICON_SIZE = 16
    SPACING = 6
    MARGINS = (0, 0, 0, 0)

    def __init__(self, text="", icon_name="", icon_key="slate", parent=None):
        super().__init__(parent)
        self._text = str(text or "")
        self._icon_name = icon_name
        self._icon_key = icon_key
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        row = QHBoxLayout(self)
        row.setContentsMargins(*self.MARGINS)
        row.setSpacing(self.SPACING)
        self._row = row
        self._glyph = IconLabel(icon_name or "x", icon_key, self.ICON_SIZE, 2)
        self._glyph.setVisible(bool(icon_name))
        self._label = QLabel(self._text)
        self._label.setObjectName(self.TEXT_NAME)
        self._label.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        middle = _align_flag("AlignVCenter")
        for child in (self._glyph, self._label):
            child.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
        self._lead = QHBoxLayout()
        self._lead.setContentsMargins(0, 0, 0, 0)
        self._lead.setSpacing(self.SPACING)
        self._lead.addWidget(self._glyph, 0, middle)
        self._lead.addWidget(self._label, 0, middle)
        self._arrange(row)
        align_icon(self._glyph, self._label, size=self.ICON_SIZE)
        self.setAccessibleName(self._text)

    def _arrange(self, row):
        row.addLayout(self._lead)
        row.addStretch(1)

    # -- the QPushButton text surface ---------------------------------------------
    def setText(self, text):  # noqa: N802 - mirrors QAbstractButton
        self._text = str(text or "")
        self._label.setText(self._text)
        self.setAccessibleName(self._text)
        self.updateGeometry()

    def text(self):  # noqa: D401 - the visible text
        return self._text

    def set_icon_name(self, name):
        self._icon_name = name
        if name:
            self._glyph.set_icon(name)
        self._glyph.setVisible(bool(name))
        self.updateGeometry()

    def retheme(self):
        key = self._icon_key if self.isEnabled() else "faint"
        if self._icon_name:
            self._glyph.set_icon(self._icon_name, key)

    def sizeHint(self):  # noqa: N802 - Qt virtual name
        hint = self.layout().sizeHint()
        return QSize(hint.width(), max(hint.height(), self.minimumHeight()))

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual name
        return self.sizeHint()

    def _set_label_state(self, name, value):
        self._label.setProperty(name, value)
        _repolish(self._label)

    def changeEvent(self, event):  # noqa: N802 - Qt virtual name
        super().changeEvent(event)
        if event.type() == enum_member(QEvent, "Type", "EnabledChange"):
            self._set_label_state("off", "false" if self.isEnabled() else "true")
            self.retheme()

    def enterEvent(self, event):  # noqa: N802 - Qt virtual name
        super().enterEvent(event)
        self._set_label_state("hover", "true")

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual name
        super().leaveEvent(event)
        self._set_label_state("hover", "false")


class SecondaryButton(_ContentButton):
    """A raised surface with a border: its icon and text centred together."""

    MARGINS = (14, 0, 14, 0)
    SPACING = 8

    def __init__(self, text="", icon_name="", parent=None):
        super().__init__(text, icon_name, "slate", parent)
        self.setObjectName("mapdexSecondaryButton")
        self.setMinimumHeight(40)

    def _arrange(self, row):
        row.addStretch(1)
        row.addLayout(self._lead)
        row.addStretch(1)

    def retheme(self):
        key = "ink" if self.isEnabled() else "faint"
        if self._icon_name:
            self._glyph.set_icon(self._icon_name, key)


def secondary_button(text, icon_name=""):
    """The secondary control. Plain text keeps the native button; with an icon
    it is a SecondaryButton, so the icon sits on the text's line."""
    if icon_name:
        return SecondaryButton(text, icon_name)
    button = QPushButton(text)
    button.setObjectName("mapdexSecondaryButton")
    button.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
    return button


class LinkButton(_ContentButton):
    """A quiet action, as a text link. `kind` is link, muted or danger."""

    TEXT_NAME = "mapdexLinkText"
    ICON_SIZE = 14
    _NAMES = {"link": "mapdexLink", "muted": "mapdexMutedLink", "danger": "mapdexDangerLink"}
    _COLOURS = {"link": "link", "muted": "muted", "danger": "err_text"}

    def __init__(self, text, kind="link", icon_name="", parent=None):
        self._kind = kind if kind in self._NAMES else "link"
        super().__init__(text, icon_name, self._COLOURS[self._kind], parent)
        self.setObjectName(self._NAMES[self._kind])
        self.setFlat(True)
        self.setSizePolicy(_policy("Maximum", "Fixed"))
        self._label.setProperty("kind", self._kind)
        _repolish(self._label)

    def kind(self):
        return self._kind


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
        column.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignTop"))
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self.text_label = label(text, "mapdexInk")
        column.addWidget(self.text_label)
        self.hint = label(hint, "mapdexMeta")
        self.hint.setVisible(bool(hint))
        column.addWidget(self.hint)
        row.addLayout(column, 1)
        align_icon(self.glyph, self.text_label, size=16)
        self.setAccessibleName(text)
        if hint:
            self.setToolTip(plain_tooltip(hint))

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


class TabButton(QPushButton):
    """One tab: its word, and a 2 px ink underline exactly as wide as the word.

    A QToolButton adds a style margin each side of its text, which put the tab
    words 4 px off the panel's left edge and the underline wider than the word.
    """

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexTab")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(enum_member(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(_policy("Fixed", "Fixed"))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 12, 0, 10)
        row.setSpacing(0)
        self._label = QLabel(str(title))
        self._label.setObjectName("mapdexTabText")
        self._label.setTextFormat(enum_member(Qt, "TextFormat", "PlainText"))
        self._label.setAttribute(enum_member(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"), True)
        row.addWidget(self._label)
        self.setAccessibleName(str(title))
        self.toggled.connect(self._sync)
        self._sync(False)

    def text(self):  # noqa: D401 - the tab's word
        return self._label.text()

    def setText(self, text):  # noqa: N802 - mirrors QAbstractButton
        self._label.setText(str(text or ""))
        self.setAccessibleName(str(text or ""))
        self.updateGeometry()

    def _sync(self, checked):
        self._label.setProperty("on", "true" if checked else "false")
        _repolish(self._label)

    def sizeHint(self):  # noqa: N802 - Qt virtual name
        hint = self.layout().sizeHint()
        return QSize(hint.width(), hint.height() + 2)

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual name
        return self.sizeHint()

    def enterEvent(self, event):  # noqa: N802 - Qt virtual name
        super().enterEvent(event)
        self._label.setProperty("hover", "true")
        _repolish(self._label)

    def leaveEvent(self, event):  # noqa: N802 - Qt virtual name
        super().leaveEvent(event)
        self._label.setProperty("hover", "false")
        _repolish(self._label)


class TabBar(QWidget):
    """Nivo · Task · Jobs.

    A tab can carry ONE marker: the amber count of what needs the user, or,
    while work runs and nothing waits, a turning ring and the running count.
    Neither when nothing runs or waits.
    """

    selected = pyqtSignal(int)

    def __init__(self, titles, parent=None):
        super().__init__(parent)
        self.setObjectName("mapdexTabBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(24)
        self.buttons = []
        self.badges = []
        self.activities = []
        self.activity_counts = []
        middle = _align_flag("AlignVCenter")
        for index, title in enumerate(titles):
            holder = QHBoxLayout()
            holder.setContentsMargins(0, 0, 0, 0)
            holder.setSpacing(6)
            button = TabButton(title)
            button.clicked.connect(lambda _checked=False, target=index: self.selected.emit(target))
            badge = QLabel("")
            badge.setObjectName("mapdexTabBadge")
            badge.setAlignment(enum_member(Qt, "AlignmentFlag", "AlignCenter"))
            badge.setFixedHeight(18)
            badge.setMinimumWidth(18)
            badge.setVisible(False)
            activity = StatusIcon("working", 14)
            activity.setVisible(False)
            count = QLabel("")
            count.setObjectName("mapdexTabCount")
            count.setVisible(False)
            holder.addWidget(button, 0, middle)
            holder.addWidget(badge, 0, middle)
            holder.addWidget(activity, 0, middle)
            holder.addWidget(count, 0, middle)
            row.addLayout(holder)
            self.buttons.append(button)
            self.badges.append(badge)
            self.activities.append(activity)
            self.activity_counts.append(count)
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
        if count:
            self._show_activity(index, 0)

    def set_activity(self, index, running):
        """A turning ring and the running count, unless the amber count shows."""
        if not 0 <= index < len(self.activities):
            return
        if self.badges[index].isVisibleTo(self):
            running = 0
        self._show_activity(index, running)

    def _show_activity(self, index, running):
        self.activities[index].setVisible(bool(running))
        self.activity_counts[index].setText(str(running) if running else "")
        self.activity_counts[index].setVisible(bool(running))
        self.activities[index].setAccessibleName("{} running".format(running) if running else "")


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


class ProseLabel(QLabel):
    """Text at a 1.5 line height that can be selected and copied.

    People copy measured numbers out of answers, so the text stays a
    selectable QLabel. Its line height comes from rich text, which is the only
    way a QLabel takes one on Qt 5 and Qt 6; the text is escaped first, so
    model and user text is still shown as text and never read as markup.
    `text()` returns the string exactly as given.
    """

    LINE_HEIGHT = 1.5

    def __init__(self, text="", name="mapdexTurnBody", parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setTextFormat(enum_member(Qt, "TextFormat", "RichText"))
        self.setWordWrap(True)
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(
            enum_member(Qt, "TextInteractionFlag", "TextSelectableByMouse")
            | enum_member(Qt, "TextInteractionFlag", "TextSelectableByKeyboard")
        )
        self.setMinimumWidth(1)
        self.setSizePolicy(_policy("Expanding", "Preferred"))
        self._plain = ""
        self.setText(text)

    @classmethod
    def markup(cls, text):
        """The escaped text as one paragraph per line, at the reading height."""
        style = "margin:0; line-height:{}%;".format(int(round(cls.LINE_HEIGHT * 100)))
        paragraphs = [html.escape(part, quote=False) or "&nbsp;"
                      for part in str(text or "").split("\n")]
        return "".join('<p style="{}">{}</p>'.format(style, part) for part in paragraphs)

    def setText(self, text):  # noqa: N802 - Qt virtual name
        self._plain = str(text or "")
        super().setText(self.markup(self._plain))
        self.updateGeometry()

    def text(self):  # noqa: D401 - the text as given, not the markup
        return self._plain


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
