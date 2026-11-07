"""Small painted widgets; all audio work lives outside the UI thread."""
from __future__ import annotations

import math
from PySide6.QtCore import Qt, Signal, QSize, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy
from .theme import Palette


def clock_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


class Waveform(QWidget):
    seek = Signal(float)

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.palette = palette
        self.peaks: list[float] = []
        self.progress = 0.0
        self.recording = False
        self.phase = 0.0
        self.setMinimumHeight(125)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName('Audio waveform. Click to seek.')
        self.setToolTip('Click the waveform to seek')

    def set_audio(self, peaks: list[float]):
        self.peaks = peaks or []
        self.progress = 0
        self.update()

    def set_progress(self, value: float):
        self.progress = min(1.0, max(0.0, value))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.rect().adjusted(5, 12, -5, -12)
        mid = area.center().y()
        painter.setPen(QPen(QColor(self.palette.border), 1))
        painter.drawLine(area.left(), mid, area.right(), mid)
        count = max(1, area.width() // 6)
        if not self.peaks and not self.recording:
            return
        for i in range(count):
            if self.recording:
                # A clearly animated activity indicator, not fabricated audio samples.
                magnitude = 0.12 + 0.16 * (1 + math.sin(i * 0.33 + self.phase)) / 2
            else:
                begin = int(i * len(self.peaks) / count)
                end = max(begin + 1, int((i + 1) * len(self.peaks) / count))
                magnitude = max(self.peaks[begin:end], default=0.02)
            height = max(3, min(1.0, abs(magnitude)) * area.height() * 0.94)
            color = self.palette.red if self.recording else (self.palette.accent if i / count <= self.progress else self.palette.muted)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(area.left() + i * 6, mid - height / 2, 3, height), 1.5, 1.5)
        if not self.recording:
            x = area.left() + self.progress * area.width()
            painter.setPen(QPen(QColor(self.palette.accent), 1))
            painter.drawLine(int(x), area.top(), int(x), area.bottom())

    def mousePressEvent(self, event):
        if self.peaks and not self.recording and event.button() == Qt.MouseButton.LeftButton:
            self.seek.emit(min(1., max(0., (event.position().x() - 5) / max(1, self.width() - 10))))


class MemoRow(QWidget):
    def __init__(self, title: str, subtitle: str, duration: float, palette: Palette):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 13, 13, 13)
        layout.setSpacing(7)
        title_label = QLabel(title)
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        title_label.setStyleSheet('font-weight: 600;')
        title_label.setMinimumWidth(0)
        # Keep rows compact even when imported filenames are very long.
        title_label.setText(title if len(title) <= 25 else title[:24] + '…')
        layout.addWidget(title_label)
        line = QHBoxLayout()
        date = QLabel(subtitle)
        date.setStyleSheet(f'color: {palette.muted}; font-size: 10px;')
        time = QLabel(clock_text(duration))
        time.setStyleSheet(f'color: {palette.muted}; font-size: 10px;')
        line.addWidget(date)
        line.addStretch()
        line.addWidget(time)
        layout.addLayout(line)

    def sizeHint(self):
        return QSize(230, 73)


class ElidingLabel(QLabel):
    """Single-line title with the full name available in the tooltip."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, text):
        self.full_text = text
        self.setToolTip(text)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        self._elide()
        super().resizeEvent(event)
