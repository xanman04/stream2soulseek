"""Small palette-aware line icons shared by native navigation and actions."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication


def icon(name: str, size: int = 20, primary: bool = False, checked_primary: bool = False) -> QIcon:
    app = QApplication.instance()
    colors = app.property("s2sColors") or {}
    result = QIcon()
    for mode, state, color in (
        (QIcon.Mode.Normal, QIcon.State.Off, "#ffffff" if primary else app.palette().color(QPalette.ColorRole.ButtonText)),
        (QIcon.Mode.Normal, QIcon.State.On, "#ffffff" if checked_primary else colors.get("selected", "#e8edf3")),
        (QIcon.Mode.Disabled, QIcon.State.Off, colors.get("disabled", "#73808e")),
    ):
        from PySide6.QtGui import QColor
        pix = QPixmap(size * 2, size * 2)
        pix.setDevicePixelRatio(2)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(size / 24, size / 24)
        p.setPen(QPen(QColor(color), 1.65, Qt.PenStyle.SolidLine,
                     Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if name == "home":
            path.moveTo(3, 10); path.lineTo(12, 3); path.lineTo(21, 10)
            path.moveTo(5, 9); path.lineTo(5, 21); path.lineTo(10, 21)
            path.lineTo(10, 15); path.lineTo(14, 15); path.lineTo(14, 21)
            path.lineTo(19, 21); path.lineTo(19, 9)
        elif name in ("search", "quick"):
            p.drawEllipse(QRectF(4, 4, 12, 12))
            path.moveTo(15, 15); path.lineTo(21, 21)
            if name == "quick":
                path.moveTo(10, 7); path.lineTo(10, 13)
                path.moveTo(7, 10); path.lineTo(13, 10)
        elif name == "playlist":
            for y in (5, 10, 15):
                path.moveTo(4, y); path.lineTo(14 if y < 15 else 10, y)
            path.moveTo(19, 10); path.lineTo(19, 18)
            p.drawEllipse(QRectF(14, 17, 5, 4))
            path.moveTo(19, 10); path.lineTo(22, 9)
        elif name == "transfer":
            path.moveTo(7, 3); path.lineTo(7, 19); path.moveTo(3, 15)
            path.lineTo(7, 19); path.lineTo(11, 15)
            path.moveTo(17, 21); path.lineTo(17, 5); path.moveTo(13, 9)
            path.lineTo(17, 5); path.lineTo(21, 9)
        elif name == "settings":
            for x, y in ((5, 8), (12, 16), (19, 9)):
                path.moveTo(x, 3); path.lineTo(x, y - 2)
                path.moveTo(x, y + 2); path.lineTo(x, 21)
                p.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
        elif name == "filter":
            for y, left, right in ((5, 3, 21), (12, 6, 18), (19, 10, 14)):
                path.moveTo(left, y); path.lineTo(right, y)
        elif name == "connection":
            p.drawEllipse(QRectF(9, 9, 6, 6))
            path.moveTo(6, 6); path.cubicTo(1, 9, 1, 15, 6, 18)
            path.moveTo(18, 6); path.cubicTo(23, 9, 23, 15, 18, 18)
        elif name in ("next", "back"):
            x, other = (9, 15) if name == "next" else (15, 9)
            path.moveTo(x, 5); path.lineTo(other, 12); path.lineTo(x, 19)
        elif name == "close":
            path.moveTo(7, 7); path.lineTo(17, 17)
            path.moveTo(7, 17); path.lineTo(17, 7)
        else:  # forward action
            path.moveTo(4, 12); path.lineTo(20, 12)
            path.moveTo(14, 6); path.lineTo(20, 12); path.lineTo(14, 18)
        p.drawPath(path)
        p.end()
        result.addPixmap(pix, mode, state)
    return result


def set_icon(button, name, size=20, primary=False, checked_primary=False):
    spec = [name, size, primary, checked_primary]
    button.setProperty("s2sIcon", spec)
    button.setIcon(icon(*spec))
