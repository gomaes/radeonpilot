"""Lightweight rolling line graph drawn with QPainter (no extra deps)."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

HISTORY_SECONDS = 60


class _Series:
    def __init__(self, name: str, color: str) -> None:
        self.name = name
        self.color = QColor(color)
        self.values: deque[float | None] = deque([None] * HISTORY_SECONDS, maxlen=HISTORY_SECONDS)


class RollingGraph(QWidget):
    """Plots the last HISTORY_SECONDS samples of one or more series."""

    def __init__(self, title: str, unit: str, y_max: float | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._fixed_max = y_max
        self._series: list[_Series] = []
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add_series(self, name: str, color: str) -> None:
        self._series.append(_Series(name, color))

    def push(self, values: dict[str, float | None]) -> None:
        for series in self._series:
            series.values.append(values.get(series.name))
        self.update()

    def _y_max(self) -> float:
        if self._fixed_max is not None:
            return self._fixed_max
        peak = max(
            (v for s in self._series for v in s.values if v is not None),
            default=0.0,
        )
        if peak <= 0:
            return 1.0
        # Round up to a "nice" number so the axis does not jitter.
        magnitude = 10 ** (len(str(int(peak))) - 1)
        return float(((int(peak * 1.1) // magnitude) + 1) * magnitude)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg = pal.color(QPalette.ColorRole.WindowText)
        grid = QColor(fg)
        grid.setAlpha(40)

        painter.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))

        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        fm = painter.fontMetrics()
        line_h = fm.height()

        margin_left = fm.horizontalAdvance("00000") + 8
        plot = QRectF(margin_left, line_h + 8, self.width() - margin_left - 8, self.height() - line_h * 2 - 14)
        if plot.width() <= 10 or plot.height() <= 10:
            return

        # Title
        painter.setPen(fg)
        painter.drawText(QPointF(6, line_h), f"{self._title} ({self._unit})")

        # Legend with latest values, right-aligned in the title row.
        x = self.width() - 6
        for series in reversed(self._series):
            latest = series.values[-1]
            text = f"{series.name}: {'—' if latest is None else f'{latest:.0f}'}"
            w = fm.horizontalAdvance(text)
            x -= w
            painter.setPen(series.color)
            painter.drawText(QPointF(x, line_h), text)
            x -= 12

        y_max = self._y_max()

        # Grid + y labels
        painter.setPen(QPen(grid, 1))
        steps = 4
        for i in range(steps + 1):
            y = plot.top() + plot.height() * i / steps
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            label = f"{y_max * (steps - i) / steps:.0f}"
            painter.setPen(fg)
            painter.drawText(
                QRectF(0, y - line_h / 2, margin_left - 4, line_h),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                label,
            )
            painter.setPen(QPen(grid, 1))
        painter.setPen(fg)
        painter.drawText(QPointF(plot.left(), plot.bottom() + line_h + 2), f"-{HISTORY_SECONDS}s")
        now = "now"
        painter.drawText(QPointF(plot.right() - fm.horizontalAdvance(now), plot.bottom() + line_h + 2), now)

        # Series
        dx = plot.width() / (HISTORY_SECONDS - 1)
        for series in self._series:
            path = QPainterPath()
            pen_down = False
            for i, v in enumerate(series.values):
                if v is None:
                    pen_down = False
                    continue
                ratio = min(max(v / y_max, 0.0), 1.0)
                pt = QPointF(plot.left() + i * dx, plot.bottom() - ratio * plot.height())
                if pen_down:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    pen_down = True
            painter.setPen(QPen(series.color, 2))
            painter.drawPath(path)
        painter.end()
