"""Dark theme for the whole application.

One stylesheet plus a matching QPalette - the palette matters because several native
widgets (tooltips, item-view selections, the text cursor) ignore stylesheets.

Colour is used consistently for review decisions, confidence bars and field sources:

1. **Status colour = review status.** Stripe down the row, pill in the Status column,
   counts in the toolbar. Always the same hue for the same status.
2. **Source colour = where a value came from.** The same hue marks that source in the
   table cell, in the Fields list, and on that source's card in the explanation panel.
   Learn "violet = the language model" once and it holds everywhere.

Confidence bands colour only the confidence number and bar. They never replace the
review status or tint the row.

Full-row status washes exist (Interface settings) but are off by default: a screen of
saturated rows has no emphasis left to spend.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap

# --------------------------------------------------------------------- palette
# Low-contrast neutrals on a cool grey ramp, one accent. Each step is a real step -
# adjacent surfaces are distinguishable without a border between them.
BG_DARKEST = '#131519'
BG_DARK = '#191c22'
BG_BASE = '#20242c'
BG_RAISED = '#282d37'
BG_HOVER = '#313742'
BORDER = '#363c48'
BORDER_SOFT = '#2b303a'
# Input surfaces are lighter than the page they sit on. A field the same colour as
# its background is not a field, it is a rumour.
FIELD = '#333a47'
FIELD_HOVER = '#3d4553'
FIELD_BORDER = '#525c6e'
TEXT = '#e6e9f0'
# One step below TEXT, for a second line that is subordinate but still meant to be
# read - the path under a filename. TEXT_DIM and TEXT_FAINT are for labels and for
# things that are genuinely absent; using them for real content makes it work to read.
TEXT_SECONDARY = '#cbd2df'
TEXT_DIM = '#98a1b2'
TEXT_FAINT = '#69717f'

# Hyperlinks. White rather than an accent tint: a link is usually the brightest thing
# in a line of dim helper text, and Qt's own default is a near-black blue that is
# invisible on this background. Underlined by Qt, so colour is not carrying the whole
# signal on its own.
LINK = '#ffffff'

# "You cannot press this." Deliberately darker than TEXT_FAINT: a disabled control has
# to be distinguishable from a merely quiet one at a glance, across the width of a
# toolbar, without comparing it to its neighbour. Kept in step with
# icons.DISABLED_COLOUR, which draws the icon beside the label.
DISABLED_TEXT = '#464c58'

# Named accents the user can pick between (Interface settings). Each is a (base, dark,
# soft) triple so the whole UI re-tints coherently, not just one button.
ACCENTS = {
    'blue': ('#5aa9e6', '#3d84bd', '#2b4a66'),
    'teal': ('#4ec9b0', '#35a08b', '#244c46'),
    'violet': ('#a98bf0', '#7f63c4', '#3d3459'),
    'amber': ('#e0a458', '#b57f3c', '#4d3c22'),
    'rose': ('#e87f96', '#bd5c72', '#4f2c38'),
    'slate': ('#9aa8bd', '#71809a', '#38404e'),
}


def _configured_accent() -> tuple:
    """Read the accent at import time, so every ``from .theme import ACCENT`` in the
    application gets the right value. Changing it therefore needs a restart, which the
    Interface settings page says plainly."""
    try:
        from ..settings import Settings
        return ACCENTS.get(Settings().get('AO_UI_ACCENT'), ACCENTS['blue'])
    except Exception:  # settings unreadable - the theme must still load
        return ACCENTS['blue']


ACCENT, ACCENT_DARK, ACCENT_SOFT = _configured_accent()

# Selection is drawn as a frame, not a fill: the row's fill already means its review
# status, and repainting it on selection would erase that. A bright rule reads clearly
# over green, red, amber and plain rows alike.
SELECTION_BORDER = '#ffffff'

# --------------------------------------------------------------------- status
# The stripe/pill hue per status, and the muted fill used behind the pill.
STATUS_HUES = {
    'pending': TEXT,
    'likely': '#3ddc84',
    'uncertain': '#ffc233',
    'unlikely': '#ff5c5c',
    'approved': '#5fd39a',
    'rejected': '#ef7583',
    'applied': '#5aa9e6',
    'risky': '#e8bd66',
    'duplicate': '#ff3b47',   # sharp red - a duplicate is a real problem, not a note
}
# Optional full-row wash. Same hues, dropped to near-background so text stays legible.
STATUS_COLORS = {
    'pending': BG_BASE,
    'likely': BG_BASE,
    'uncertain': BG_BASE,
    'unlikely': BG_BASE,
    'approved': '#1e3628',
    'rejected': '#3a2126',
    'applied': '#1b2f3c',
    'risky': BG_BASE,
    'duplicate': '#4a1a1f',
}
STATUS_TEXT = dict(STATUS_HUES)

# ------------------------------------------------------------------- sources
# One hue per identification source, used in every place that source is named. The
# API providers share a family of blues because they are all "a book database".
SOURCE_COLORS = {
    'metadata': '#5fd39a',    # green   - read from the file's own tags
    'regex': '#e8bd66',       # amber   - parsed out of the filename
    'api': '#5aa9e6',         # blue    - a book database
    'audnexus': '#5aa9e6',
    'googlebooks': '#7cbfe8',
    'openlibrary': '#9dc4de',
    'search': '#e89a5f',      # orange  - scraped from a web search
    'llm': '#b995ee',         # violet  - the language model guessed it
    'folder': '#6fc9c0',      # teal    - inferred from sibling books
    'user': '#ffffff',        # white   - you typed it; it outranks everything
    'dedupe': '#b995ee',
    'quality': '#e8bd66',     # amber - a heads-up, not a verdict
    'auto': '#5fd39a',
    '': TEXT_FAINT,
}


def source_color(source: str) -> str:
    """The one hue that means "this came from <source>"."""
    return SOURCE_COLORS.get((source or '').lower(), TEXT_DIM)

# ------------------------------------------------------------------- metrics
# One spacing unit, used everywhere, so gaps are multiples of the same number.
UNIT = 4
ROW_HEIGHTS = {'compact': 34, 'normal': 58, 'comfortable': 74}
RADIUS = 6


def confidence_color(value: float) -> str:
    """Confidence is a number, not a traffic light - it is always neutral text.

    Kept as a function so callers stay unchanged; low confidence is merely dimmer,
    which reads as "less certain" without competing with the status colours.
    """
    if value >= 0.8:
        return TEXT
    if value >= 0.55:
        return TEXT_DIM
    return TEXT_FAINT


# The traffic light for the confidence bar (AO_UI_CONFIDENCE_COLOR). Deliberately
# more saturated than the status hues: the bar is three pixels tall, and a muted colour
# at that size is a grey smudge. These colours are confined to confidence itself.
CONFIDENCE_HUES = (
    (0.80, '#3ddc84'),   # green  - good enough to file
    (0.55, '#ffc233'),   # amber  - worth a glance
    (0.00, '#ff5c5c'),   # red    - do not trust this
)


def vivid_confidence_color(value: float, confident: float = 0.80,
                           doubtful: float = 0.50) -> str:
    """The saturated traffic-light hue for a confidence, for the bar in the table."""
    if value >= confident:
        return CONFIDENCE_HUES[0][1]
    if value >= doubtful:
        return CONFIDENCE_HUES[1][1]
    return CONFIDENCE_HUES[2][1]


# ---------------------------------------------------------------------- icons

def glyph_icon(glyph: str, colour: str = TEXT, size: int = 20) -> QIcon:
    """A monochrome icon painted from one character.

    The app ships no image assets, and a themed glyph beats a PNG that ignores the
    palette. Painted at 2x and let Qt downscale, so it stays crisp on HiDPI.
    """
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont('Segoe UI Symbol')
    # Nearly the full box: a glyph floating in the middle of a large button reads as
    # a small button with padding, which is exactly what it should not read as.
    font.setPixelSize(int(size * scale * 0.98))
    painter.setFont(font)
    painter.setPen(QColor(colour))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()

    icon = QIcon(pixmap)
    icon.actualSize(QSize(size, size))
    return icon


STYLESHEET = f"""
QWidget {{
    background-color: {BG_DARK};
    color: {TEXT};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {BG_DARKEST}; }}

