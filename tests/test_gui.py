"""Offscreen smoke tests of the GUI against the emulated daemon."""

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from radeonpilot.daemon.server import DaemonServer  # noqa: E402
from radeonpilot.protocol import DaemonClient  # noqa: E402
from tests.conftest import RX7900XTX, RX9070XT  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialogs(monkeypatch):
    """Auto-answer message boxes and record them."""
    seen = []

    def question(parent, title, text, *a, **k):
        seen.append(("question", title, text))
        return QMessageBox.StandardButton.Yes

    def record(kind):
        def fn(parent, title, text, *a, **k):
            seen.append((kind, title, text))
            return QMessageBox.StandardButton.Ok
        return fn

    monkeypatch.setattr(QMessageBox, "question", question)
    for kind in ("critical", "warning", "information"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    return seen


@pytest.fixture
def window(qapp, controller, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    srv = DaemonServer(tmp_path / "rp.sock", controller, None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from radeonpilot.gui.main_window import MainWindow

    w = MainWindow(DaemonClient(srv.socket_path))
    w.timer.stop()
    w.daemon_timer.stop()
    w.show()
    yield w
    w.close()
    srv.shutdown()
    srv.server_close()


def ctrl_for(window, pci):
    return next(c for c in window.controls if c.gpu.pci_address == pci)


def test_tabs(window):
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert labels == ["card1: AMD Radeon RX 9070 XT", "card2: AMD Radeon RX 7900 XTX", "ランチャー"]
    window.refresh()
    cell = window.monitors[0].cells["sclk"].value.text()
    assert cell.endswith("MHz")


def test_power_and_perf(window, controller, dialogs):
    c = ctrl_for(window, RX9070XT)
    assert c.daemon_ok and c.power_apply.isEnabled()
    assert (c.power_spin.minimum(), c.power_spin.maximum()) == (122, 334)  # 122 = 40 % of 304 (software limit)
    c.power_spin.setValue(290)
    c.apply_power()
    assert controller.state(RX9070XT)["power"]["current_w"] == 290
    c.perf_combo.setCurrentIndex(c.perf_combo.findData("high"))
    c.apply_perf()
    assert controller.state(RX9070XT)["perf_level"] == "high"
    assert not [d for d in dialogs if d[0] == "critical"]


def test_od_requires_confirmation(window, controller, dialogs, monkeypatch):
    c = ctrl_for(window, RX7900XTX)
    assert set(c.od_spins) == {"sclk_min", "sclk_max", "mclk_min", "mclk_max", "voltage_offset"}
    assert c.od_spins["sclk_max"].maximum() == 3000  # clamped to the driver range
    c.od_spins["sclk_max"].setValue(2750)
    c.od_spins["voltage_offset"].setValue(-40)

    # Declining the confirmation writes nothing.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    c.apply_od()
    assert controller.state(RX7900XTX)["od"]["values"]["sclk_max"] == 2500

    monkeypatch.setattr(QMessageBox, "question",
                        lambda p, t, text, *a, **k: dialogs.append(("question", t, text)) or QMessageBox.StandardButton.Yes)
    c.od_spins["sclk_max"].setValue(2750)
    c.od_spins["voltage_offset"].setValue(-40)
    c.apply_od()
    assert "2500 → 2750" in dialogs[-1][2]
    vals = controller.state(RX7900XTX)["od"]["values"]
    assert vals["sclk_max"] == 2750 and vals["voltage_offset"] == -40


def test_rdna4_offset_ui(window):
    c = ctrl_for(window, RX9070XT)
    assert "sclk_offset" in c.od_spins and "sclk_max" not in c.od_spins
    assert (c.od_spins["sclk_offset"].minimum(), c.od_spins["sclk_offset"].maximum()) == (-500, 1000)


def test_write_failure_is_reported(window, backend, dialogs):
    c = ctrl_for(window, RX7900XTX)
    backend.fail.add("pp_od_clk_voltage:c")
    c.od_spins["sclk_max"].setValue(2600)
    c.apply_od()
    backend.fail.clear()
    kind, title, text = dialogs[-1]
    assert kind == "critical" and "デフォルトに戻しました" in text
    assert c.od_spins["sclk_max"].value() == 2500  # UI refreshed from the reset state


def test_fan_curve(window, controller):
    c = ctrl_for(window, RX9070XT)
    assert "自動制御" in c.fan_status.text()
    c.apply_fan()
    st = controller.state(RX9070XT)["fan_curve"]
    assert not st["driver_default"]
    c.reset_fan()
    assert controller.state(RX9070XT)["fan_curve"]["driver_default"]


def test_profiles_and_reset(window, controller, monkeypatch, dialogs):
    c = ctrl_for(window, RX9070XT)
    c.power_spin.setValue(300)
    c.apply_power()
    monkeypatch.setattr("radeonpilot.gui.control_tab.QInputDialog.getText", lambda *a, **k: ("Quiet", True))
    c.save_profile()
    assert c.profile_combo.currentData() == "Quiet"
    c.boot_check.setChecked(True)
    c.toggle_boot(True)
    assert controller.list_profiles(RX9070XT)["boot_profile"] == "Quiet"
    c.reset_all()
    assert controller.state(RX9070XT)["power"]["current_w"] == 304
    c.apply_profile()
    assert controller.state(RX9070XT)["power"]["current_w"] == 300


def test_daemon_down_disables_controls(qapp, emu_root, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    from radeonpilot.gui.main_window import MainWindow

    w = MainWindow(DaemonClient(tmp_path / "missing.sock"))
    w.timer.stop()
    w.daemon_timer.stop()
    c = w.controls[0]
    assert not c.daemon_ok and not c.power_apply.isEnabled() and not c.od_box.isEnabled()
    assert not c.daemon_banner.isHidden()
    w.close()


def test_od_disabled_banner(qapp, tmp_path, monkeypatch):
    from radeonpilot.daemon.controller import Controller
    from radeonpilot.daemon.profiles import ProfileStore
    from radeonpilot.emulator import EmulatedBackend, build_tree
    from radeonpilot.gui.main_window import MainWindow

    root = build_tree(tmp_path / "s", od_enabled=False)
    monkeypatch.setenv("RADEONPILOT_SYSFS_ROOT", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ctl = Controller(EmulatedBackend(root), root, ProfileStore(tmp_path / "c.json"))
    srv = DaemonServer(tmp_path / "rp.sock", ctl, None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        w = MainWindow(DaemonClient(srv.socket_path))
        w.timer.stop()
        w.daemon_timer.stop()
        c = w.controls[0]
        assert not c.od_banner.isHidden() and "amdgpu.ppfeaturemask=0xffffffff" in c.od_banner.text()
        assert not c.od_box.isEnabled() and not c.fan_box.isEnabled()
        assert c.power_apply.isEnabled()  # power cap does not need OverDrive
        w.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_launcher_tab(window, tmp_path, monkeypatch):
    from radeonpilot.launcher import AppEntry

    lt = window.launcher
    assert lt.steam_text.text() == "DRI_PRIME=pci-0000_03_00_0 MESA_VK_DEVICE_SELECT=1002:7550 %command%"
    lt.steam_gpu.setCurrentIndex(1)
    assert "1002:744c" in lt.steam_text.text()
    lt.apps.append(AppEntry(name="vkcube", command="vkcube --c 100", gpu=RX7900XTX))
    lt._save()
    lt.table.selectRow(0)
    lt.create_desktop()
    files = list((tmp_path / "xdg-data/applications").glob("radeonpilot-vkcube-*.desktop"))
    assert len(files) == 1 and "MESA_VK_DEVICE_SELECT=1002:744c" in files[0].read_text()
    lt.copy_app_steam()
    assert QApplication.clipboard().text().startswith("DRI_PRIME=pci-0000_08_00_0 ")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    lt.delete_app()
    assert not files[0].exists() and lt.apps == []


def test_power_preset_minus_40_uses_software_limit(window, controller, dialogs):
    c = ctrl_for(window, RX9070XT)
    c.set_power_preset(-40)
    assert c.power_spin.value() == 182 and "-40%" in c.power_pct.text()
    c.apply_power()
    assert "ドライバの下限 274 W" in dialogs[-1][2]  # explained before applying
    st = controller.state(RX9070XT)
    assert st["power"]["current_w"] == 274 and st["soft_limit"]["target_w"] == 182
    assert "ソフトウェア制限: 目標 182 W" in c.soft_status.text()
    c.set_power_preset(-10)
    c.apply_power()
    assert controller.state(RX9070XT)["soft_limit"] is None


def test_decline_software_limit_writes_nothing(window, controller, backend, monkeypatch):
    c = ctrl_for(window, RX7900XTX)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    backend.writes.clear()
    c.set_power_preset(-40)
    c.apply_power()
    assert backend.writes == [] and controller.state(RX7900XTX)["soft_limit"] is None


def test_asleep_gpu_is_not_read(window, backend, monkeypatch):
    from radeonpilot import control, sysfs

    c = ctrl_for(window, RX7900XTX)
    gpu = c.gpu
    backend._state["gpus"]["7900xtx"]["sim"]["asleep"] = True
    (gpu.device_path / "power/runtime_status").write_text("suspended\n")
    reads = []
    monkeypatch.setattr(control, "read_control_state", lambda *a, **k: reads.append(1))
    c.refresh()
    assert reads == [] and not c.sleep_box.isHidden() and not c.od_box.isEnabled()
    window.tabs.setCurrentIndex(1)
    window.pages[1].setCurrentIndex(0)
    window.refresh()
    assert window.monitors[1].cells["sclk"].value.text() == "スリープ中"
    assert "スリープ中" in window.tabs.tabText(1)
    monkeypatch.undo()
    c.wake_gpu()  # through the daemon
    assert not sysfs.is_asleep(gpu) and c.sleep_box.isHidden() and c.od_box.isEnabled()


def test_only_visible_gpu_is_polled(window, monkeypatch):
    import radeonpilot.gui.main_window as mw
    from radeonpilot.sysfs import GpuStats

    polled = []
    monkeypatch.setattr(mw, "read_stats", lambda g: polled.append(g.pci_address) or GpuStats())
    window.tabs.setCurrentIndex(0)
    window.pages[0].setCurrentIndex(0)
    window.refresh()
    assert polled == [RX9070XT]
    window.pages[0].setCurrentIndex(1)  # control sub-tab: no monitor polling
    window.refresh()
    window.tabs.setCurrentIndex(2)      # launcher
    window.refresh()
    window.showMinimized()
    window.tabs.setCurrentIndex(0)
    window.pages[0].setCurrentIndex(0)
    window.refresh()
    assert polled == [RX9070XT]


def test_memory_stage_checkbox(window, controller, dialogs):
    c = ctrl_for(window, RX9070XT)
    assert not c.mem_stage_check.isChecked() and c.mem_stage_check.isEnabled()
    c.power_spin.setValue(150)
    c.apply_power()
    assert "メモリクロックは定格のまま" in dialogs[-1][2]
    assert controller.state(RX9070XT)["soft_limit"]["memory"] is None
    c.mem_stage_check.setChecked(True)
    c.apply_power()
    assert "メモリクロック上限も" in dialogs[-1][2]
    assert controller.state(RX9070XT)["soft_limit"]["memory"]["floor"] == 456
    assert c.mem_stage_check.isChecked()  # reflects the daemon state after refresh


def test_base_clock_pin_checkbox(window, controller, dialogs):
    c = ctrl_for(window, RX9070XT)
    assert not c.pin_stage_check.isChecked() and c.pin_stage_check.isEnabled()
    c.power_spin.setValue(150)
    c.apply_power()
    assert "ベースクロック固定は使いません" in dialogs[-1][2]
    assert controller.state(RX9070XT)["soft_limit"]["base_clock_pin"] is None
    c.pin_stage_check.setChecked(True)
    c.apply_power()
    assert "profile_standard" in dialogs[-1][2]
    assert controller.state(RX9070XT)["soft_limit"]["base_clock_pin"] == {"active": False, "restore_level": "auto"}
    assert c.pin_stage_check.isChecked()
    c.pin_stage_check.setChecked(False)  # switch it off again
    c.apply_power()
    assert controller.state(RX9070XT)["soft_limit"]["base_clock_pin"] is None


def test_perf_combo_shows_user_level_while_pinned(window, controller, backend):
    c = ctrl_for(window, RX9070XT)
    controller.set_power_target(RX9070XT, 150, allow_base_clock=True)
    lim = controller.limiter.get(RX9070XT)
    gpu = c.gpu
    lim.current = lim.floor
    (gpu.device_path / "gpu_busy_percent").write_text("95\n")
    (gpu.hwmon_path / "power1_input").write_text("220000000\n")
    controller.limiter._sample(gpu, lim, 1e9)
    assert (gpu.device_path / "power_dpm_force_performance_level").read_text().strip() == "profile_standard"
    c.refresh()
    assert c.perf_combo.currentData() == "auto"
    assert "ベースクロック固定中" in c.soft_status.text()
