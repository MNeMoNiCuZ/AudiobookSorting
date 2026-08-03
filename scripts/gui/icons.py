"""Vector icons, drawn rather than typed.

The toolbar used to render Unicode glyphs in a symbol font. That is fine at 16px and
falls apart at 34: the glyphs come from different design eras, have wildly different
stroke weights and optical sizes, and several of them simply are not the icon you
wanted - they were the nearest codepoint that existed.

So these are drawn: one 24x24 grid, one stroke weight, round caps and joins, and the
same visual density across the set. Everything scales cleanly to any button size and
takes the theme colour, because it is painted, not looked up in a font.
"""

from __future__ import annotations

from typing import Callable, Dict

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap)

GRID = 24.0      # every path below is drawn in a 24x24 box
STROKE = 2.0     # one weight for the whole set

# The four icons that carry their own colour whatever the theme. They are the ones
# where the colour *is* part of the meaning - the identify spark, and the three
# buttons at the business end of the toolbar - so they ignore the passed-in tint.
SPARK_COLOUR = '#f5c451'    # identify: the "we worked it out" star
BOOK_COLOUR = '#5b9dff'     # identify: the book itself
PREVIEW_COLOUR = '#f5c451'  # preview: look before you leap
SAVE_COLOUR = '#5cc46a'     # save: the one button that writes


def _pen(colour: str, width: float = STROKE) -> QPen:
    pen = QPen(QColor(colour))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _arrow_head(painter: QPainter, tip: QPointF, base: QPointF, half: float,
                colour: str) -> None:
    """A solid triangle from `base` to `tip`, `half` wide at the base.

    Both ends are given as points rather than an angle and a length. The angle form
    was the bug in the undo arrow: a head laid along the *tangent* of a circle leaves
    the circle immediately, so its base corner stuck out past the ring - the "weird
    ugly thing". Given the two points on the arc itself, the head sits on the stroke.
    """
    dx, dy = tip.x() - base.x(), tip.y() - base.y()
    length = math.hypot(dx, dy)
    if not length:
        return
    px, py = dy / length, -dx / length      # unit perpendicular to the axis

    path = QPainterPath()
    path.moveTo(tip)
    path.lineTo(base.x() + half * px, base.y() + half * py)
    path.lineTo(base.x() - half * px, base.y() - half * py)
    path.closeSubpath()

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(colour)))
    painter.drawPath(path)


# --------------------------------------------------------------------- shapes

def _folder(painter: QPainter, colour: str, rect: QRectF, tab: float = 4.6) -> None:
    """The folder silhouette shared by scan and apply, so the two read as a pair."""
    path = QPainterPath()
    path.moveTo(rect.left(), rect.bottom())
    path.lineTo(rect.left(), rect.top() + 1.6)
    path.lineTo(rect.left() + tab, rect.top() + 1.6)
    path.lineTo(rect.left() + tab + 1.6, rect.top())
    path.lineTo(rect.right(), rect.top())
    path.lineTo(rect.right(), rect.bottom())
    path.closeSubpath()
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)


def _scan(painter: QPainter, colour: str) -> None:
    """A folder being looked through - "go and find the books again".

    The old circular arrow said "refresh", which is what the button does mechanically
    but not what it means: every other refresh arrow in the set (reset, undo) is about
    reversing a decision, and scan is not one of those.
    """
    # A magnifier over a stack of entries: the folder outline underneath it turned
    # into visual mush at 26px, and the list says "many items" more clearly anyway.
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # The middle line used to run under the magnifier and read as a stray stroke
    # crossing the lens. Each line now stops short of where the glass begins.
    for y, right in ((5.6, 12.4), (9.6, 8.8), (13.6, 8.4)):
        painter.drawLine(QPointF(2.8, y), QPointF(right, y))
    painter.setPen(_pen(colour, STROKE * 1.1))
    painter.drawEllipse(QPointF(14.6, 13.4), 5.4, 5.4)
    painter.drawLine(QPointF(18.6, 17.4), QPointF(21.6, 20.6))


def _sources(painter: QPainter, colour: str) -> None:
    """Three sliders - which sources are switched on."""
    painter.setPen(_pen(colour))
    for y, knob in ((7.0, 15.5), (12.0, 9.0), (17.0, 14.0)):
        painter.drawLine(QPointF(3.5, y), QPointF(20.5, y))
    painter.setBrush(QBrush(QColor(colour)))
    painter.setPen(Qt.PenStyle.NoPen)
    for y, knob in ((7.0, 15.5), (12.0, 9.0), (17.0, 14.0)):
        painter.drawEllipse(QPointF(knob, y), 2.6, 2.6)