QLabel {{ background: transparent; }}
QLabel[heading="true"] {{ font-size: 15px; font-weight: 600; padding: 2px 0; }}
QLabel[dim="true"] {{ color: {TEXT_DIM}; }}
/* Small all-caps label used above groups of controls. */
QLabel[eyebrow="true"] {{
    color: {TEXT_FAINT}; font-size: 10px; font-weight: 700; letter-spacing: 1px;
}}

/* The toolbar is a row of icons, not a panel: every vertical pixel it does not need
   is a pixel stolen from the table underneath it. Horizontal padding stays - buttons
   need room to sit apart - but vertically this is as tight as it goes without the
   hover highlight touching the window edge. */
QToolBar {{
    background-color: {BG_DARKEST};
    border-bottom: 1px solid {BORDER};
    spacing: 3px; padding: 0 8px; margin: 0;
}}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 10px; }}
QToolButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: {RADIUS}px; padding: 0 4px; margin: 0;
}}
QToolButton:hover {{ background: {BG_RAISED}; border-color: {BORDER}; }}
QToolButton:pressed {{ background: {BG_BASE}; }}
/* A disabled button has to look disabled at a glance, not on inspection. Qt's default
   is the palette's Disabled text role, which on a dark theme is a mild fade off the
   normal colour - so the label is stated explicitly here, and the icon is drawn in the
   matching grey rather than left to Qt's wash (see icons.DISABLED_COLOUR). */
