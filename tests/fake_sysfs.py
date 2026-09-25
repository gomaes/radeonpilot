"""Build a fake amdgpu sysfs tree for tests and offscreen screenshots."""

from __future__ import annotations

import os
from pathlib import Path

SCLK = "0: 500Mhz\n1: 2310Mhz *\n2: 2525Mhz\n"
MCLK = "0: 96Mhz\n1: 456Mhz\n2: 772Mhz\n3: 1250Mhz *\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def add_gpu(root: Path, card: int, pci: str, device_id: int, *, name: str | None = None,
            hwmon: bool = True, vendor: int = 0x1002) -> Path:
    dev = root / "sys/devices/pci0000:00" / pci
    _write(dev / "vendor", f"0x{vendor:04x}\n")
    _write(dev / "device", f"0x{device_id:04x}\n")
    if name:
        _write(dev / "product_name", name + "\n")
    drv = root / "sys/bus/pci/drivers/amdgpu"
    drv.mkdir(parents=True, exist_ok=True)
    if not (dev / "driver").exists():
        os.symlink(drv, dev / "driver")
    _write(dev / "pp_dpm_sclk", SCLK)
    _write(dev / "pp_dpm_mclk", MCLK)
    _write(dev / "gpu_busy_percent", "87\n")
    _write(dev / "mem_info_vram_used", str(6 * 1024**3) + "\n")
    _write(dev / "mem_info_vram_total", str(16 * 1024**3) + "\n")
    if hwmon:
        hw = dev / "hwmon/hwmon3"
        _write(hw / "name", "amdgpu\n")
        _write(hw / "power1_average", "245000000\n")
        _write(hw / "power1_cap", "263000000\n")
        _write(hw / "fan1_input", "1450\n")
        for i, (label, v) in enumerate((("edge", 62000), ("junction", 78000), ("mem", 70000)), 1):
            _write(hw / f"temp{i}_label", label + "\n")
            _write(hw / f"temp{i}_input", f"{v}\n")
    drm = root / "sys/class/drm"
    drm.mkdir(parents=True, exist_ok=True)
    card_dir = drm / f"card{card}"
    card_dir.mkdir(exist_ok=True)
    os.symlink(dev, card_dir / "device")
    # Connector entries must be ignored by discovery.
    (drm / f"card{card}-DP-1").mkdir(exist_ok=True)
    return dev


def build_default(root: Path) -> Path:
    add_gpu(root, 0, "0000:03:00.0", 0x7550, name="AMD Radeon RX 9070 XT")
    add_gpu(root, 1, "0000:0c:00.0", 0x164e, hwmon=False)
    return root