def _identify(painter: QPainter, colour: str) -> None:
    """A book with a spark on it - work out *what this book is*.

    A play triangle only ever meant "a job starts", which is true of half the toolbar.
    """
    colour = BOOK_COLOUR
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Open book: spine down the middle, two covers falling away from it.
    book = QPainterPath()
    book.moveTo(2.6, 8.4)
    book.quadTo(7.0, 7.0, 11.0, 9.4)
    book.lineTo(11.0, 20.4)
    book.quadTo(7.0, 18.2, 2.6, 19.6)
    book.closeSubpath()
    painter.drawPath(book)
    mirrored = QPainterPath()
    mirrored.moveTo(19.4, 8.4)
    mirrored.quadTo(15.0, 7.0, 11.0, 9.4)
    mirrored.lineTo(11.0, 20.4)
    mirrored.quadTo(15.0, 18.2, 19.4, 19.6)
    mirrored.closeSubpath()
    painter.drawPath(mirrored)
    # Four-point spark over the book's corner - the "we worked it out" mark. The star
    # is the only deliberately coloured mark in the set, so it is drawn big enough to
    # carry that on its own: it sits lower and wider than the book's edge, overlapping
    # the corner rather than hiding in the gap above it.
    _spark(painter, QPointF(17.6, 6.4), 6.4, SPARK_COLOUR)


def _spark(painter: QPainter, centre: QPointF, size: float, colour: str) -> None:
    """A four-pointed star with concave sides."""
    path = QPainterPath()
    path.moveTo(centre.x(), centre.y() - size)
    path.quadTo(centre.x(), centre.y(), centre.x() + size, centre.y())
    path.quadTo(centre.x(), centre.y(), centre.x(), centre.y() + size)
    path.quadTo(centre.x(), centre.y(), centre.x() - size, centre.y())
    path.quadTo(centre.x(), centre.y(), centre.x(), centre.y() - size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(colour)))
    painter.drawPath(path)


def _approve(painter: QPainter, colour: str) -> None:
    """Tick in a circle - a decision recorded, not just a mark."""
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(12, 12), 8.8, 8.8)
    tick = QPainterPath()
    tick.moveTo(7.4, 12.2)
    tick.lineTo(10.7, 15.6)
    tick.lineTo(16.8, 8.6)
    painter.setPen(_pen(colour, STROKE * 1.15))
    painter.drawPath(tick)


def _reject(painter: QPainter, colour: str) -> None:
    """Cross in a circle - the exact counterpart of approve, same weight, same ring."""
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(12, 12), 8.8, 8.8)
    painter.setPen(_pen(colour, STROKE * 1.15))
    painter.drawLine(QPointF(8.4, 8.4), QPointF(15.6, 15.6))
    painter.drawLine(QPointF(15.6, 8.4), QPointF(8.4, 15.6))


def _reset(painter: QPainter, colour: str) -> None:
    """An empty ring - the decision slot with nothing in it.

    Approve fills it with a tick, reject with a cross, and this is the same ring left
    blank. A dot in the middle read as a third *state* rather than the absence of one.
    """
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(12, 12), 8.8, 8.8)


def _preview(painter: QPainter, colour: str) -> None:
    """The folder tree that applying *would* produce - a document with an indented tree.

    Preview shows a structure, so the icon shows a structure. An eye only ever said
    "look", which does not distinguish it from any other read-only view.
    """
    colour = PREVIEW_COLOUR
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Page outline with a folded corner.
    page = QPainterPath()
    page.moveTo(4.0, 2.6)
    page.lineTo(14.2, 2.6)
    page.lineTo(19.4, 7.8)
    page.lineTo(19.4, 21.4)
    page.lineTo(4.0, 21.4)
    page.closeSubpath()
    painter.drawPath(page)
    painter.drawPolyline(*[QPointF(14.2, 2.6), QPointF(14.2, 7.8), QPointF(19.4, 7.8)])

    # Tree: a spine with two branches, the shape of a rendered folder listing.
    painter.setPen(_pen(colour, STROKE * 0.8))
    painter.drawLine(QPointF(8.0, 11.0), QPointF(8.0, 18.0))
    for y in (13.6, 18.0):
        painter.drawLine(QPointF(8.0, y), QPointF(11.4, y))
        painter.drawLine(QPointF(11.4, y), QPointF(16.0, y))


