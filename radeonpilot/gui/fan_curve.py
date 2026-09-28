"""Fan curve editor: spin boxes per point plus a live preview."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QSpinBox, QWidget

from ..control import Range


class FanCurvePlot(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points: list[tuple[int, int]] = []
        self.temp_range = Range(20, 100)
        self.setMinimumSize(260, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points, temp_range: Range) -> None:
        self.points = list(points)
        self.temp_range = temp_range
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        grid = QColor(fg)
        grid.setAlpha(40)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        fm = p.fontMetrics()
        left = fm.horizontalAdvance("100%") + 8
        plot = QRectF(left, 8, self.width() - left - 10, self.height() - fm.height() - 16)
        t_lo, t_hi = 0, max(self.temp_range.hi, 100)

        def pt(t, pwm):
            return QPointF(
                plot.left() + plot.width() * (t - t_lo) / (t_hi - t_lo),
                plot.bottom() - plot.height() * pwm / 100,
            )

        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(fg)
            p.drawText(QRectF(0, y - fm.height() / 2, left - 4, fm.height()),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"{100 - i * 25}%")
        for t in range(t_lo, t_hi + 1, 20):
            x = pt(t, 0).x()
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(fg)
            label = f"{t}°C"
            p.drawText(QPointF(x - fm.horizontalAdvance(label) / 2, plot.bottom() + fm.height() + 2), label)

        if not self.points:
            return
        color = QColor("#20b0c0")
        path = QPainterPath(pt(t_lo, self.points[0][1]))
        for t, pwm in self.points:
            path.lineTo(pt(t, pwm))
        path.lineTo(pt(t_hi, self.points[-1][1]))
        p.setPen(QPen(color, 2))
        p.drawPath(path)
        p.setBrush(color)
        for t, pwm in self.points:
            p.drawEllipse(pt(t, pwm), 4, 4)
        p.end()


class FanCurveEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.addWidget(QLabel("点"), 0, 0)
        self._grid.addWidget(QLabel("温度 (junction)"), 0, 1)
        self._grid.addWidget(QLabel("ファン"), 0, 2)
        self._rows: list[tuple[QSpinBox, QSpinBox]] = []
        self.plot = FanCurvePlot()
        self.temp_range = Range(0, 100)
        layout.addWidget(self._grid_host)
        layout.addWidget(self.plot, 1)

    def set_curve(self, points, temp_range: Range, pwm_range: Range) -> None:
        self.temp_range = temp_range
        while len(self._rows) < len(points):
            i = len(self._rows)
            t, w = QSpinBox(), QSpinBox()
            t.setSuffix(" °C")
            w.setSuffix(" %")
            t.valueChanged.connect(self._on_change)
            w.valueChanged.connect(self._on_change)
            self._grid.addWidget(QLabel(str(i)), i + 1, 0)
            self._grid.addWidget(t, i + 1, 1)
            self._grid.addWidget(w, i + 1, 2)
            self._rows.append((t, w))
        for (t_spin, w_spin), (temp, pwm) in zip(self._rows, points):
            for spin in (t_spin, w_spin):
                spin.blockSignals(True)
            t_spin.setRange(temp_range.lo, temp_range.hi)
            w_spin.setRange(pwm_range.lo, pwm_range.hi)
            t_spin.setValue(temp)
            w_spin.setValue(pwm)
            for spin in (t_spin, w_spin):
                spin.blockSignals(False)
        self._on_change()

    def points(self) -> list[list[int]]:
        return [[t.value(), w.value()] for t, w in self._rows]

    def _on_change(self) -> None:
        self.plot.set_points([tuple(p) for p in self.points()], self.temp_range)
        self.changed.emit()
