"""Top-level window: one tab per detected amdgpu device."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget

from .. import __version__
from ..sysfs import discover_gpus, read_stats
from .monitor_tab import MonitorTab

POLL_INTERVAL_MS = 1000


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"RadeonPilot {__version__}")
        self.resize(1000, 760)

        self.gpus = discover_gpus()
        self.tabs: list[MonitorTab] = []

        if not self.gpus:
            msg = QLabel(
                "amdgpu ドライバで動作している AMD GPU が見つかりませんでした。\n"
                "/sys/class/drm/card*/device を確認してください。"
            )
            msg.setMargin(24)
            self.setCentralWidget(msg)
            return

        tab_widget = QTabWidget()
        for gpu in self.gpus:
            tab = MonitorTab(gpu)
            self.tabs.append(tab)
            tab_widget.addTab(tab, f"{gpu.card}: {gpu.name}")
            tab_widget.setTabToolTip(tab_widget.count() - 1, gpu.pci_address)
        self.setCentralWidget(tab_widget)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_INTERVAL_MS)
        self.refresh()

    def refresh(self) -> None:
        for tab in self.tabs:
            tab.update_stats(read_stats(tab.gpu))
