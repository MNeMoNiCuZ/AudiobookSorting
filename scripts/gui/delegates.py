"""How a review row is drawn.

Qt's default item painting gives you one line of text per cell and a full-cell
selection block. This table needs more than that to be readable at a glance, so the
cells are painted here:

* a **status stripe** down the left edge of the row, and a **status pill** in the
  status column - the only two places colour is spent;
* the file cell as two lines, folder over filename, with the filename quieter;
* confidence as a number over a thin bar, so a column of them scans as a shape;
* covers rounded, with a drawn placeholder rather than a hole when there is no art.

Everything reads its inputs from item data roles, so the delegate never needs to know
about ``BookEntry``.
"""

from __future__ import annotations

from PyQt6.QtCore import (QEvent, QModelIndex, QRect, QRectF, QSize, Qt,
                          pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient,
                         QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QAbstractItemDelegate, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem)

from .theme import (ACCENT, BG_BASE, BG_HOVER, BG_RAISED, BORDER_SOFT,
                    SELECTION_BORDER, STATUS_COLORS, STATUS_HUES, TEXT, TEXT_DIM,
                    TEXT_FAINT, TEXT_SECONDARY, vivid_confidence_color)

# Item data roles. Kept here so the delegate and the window agree in one place.
ROLE_ENTRY_ID = Qt.ItemDataRole.UserRole
ROLE_STATUS = Qt.ItemDataRole.UserRole + 1
ROLE_CONFIDENCE = Qt.ItemDataRole.UserRole + 2
ROLE_SECONDARY = Qt.ItemDataRole.UserRole + 3
ROLE_KIND = Qt.ItemDataRole.UserRole + 4
# 0..1 while a long job is working on this row, None otherwise. Drawn as a fill
# behind the Files cell, so a book being encoded says so where the book is.
ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 5
ROLE_PROGRESS_TEXT = Qt.ItemDataRole.UserRole + 6

# What a cell should look like. Set as ROLE_KIND on the item.
KIND_TEXT = 'text'
KIND_COVER = 'cover'
KIND_FILES = 'files'
KIND_CONFIDENCE = 'confidence'
KIND_STATUS = 'status'

STRIPE_WIDTH = 3
PAD = 10


def _mix(a: str, b: str, t: float) -> QColor:
    """Blend two hex colours - used for the muted pill fills."""
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t),
                  int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t))


