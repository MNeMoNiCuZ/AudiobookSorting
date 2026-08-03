"""The application's own icon - taskbar, title bar, alt-tab, and the built .exe.

Drawn rather than shipped as a file, for the same reason the toolbar icons are: one
source of truth, no binary asset to keep in step with the theme, and every size is
rendered exactly rather than scaled from whatever resolution someone happened to save.

It is the Identify mark promoted to a logo: an open book with the "we worked it out"
spark over its corner, on a rounded dark tile so it still reads against a light taskbar.
The toolbar version is a line drawing on transparent, which disappears at 16px on a
pale background - a program icon cannot afford that.

``write_ico`` is what build.bat calls: Windows wants a real multi-resolution .ico, and
this writes one from the same drawing.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .icons import BOOK_COLOUR, SPARK_COLOUR, _spark

GRID = 64.0

# The tile. Dark enough that the pale book reads on it, light enough not to vanish
# into a dark taskbar - hence the border, which is the only thing separating the two.
TILE_TOP = '#2b3140'
TILE_BOTTOM = '#191c22'
TILE_EDGE = '#454d5e'

# Every size Windows Explorer, the taskbar and the alt-tab switcher ask for.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _draw(painter: QPainter) -> None:
    """The whole mark, in a 64x64 box."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    from PyQt6.QtGui import QLinearGradient

    tile = QRectF(1.5, 1.5, GRID - 3, GRID - 3)
    gradient = QLinearGradient(0, 0, 0, GRID)
    gradient.setColorAt(0.0, QColor(TILE_TOP))
    gradient.setColorAt(1.0, QColor(TILE_BOTTOM))
    painter.setBrush(QBrush(gradient))
    painter.setPen(QPen(QColor(TILE_EDGE), 2.0))
    painter.drawRoundedRect(tile, 13.0, 13.0)

    # The open book, at 2.4x the toolbar drawing's scale and with a heavier stroke so
    # the pages survive being rendered at sixteen pixels.
    pen = QPen(QColor(BOOK_COLOUR), 3.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    left = QPainterPath()
    left.moveTo(10.5, 22.0)
    left.quadTo(21.0, 18.0, 32.0, 25.0)
    left.lineTo(32.0, 52.0)
    left.quadTo(21.0, 46.0, 10.5, 50.0)
    left.closeSubpath()
    painter.drawPath(left)

    right = QPainterPath()
    right.moveTo(53.5, 22.0)
    right.quadTo(43.0, 18.0, 32.0, 25.0)
    right.lineTo(32.0, 52.0)
    right.quadTo(43.0, 46.0, 53.5, 50.0)
    right.closeSubpath()
    painter.drawPath(right)

    # The spark, over the book's top-right corner exactly as on the toolbar button.
    _spark(painter, QPointF(48.0, 17.0), 15.0, SPARK_COLOUR)


def pixmap(size: int = 256) -> QPixmap:
    """The icon at one size, rendered at that size rather than scaled to it."""
    image = QPixmap(size, size)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.scale(size / GRID, size / GRID)
    _draw(painter)
    painter.end()
    return image


def app_icon() -> QIcon:
    """A QIcon carrying every size, so Qt never has to resample one."""
    icon = QIcon()
    for size in ICO_SIZES:
        icon.addPixmap(pixmap(size))
    return icon


def write_ico(path: Path, sizes: List[int] = None) -> Path:
    """Write a multi-resolution Windows .ico. Used by build.bat before packaging.

    Qt cannot write .ico, so the frames are rendered here and assembled with Pillow -
    the one place in this project that needs it, and only at build time.
    """
    from io import BytesIO

    from PIL import Image
    from PyQt6.QtCore import QBuffer, QByteArray

    sizes = sizes or list(ICO_SIZES)
    frames = []
    for size in sizes:
        # QImage writes to a QIODevice, not to a Python file object, so the PNG goes
        # through a QBuffer and comes back out as bytes for Pillow to assemble.
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        pixmap(size).toImage().save(buffer, 'PNG')
        buffer.close()
        frames.append(Image.open(BytesIO(bytes(data))).convert('RGBA'))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(path, format='ICO',
                    sizes=[(size, size) for size in sizes])
    return path


def _main() -> int:
    """``python -m scripts.gui.app_icon <out.ico>`` - what build.bat runs."""
    import sys

    from PyQt6.QtWidgets import QApplication

    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'build/audiobook_organizer.ico')
    # QPixmap needs a QApplication even when nothing is shown.
    application = QApplication.instance() or QApplication([])
    write_ico(target)
    print(f'Wrote {target}')
    del application
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