QToolButton:disabled {{
    background: transparent; border-color: transparent; color: {DISABLED_TEXT};
}}
QToolButton:disabled:hover {{ background: transparent; border-color: transparent; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QPushButton {{
    background-color: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 6px 13px;
    color: {TEXT};
}}
QPushButton:hover {{ background-color: {BG_HOVER}; border-color: {ACCENT_DARK}; }}
QPushButton:pressed {{ background-color: {BG_BASE}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {BG_BASE}; border-color: {BG_RAISED}; }}
QPushButton[accent="true"] {{
    background-color: {ACCENT_DARK}; border-color: {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background-color: {ACCENT}; }}
/* An accented button that cannot be pressed must not keep shouting. A highlighted
   Save with nothing to save reads as "you have unsaved work" - the exact opposite of
   the truth. Disabled always wins over the accent. */
QPushButton[accent="true"]:disabled {{
    background-color: {BG_BASE}; border-color: {BG_RAISED};
    color: {TEXT_FAINT}; font-weight: 400;
}}
QPushButton[danger="true"] {{ background-color: #6b2b30; border-color: #8a3a40; }}
QPushButton[danger="true"]:hover {{ background-color: #8a3a40; }}
QPushButton[success="true"] {{ background-color: #24583a; border-color: #2f7049; }}
QPushButton[success="true"]:hover {{ background-color: #2f7049; }}

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_BASE};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 8px;
    /* A floor, not a size. Without it a field is free to be squeezed to nothing by an
       over-constrained layout, and an input two pixels tall is not an input. */
    min-height: 20px;
    selection-background-color: {ACCENT_DARK};
    selection-color: #ffffff;
}}
/* Filter and search controls sit on the window background, where a dark field on a
   dark surface is invisible. These are deliberately *lighter* than the page with a
   bright border, so they read as things you can click and type into. */
QComboBox[filter="true"] {{
    background-color: {FIELD};
    border: 1px solid {FIELD_BORDER};
    padding: 8px 22px 8px 13px;
    color: {TEXT};
    font-size: 14px;
    min-height: 20px;
}}
QComboBox[filter="true"]:hover {{
    background-color: {FIELD_HOVER}; border-color: {ACCENT};
}}
QComboBox[filter="true"][active="true"] {{
    border-color: {ACCENT}; color: {ACCENT}; font-weight: 600;
}}
QLineEdit[search="true"] {{
    background-color: {FIELD};
    border: 1px solid {FIELD_BORDER};
    border-radius: 5px;
    padding: 8px 13px;
    min-height: 20px;
    font-size: 14px;
    color: {TEXT};
}}
QLineEdit[search="true"]:hover {{ border-color: {ACCENT}; background-color: {FIELD_HOVER}; }}
QLineEdit[search="true"]:focus {{
    border: 2px solid {ACCENT}; background-color: {FIELD_HOVER}; padding: 7px 12px;
}}
QLabel[count="true"] {{ color: {TEXT}; font-size: 14px; font-weight: 600; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {TEXT_FAINT}; background-color: {BG_DARK}; }}

/* Only the drop-down *button* is styled. The arrow itself is left to Fusion, which
   draws a real triangle from the palette - the CSS "zero-size box with borders"
   triangle is a web trick, and Qt renders it as a filled square. */
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    border: none; background: transparent; width: 14px;
}}
/* combobox-popup: 0 is not cosmetic. Without it Qt sizes a popup to *every* item and
   then clips whatever will not fit the screen - so a 51-model list opened from a combo
   near the bottom of the display becomes a few-pixel sliver instead of a scrollable
   list. With it, the popup honours maxVisibleItems and scrolls. */
QComboBox {{ combobox-popup: 0; }}
QComboBox QAbstractItemView {{
    background-color: {BG_BASE}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DARK}; outline: none;
}}

QTableWidget, QTableView, QTreeWidget {{
    background-color: {BG_BASE};
    alternate-background-color: {BG_DARK};
    border: 1px solid {BORDER_SOFT};
    border-radius: {RADIUS}px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
    outline: none;
}}
/* No "::item" rule here, deliberately. The moment one exists, QStyleSheetStyle takes
   over item painting and silently ignores every per-item background brush - which is
   exactly how the review status is drawn. Selection colours come from the view's
   selection-background-color above, which works without an item rule.
   QTreeWidget has no status tinting, so it may keep its item rule. */
QTreeWidget::item {{ padding: 3px 5px; border: none; }}
QTreeWidget::item:selected {{ background-color: {ACCENT_DARK}; color: #ffffff; }}
QHeaderView {{ background-color: {BG_DARK}; }}
QHeaderView::section {{
    background-color: {BG_DARK};
    color: {TEXT};
    padding: 9px 10px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-size: 12px; font-weight: 700; letter-spacing: 0.8px;
}}
QHeaderView::section:hover {{ background-color: {BG_HOVER}; color: {TEXT}; }}
QTableCornerButton::section {{ background-color: {BG_RAISED}; border: none; }}

QScrollBar:vertical {{ background: {BG_DARK}; width: 12px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{
    background: {BG_HOVER}; border-radius: 6px; min-height: 30px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {BORDER}; }}
QScrollBar:horizontal {{ background: {BG_DARK}; height: 12px; margin: 0; border: none; }}
QScrollBar::handle:horizontal {{
    background: {BG_HOVER}; border-radius: 6px; min-width: 30px; margin: 2px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QProgressBar {{
    background-color: {BG_BASE}; border: 1px solid {BORDER};
    border-radius: 5px; text-align: center; color: {TEXT}; height: 18px;
}}
QProgressBar::chunk {{ background-color: {ACCENT_DARK}; border-radius: 4px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{
    background: {BG_DARK}; color: {TEXT_DIM};
    padding: 8px 16px; border: 1px solid {BORDER}; border-bottom: none;
    border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {BG_BASE}; color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ background: {BG_RAISED}; color: {TEXT}; }}

QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 6px;
    margin-top: 12px; padding-top: 10px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {ACCENT}; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {BORDER};
    border-radius: 4px; background: {BG_BASE};
}}
QCheckBox::indicator:checked {{ background: {ACCENT_DARK}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QRadioButton::indicator:checked {{ background: {ACCENT_DARK}; border-color: {ACCENT}; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QSplitter::handle:hover {{ background: {ACCENT_DARK}; }}

QStatusBar {{ background: {BG_DARKEST}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar::item {{ border: none; }}

QMenu {{ background: {BG_BASE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DARK}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QToolTip {{
    background-color: {BG_RAISED}; color: {TEXT};
    border: 1px solid {ACCENT_DARK}; border-radius: 4px; padding: 5px;
}}
"""


def _arrow_image() -> str:
    """Write the combo-box arrow to a PNG and return its path.

    Qt stops drawing the native arrow the moment a stylesheet touches the combo, and
    it will not accept a CSS-triangle - ``image:`` wants a real file. So we draw one
    once, in the theme colour, and point the stylesheet at it.
    """
    from ..paths import temp_file

    path = temp_file('combo_arrow.png')
    scale = 3
    pixmap = QPixmap(10 * scale, 6 * scale)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(TEXT_DIM))
    from PyQt6.QtCore import QPoint
    painter.drawPolygon(QPoint(0, 0), QPoint(10 * scale, 0),
                        QPoint(5 * scale, 6 * scale))
    painter.end()
    pixmap.save(str(path), 'PNG')
    return path.as_posix()


def table_modal_width(parent, fallback: int) -> int:
    """The width a modal holding a preview table should open at.

    The same width as the review table it is previewing - not the width of the whole
    window, and not a number picked per dialog. These modals show the review table's
    own rows at the review table's own column widths, so matching its width is what
    lets you read down the same values at the same scale instead of re-finding them.

    `fallback` is used when there is no parent window to measure - a dialog opened from
    a test, or from the CLI.
    """
    table = getattr(parent, 'table', None)
    width = table.width() if table is not None else 0
    if width <= 0:
        return fallback
    # The dialog's own contents margins (14 a side) plus the window frame.
    return max(700, width + 40)


def apply_theme(app) -> None:
    """Apply the dark palette and stylesheet to a QApplication."""
    app.setStyle('Fusion')  # the only style that themes consistently across platforms

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_DARK))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_BASE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_DARK))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_DARK))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_FAINT))
    # Qt defaults this to a dark blue meant for white backgrounds, which on this
    # palette renders a link as near-black text on near-black.
    palette.setColor(QPalette.ColorRole.Link, QColor(LINK))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(LINK))
    # TEXT_FAINT was not faint enough to read as "unavailable" - it is the same grey
    # used for values that are merely absent. Disabled controls get their own, darker
    # colour, on every role Fusion reaches for.
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_TEXT))
    app.setPalette(palette)
    arrow = _arrow_image()
    app.setStyleSheet(STYLESHEET + f"""
QComboBox::down-arrow {{
    image: url("{arrow}"); width: 10px; height: 6px; margin-right: 10px;
}}
QComboBox::down-arrow:disabled {{ image: none; }}
""")
