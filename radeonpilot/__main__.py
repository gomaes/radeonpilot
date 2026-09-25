"""Entry point: ``python -m radeonpilot``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .sysfs import discover_gpus, read_stats


def dump() -> int:
    """Print detected GPUs and one sample of their stats (no GUI)."""
    gpus = discover_gpus()
    if not gpus:
        print("No amdgpu device found.")
        return 1
    for gpu in gpus:
        s = read_stats(gpu)
        print(f"{gpu.card}  {gpu.pci_address}  {gpu.name}  [{gpu.vk_device_select}]")
        print(f"  hwmon      : {gpu.hwmon_path}")
        print(f"  sclk/mclk  : {s.sclk_mhz} / {s.mclk_mhz} MHz")
        print(f"  power      : {s.power_w} W (cap {s.power_cap_w} W)")
        print(f"  busy       : {s.busy_percent} %")
        print(f"  vram       : {s.vram_used} / {s.vram_total} bytes")
        print(f"  fan        : {s.fan_rpm} RPM")
        print(f"  temps      : {s.temps_c}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radeonpilot")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--dump", action="store_true", help="print GPU stats to stdout and exit")
    args, qt_args = parser.parse_known_args(argv)

    if args.dump:
        return dump()

    from PySide6.QtWidgets import QApplication

    from .gui.main_window import MainWindow

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("RadeonPilot")
    app.setDesktopFileName("radeonpilot")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
