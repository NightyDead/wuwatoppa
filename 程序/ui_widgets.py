"""Painted chrome and the global stylesheet, following the Tk version's rhythm."""
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QFrame, QSlider, QWidget)

import appearance
import ui_theme


def app_qss(palette, family, point, scale):
    """Qt stylesheet built from the resolved palette, at the chosen font size."""
    p = palette
    def px(value): return round(value * scale)
    radius = px(6)
    return f'''
QWidget {{ color: {p['text']}; font-family: "{family}"; font-size: {point}pt; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {p['muted']}; }}
QLabel#hint {{ color: {p['muted']}; }}
QLabel#status {{ color: {p['success']}; }}
QLabel#count, QLabel#countTitle {{ color: {p['accent']}; font-size: {point + 8}pt;
    font-weight: bold; }}
QLabel#brand {{ font-size: {point + 10}pt; font-weight: bold; }}
QLabel#pageTitle {{ font-size: {point + 8}pt; font-weight: bold; }}
QFrame#line {{ background: {p['border']}; border: none; }}
QLineEdit {{
    background: {p['field']}; border: 1px solid {p['border']}; border-radius: {radius}px;
    padding: {px(2)}px {px(7)}px; color: {p['text']};
    selection-background-color: {p['accent']}; selection-color: {p['accent_text']};
}}
QLineEdit:focus {{ border-color: {p['accent']}; }}
QLineEdit:disabled {{ color: {p['muted']}; }}
QPushButton, QToolButton {{
    background: {p['field']}; border: 1px solid {p['border']}; border-radius: {radius}px;
    padding: {px(4)}px {px(10)}px; color: {p['text']};
}}
QPushButton:hover, QToolButton:hover {{ background: {p['selection']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {p['muted']}; border-color: {p['border']}; }}
QPushButton#accent {{ background: {p['accent']}; border-color: {p['accent']}; color: {p['accent_text']}; }}
QPushButton#accent:hover {{ background: {appearance.blend(p['accent'], p['panel'], .85)}; }}
QPushButton#danger {{ color: {p['danger']}; }}
QPushButton:checked {{ background: {p['selection']}; border-color: {p['accent']}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QComboBox {{
    background: {p['field']}; border: 1px solid {p['border']}; border-radius: {radius}px;
    padding: {px(2)}px {px(8)}px; color: {p['text']};
}}
QComboBox:focus {{ border-color: {p['accent']}; }}
QComboBox QAbstractItemView {{
    background: {p['field']}; color: {p['text']}; border: 1px solid {p['border']};
    selection-background-color: {p['selection']}; selection-color: {p['text']};
}}
QPlainTextEdit#log {{
    background: {p['field']}; border: 1px solid {p['border']}; border-radius: {radius}px;
    color: {p['text']}; padding: {px(5)}px {px(7)}px;
    selection-background-color: {p['selection']}; selection-color: {p['text']};
}}
QTableWidget {{
    background: {p['field']}; border: 1px solid {p['border']}; border-radius: {radius}px;
    gridline-color: transparent; color: {p['text']};
    selection-background-color: {p['selection']}; selection-color: {p['text']};
}}
QTableWidget::item {{ padding: {px(4)}px {px(8)}px; }}
QHeaderView::section {{
    background: {p['panel']}; color: {p['muted']}; border: none;
    padding: {px(5)}px {px(7)}px;
}}
QTableCornerButton::section {{ background: {p['panel']}; border: none; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget, QWidget#page {{ background: transparent; }}
QDialog {{ background: {p['panel']}; }}
QScrollBar:vertical {{ background: transparent; width: {px(8)}px; margin: {px(2)}px; }}
QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: {px(4)}px;
    min-height: {px(24)}px; }}
QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: {px(8)}px; margin: {px(2)}px; }}
QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: {px(4)}px;
    min-width: {px(24)}px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSlider::groove:horizontal {{ height: {px(4)}px; background: {p['border']};
    border-radius: {px(2)}px; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: {px(2)}px; }}
QSlider::handle:horizontal {{ width: {px(14)}px; height: {px(14)}px;
    margin: -{px(5)}px 0; border-radius: {px(7)}px;
    background: {p['panel']}; border: {px(3)}px solid {p['accent']}; }}
QMenu {{ background: {p['panel']}; border: 1px solid {p['border']}; padding: {px(4)}px; }}
QMenu::item {{ padding: {px(5)}px {px(16)}px; border-radius: {px(4)}px; }}
QMenu::item:selected {{ background: {p['selection']}; color: {p['text']}; }}
QMenu::separator {{ height: 1px; background: {p['border']};
    margin: {px(4)}px {px(8)}px; }}
QToolTip {{ background: {p['panel']}; color: {p['text']};
    border: 1px solid {p['border']}; padding: 4px 8px; }}
'''


class Panel(QWidget):
    """Translucent plate behind the sidebar, the page and the footer."""

    def __init__(self, palette=None, radius=0, parent=None):
        super().__init__(parent)
        self.palette = palette or {}
        self.radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def set_palette(self, palette):
        self.palette = palette
        self.update()

    def paintEvent(self, event):
        if not self.palette: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self.palette['panel'])
        color.setAlphaF(max(0.0, min(1.0, self.palette['panel_opacity'])))
        rect = QRectF(self.rect())
        if self.radius:
            path = ui_theme.rounded_path(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                                         self.radius)
            painter.fillPath(path, color)
        else:
            painter.fillRect(rect, color)
        painter.end()


class Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('line')
        self.setFixedHeight(1)


