"""App icon drawn in code, so no image files need packaging."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def app_icon(active: bool = True) -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        icon.addPixmap(_draw(size, active))
    return icon


def _draw(size: int, active: bool) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 32.0
    color = QColor("#2f6fdb") if active else QColor("#8a8f98")
    # Two stacked pages
    p.setPen(Qt.NoPen)
    p.setBrush(color.lighter(160))
    p.drawRoundedRect(QRectF(7 * s, 3 * s, 17 * s, 21 * s), 2 * s, 2 * s)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(4 * s, 6 * s, 17 * s, 21 * s), 2 * s, 2 * s)
    # Magnifier
    pen = QPen(QColor("white"), max(1.0, 2.2 * s))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(8 * s, 11 * s, 9 * s, 9 * s))
    pen.setColor(QColor("#1d1f23"))
    pen.setWidthF(max(1.5, 3.2 * s))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(int(19 * s), int(22 * s), int(27 * s), int(30 * s))
    p.end()
    return pm
