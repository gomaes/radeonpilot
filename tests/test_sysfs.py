import os

import pytest

from radeonpilot import sysfs
from radeonpilot.emulator import build_tree
from tests.conftest import RX7900XTX, RX9070XT


def test_parse_dpm_clock():
    assert sysfs.parse_dpm_clock("0: 500Mhz\n1: 2310Mhz *\n") == 2310
    assert sysfs.parse_dpm_clock("0: 500Mhz\n") is None
    assert sysfs.parse_dpm_clock(None) is None
    assert sysfs.parse_dpm_clock("S: 19Mhz *\n0: 500Mhz\n") == 19


def test_discover(emu_root):
    gpus = sysfs.discover_gpus(emu_root)
    assert [(g.card, g.pci_address) for g in gpus] == [("card1", RX9070XT), ("card2", RX7900XTX)]
    rdna4, rdna3 = gpus
    assert rdna4.name == "AMD Radeon RX 9070 XT"
    assert rdna3.name == "AMD Radeon RX 7900 XTX"
    assert rdna4.dri_prime_id == "pci-0000_03_00_0"
    assert rdna4.vk_device_select == "1002:7550"
    assert rdna3.vk_device_select == "1002:744c"
    assert rdna4.hwmon_path.name == "hwmon2"


def test_non_amd_ignored(tmp_path):
    root = build_tree(tmp_path, gpus=())
    dev = root / "sys/devices/pci0000:00/0000:01:00.0"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text("0x10de\n")
    (dev / "device").write_text("0x2684\n")
    (root / "sys/class/drm/card3").mkdir()
    os.symlink(dev, root / "sys/class/drm/card3/device")
    assert sysfs.discover_gpus(root) == []


def test_read_stats(emu_root):
    for g in sysfs.discover_gpus(emu_root):
        s = sysfs.read_stats(g)
        assert s.sclk_mhz and s.mclk_mhz
        assert s.power_w is not None and s.power_w > 0  # 9070 XT: power1_input, 7900 XTX: power1_average
        assert s.power_cap_w in (304, 339)
        assert s.busy_percent is not None
        assert 0 < s.vram_percent < 100
        assert s.fan_rpm is not None
        assert set(s.temps_c) == {"edge", "junction", "mem"}


def test_stats_without_hwmon(emu_root):
    g = sysfs.discover_gpus(emu_root)[0]
    g.hwmon_path = None
    s = sysfs.read_stats(g)
    assert s.power_w is None and s.temps_c == {} and s.sclk_mhz