class NoWheelSlider(QSlider):
    """Leaving the wheel to the page keeps scrolling from changing the value."""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    """Same as NoWheelSlider: the wheel scrolls the page, not the selection."""

    def wheelEvent(self, event):
        event.ignore()


class NavItem(QAbstractButton):
    """Sidebar row: drawn icon, accent marker and bold text when selected."""

    ICONS = ('clicker', 'collector', 'plan', 'history', 'appearance')

    def __init__(self, key, title, parent=None):
        super().__init__(parent)
        if key not in self.ICONS: raise ValueError('未知导航图标。')
        self.key, self.title = key, title
        self.collapsed = False
        self.palette = {}
        self.scale = 1.0
        self.family = 'Microsoft YaHei UI'
        self.point = 10
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    @staticmethod
    def icon_path(kind):
        path = QPainterPath()
        if kind == 'clicker':
            points = (5, 3, 18, 13, 12.7, 14, 15.8, 20, 12.8, 21, 10, 15, 6, 19)
            path.moveTo(points[0], points[1])
            for index in range(2, len(points), 2):
                path.lineTo(points[index], points[index+1])
            path.closeSubpath()
        elif kind == 'collector':
            path.addRoundedRect(QRectF(4, 10, 16, 11), 1.5, 1.5)
            path.addRoundedRect(QRectF(3, 7, 18, 4), 1.5, 1.5)
            path.addEllipse(QRectF(6, 2, 6, 5))
            path.addEllipse(QRectF(12, 2, 6, 5))
        elif kind == 'plan':
            path.addEllipse(QRectF(3, 3, 18, 18))
        elif kind == 'history':
            path.moveTo(6, 2); path.lineTo(15, 2); path.lineTo(19, 6)
            path.lineTo(19, 21); path.lineTo(6, 21); path.closeSubpath()
            path.moveTo(15, 2); path.lineTo(15, 6); path.lineTo(19, 6)
        else:
            points = (8, 3, 10, 6, 14, 6, 16, 3, 22, 7, 19, 12, 17, 11, 17, 21,
                      7, 21, 7, 11, 5, 12, 2, 7)
            path.moveTo(points[0], points[1])
            for index in range(2, len(points), 2):
                path.lineTo(points[index], points[index+1])
            path.closeSubpath()
        return path

    def set_style(self, palette, family, point, scale, collapsed):
        self.palette, self.family, self.point = palette, family, point
        self.scale, self.collapsed = scale, collapsed
        self.update()

    def paintEvent(self, event):
        if not self.palette: return
        p, scale = self.palette, self.scale
        width, height = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        active = self.isChecked() or self.underMouse()
        if active:
            painter.fillRect(QRectF(0, 0, width, height),
                             QColor(p.get('nav_selection', p['selection'])))
        if self.isChecked():
            painter.fillRect(QRectF(0, 0, max(3, round(3*scale)), height),
                             QColor(p.get('nav_accent', p['accent'])))
        color = QColor(p.get('nav_accent', p['accent']) if self.isChecked() else p['text'])
        size = max(16.0, 20*scale)
        painter.save()
        painter.translate(width/2 - size/2 if self.collapsed else 25*scale - size/2,
                          height/2 - size/2)
        painter.scale(size/24, size/24)
        painter.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(color if self.isChecked() else Qt.BrushStyle.NoBrush)
        painter.drawPath(self.icon_path(self.key))
        painter.restore()
        if not self.collapsed:
            font = QFont(self.family, self.point)
            font.setBold(self.isChecked())
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(QRectF(45*scale, 0, width-45*scale-8, height),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self.title)
        painter.end()


class WindowButton(QAbstractButton):
    """Minimise / maximise / close, drawn with one line weight."""

    def __init__(self, shape, parent=None):
        super().__init__(parent)
        self.shape = shape
        self.hover = False
        self.maximized = False
        self.palette = {}
        self.scale = 1.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip({'min': '收进通知区域', 'max': '最大化', 'close': '关闭'}[shape])

    def set_style(self, palette, scale):
        self.palette, self.scale = palette, scale
        self.setFixedSize(round(30*scale), round(22*scale))
        self.update()

    def enterEvent(self, event):
        self.hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self.palette: return
        p, scale = self.palette, self.scale
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p['danger'] if self.shape == 'close' else p['selection']))
            painter.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                                    round(6*scale), round(6*scale))
        color = QColor(p.get('accent_text', '#ffffff') if self.hover else p['muted'])
        pen = QPen(color, max(1.0, 1.4*scale))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.scale(scale, scale)
        if self.shape == 'min':
            painter.drawLine(QPointF(9, 11), QPointF(21, 11))
        elif self.shape == 'max' and self.maximized:
            painter.drawRect(QRectF(12, 7, 9, 9))
            painter.drawRect(QRectF(8, 11, 9, 9))
        elif self.shape == 'max':
            painter.drawRect(QRectF(10.5, 7.5, 9, 9))
        else:
            painter.drawLine(QPointF(10, 7), QPointF(20, 17))
            painter.drawLine(QPointF(20, 7), QPointF(10, 17))
        painter.end()


class DragFilter(QObject):
    """Move the frameless window from its blank chrome; double-click zooms."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.offset = None

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.offset = (event.globalPosition().toPoint()
                           - self.window.frameGeometry().topLeft())
            return True
        if kind == QEvent.Type.MouseMove and self.offset is not None:
            if not getattr(self.window, 'maximized', False):
                self.window.move(event.globalPosition().toPoint() - self.offset)
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            self.offset = None
            return True
        if kind == QEvent.Type.MouseButtonDblClick:
            self.window.toggle_max()
            return True
        return False
