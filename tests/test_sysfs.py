from pathlib import Path

import pytest

from radeonpilot import sysfs
from tests.fake_sysfs import add_gpu, build_default


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return build_default(tmp_path)


def test_parse_dpm_clock():
    assert sysfs.parse_dpm_clock("0: 500Mhz\n1: 2310Mhz *\n") == 2310
    assert sysfs.parse_dpm_clock("0: 500Mhz\n") is None
    assert sysfs.parse_dpm_clock(None) is None
    assert sysfs.parse_dpm_clock("S: 19Mhz *\n0: 500Mhz\n") == 19


def test_discover(root):
    gpus = sysfs.discover_gpus(root)
    assert [g.card for g in gpus] == ["card0", "card1"]
    g = gpus[0]
    assert g.pci_address == "0000:03:00.0"
    assert g.name == "AMD Radeon RX 9070 XT"
    assert g.dri_prime_id == "pci-0000_03_00_0"
    assert g.vk_device_select == "1002:7550"
    assert gpus[1].name  # product_name missing: pci.ids lookup or fallback
    assert gpus[1].hwmon_path is None


def test_non_amd_ignored(tmp_path):
    add_gpu(tmp_path, 0, "0000:01:00.0", 0x2684, vendor=0x10DE)
    assert sysfs.discover_gpus(tmp_path) == []


def test_read_stats(root):
    g = sysfs.discover_gpus(root)[0]
    s = sysfs.read_stats(g)
    assert s.sclk_mhz == 2310
    assert s.mclk_mhz == 1250
    assert s.power_w == pytest.approx(245.0)
    assert s.power_cap_w == pytest.approx(263.0)
    assert s.busy_percent == 87
    assert s.vram_percent == pytest.approx(37.5)
    assert s.fan_rpm == 1450
    assert s.temps_c == {"edge": 62.0, "junction": 78.0, "mem": 70.0}


def test_power_input_fallback(root):
    g = sysfs.discover_gpus(root)[0]
    (g.hwmon_path / "power1_average").unlink()
    (g.hwmon_path / "power1_input").write_text("12000000\n")
    assert sysfs.read_stats(g).power_w == pytest.approx(12.0)


def test_stats_without_hwmon(root):
    g = sysfs.discover_gpus(root)[1]
    s = sysfs.read_stats(g)
    assert s.power_w is None and s.temps_c == {} and s.sclk_mhz == 2310
