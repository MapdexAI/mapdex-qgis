"""The panel's colours, sizes and stylesheet, in one place.

The panel used to paint its own #191919 ground whatever theme QGIS wore, with
an indigo accent the web app had already dropped, and roughly a hundred colour
literals spread through one stylesheet and a dozen inline `setStyleSheet`
calls. Two of those disagreed about the same grey, and none could follow a
light QGIS theme.

This module owns every colour and size the panel draws with, for both a dark
and a light interface, and builds the one stylesheet from them. The values are
the web app's (`docs/DESIGN.md` 4.1): a neutral grey dark mode that is never
near-black or purple, exactly one filled blue action per screen, and status as
a solid icon plus a coloured word, never a tinted pill.

No Qt import, so which colour goes where is decided somewhere a test can reach.
"""
from __future__ import annotations

from typing import Mapping

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

DARK = {
    "name": "dark",
    "app": "#17181A",
    "surface": "#202124",
    "raised": "#2A2B2E",
    "field": "#17181A",
    "border": "#3C4043",
    "border_strong": "#5F6368",
    "ink": "#E8EAED",
    "slate": "#BDC1C6",
    "muted": "#9AA0A6",
    "faint": "#5F6368",
    "link": "#8AB4F8",
    "link_hover": "#AECBFA",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "primary_text": "#FFFFFF",
    "primary_disabled": "#2A2B2E",
    "primary_disabled_text": "#5F6368",
    "selected_bg": "#1E2A3D",
    "selected_border": "#3B5A8A",
    "radio_on": "#8AB4F8",
    "ok": "#22C55E",
    "ok_text": "#22C55E",
    "warn": "#F59E0B",
    "warn_text": "#F59E0B",
    "err": "#F87171",
    "err_text": "#F87171",
    "working": "#60A5FA",
    "working_text": "#8AB4F8",
    "badge_bg": "#F59E0B",
    "badge_text": "#202124",
    "hover": "#2A2B2E",
    "bubble": "#2A2B2E",
}

LIGHT = {
    "name": "light",
    "app": "#F4F4F5",
    "surface": "#FFFFFF",
    "raised": "#FFFFFF",
    "field": "#FFFFFF",
    "border": "#E4E4E7",
    "border_strong": "#D4D4D8",
    "ink": "#18181B",
    "slate": "#52525B",
    "muted": "#71717A",
    "faint": "#A1A1AA",
    "link": "#1D4ED8",
    "link_hover": "#1E40AF",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "primary_text": "#FFFFFF",
    "primary_disabled": "#E4E4E7",
    "primary_disabled_text": "#A1A1AA",
    "selected_bg": "#EFF6FF",
    "selected_border": "#93C5FD",
    "radio_on": "#2563EB",
    "ok": "#16A34A",
    "ok_text": "#15803D",
    "warn": "#D97706",
    "warn_text": "#B45309",
    "err": "#B91C1C",
    "err_text": "#B91C1C",
    "working": "#2563EB",
    "working_text": "#1D4ED8",
    "badge_bg": "#D97706",
    "badge_text": "#FFFFFF",
    "hover": "#F4F4F5",
    # A user turn sits on the panel surface, which in light is white; the
    # bubble needs a fill the eye can find without a border.
    "bubble": "#F4F4F5",
}

# One radius, as on the web (DESIGN.md 4.6). A second one is how a panel comes
# to look assembled from two kits.
RADIUS = 6

# The type scale from the approved boards. The FAMILY is deliberately absent:
# the panel uses the QGIS/system UI font so it reads as part of the host.
TITLE_PX = 22
SUBTITLE_PX = 18
BODY_PX = 14
LABEL_PX = 13
META_PX = 12

# Status tones. Every one is an icon AND a word; the colour is the third
# carrier, never the only one (DESIGN.md 8).
TONES = ("ok", "warn", "err", "working", "neutral")


def theme_for_lightness(lightness: int) -> Mapping[str, str]:
    """The token set for a window background of this HSL lightness (0-255).

    Read from the value rather than from a theme NAME: QGIS ships Night
    Mapping and Blend of Gray, users install their own, and the system can
    darken the application with no QGIS setting changing at all. Below the
    midpoint the window is dark, whatever anyone called it.
    """
    try:
        value = int(lightness)
    except (TypeError, ValueError):
        return DARK
    return DARK if value < 128 else LIGHT


