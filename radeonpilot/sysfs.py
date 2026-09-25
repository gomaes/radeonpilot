"""Read-only access to amdgpu sysfs / hwmon attributes.

Everything in this module only *reads* from sysfs. Writes are the
privileged daemon's job and live elsewhere.

All paths are resolved relative to a configurable root so the code can be
exercised against a fake sysfs tree (see ``RADEONPILOT_SYSFS_ROOT``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

AMD_VENDOR_ID = 0x1002

PCI_IDS_PATHS = (
    "/usr/share/hwdata/pci.ids",
    "/usr/share/misc/pci.ids",
    "/usr/share/pci.ids",
)

# Exact retail names keyed by (device id, PCI revision). Checked before pci.ids
# because pci.ids only lists the shared chip name (e.g. "Navi 31 [...]").
KNOWN_MODELS = {
    (0x744C, 0xC8): "AMD Radeon RX 7900 XTX",
    (0x744C, 0xCC): "AMD Radeon RX 7900 XT",
    (0x7550, 0xC0): "AMD Radeon RX 9070 XT",
    (0x7550, 0xC3): "AMD Radeon RX 9070",
}

# Chip names used when pci.ids is not installed.
KNOWN_CHIPS = {
    0x744C: "Navi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE]",
    0x7448: "Navi 31 [Radeon Pro W7900]",
    0x747E: "Navi 32 [Radeon RX 7700 XT/7800 XT]",
    0x7480: "Navi 33 [Radeon RX 7600/7600 XT]",
    0x7550: "Navi 48 [Radeon RX 9070/9070 XT/9070 GRE]",
    0x7590: "Navi 44 [Radeon RX 9060 XT]",
}

_CARD_RE = re.compile(r"^card(\d+)$")
_DPM_LINE_RE = re.compile(r"^\s*(\w+):\s*(\d+)\s*[Mm][Hh]z\s*(\*)?")


def sysfs_root() -> Path:
    return Path(os.environ.get("RADEONPILOT_SYSFS_ROOT", "/"))


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def parse_dpm_clock(text: str | None) -> int | None:
    """Return the active clock (MHz) from a pp_dpm_sclk/pp_dpm_mclk dump.

    The active level is marked with ``*``. Returns None if nothing is marked.
    """
    if not text:
        return None
    for line in text.splitlines():
        m = _DPM_LINE_RE.match(line)
        if m and m.group(3):
            return int(m.group(2))
    return None


def _lookup_pci_name(vendor: int, device: int, subvendor: int | None, subdevice: int | None) -> str | None:
    """Look up a device name in pci.ids (subsystem name preferred)."""
    for candidate in PCI_IDS_PATHS:
        path = Path(candidate)
        if not path.is_file():
            continue
        vendor_key = f"{vendor:04x}"
        device_key = f"{device:04x}"
        sub_key = (
            f"{subvendor:04x} {subdevice:04x}"
            if subvendor is not None and subdevice is not None
            else None
        )
        in_vendor = False
        device_name = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            break
                        in_vendor = line.startswith(vendor_key + " ")
                        continue
                    if not in_vendor:
                        continue
                    if line.startswith("\t\t"):
                        if device_name and sub_key and line[2:].startswith(sub_key):
                            return line[2:].split("  ", 1)[-1].strip()
                    else:
                        if device_name:
                            # Left the matching device block without a subsystem hit.
                            return device_name
                        if line[1:].startswith(device_key + " "):
                            device_name = line[1:].split("  ", 1)[-1].strip()
        except OSError:
            continue
        if device_name:
            return device_name
    return None


@dataclass
class GpuInfo:
    """Static identification of one amdgpu device."""

    card: str  # e.g. "card1"
    card_path: Path  # /sys/class/drm/cardN
    device_path: Path  # resolved PCI device dir
    pci_address: str  # e.g. "0000:03:00.0"
    vendor_id: int
    device_id: int
    name: str
    hwmon_path: Path | None

    @property
    def dri_prime_id(self) -> str:
        """PCI address in DRI_PRIME form: pci-0000_03_00_0."""
        return "pci-" + re.sub(r"[:.]", "_", self.pci_address)

    @property
    def vk_device_select(self) -> str:
        """vendor:device in MESA_VK_DEVICE_SELECT form."""
        return f"{self.vendor_id:04x}:{self.device_id:04x}"


@dataclass
class GpuStats:
    """One sample of runtime metrics. Missing values are None."""

    sclk_mhz: int | None = None
    mclk_mhz: int | None = None
    power_w: float | None = None
    power_cap_w: float | None = None
    busy_percent: int | None = None
    vram_used: int | None = None
    vram_total: int | None = None
    fan_rpm: int | None = None
    temps_c: dict[str, float] = field(default_factory=dict)

    @property
    def vram_percent(self) -> float | None:
        if self.vram_used is None or not self.vram_total:
            return None
        return 100.0 * self.vram_used / self.vram_total


def _find_hwmon(device_path: Path) -> Path | None:
    hwmon_dir = device_path / "hwmon"
    try:
        entries = sorted(p for p in hwmon_dir.iterdir() if p.name.startswith("hwmon"))
    except OSError:
        return None
    for entry in entries:
        if read_text(entry / "name") == "amdgpu":
            return entry
    return entries[0] if entries else None


def _gpu_name(device_path: Path, vendor: int, device: int) -> str:
    # Some boards expose a marketing name directly (read from the FRU EEPROM).
    name = read_text(device_path / "product_name")
    if name:
        return name
    revision = read_int(device_path / "revision")
    if (device, revision) in KNOWN_MODELS:
        return KNOWN_MODELS[(device, revision)]
    name = _lookup_pci_name(
        vendor,
        device,
        read_int(device_path / "subsystem_vendor"),
        read_int(device_path / "subsystem_device"),
    )
    if name:
        return name
    if device in KNOWN_CHIPS:
        return KNOWN_CHIPS[device]
    return f"AMD GPU [{vendor:04x}:{device:04x}]"


def discover_gpus(root: Path | None = None) -> list[GpuInfo]:
    """Return all amdgpu-driven cards found under <root>/sys/class/drm."""
    root = root if root is not None else sysfs_root()
    drm = root / "sys/class/drm"
    try:
        cards = [p for p in drm.iterdir() if _CARD_RE.match(p.name)]
    except OSError:
        return []

    gpus: list[GpuInfo] = []
    seen: set[str] = set()
    for card in sorted(cards, key=lambda p: int(_CARD_RE.match(p.name).group(1))):
        dev = card / "device"
        vendor = read_int(dev / "vendor")
        if vendor != AMD_VENDOR_ID:
            continue
        driver = dev / "driver"
        if driver.exists() and driver.resolve().name != "amdgpu":
            continue
        device_id = read_int(dev / "device") or 0
        resolved = dev.resolve()
        pci_address = resolved.name
        if pci_address in seen:
            continue
        seen.add(pci_address)
        gpus.append(
            GpuInfo(
                card=card.name,
                card_path=card,
                device_path=resolved,
                pci_address=pci_address,
                vendor_id=vendor,
                device_id=device_id,
                name=_gpu_name(resolved, vendor, device_id),
                hwmon_path=_find_hwmon(resolved),
            )
        )
    return gpus


def _hwmon_temps(hwmon: Path) -> dict[str, float]:
    temps: dict[str, float] = {}
    for idx in range(1, 10):
        value = read_int(hwmon / f"temp{idx}_input")
        if value is None:
            continue
        label = read_text(hwmon / f"temp{idx}_label") or f"temp{idx}"
        temps[label] = value / 1000.0
    return temps


def read_stats(gpu: GpuInfo) -> GpuStats:
    dev = gpu.device_path
    stats = GpuStats(
        sclk_mhz=parse_dpm_clock(read_text(dev / "pp_dpm_sclk")),
        mclk_mhz=parse_dpm_clock(read_text(dev / "pp_dpm_mclk")),
        busy_percent=read_int(dev / "gpu_busy_percent"),
        vram_used=read_int(dev / "mem_info_vram_used"),
        vram_total=read_int(dev / "mem_info_vram_total"),
    )
    hw = gpu.hwmon_path
    if hw is not None:
        # hwmon freqN_input is in Hz; use it when pp_dpm_* has no marked level.
        if stats.sclk_mhz is None:
            hz = read_int(hw / "freq1_input")
            stats.sclk_mhz = hz // 1_000_000 if hz is not None else None
        if stats.mclk_mhz is None:
            hz = read_int(hw / "freq2_input")
            stats.mclk_mhz = hz // 1_000_000 if hz is not None else None
        power_uw = read_int(hw / "power1_average")
        if power_uw is None:
            power_uw = read_int(hw / "power1_input")
        if power_uw is not None:
            stats.power_w = power_uw / 1_000_000
        cap_uw = read_int(hw / "power1_cap")
        if cap_uw is not None:
            stats.power_cap_w = cap_uw / 1_000_000
        stats.fan_rpm = read_int(hw / "fan1_input")
        stats.temps_c = _hwmon_temps(hw)
    return stats