def _undo(painter: QPainter, colour: str) -> None:
    """Three-quarter circle sweeping anticlockwise, with a solid head.

    Same stroke weight and the same radius family as the gear, so the two read as
    members of one set rather than two people's drawings.
    """
    radius = 8.2
    head_at = 100.0      # degrees, Qt convention: anticlockwise from due east
    finish = -150.0      # where the tail ends, sweeping clockwise on screen
    head_span = 40.0     # how much arc the head itself occupies

    def point(degrees: float) -> QPointF:
        radians = math.radians(degrees)
        return QPointF(12 + radius * math.cos(radians),
                       12 - radius * math.sin(radians))

    # A flat cap, because the arc runs into the head's base rather than ending in
    # the open - a round cap there is what put a bump on the join.
    pen = _pen(colour, STROKE * 1.3)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    box = QRectF(12 - radius, 12 - radius, radius * 2, radius * 2)
    painter.drawArc(box, int(head_at * 16), int((finish - head_at) * 16))

    # Base on the arc's end, tip further round the same circle: the head continues
    # the stroke anticlockwise, which is the direction undo goes.
    _arrow_head(painter, point(head_at + head_span), point(head_at), 3.6, colour)


def _save(painter: QPainter, colour: str) -> None:
    """Floppy disk: shutter at the top, label at the bottom, clipped corner."""
    colour = SAVE_COLOUR
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    body = QPainterPath()
    body.moveTo(4.4, 4.0)
    body.lineTo(16.6, 4.0)
    body.lineTo(20.0, 7.4)          # the corner a real disk has cut off
    body.lineTo(20.0, 20.0)
    body.lineTo(4.4, 20.0)
    body.closeSubpath()
    painter.drawPath(body)
    painter.drawRect(QRectF(8.4, 4.0, 6.4, 4.6))    # metal shutter
    painter.drawRect(QRectF(7.4, 13.0, 9.2, 7.0))   # paper label


def _settings(painter: QPainter, colour: str) -> None:
    """Gear drawn as one outline - eight rounded teeth around a hub."""
    import math

    outer, inner, hub = 10.4, 6.6, 3.4
    path = QPainterPath()
    steps = 7   # fewer, larger teeth survive being drawn at 26 pixels
    for tooth in range(steps):
        base = tooth * (360.0 / steps)
        # Each tooth is a flat-topped block: out, across, back in, along the root.
        for angle, radius in ((base - 21, inner), (base - 13, outer),
                              (base + 13, outer), (base + 21, inner)):
            radians = math.radians(angle)
            point = QPointF(12 + radius * math.cos(radians),
                            12 + radius * math.sin(radians))
            if tooth == 0 and radius == inner and path.elementCount() == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()

    painter.setPen(_pen(colour, STROKE * 0.95))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.drawEllipse(QPointF(12, 12), hub, hub)


def _goodreads(painter: QPainter, colour: str) -> None:
    """An open book with "G R" across its two pages."""
    painter.setPen(_pen(colour))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    left = QPainterPath()
    left.moveTo(2.4, 6.0)
    left.quadTo(6.8, 4.6, 11.2, 7.0)
    left.lineTo(11.2, 19.4)
    left.quadTo(6.8, 17.2, 2.4, 18.6)
    left.closeSubpath()
    painter.drawPath(left)
    right = QPainterPath()
    right.moveTo(20.0, 6.0)
    right.quadTo(15.6, 4.6, 11.2, 7.0)
    right.lineTo(11.2, 19.4)
    right.quadTo(15.6, 17.2, 20.0, 18.6)
    right.closeSubpath()
    painter.drawPath(right)

    # The letters are drawn as paths, not text. Rendering a font at a few pixels
    # inside a scaled painter produces tofu boxes on systems missing the family, and
    # an icon must look the same everywhere.
    _letter_g(painter, QRectF(4.2, 10.2, 5.0, 6.0), colour)
    _letter_r(painter, QRectF(13.6, 10.2, 4.6, 6.0), colour)


def _letter_g(painter: QPainter, box: QRectF, colour: str) -> None:
    """A capital G: an open ring whose lower terminal turns in as a crossbar.

    The terminal is derived from where the arc actually ends rather than from
    hand-typed coordinates. The previous version guessed, so the stem started below
    the arc's endpoint and to the right of it - a G with a broken shoulder and a
    stray tick floating off the top right, which is why it did not read as a G.
    """
    import math

    painter.setPen(_pen(colour, 1.25))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    opening = 34.0     # half-width of the gap on the right-hand side, in degrees
    painter.drawArc(box, int(opening * 16), int((360 - 2 * opening) * 16))

    # Where the arc's lower end sits, in ellipse coordinates.
    radians = math.radians(-opening)
    centre = box.center()
    end = QPointF(centre.x() + box.width() / 2 * math.cos(radians),
                  centre.y() - box.height() / 2 * math.sin(radians))
    # Up to the crossbar height, then in to the middle: the G's bar.
    bar = centre.y() + box.height() * 0.06
    painter.drawLine(end, QPointF(end.x(), bar))
    painter.drawLine(QPointF(end.x(), bar), QPointF(centre.x() - 0.2, bar))