def tone_colour(tokens: Mapping[str, str], tone: str, for_text: bool = False) -> str:
    """The colour of a status icon (or of its word, with `for_text`)."""
    tone = str(tone or "neutral")
    if tone == "neutral":
        return tokens["muted"]
    key = "{}_text".format(tone) if for_text else tone
    return tokens.get(key) or tokens.get(tone) or tokens["muted"]


def stylesheet(tokens: Mapping[str, str]) -> str:
    """The panel's whole stylesheet, from one token set.

    Selectors are object names, so a widget is styled by what it IS rather than
    by where it happens to sit. Inline `setStyleSheet` on a container applies to
    every descendant, which is how one `border: 0` once erased the border of
    every chip inside a card.
    """
    t = dict(tokens)
    t["r"] = RADIUS
    t["title"] = TITLE_PX
    t["subtitle"] = SUBTITLE_PX
    t["body"] = BODY_PX
    t["label"] = LABEL_PX
    t["meta"] = META_PX
    return _SHEET.format(**t)


_SHEET = """
QWidget#mapdexPluginRoot {{ background: {surface}; color: {ink}; }}
QWidget#mapdexView, QWidget#mapdexPage, QWidget#mapdexScrollBody,
QWidget#mapdexColumn, QWidget#mapdexChatTranscriptContent {{ background: {surface}; }}
QScrollArea#mapdexScroll, QScrollArea#mapdexChatTranscript {{ background: {surface}; border: 0; }}
QScrollArea#mapdexScroll > QWidget > QWidget, QScrollArea#mapdexChatTranscript > QWidget > QWidget {{ background: {surface}; }}
QFrame#mapdexBand {{ background: {surface}; border: 0; border-bottom: 1px solid {border}; }}
QFrame#mapdexFooterBand {{ background: {surface}; border: 0; border-top: 1px solid {border}; }}
QFrame#mapdexPlainBand {{ background: {surface}; border: 0; }}
QFrame#mapdexDivider {{ background: {border}; border: 0; }}

QLabel {{ color: {ink}; background: transparent; }}
QLabel#mapdexWordmark {{ color: {ink}; font-size: 15px; font-weight: 600; }}
QLabel#mapdexWorkspace {{ color: {slate}; font-size: {label}px; font-weight: 500; }}
QLabel#mapdexViewTitle {{ color: {ink}; font-size: 15px; font-weight: 600; }}
QLabel#mapdexPageTitle {{ color: {ink}; font-size: {title}px; font-weight: 600; }}
QLabel#mapdexConnectTitle {{ color: {ink}; font-size: 24px; font-weight: 600; }}
QLabel#mapdexCardTitle, QLabel#mapdexSectionTitle {{
    color: {ink}; font-size: {subtitle}px; font-weight: 600;
}}
QLabel#mapdexBody, QLabel#mapdexBalanceNotice, QLabel#mapdexLockedNotice {{
    color: {slate}; font-size: {body}px;
}}
QLabel#mapdexConnectBody {{ color: {slate}; font-size: 15px; }}
QLabel#mapdexInk {{ color: {ink}; font-size: {body}px; }}
QLabel#mapdexAnswer, QLabel#mapdexTurnBody {{ color: {ink}; font-size: 15px; }}
QLabel#mapdexTurnBodyUser, QLabel#mapdexJobItemTitle, QLabel#mapdexSourceName {{
    color: {ink}; font-size: {body}px;
}}
QLabel#mapdexSectionLabel {{ color: {slate}; font-size: {label}px; font-weight: 600; }}
/* Labels renamed after creation keep the style they were made with: a
   rename that drops the font size reads as a different, larger voice. */
QLabel#mapdexMeta, QLabel#mapdexHistoryState, QLabel#mapdexLayerPickerState,
QLabel#mapdexOptionsChip, QLabel#mapdexPageFootnote, QLabel#mapdexPriceLine,
QLabel#mapdexSourceDetail, QLabel#mapdexSourceOverflow, QLabel#mapdexSourceSummary {{
    color: {muted}; font-size: {meta}px;
}}
QLabel#mapdexKeyState {{ color: {slate}; font-size: {meta}px; }}
QLabel#mapdexMetaCentred {{ color: {muted}; font-size: {meta}px; }}
QLabel#mapdexDetail {{ color: {muted}; font-size: {label}px; }}
QLabel#mapdexDtLabel {{ color: {muted}; font-size: {label}px; }}
QLabel#mapdexDdValue {{ color: {ink}; font-size: {label}px; }}
QLabel#mapdexClock {{ color: {ink}; font-size: {label}px; font-weight: 600; }}
QLabel#mapdexBadgeWord {{ color: {muted}; font-size: {meta}px; font-weight: 500; }}

QLabel#mapdexStatusWord {{ font-size: {label}px; font-weight: 600; color: {muted}; }}
QLabel#mapdexStatusWord[tone="ok"] {{ color: {ok_text}; }}
QLabel#mapdexStatusWord[tone="warn"] {{ color: {warn_text}; }}
QLabel#mapdexStatusWord[tone="err"] {{ color: {err_text}; }}
QLabel#mapdexStatusWord[tone="working"] {{ color: {working_text}; }}
QLabel#mapdexStatusDetail {{ color: {muted}; font-size: {label}px; }}
QLabel#mapdexNoticeText {{ color: {slate}; font-size: {label}px; }}

/* One filled blue button per screen. */
QPushButton#mapdexPrimaryButton {{
    min-height: 40px; padding: 0 16px; font-size: {body}px; font-weight: 600;
    color: {primary_text}; background: {primary}; border: 0; border-radius: {r}px;
}}
QPushButton#mapdexPrimaryButton:hover {{ background: {primary_hover}; }}
QPushButton#mapdexPrimaryButton:disabled {{ color: {primary_disabled_text}; background: {primary_disabled}; }}
QLabel#mapdexPrimaryLabel {{ color: {primary_text}; font-size: 15px; font-weight: 600; }}
QLabel#mapdexPrimaryPrice {{ color: {primary_text}; font-size: {label}px; font-weight: 500; }}
QLabel#mapdexPrimaryLabel[off="true"], QLabel#mapdexPrimaryPrice[off="true"] {{ color: {primary_disabled_text}; }}

/* Secondary: a raised surface with a border. */
QPushButton#mapdexSecondaryButton {{
    min-height: 40px; padding: 0 16px; font-size: {body}px; font-weight: 500;
    color: {ink}; background: {raised}; border: 1px solid {border_strong}; border-radius: {r}px;
}}
QPushButton#mapdexSecondaryButton:hover {{ border-color: {muted}; }}
QPushButton#mapdexSecondaryButton:disabled {{ color: {faint}; border-color: {border}; }}

/* Quiet actions are text links. */
QPushButton#mapdexLink, QPushButton#mapdexQuietButton, QPushButton#mapdexDangerLink,
QPushButton#mapdexMutedLink, QPushButton#mapdexJobItemAction, QPushButton#mapdexDropLink,
QPushButton#mapdexOptionsHeader {{
    background: transparent; border: 0; padding: 2px 0; text-align: left;
    font-size: {label}px; font-weight: 500; color: {link};
}}
QPushButton#mapdexLink:hover, QPushButton#mapdexQuietButton:hover,
QPushButton#mapdexJobItemAction:hover, QPushButton#mapdexDropLink:hover {{ color: {link_hover}; }}
QPushButton#mapdexMutedLink, QPushButton#mapdexOptionsHeader {{ color: {muted}; }}
QPushButton#mapdexMutedLink:hover, QPushButton#mapdexOptionsHeader:hover {{ color: {ink}; }}
QPushButton#mapdexDangerLink {{ color: {err_text}; font-size: {body}px; }}
QPushButton#mapdexLink:disabled, QPushButton#mapdexQuietButton:disabled,
QPushButton#mapdexDangerLink:disabled, QPushButton#mapdexMutedLink:disabled {{ color: {faint}; }}

QToolButton#mapdexIconButton {{
    background: transparent; border: 0; border-radius: {r}px; padding: 0;
}}
QToolButton#mapdexIconButton:hover {{ background: {hover}; }}
QToolButton#mapdexIconButton:disabled {{ background: transparent; }}
QToolButton#mapdexSendButton {{ background: {primary}; border: 0; border-radius: {r}px; }}
QToolButton#mapdexSendButton:hover {{ background: {primary_hover}; }}
QToolButton#mapdexSendButton:disabled {{ background: {raised}; border: 1px solid {border}; }}
QToolButton#mapdexStopButton {{
    color: {ink}; background: {raised}; border: 1px solid {border_strong};
    border-radius: {r}px; padding: 0 10px; font-size: {label}px; font-weight: 500;
}}

/* Tabs: ink and a 2 px ink underline when chosen, muted otherwise. */
QToolButton#mapdexTab {{
    color: {muted}; background: transparent; border: 0; border-bottom: 2px solid transparent;
    border-radius: 0; padding: 12px 0 10px 0; font-size: {body}px; font-weight: 500;
}}
QToolButton#mapdexTab:hover {{ color: {ink}; }}
QToolButton#mapdexTab:checked {{ color: {ink}; border-bottom: 2px solid {ink}; font-weight: 600; }}
QLabel#mapdexTabBadge {{
    color: {badge_text}; background: {badge_bg}; border-radius: 9px;
    font-size: 11px; font-weight: 700; padding: 0 5px;
}}

/* Cards. */
QFrame#mapdexCard {{ background: {raised}; border: 1px solid {border}; border-radius: {r}px; }}
QFrame#mapdexConfirmCard {{ background: {raised}; border: 1px solid {border_strong}; border-radius: {r}px; }}
QFrame#mapdexList {{ background: transparent; border: 1px solid {border}; border-radius: {r}px; }}
QFrame#mapdexListRow, QFrame#mapdexJobItemRow {{
    background: transparent; border: 0; border-bottom: 1px solid {border};
}}
QFrame#mapdexListRow[last="true"], QFrame#mapdexJobItemRow[last="true"] {{ border-bottom: 0; }}
QFrame#mapdexUserBubble {{ background: {bubble}; border: 0; border-radius: {r}px; }}
QFrame#mapdexPromptCard {{ background: {raised}; border: 1px solid {border}; border-radius: {r}px; }}
QFrame#mapdexPromptCard:hover {{ border-color: {border_strong}; }}
QFrame#mapdexRadioCard {{ background: {raised}; border: 1px solid {border}; border-radius: {r}px; }}
QFrame#mapdexRadioCard:hover {{ border-color: {border_strong}; }}
QFrame#mapdexRadioCard[checked="true"] {{ background: {selected_bg}; border: 1px solid {selected_border}; }}
QFrame#mapdexRadioCard[disabled="true"] {{ background: transparent; }}
QLabel#mapdexRadioTitle {{ color: {ink}; font-size: {body}px; font-weight: 600; }}
QLabel#mapdexRadioDescription {{ color: {muted}; font-size: {label}px; }}
QLabel#mapdexAvatar {{ background: {border}; border-radius: 16px; }}

/* The drop zone: compact, dashed, the whole of it is the button. */
QFrame#mapdexDropZone {{ background: transparent; border: 1px dashed {border_strong}; border-radius: {r}px; }}
QFrame#mapdexDropZone[dropActive="true"] {{ background: {selected_bg}; border: 1px dashed {radio_on}; }}
QPushButton#mapdexDropButton {{
    background: transparent; border: 0; min-height: 42px; color: {slate};
    font-size: {label}px; font-weight: 500;
}}
QPushButton#mapdexDropButton:hover {{ color: {ink}; }}

QFrame#mapdexOptionsBody {{
    background: transparent; border: 1px solid {border}; border-radius: {r}px;
}}
QToolButton#mapdexRowRemove, QToolButton#mapdexSourceRemove {{
    background: transparent; border: 0; border-radius: {r}px; padding: 2px;
}}
QToolButton#mapdexRowRemove:hover, QToolButton#mapdexSourceRemove:hover {{ background: {hover}; }}

/* Fields. */
QComboBox, QLineEdit {{
    background: {field}; color: {ink}; selection-background-color: {primary};
    selection-color: {primary_text}; border: 1px solid {border_strong};
    border-radius: {r}px; padding: 6px 9px; min-height: 22px; font-size: {body}px;
}}
QComboBox:focus, QLineEdit:focus {{ border-color: {radio_on}; }}
QComboBox:disabled, QLineEdit:disabled {{ color: {faint}; border-color: {border}; }}
QComboBox::drop-down {{ border: 0; width: 28px; }}
QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox QAbstractItemView {{
    background: {raised}; color: {ink}; border: 1px solid {border_strong};
    selection-background-color: {primary}; selection-color: {primary_text}; outline: 0;
}}
QCheckBox {{ color: {ink}; font-size: {label}px; spacing: 8px; }}
QCheckBox:disabled {{ color: {faint}; }}

QFrame#mapdexComposer {{ background: {field}; border: 1px solid {border_strong}; border-radius: {r}px; }}
QLineEdit#mapdexNivoInput {{
    background: transparent; border: 0; padding: 4px 2px; min-height: 28px; font-size: {body}px;
}}

QProgressBar {{ background: {raised}; border: 0; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {working}; border-radius: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border_strong}; border-radius: 4px; min-height: 24px; margin: 2px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QToolTip {{ color: {ink}; background: {raised}; border: 1px solid {border_strong}; padding: 4px 6px; }}
"""
