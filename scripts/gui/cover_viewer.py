"""Full-size cover viewer, opened by clicking a thumbnail in the review table.

The table's cover cell is 48px, which is enough to tell "has art" from "has none" and
nothing else. This is where you actually look at the art: whether the image is the
right book, whether it is a scan of a CD sleeve, whether the folder holds three images
because two of them are back covers.

Navigation mirrors what the table is already doing, so the viewer is a lens on the
review rather than a separate place: left/right walk the images of the current book,
up/down walk the books themselves - the table's selection follows, so closing the
viewer leaves you on the row you were last looking at.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from PyQt6.QtCore import QEvent, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QKeyEvent, QPainter, QPainterPath, QPen,
                         QPixmap)
from PyQt6.QtWidgets import (QAbstractButton, QDialog, QLabel, QSizePolicy,
                             QVBoxLayout)

from .theme import BG_DARKEST, BG_RAISED, BORDER, TEXT, TEXT_DIM, TEXT_FAINT

# One image: a label to show under it, and where to load it from - either the bytes
# lifted out of the audio file's tags, or a path to a file sitting in the folder.
CoverSource = Tuple[str, Union[bytes, Path]]

ARROW = 40      # button side, in pixels
ARROW_MARGIN = 10
WHEEL_NOTCH = 120   # what one click of a conventional mouse wheel reports


class _Arrow(QAbstractButton):
    """One navigation chevron, floated over the image.

    Drawn rather than set as text: the arrow glyphs that look right here live in
    fonts that are not on every machine, and a missing glyph is a box in the middle
    of the artwork. A path costs nothing and looks the same everywhere.
    """

    def __init__(self, direction: str, parent=None):
        super().__init__(parent)
        self.direction = direction          # 'left' | 'right' | 'up' | 'down'
        self.setFixedSize(ARROW, ARROW)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # keys stay with the dialog
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # The arrows sit on top of artwork whose colour is unknown, so they carry
        # their own backing disc - a bare chevron disappears against a busy cover.
        lit = self.underMouse() or self.isDown()
        disc = QColor(BG_DARKEST)
        disc.setAlpha(210 if lit else 130)
        painter.setBrush(disc)
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))

        centre = self.rect().center()
        reach = ARROW // 5
        painter.setPen(QPen(QColor(TEXT if lit else TEXT_DIM), 2.4,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # A chevron is three points; which three depends only on the direction.
        offsets = {
            'left':  [(reach, -reach), (-reach, 0), (reach, reach)],
            'right': [(-reach, -reach), (reach, 0), (-reach, reach)],
            'up':    [(-reach, reach), (0, -reach), (reach, reach)],
            'down':  [(-reach, -reach), (0, reach), (reach, -reach)],
        }[self.direction]
        path = QPainterPath()
        points = [QPointF(centre.x() + dx, centre.y() + dy) for dx, dy in offsets]
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.drawPath(path)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()


class CoverViewer(QDialog):
    """A single book's images at full size (#26).

    The dialog never reaches back into the table itself; it asks for a step and the
    main window decides which row that lands on, because only the window knows which
    rows are filtered out.
    """

    # +1 / -1: move to the next or previous book in the visible table order.
    step_book = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Cover')
        self.setModal(False)
        self._sources: List[CoverSource] = []
        self._index = 0
        self._pixmap: Optional[QPixmap] = None
        self._title = ''
        # Whether the table has a visible row above/below the one being shown. Only
        # the window that owns the table can know this, so it is told to us.
        self._has_prev = False
        self._has_next = False
        self._wheel = 0                 # accumulated wheel delta, see wheelEvent

        self.resize(760, 820)
        self.setStyleSheet(f'QDialog {{ background: {BG_DARKEST}; }}')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(200, 200)
        # Ignored, not Expanding: a QLabel's size hint follows the pixmap it holds, so
        # with any other policy setting the scaled cover re-lays out the label, which
        # rescales the cover, which resizes the label again. The arrows are placed
        # against the artwork's rect, and that loop left them a few pixels off it.
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored,
                                 QSizePolicy.Policy.Ignored)
        # The label is resized by the layout well after set_book returns - a caption
        # that wraps to a second line takes a few pixels back off it - so scaling and
        # arrow placement hang off the label's own resize rather than the dialog's.
        # Rendering from set_book alone measured a size the layout was about to change.
        self.image.installEventFilter(self)
        layout.addWidget(self.image, 1)

        # Children of the image, not of the layout: they float over the artwork and
        # are placed by hand in _place_arrows, so they never steal space from it.
        self.arrows = {name: _Arrow(name, self.image)
                       for name in ('left', 'right', 'up', 'down')}
        self.arrows['left'].clicked.connect(lambda: self._step_image(-1))
        self.arrows['right'].clicked.connect(lambda: self._step_image(1))
        self.arrows['up'].clicked.connect(lambda: self.step_book.emit(-1))
        self.arrows['down'].clicked.connect(lambda: self.step_book.emit(1))
        for arrow in self.arrows.values():
            arrow.installEventFilter(self)      # so a wheel notch over one still steps
        self.arrows['left'].setToolTip('Previous image (←)')
        self.arrows['right'].setToolTip('Next image (→)')
        self.arrows['up'].setToolTip('Previous book (↑)')
        self.arrows['down'].setToolTip('Next book (↓)')

        self.caption = QLabel()
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setStyleSheet(f'color: {TEXT}; font-size: 13px;')
        layout.addWidget(self.caption)

        self.hint = QLabel('← → images  ·  ↑ ↓ or scroll for books  ·  Esc closes')
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet(f'color: {TEXT_FAINT}; font-size: 11px;')
        layout.addWidget(self.hint)

    # ----------------------------------------------------------------- content

    def set_book(self, title: str, sources: Sequence[CoverSource],
                 has_prev: bool = False, has_next: bool = False) -> None:
        """Show ``title``'s images, starting at the first one.

        A book with no art is not an error and not an empty window - it gets the same
        frame with a placeholder in it, so stepping through a shelf does not flicker
        between two different window shapes.

        ``has_prev``/``has_next`` say whether the table has another visible row above
        or below this one; they only decide whether the up/down arrows are drawn.
        """
        self._sources = list(sources)
        self._index = 0
        self._title = title or 'Untitled'
        self._has_prev = has_prev
        self._has_next = has_next
        self.setWindowTitle(f'Cover - {self._title}')
        self._load()

    def _step_image(self, delta: int) -> None:
        if len(self._sources) < 2:
            return
        self._index = (self._index + delta) % len(self._sources)
        self._load()

    def _load(self) -> None:
        self._pixmap = None
        if self._sources:
            label, source = self._sources[self._index]
            pixmap = QPixmap()
            try:
                if isinstance(source, bytes):
                    pixmap.loadFromData(source)
                else:
                    pixmap.load(str(source))
            except Exception:
                pixmap = QPixmap()
            if not pixmap.isNull():
                self._pixmap = pixmap
        else:
            label = ''

        count = len(self._sources)
        if self._pixmap is not None:
            size = f'{self._pixmap.width()}×{self._pixmap.height()}'
            position = f'  ·  {self._index + 1}/{count}' if count > 1 else ''
            self.caption.setText(
                f'{self._title}<br>'
                f'<span style="color:{TEXT_DIM}">{label}  ·  {size}{position}</span>')
        else:
            # Either the book has no images at all, or the one it has failed to decode
            # - both mean "there is nothing to look at here", so both say so the same
            # way rather than leaving a blank rectangle to interpret.
            note = 'No cover' if not self._sources else f'{label} - could not be read'
            self.caption.setText(
                f'{self._title}<br><span style="color:{TEXT_DIM}">{note}</span>')
        self._render()

    def _art_area(self) -> QSize:
        """How much room the picture itself may take.

        A gutter wide enough for an arrow is reserved on all four sides, whether or
        not an arrow is currently in it. Reserving only for the arrows actually shown
        would resize the artwork as you stepped between books - the cover would grow
        the moment you reached the end of the list, which reads as the image changing
        rather than the navigation running out.
        """
        gutter = 2 * (ARROW + 2 * ARROW_MARGIN)
        return QSize(max(60, self.image.width() - gutter),
                     max(60, self.image.height() - gutter))

    def _render(self) -> None:
        if self.image.width() < 10 or self.image.height() < 10:
            return
        area = self._art_area()
        if self._pixmap is not None:
            # Fill the space, upscaling a small image rather than leaving it a stamp in
            # the middle of an empty window. Plenty of embedded art is 200-300px, and
            # at that size you cannot see the thing you opened the window to check.
            # The caption still gives the true pixel size, so a soft-looking cover is
            # explained rather than mysterious.
            scaled = self._pixmap.scaled(
                area.width(), area.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.image.setPixmap(scaled)
        else:
            self.image.setPixmap(self._placeholder(area.width(), area.height()))
        self._place_arrows()

    def _place_arrows(self) -> None:
        """Show the arrows that lead somewhere, and set them around the artwork.

        An arrow pointing at nothing is worse than no arrow: it says there is another
        image when there is not. So left/right appear only for a book with more than
        one image, and up/down only when the table really has a row that way.

        They sit in the gutter *outside* the picture, never on top of it - the whole
        point of the window is to look at the art, and a control parked over the
        corner of a cover hides the part you were trying to read.
        """
        multiple = len(self._sources) > 1
        visible = {'left': multiple, 'right': multiple,
                   'up': self._has_prev, 'down': self._has_next}

        shown = self.image.pixmap()
        if shown is None or shown.isNull():
            art = self.image.rect()
        else:
            # QLabel centres the pixmap it is given, so the artwork's rect follows
            # from its size and the label's.
            art = QRect(
                (self.image.width() - shown.width()) // 2,
                (self.image.height() - shown.height()) // 2,
                shown.width(), shown.height())

        centre_x = art.center().x() - ARROW // 2
        centre_y = art.center().y() - ARROW // 2
        positions = {
            'left':  (art.left() - ARROW - ARROW_MARGIN, centre_y),
            'right': (art.right() + ARROW_MARGIN, centre_y),
            'up':    (centre_x, art.top() - ARROW - ARROW_MARGIN),
            'down':  (centre_x, art.bottom() + ARROW_MARGIN),
        }
        for name, arrow in self.arrows.items():
            arrow.setVisible(visible[name])
            arrow.move(*positions[name])

    @staticmethod
    def _placeholder(width: int, height: int) -> QPixmap:
        side = max(60, min(width, height))
        pixmap = QPixmap(side, side)
        pixmap.fill(QColor(BG_RAISED))
        painter = QPainter(pixmap)
        painter.setPen(QColor(BORDER))
        painter.drawRect(0, 0, side - 1, side - 1)
        painter.setPen(QColor(TEXT_FAINT))
        font = QFont()
        font.setPixelSize(max(12, side // 12))
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, 'No Cover')
        painter.end()
        return pixmap

    # ------------------------------------------------------------------ events

    def eventFilter(self, watched, event) -> bool:
        if watched is self.image and event.type() == QEvent.Type.Resize:
            # Safe against recursion: the label's size policy is Ignored, so setting a
            # pixmap here cannot resize it back.
            self._render()
        elif event.type() == QEvent.Type.Wheel:
            # The picture and the arrows cover most of the window, and a wheel notch
            # over a child is still a wheel notch over the viewer. Handled here rather
            # than left to propagate, so it works the same over a button as over art.
            self.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render()

    def wheelEvent(self, event) -> None:
        """A wheel notch anywhere over the window steps a book, like Up/Down.

        The wheel is the reflex for "next one" in every image viewer, and there is
        nothing here to scroll otherwise - the picture is always sized to fit.
        """
        # Free-spinning wheels and trackpads send many small deltas rather than one
        # notch of 120, so they are accumulated: without this a trackpad flick skips
        # through a dozen books at once.
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= WHEEL_NOTCH:
            step = -1 if self._wheel > 0 else 1     # wheel away from you = previous
            self._wheel -= WHEEL_NOTCH if self._wheel > 0 else -WHEEL_NOTCH
            if (step < 0 and self._has_prev) or (step > 0 and self._has_next):
                self.step_book.emit(step)
            else:
                # At either end there is nowhere to go, and leftover delta would fire
                # the moment a neighbour existed again.
                self._wheel = 0
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Left) and len(self._sources) > 1:
            self._step_image(1 if key == Qt.Key.Key_Right else -1)
            return
        if key == Qt.Key.Key_Down and self._has_next:
            self.step_book.emit(1)
            return
        if key == Qt.Key.Key_Up and self._has_prev:
            self.step_book.emit(-1)
            return
        super().keyPressEvent(event)