class ReviewDelegate(QStyledItemDelegate):
    """Paints every column of the review table."""

    # Emitted after Enter commits an edit, with how far to move (rows, columns).
    # The view does the moving - the delegate has no business knowing which rows are
    # hidden by the current filter.
    move_after_edit = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Toggled by the Interface settings; see MainWindow.apply_ui_settings.
        self.show_stripe = True
        self.row_tint = False
        self.show_covers = True
        self.colour_confidence = True
        # 0..1, advanced by MainWindow's spinner timer. Drives the band that sweeps
        # across an in-progress row - see _paint_progress.
        self.phase = 0.0

    def eventFilter(self, editor, event) -> bool:
        """Enter commits and moves to the same field on the next book.

        Qt's own behaviour is to commit and stop, which means correcting the author of
        six books is six double-clicks. Tab already walks across a row; Enter walking
        *down* a column is the other half, and it is what the grid editor does - the
        two should not disagree about what Enter means.
        """
        if event is not None and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.commitData.emit(editor)
                self.closeEditor.emit(editor,
                                      QAbstractItemDelegate.EndEditHint.NoHint)
                self.move_after_edit.emit(-1 if shift else 1, 0)
                return True
        return super().eventFilter(editor, event)

    def _first_visible_column(self) -> int:
        view = self.parent()
        if view is None or not hasattr(view, 'isColumnHidden'):
            return 0
        for column in range(view.columnCount()):
            if not view.isColumnHidden(column):
                return column
        return 0

    # ------------------------------------------------------------------ paint

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Every pixel of this cell is painted below, so the style must not also draw
        # its own focus rectangle on the current cell. Left on, Fusion outlines the
        # cell the cursor is in *in addition* to the selection frame, and arrowing
        # around the table leaves that outline looking like a second, ghost cursor.
        option.state &= ~QStyle.StateFlag.State_HasFocus

        rect = option.rect
        status = str(index.data(ROLE_STATUS) or 'pending')
        hue = STATUS_HUES.get(status, TEXT_FAINT)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        self._paint_background(painter, rect, status, selected, hovered,
                               index.row() % 2 == 1)

        # The stripe belongs to the leftmost *visible* column - the cover column is
        # hidden in compact rows, and the stripe must not vanish with it.
        if (self.show_stripe and status != 'pending'
                and index.column() == self._first_visible_column()):
            painter.fillRect(QRect(rect.left(), rect.top(), STRIPE_WIDTH,
                                   rect.height()), QColor(hue))

        # Extra left padding on the leading column so text clears the status stripe.
        leading = index.column() == self._first_visible_column()
        body = rect.adjusted(PAD if leading else PAD // 2, 0, -PAD // 2, 0)
        kind = str(index.data(ROLE_KIND) or KIND_TEXT)

        if kind == KIND_COVER:
            self._paint_cover(painter, rect, index)
        elif kind == KIND_FILES:
            self._paint_row_progress(painter, rect, index)
            self._paint_files(painter, body, index, selected)
        elif kind == KIND_CONFIDENCE:
            self._paint_confidence(painter, body, index)
        elif kind == KIND_STATUS:
            self._paint_status(painter, rect, status, hue)
        else:
            self._paint_text(painter, body, index, selected)

        painter.restore()

    def _paint_background(self, painter: QPainter, rect: QRect, status: str,
                          selected: bool, hovered: bool, odd: bool = False) -> None:
        """Fill by status, band by row parity, and mark selection with a border.

        Selection deliberately does *not* recolour the row: the fill already carries
        the review status, and overwriting it means a selected row stops telling you
        whether it is approved. The selection is a frame drawn on top instead, which
        composes with every status rather than replacing it.
        """
        base = QColor(STATUS_COLORS.get(status, BG_BASE)) if self.row_tint             else QColor(BG_BASE)
        # Zebra: every other row lifted a step. Mixing toward the neutral hover grey
        # washed the colour out of tinted rows - a risky row's amber went muddy - so
        # the band is a lightness change on the row's own colour instead.
        if odd:
            base = base.lighter(128) if base.value() > 24 else QColor(BG_HOVER)
        if hovered:
            base = base.lighter(122)
        painter.fillRect(rect, base)

        # Hairline between rows instead of a full grid - separation without a cage.
        painter.setPen(QColor(BORDER_SOFT))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        if selected:
            self._paint_selection_frame(painter, rect)

    def _paint_selection_frame(self, painter: QPainter, rect: QRect) -> None:
        """A bright rule along the top and bottom of the selected cell, closed at the
        sides by a thinner vertical edge.

        Drawn per cell, so the top and bottom edges join up across a whole selected row
        and read as one continuous band. The verticals are half the weight on purpose:
        they close the box - so selecting three cells in a column is visibly three
        cells and not one smear - without turning every selection into a hard cage.
        """
        pen = QPen(QColor(SELECTION_BORDER))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(rect.left(), rect.top() + 1, rect.right(), rect.top() + 1)
        painter.drawLine(rect.left(), rect.bottom() - 1,
                         rect.right(), rect.bottom() - 1)

        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(rect.left(), rect.top() + 1, rect.left(), rect.bottom() - 1)
        painter.drawLine(rect.right(), rect.top() + 1,
                         rect.right(), rect.bottom() - 1)

    # ------------------------------------------------------------- cell kinds

    def _paint_cover(self, painter: QPainter, rect: QRect, index) -> None:
        if not self.show_covers:
            return
        side = min(rect.height() - 12, rect.width() - STRIPE_WIDTH - 10)
        if side < 12:
            return
        box = QRectF(rect.left() + STRIPE_WIDTH + 7,
                     rect.top() + (rect.height() - side) / 2, side, side)

        path = QPainterPath()
        path.addRoundedRect(box, 4, 4)
        painter.setClipPath(path)

        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(
                int(box.width()), int(box.height()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(box.toRect(), scaled)
        else:
            # A drawn placeholder, so a row without art still has the same rhythm.
            painter.fillRect(box, QColor(BG_RAISED))
            painter.setPen(QColor(TEXT_FAINT))
            font = QFont('Segoe UI Symbol')
            font.setPixelSize(int(box.height() * 0.5))
            painter.setFont(font)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, '♪')
        painter.setClipping(False)

    def _paint_row_progress(self, painter: QPainter, rect: QRect, index) -> None:
        """A fill sweeping across the Files cell while a job works on this book.

        A merge is a two-minute job on one row, and the toolbar's single progress bar
        cannot say *which* row - so the row says it itself. It is a background wash
        rather than a widget: it has to sit behind the filename without moving it, and
        it must survive the table being scrolled, sorted or filtered underneath it.
        """
        value = index.data(ROLE_PROGRESS)
        if value is None:
            return
        try:
            fraction = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return

        wash = QColor(ACCENT)
        wash.setAlpha(52)
        filled = int(rect.width() * fraction)
        painter.fillRect(QRect(rect.left(), rect.top(), filled, rect.height()), wash)

        # A chapter takes minutes, so between two steps the bar does not move at all.
        # A band sweeping across the part that is still to do is the difference
        # between "working" and "hung", and it is the only thing on screen that can
        # say so - the number genuinely has not changed.
        remaining = rect.width() - filled
        if remaining > 8:
            sweep = QRect(rect.left() + filled, rect.top(), remaining, rect.height())
            gradient = QLinearGradient(sweep.left(), 0, sweep.right(), 0)
            glow = QColor(ACCENT)
            glow.setAlpha(34)
            clear = QColor(ACCENT)
            clear.setAlpha(0)
            # `phase` runs 0..1 and is advanced by the window's spinner timer.
            head = self.phase
            for stop, colour in ((0.0, clear), (max(0.0, head - 0.18), clear),
                                 (head, glow), (min(1.0, head + 0.18), clear),
                                 (1.0, clear)):
                gradient.setColorAt(min(1.0, max(0.0, stop)), colour)
            painter.fillRect(sweep, QBrush(gradient))

        # A hairline at the leading edge, so slow progress is still visibly progress.
        edge = QColor(ACCENT)
        edge.setAlpha(190)
        x = rect.left() + filled
        painter.fillRect(QRect(max(rect.left(), x - 2), rect.top(), 2, rect.height()),
                         edge)

        label = str(index.data(ROLE_PROGRESS_TEXT) or '')
        if label:
            font = QFont(painter.font())
            font.setPixelSize(max(9, (font.pixelSize() or 12) - 2))
            painter.setFont(font)
            painter.setPen(QColor(ACCENT))
            painter.drawText(rect.adjusted(0, 0, -6, -2),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                             label)

    def _paint_files(self, painter: QPainter, rect: QRect, index,
                     selected: bool) -> None:
        """Filename on top in bold, the path from the library root underneath.

        The filename leads and is the heaviest thing in the cell because it is what
        identifies the row at a glance. The path is a whole step quieter, but only a
        step: it carries the author and series often enough that reading it must not be
        work, which is why it is no longer drawn in the faintest grey in the palette.
        """
        primary = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        secondary = str(index.data(ROLE_SECONDARY) or '')
        font = painter.font()
        bold = QFont(font)
        bold.setBold(True)
        metrics = QFontMetrics(bold)
        line = metrics.height()
        # A compact row has no space for two lines; the path is in the tooltip.
        if not secondary or rect.height() < line * 2 + 6:
            painter.setFont(bold)
            self._paint_text(painter, rect, index, selected)
            painter.setFont(font)
            return

        small = QFont(font)
        small.setBold(False)
        small.setPixelSize(max(9, font.pixelSize() - 1 if font.pixelSize() > 0 else 12))
        gap = 2
        total = line * 2 + gap
        top = rect.top() + (rect.height() - total) // 2

        painter.setFont(bold)
        painter.setPen(QColor(TEXT))
        painter.drawText(QRect(rect.left(), top, rect.width(), line),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(primary, Qt.TextElideMode.ElideRight,
                                            rect.width()))
        painter.setFont(small)
        painter.setPen(QColor(TEXT_SECONDARY))
        small_metrics = QFontMetrics(small)
        painter.drawText(QRect(rect.left(), top + line + gap, rect.width(), line),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         small_metrics.elidedText(secondary,
                                                  Qt.TextElideMode.ElideMiddle,
                                                  rect.width()))
        painter.setFont(font)

    def _paint_confidence(self, painter: QPainter, rect: QRect, index) -> None:
        """The number, with a bar under it - a column of these scans as a shape.

        With ``colour_confidence`` on (Interface settings) the bar is drawn in a
        saturated red / amber / green rather than grey. That deliberately spends a
        third colour, so it is a setting: the bar is the one place where "how sure are
        we" needs to be readable across a whole screen without stopping to read.
        """
        value = float(index.data(ROLE_CONFIDENCE) or 0.0)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')

        metrics = QFontMetrics(painter.font())
        # A coloured bar earns a couple more pixels; grey stays a hairline.
        bar_height = 5 if self.colour_confidence else 3
        block = metrics.height() + 4 + bar_height
        top = rect.top() + (rect.height() - block) // 2

        hue = (vivid_confidence_color(value) if self.colour_confidence
               else (TEXT if value >= 0.8 else
                     TEXT_DIM if value >= 0.55 else TEXT_FAINT))
        painter.setPen(QColor(hue if self.colour_confidence else hue))
        # Left, under a left-aligned heading. Only "#" is centred in this table.
        painter.drawText(QRect(rect.left() + 2, top, rect.width() - 4,
                               metrics.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         text)

        radius = bar_height / 2.0
        track = QRectF(rect.left() + 2, top + metrics.height() + 4,
                       max(0, rect.width() - 4), bar_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(BG_RAISED)))
        painter.drawRoundedRect(track, radius, radius)
        if value > 0:
            filled = QRectF(track)
            filled.setWidth(max(bar_height, track.width() * min(1.0, value)))
            painter.setBrush(QBrush(QColor(
                hue if self.colour_confidence else TEXT_DIM)))
            painter.drawRoundedRect(filled, radius, radius)

    def _paint_status(self, painter: QPainter, rect: QRect, status: str,
                      hue: str) -> None:
        """A pill: hue text on a heavily muted fill of the same hue."""
        from ..models import pretty_status

        label = pretty_status(status)
        font = QFont(painter.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)

        metrics = QFontMetrics(font)
        width = min(rect.width() - 10, metrics.horizontalAdvance(label) + 18)
        height = metrics.height() + 6
        # Left, so the pill starts where the STATUS heading does.
        pill = QRectF(rect.left() + 2, rect.center().y() - height / 2, width, height)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_mix(BG_BASE, hue, 0.16)))
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(QColor(hue))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_text(self, painter: QPainter, rect: QRect, index,
                    selected: bool) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        colour = index.data(Qt.ItemDataRole.ForegroundRole)
        painter.setPen(QColor(colour) if colour is not None else QColor(TEXT))

        alignment = index.data(Qt.ItemDataRole.TextAlignmentRole)
        flags = (Qt.AlignmentFlag(alignment) if alignment is not None
                 else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if not (flags & (Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignTop
                         | Qt.AlignmentFlag.AlignBottom)):
            flags |= Qt.AlignmentFlag.AlignVCenter

        metrics = QFontMetrics(painter.font())
        painter.drawText(rect, flags,
                         metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                            rect.width()))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + PAD, hint.height())
