"""Render README screenshots from the emulator (no hardware needed).

    QT_QPA_PLATFORM=offscreen python scripts/screenshots.py docs/screenshots
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rp-shots-"))
    os.environ["XDG_CONFIG_HOME"] = str(work / "xdg-config")
    os.environ["XDG_DATA_HOME"] = str(work / "xdg-data")

    from PySide6.QtWidgets import QApplication

    from radeonpilot.daemon.controller import Controller
    from radeonpilot.daemon.profiles import ProfileStore
    from radeonpilot.daemon.server import DaemonServer
    from radeonpilot.emulator import EmulatedBackend, Simulator, build_tree
    from radeonpilot.launcher import AppEntry, AppStore
    from radeonpilot.protocol import DaemonClient

    app = QApplication([])

    def stack(name: str, od: bool):
        root = build_tree(work / name, od_enabled=od)
        os.environ["RADEONPILOT_SYSFS_ROOT"] = str(root)
        backend = EmulatedBackend(root)
        ctl = Controller(backend, root, ProfileStore(work / f"{name}.json"))
        srv = DaemonServer(work / f"{name}.sock", ctl, None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return backend, ctl, srv

    from radeonpilot.gui.main_window import MainWindow

    backend, ctl, srv = stack("od", True)
    ctl.set_od("0000:08:00.0", {"sclk_max": 2700, "voltage_offset": -50})
    ctl.set_fan_curve("0000:08:00.0", [[40, 20], [55, 35], [70, 55], [85, 80], [95, 100]])
    ctl.set_power_cap("0000:03:00.0", 290)
    ctl.save_profile("0000:08:00.0", "静音 + 省電力")
    ctl.set_boot_profile("0000:08:00.0", "静音 + 省電力")
    AppStore().save([
        AppEntry(name="My Game", command="'/home/user/Games/My Game/start.sh' --fullscreen", gpu="0000:03:00.0"),
        AppEntry(name="Blender", command="blender", gpu="0000:08:00.0"),
        AppEntry(name="vkcube", command="vkcube", gpu="0000:03:00.0"),
    ])

    w = MainWindow(DaemonClient(srv.socket_path))
    w.timer.stop()
    w.daemon_timer.stop()
    sim = Simulator(backend)
    for _ in range(60):
        sim.step(1.0)
        w.refresh()
    w.resize(1040, 820)
    w.show()

    def shot(name: str) -> None:
        app.processEvents()
        w.grab().save(str(out / name))
        print("wrote", out / name)

    w.tabs.setCurrentIndex(0)
    shot("monitor.png")
    w.tabs.setCurrentIndex(1)
    page = w.tabs.currentWidget()
    page.setCurrentIndex(1)
    shot("control.png")
    w.tabs.setCurrentIndex(2)
    w.launcher.table.selectRow(0)
    shot("launcher.png")
    w.close()
    srv.shutdown()

    _, _, srv2 = stack("nood", False)
    w2 = MainWindow(DaemonClient(srv2.socket_path))
    w2.timer.stop()
    w2.daemon_timer.stop()
    w2.resize(1040, 820)
    w2.show()
    w2.tabs.currentWidget().setCurrentIndex(1)
    app.processEvents()
    w2.grab().save(str(out / "od-disabled.png"))
    print("wrote", out / "od-disabled.png")
    srv2.shutdown()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots"))
