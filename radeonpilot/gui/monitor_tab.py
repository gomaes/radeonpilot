"""Per-GPU monitoring page: current values + 60 second graphs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..sysfs import GpuInfo, GpuStats
from .graph import RollingGraph

TEMP_LABELS = ("edge", "junction", "mem")

COLORS = {
    "core": "#e8433a",
    "mem": "#3a8ee8",
    "power": "#f0a020",
    "cap": "#888888",
    "load": "#3ab86a",
    "vram": "#a05ae0",
    "edge": "#3ab86a",
    "junction": "#e8433a",
    "mem_temp": "#a05ae0",
    "fan": "#20b0c0",
}


def _fmt(value, spec: str, unit: str) -> str:
    return "—" if value is None else f"{value:{spec}} {unit}"


def _gib(n: int | None) -> str:
    return "—" if n is None else f"{n / 1024**3:.2f} GiB"


class _ValueCell(QFrame):
    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        cap = QLabel(caption)
        cap.setStyleSheet("color: palette(mid);")
        self.value = QLabel("—")
        font = self.value.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.value.setFont(font)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(cap)
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        self.value.setText(text)


class MonitorTab(QWidget):
    def __init__(self, gpu: GpuInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.gpu = gpu
        root = QVBoxLayout(self)

        header = QLabel(
            f"<b>{gpu.name}</b><br>"
            f"PCI: {gpu.pci_address} &nbsp; ID: {gpu.vendor_id:04x}:{gpu.device_id:04x}"
            f" &nbsp; {gpu.card}"
            + ("" if gpu.hwmon_path else " &nbsp; <i>(hwmon なし)</i>")
        )
        header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(header)

        values = QGroupBox("現在値")
        grid = QGridLayout(values)
        self.cells: dict[str, _ValueCell] = {}
        cells = [
            ("sclk", "コアクロック"),
            ("mclk", "メモリクロック"),
            ("power", "消費電力"),
            ("load", "GPU負荷"),
            ("vram", "VRAM"),
            ("fan", "ファン"),
            ("edge", "温度 edge"),
            ("junction", "温度 junction"),
            ("mem", "温度 mem"),
        ]
        for i, (key, caption) in enumerate(cells):
            cell = _ValueCell(caption)
            self.cells[key] = cell
            grid.addWidget(cell, i // 5, i % 5)
        root.addWidget(values)

        graphs = QGridLayout()
        self.g_clock = RollingGraph("クロック", "MHz")
        self.g_clock.add_series("core", COLORS["core"])
        self.g_clock.add_series("mem", COLORS["mem"])
        self.g_power = RollingGraph("消費電力", "W")
        self.g_power.add_series("power", COLORS["power"])
        self.g_power.add_series("cap", COLORS["cap"])
        self.g_load = RollingGraph("負荷 / VRAM", "%", y_max=100)
        self.g_load.add_series("load", COLORS["load"])
        self.g_load.add_series("vram", COLORS["vram"])
        self.g_temp = RollingGraph("温度", "°C", y_max=120)
        for label in TEMP_LABELS:
            self.g_temp.add_series(label, COLORS["mem_temp" if label == "mem" else label])
        self.g_fan = RollingGraph("ファン", "RPM")
        self.g_fan.add_series("fan", COLORS["fan"])

        graphs.addWidget(self.g_clock, 0, 0)
        graphs.addWidget(self.g_power, 0, 1)
        graphs.addWidget(self.g_load, 1, 0)
        graphs.addWidget(self.g_temp, 1, 1)
        graphs.addWidget(self.g_fan, 2, 0, 1, 2)
        root.addLayout(graphs, 1)

    def update_stats(self, s: GpuStats) -> None:
        self.cells["sclk"].set(_fmt(s.sclk_mhz, "d", "MHz"))
        self.cells["mclk"].set(_fmt(s.mclk_mhz, "d", "MHz"))
        power = _fmt(s.power_w, ".1f", "W")
        if s.power_w is not None and s.power_cap_w is not None:
            power += f" / {s.power_cap_w:.0f} W"
        self.cells["power"].set(power)
        self.cells["load"].set(_fmt(s.busy_percent, "d", "%"))
        if s.vram_used is not None and s.vram_total:
            self.cells["vram"].set(f"{_gib(s.vram_used)} / {_gib(s.vram_total)}")
        else:
            self.cells["vram"].set("—")
        self.cells["fan"].set(_fmt(s.fan_rpm, "d", "RPM"))
        for label in TEMP_LABELS:
            self.cells[label].set(_fmt(s.temps_c.get(label), ".0f", "°C"))

        self.g_clock.push({"core": s.sclk_mhz, "mem": s.mclk_mhz})
        self.g_power.push({"power": s.power_w, "cap": s.power_cap_w})
        self.g_load.push({"load": s.busy_percent, "vram": s.vram_percent})
        self.g_temp.push({label: s.temps_c.get(label) for label in TEMP_LABELS})
        self.g_fan.push({"fan": s.fan_rpm})