def _letter_r(painter: QPainter, box: QRectF, colour: str) -> None:
    """A capital R: stem, bowl, leg."""
    painter.setPen(_pen(colour, 1.25))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(box.left(), box.top()), QPointF(box.left(), box.bottom()))

    waist = box.top() + box.height() * 0.52
    bowl = QPainterPath()
    bowl.moveTo(box.left(), box.top())
    bowl.lineTo(box.right() - 1.0, box.top())
    bowl.quadTo(box.right() + 0.6, (box.top() + waist) / 2, box.right() - 1.0, waist)
    bowl.lineTo(box.left(), waist)
    painter.drawPath(bowl)
    painter.drawLine(QPointF(box.left() + box.width() * 0.45, waist),
                     QPointF(box.right(), box.bottom()))


SHAPES: Dict[str, Callable[[QPainter, str], None]] = {
    'scan': _scan,
    'sources': _sources,
    'identify': _identify,
    'approve': _approve,
    'reject': _reject,
    'reset': _reset,
    'preview': _preview,
    'apply': _save,
    'undo': _undo,
    'goodreads': _goodreads,
    'settings': _settings,
    'cancel': _reject,
}


def draw(name: str, colour: str, size: int) -> QPixmap:
    """Render one icon into a transparent pixmap of ``size`` logical pixels."""
    scale = 2  # drawn at 2x so it stays sharp on scaled displays
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(QColor(0, 0, 0, 0))

    shape = SHAPES.get(name)
    if shape is None:
        return pixmap

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # The pixmap carries a device pixel ratio, so the painter already works in
    # logical coordinates - scaling by the physical size would draw at double size.
    painter.scale(size / GRID, size / GRID)
    shape(painter, colour)
    painter.end()
    return pixmap


# What a disabled button's icon is drawn in. Qt derives its own disabled rendering by
# washing the pixmap out, which on a dark theme lands somewhere between the icon colour
# and the background and reads as "slightly faded" rather than as "you cannot press
# this". So the disabled state is drawn explicitly, in a grey that is unmistakably
# darker than the toolbar's text - matched by QToolButton:disabled in theme.py.
DISABLED_COLOUR = '#464c58'


def dimmed(pixmap: QPixmap) -> QPixmap:
    """A flat grey silhouette of `pixmap`, keeping its shape and antialiasing.

    This works on the *rendered* pixmap rather than re-drawing the icon in a grey,
    which is what the first attempt did and why the Identify button stayed blue and
    gold while its label went grey: several icons - Identify's sparkle, Goodreads -
    paint their own colours and ignore the `colour` argument entirely, so asking for a
    grey one handed back exactly the same artwork.

    Flooding through SourceIn cannot be ignored by anything. Every visible pixel
    becomes DISABLED_COLOUR at its existing alpha, so a two-tone icon greys out the
    same way a single-tone one does.
    """
    result = QPixmap(pixmap)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(result.rect(), QColor(DISABLED_COLOUR))
    painter.end()
    result.setDevicePixelRatio(pixmap.devicePixelRatio())
    return result


def _with_disabled(pixmap, name: str, size: int) -> QIcon:
    """One QIcon carrying both the normal artwork and an explicitly dimmed version."""
    result = QIcon(pixmap)
    result.addPixmap(dimmed(pixmap), QIcon.Mode.Disabled)
    return result


def icon(name: str, colour: str, size: int = 24) -> QIcon:
    return _with_disabled(draw(name, colour, size), name, size)


BADGE_FILL = '#e34a4a'
BADGE_TEXT = '#ffffff'


def badged_icon(name: str, colour: str, size: int, count: int,
                fill: str = BADGE_FILL) -> QIcon:
    """An icon with a count in a filled circle on its top-right corner.

    Used for "how many identifications are queued". The badge is drawn into the pixmap
    rather than laid over the button with a child widget, because a QToolBar reflows
    its buttons and an overlay widget goes wherever the button used to be.
    """
    pixmap = draw(name, colour, size)
    if count <= 0:
        return _with_disabled(pixmap, name, size)

    from PyQt6.QtGui import QFont

    text = str(count) if count < 100 else '99+'
    # Scaled off the icon, so the badge stays proportionate at every button size.
    diameter = max(12.0, size * 0.46)
    width = max(diameter, diameter * 0.58 * len(text) + diameter * 0.42)

    # The pixmap carries a device pixel ratio, so the painter works in logical
    # coordinates - the same ones draw() used - and `size` is its logical width.
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    box = QRectF(size - width - 0.5, 0.5, width, diameter)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(fill)))
    painter.drawRoundedRect(box, diameter / 2, diameter / 2)

    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(8, int(diameter * 0.68)))
    painter.setFont(font)
    painter.setPen(QColor(BADGE_TEXT))
    painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return _with_disabled(pixmap, name, size)
