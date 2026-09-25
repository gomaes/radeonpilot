"""Top-level window: one tab per detected amdgpu device plus the launcher."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget

from .. import __version__
from ..protocol import DaemonClient
from ..sysfs import discover_gpus, read_stats
from .control_tab import ControlTab
from .launcher_tab import LauncherTab
from .monitor_tab import MonitorTab

POLL_INTERVAL_MS = 1000
DAEMON_CHECK_MS = 5000
ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "radeonpilot.svg"


class MainWindow(QMainWindow):
    def __init__(self, client: DaemonClient | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"RadeonPilot {__version__}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1040, 820)
        self.client = client or DaemonClient()

        self.gpus = discover_gpus()
        self.monitors: list[MonitorTab] = []
        self.controls: list[ControlTab] = []

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        if not self.gpus:
            msg = QLabel(
                "amdgpu ドライバで動作している AMD GPU が見つかりませんでした。\n"
                "/sys/class/drm/card*/device を確認してください。"
            )
            msg.setMargin(24)
            self.tabs.addTab(msg, "GPUなし")

        for gpu in self.gpus:
            page = QTabWidget()
            monitor = MonitorTab(gpu)
            ctrl = ControlTab(gpu, self.client)
            ctrl.status_message.connect(self.show_status)
            page.addTab(monitor, "監視")
            page.addTab(ctrl, "制御")
            page.currentChanged.connect(lambda idx, c=ctrl: c.refresh() if idx == 1 else None)
            self.monitors.append(monitor)
            self.controls.append(ctrl)
            self.tabs.addTab(page, f"{gpu.card}: {gpu.name}")
            self.tabs.setTabToolTip(self.tabs.count() - 1, f"{gpu.pci_address}  [{gpu.vk_device_select}]")

        self.launcher = LauncherTab(self.gpus)
        self.launcher.status_message.connect(self.show_status)
        self.tabs.addTab(self.launcher, "ランチャー")

        self.daemon_label = QLabel()
        self.statusBar().addPermanentWidget(self.daemon_label)
        self.check_daemon()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_INTERVAL_MS)
        self.daemon_timer = QTimer(self)
        self.daemon_timer.timeout.connect(self.check_daemon)
        self.daemon_timer.start(DAEMON_CHECK_MS)
        self.refresh()

    def refresh(self) -> None:
        for tab in self.monitors:
            tab.update_stats(read_stats(tab.gpu))

    def check_daemon(self) -> None:
        ok, err = self.client.available()
        self.daemon_label.setText("デーモン: 接続中" if ok else "デーモン: 未接続（表示のみ）")
        self.daemon_label.setToolTip("" if ok else (err or ""))
        for ctrl in self.controls:
            if ctrl.daemon_ok != ok:
                ctrl.refresh()

    def show_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)
