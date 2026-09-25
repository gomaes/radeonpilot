"""Emulated amdgpu sysfs for development without the hardware.

``build_tree`` creates a fake sysfs tree (Radeon RX 9070 XT / RDNA4 and
Radeon RX 7900 XTX / RDNA3) under an arbitrary directory. ``EmulatedBackend``
stands in for the kernel when the daemon writes: it parses the same
commands the driver accepts, enforces the same ranges (EINVAL otherwise) and
re-renders the files. ``Simulator`` animates the monitoring values.

Usage::

    python -m radeonpilot.emulator build /tmp/rp-emu [--no-od]

then start the daemon with ``--emulate --sysfs-root /tmp/rp-emu`` and the GUI
with ``RADEONPILOT_SYSFS_ROOT=/tmp/rp-emu`` (see scripts/run-emulated.sh).
"""

from __future__ import annotations

import argparse
import copy
import errno
import json
import math
import os
import random
import threading
from pathlib import Path

from .control import ATTR_FAN_CURVE, ATTR_OD, ATTR_PERF_LEVEL, ATTR_POWER_CAP, PERF_LEVELS

STATE_FILE = "radeonpilot-emulator.json"

GIB = 1024**3

SPECS = {
    "9070xt": {
        "card": 1,
        "pci": "0000:03:00.0",
        "bridge": "0000:00:01.1/0000:01:00.0/0000:02:00.0",
        "device": 0x7550,
        "revision": 0xC0,
        "subsystem": (0x1DA2, 0x475E),
        "vram": 16 * GIB,
        "od_format": "rdna4",
        "sclk_idle": 500,
        "sclk_boost": 2970,
        "mclk_levels": [96, 456, 772, 1258],
        "power_default": 304,
        "power_min": 274,
        "power_max": 334,
        "power_idle": 14,
        "power_attr": "power1_input",
        "fan_max_rpm": 3300,
        "hwmon": 2,
        "od_defaults": {"sclk_offset": 0, "mclk_min": 97, "mclk_max": 1258, "voltage_offset": 0},
        "od_ranges": {"SCLK_OFFSET": (-500, 1000), "MCLK": (97, 1500), "VDDGFX_OFFSET": (-200, 0)},
        "fan_default": [[0, 0]] * 5,
        "fan_ranges": {"temp": (25, 100), "pwm": (15, 100)},
    },
    "7900xtx": {
        "card": 2,
        "pci": "0000:08:00.0",
        "bridge": "0000:00:03.1/0000:06:00.0/0000:07:00.0",
        "device": 0x744C,
        "revision": 0xC8,
        "subsystem": (0x1002, 0x0E3B),
        "vram": 24 * GIB,
        "od_format": "rdna3",
        "sclk_idle": 500,
        "sclk_boost": 2500,
        "mclk_levels": [97, 456, 772, 1250],
        "power_default": 339,
        "power_min": 305,
        "power_max": 390,
        "power_idle": 22,
        "power_attr": "power1_average",
        "fan_max_rpm": 3200,
        "hwmon": 3,
        "od_defaults": {"sclk_min": 500, "sclk_max": 2500, "mclk_min": 97, "mclk_max": 1250, "voltage_offset": 0},
        "od_ranges": {"SCLK": (500, 3000), "MCLK": (97, 1500), "VDDGFX_OFFSET": (-450, 0)},
        "fan_default": [[0, 0]] * 5,
        "fan_ranges": {"temp": (25, 100), "pwm": (15, 100)},
    },
}

PPFEATUREMASK_OD = 0xFFFFFFFF
PPFEATUREMASK_DEFAULT = 0xFFF7BFFF  # kernel default: OverDrive bit (0x4000) off


# ---------------------------------------------------------------- rendering

def _render_od(spec: dict, od: dict) -> str:
    r = spec["od_ranges"]
    lines = []
    if spec["od_format"] == "rdna3":
        lines += ["OD_SCLK:", f"0: {od['sclk_min']}Mhz", f"1: {od['sclk_max']}Mhz"]
    else:
        lines += ["OD_SCLK_OFFSET:", f"{od['sclk_offset']}Mhz"]
    lines += ["OD_MCLK:", f"0: {od['mclk_min']}Mhz", f"1: {od['mclk_max']}MHz"]
    lines += ["OD_VDDGFX_OFFSET:", f"{od['voltage_offset']}mV"]
    lines.append("OD_RANGE:")
    if "SCLK" in r:
        lines.append(f"SCLK: {r['SCLK'][0]:7d}Mhz {r['SCLK'][1]:10d}Mhz")
    if "SCLK_OFFSET" in r:
        lines.append(f"SCLK_OFFSET: {r['SCLK_OFFSET'][0]:7d}Mhz {r['SCLK_OFFSET'][1]:10d}Mhz")
    lines.append(f"MCLK: {r['MCLK'][0]:7d}Mhz {r['MCLK'][1]:10d}Mhz")
    lines.append(f"VDDGFX_OFFSET: {r['VDDGFX_OFFSET'][0]:7d}mv {r['VDDGFX_OFFSET'][1]:10d}mv")
    return "\n".join(lines) + "\n"


def _render_fan(spec: dict, points: list) -> str:
    lines = ["OD_FAN_CURVE:"]
    lines += [f"{i}: {t}C {p}%" for i, (t, p) in enumerate(points)]
    fr = spec["fan_ranges"]
    lines += [
        "OD_RANGE:",
        f"FAN_CURVE(hotspot temp): {fr['temp'][0]}C {fr['temp'][1]}C",
        f"FAN_CURVE(fan speed): {fr['pwm'][0]}% {fr['pwm'][1]}%",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        return
    os.symlink(os.path.relpath(target, link.parent), link)


def device_dir(root: Path, spec: dict) -> Path:
    return root / "sys/devices/pci0000:00" / spec["bridge"] / spec["pci"]


def hwmon_dir(root: Path, spec: dict) -> Path:
    return device_dir(root, spec) / "hwmon" / f"hwmon{spec['hwmon']}"


# ---------------------------------------------------------------- tree

def build_tree(root: Path, gpus=("9070xt", "7900xtx"), od_enabled: bool = True) -> Path:
    """Create a fresh emulated sysfs tree under ``root``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    mask = PPFEATUREMASK_OD if od_enabled else PPFEATUREMASK_DEFAULT
    _write(root / "sys/module/amdgpu/parameters/ppfeaturemask", f"0x{mask:08x}\n")

    # simpledrm's card0 (platform device, no PCI vendor) must be ignored.
    platform = root / "sys/devices/platform/simple-framebuffer.0"
    platform.mkdir(parents=True, exist_ok=True)
    _symlink(platform, root / "sys/class/drm/card0/device")

    driver = root / "sys/bus/pci/drivers/amdgpu"
    driver.mkdir(parents=True, exist_ok=True)

    state = {"od_enabled": od_enabled, "gpus": {}}
    for key in gpus:
        spec = SPECS[key]
        dev = device_dir(root, spec)
        hw = hwmon_dir(root, spec)
        _write(dev / "vendor", "0x1002\n")
        _write(dev / "device", f"0x{spec['device']:04x}\n")
        _write(dev / "revision", f"0x{spec['revision']:02x}\n")
        _write(dev / "subsystem_vendor", f"0x{spec['subsystem'][0]:04x}\n")
        _write(dev / "subsystem_device", f"0x{spec['subsystem'][1]:04x}\n")
        _symlink(driver, dev / "driver")
        _write(dev / "mem_info_vram_total", f"{spec['vram']}\n")
        _write(dev / ATTR_PERF_LEVEL, "auto\n")
        _write(hw / "name", "amdgpu\n")
        _write(hw / "power1_cap", f"{spec['power_default'] * 1_000_000}\n")
        _write(hw / "power1_cap_default", f"{spec['power_default'] * 1_000_000}\n")
        _write(hw / "power1_cap_min", f"{spec['power_min'] * 1_000_000}\n")
        _write(hw / "power1_cap_max", f"{spec['power_max'] * 1_000_000}\n")
        _write(hw / "fan1_max", f"{spec['fan_max_rpm']}\n")
        for i, label in enumerate(("edge", "junction", "mem"), 1):
            _write(hw / f"temp{i}_label", f"{label}\n")
        _symlink(dev, root / f"sys/class/drm/card{spec['card']}/device")
        (root / f"sys/class/drm/card{spec['card']}-DP-{spec['card']}").mkdir(parents=True, exist_ok=True)

        gpu_state = {
            "power_cap_uw": spec["power_default"] * 1_000_000,
            "perf_level": "auto",
            "od": dict(spec["od_defaults"]),
            "od_active": dict(spec["od_defaults"]),
            "fan": copy.deepcopy(spec["fan_default"]),
            "fan_active": copy.deepcopy(spec["fan_default"]),
            "sim": {"t": random.random() * 100, "junction": 38.0, "edge": 33.0, "mem": 40.0},
        }
        state["gpus"][key] = gpu_state
        if od_enabled:
            _write(dev / ATTR_OD, _render_od(spec, gpu_state["od"]))
            _write(dev / "gpu_od/fan_ctrl" / ATTR_FAN_CURVE, _render_fan(spec, gpu_state["fan"]))
        else:
            for stale in (dev / ATTR_OD, dev / "gpu_od/fan_ctrl" / ATTR_FAN_CURVE):
                if stale.exists():
                    stale.unlink()
    (root / STATE_FILE).write_text(json.dumps(state, indent=1))
    sim = Simulator(EmulatedBackend(root))
    sim.step(0.0)
    return root


# ---------------------------------------------------------------- backend

def _einval(msg: str) -> OSError:
    return OSError(errno.EINVAL, f"Invalid argument ({msg})")


class EmulatedBackend:
    """Implements the kernel side of the attributes the daemon writes."""

    def __init__(self, root: Path, fail: set[str] | None = None) -> None:
        self.root = Path(root).resolve()
        if self.root == Path("/"):
            raise ValueError("emulated backend refuses to operate on the real /sys")
        env_fail = os.environ.get("RADEONPILOT_EMU_FAIL", "")
        # Entries are "<attr>" or "<attr>:<first token>" (e.g. "pp_od_clk_voltage:c").
        self.fail = set(fail or ()) | {f for f in env_fail.split(",") if f}
        self.lock = threading.RLock()
        self.writes: list[tuple[str, str]] = []  # (attr, text) log for tests
        self._state = json.loads((self.root / STATE_FILE).read_text())

    def _save(self) -> None:
        tmp = self.root / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=1))
        os.replace(tmp, self.root / STATE_FILE)

    def _gpu_for(self, path: Path) -> tuple[str, dict]:
        resolved = Path(path).resolve()
        for key in self._state["gpus"]:
            if device_dir(self.root, SPECS[key]).resolve() in resolved.parents:
                return key, SPECS[key]
        raise OSError(errno.ENOENT, f"No such emulated device for {path}")

    def gpu_items(self):
        return [(k, SPECS[k], s) for k, s in self._state["gpus"].items()]

    def write(self, path: Path, text: str) -> None:
        with self.lock:
            path = Path(path)
            key, spec = self._gpu_for(path)
            state = self._state["gpus"][key]
            attr = path.name
            tokens = text.split()
            self.writes.append((attr, text.strip()))
            if attr in self.fail or (tokens and f"{attr}:{tokens[0]}" in self.fail):
                raise OSError(errno.EIO, "Input/output error (injected by emulator)")
            if attr == ATTR_POWER_CAP:
                self._power_cap(spec, state, tokens)
            elif attr == ATTR_PERF_LEVEL:
                if len(tokens) != 1 or tokens[0] not in PERF_LEVELS:
                    raise _einval("unknown performance level")
                state["perf_level"] = tokens[0]
            elif attr == ATTR_OD:
                if not self._state["od_enabled"]:
                    raise OSError(errno.ENOENT, "No such file or directory")
                self._od(spec, state, tokens)
            elif attr == ATTR_FAN_CURVE:
                if not self._state["od_enabled"]:
                    raise OSError(errno.ENOENT, "No such file or directory")
                self._fan(spec, state, tokens)
            else:
                raise OSError(errno.EACCES, f"attribute {attr} is not writable in the emulator")
            self._render(spec, state)
            self._save()

    @staticmethod
    def _ints(tokens: list[str]) -> list[int]:
        try:
            return [int(t) for t in tokens]
        except ValueError:
            raise _einval("not an integer") from None

    def _power_cap(self, spec, state, tokens) -> None:
        if len(tokens) != 1:
            raise _einval("power1_cap expects one value")
        (uw,) = self._ints(tokens)
        if not spec["power_min"] * 1_000_000 <= uw <= spec["power_max"] * 1_000_000:
            raise _einval("power cap out of range")
        # The driver works in whole watts.
        state["power_cap_uw"] = (uw // 1_000_000) * 1_000_000

    def _od(self, spec, state, tokens) -> None:
        if not tokens:
            raise _einval("empty command")
        cmd, args = tokens[0], self._ints(tokens[1:])
        ranges = spec["od_ranges"]
        od = state["od"]

        def check(rng_key, value):
            lo, hi = ranges[rng_key]
            if not lo <= value <= hi:
                raise _einval(f"{rng_key} {value} outside [{lo}, {hi}]")

        if cmd == "r":
            state["od"] = dict(spec["od_defaults"])
            state["od_active"] = dict(spec["od_defaults"])
        elif cmd == "c":
            for prefix in ("sclk", "mclk"):
                lo, hi = od.get(f"{prefix}_min"), od.get(f"{prefix}_max")
                if lo is not None and hi is not None and lo > hi:
                    raise _einval(f"{prefix} min > max")
            state["od_active"] = dict(od)
        elif cmd == "s" and spec["od_format"] == "rdna4":
            if len(args) != 1:
                raise _einval("s expects one offset on this ASIC")
            check("SCLK_OFFSET", args[0])
            od["sclk_offset"] = args[0]
        elif cmd in ("s", "m"):
            if len(args) != 2 or args[0] not in (0, 1):
                raise _einval(f"{cmd} expects <0|1> <MHz>")
            prefix = "sclk" if cmd == "s" else "mclk"
            check(prefix.upper(), args[1])
            od[f"{prefix}_{'min' if args[0] == 0 else 'max'}"] = args[1]
        elif cmd == "vo":
            if len(args) != 1:
                raise _einval("vo expects one value")
            check("VDDGFX_OFFSET", args[0])
            od["voltage_offset"] = args[0]
        else:
            raise _einval(f"unknown command {cmd!r}")

    def _fan(self, spec, state, tokens) -> None:
        if tokens == ["r"]:
            state["fan"] = copy.deepcopy(spec["fan_default"])
            state["fan_active"] = copy.deepcopy(spec["fan_default"])
            return
        if tokens == ["c"]:
            state["fan_active"] = copy.deepcopy(state["fan"])
            return
        if len(tokens) != 3:
            raise _einval("fan_curve expects <point> <temp> <pwm>")
        idx, temp, pwm = self._ints(tokens)
        fr = spec["fan_ranges"]
        if not 0 <= idx < len(state["fan"]):
            raise _einval("point index")
        if not fr["temp"][0] <= temp <= fr["temp"][1] or not fr["pwm"][0] <= pwm <= fr["pwm"][1]:
            raise _einval("fan curve point out of range")
        state["fan"][idx] = [temp, pwm]

    def _render(self, spec, state) -> None:
        dev = device_dir(self.root, spec)
        hw = hwmon_dir(self.root, spec)
        _write(hw / "power1_cap", f"{state['power_cap_uw']}\n")
        _write(dev / ATTR_PERF_LEVEL, f"{state['perf_level']}\n")
        if self._state["od_enabled"]:
            _write(dev / ATTR_OD, _render_od(spec, state["od"]))
            _write(dev / "gpu_od/fan_ctrl" / ATTR_FAN_CURVE, _render_fan(spec, state["fan"]))

    def save_sim(self) -> None:
        with self.lock:
            self._save()


# ---------------------------------------------------------------- simulation

def _interp(points: list, x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(x1 - x0, 1)
    return points[-1][1]


def _load_pattern(key: str, t: float) -> float:
    if key == "9070xt":
        # "Gaming": sustained high load with scene changes.
        base = 0.72 + 0.25 * math.sin(t / 9.0) + 0.05 * math.sin(t * 1.7)
    else:
        # "Compute bursts": idle most of the time, periodic heavy jobs.
        base = 0.97 if (t % 40) > 26 else 0.04 + 0.03 * math.sin(t)
    return min(max(base + random.uniform(-0.03, 0.03), 0.0), 1.0)


class Simulator:
    """Produces plausible, setting-dependent monitoring values."""

    def __init__(self, backend: EmulatedBackend, interval: float = 1.0) -> None:
        self.backend = backend
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="emulator-sim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.step(self.interval)

    def step(self, dt: float) -> None:
        with self.backend.lock:
            for key, spec, state in self.backend.gpu_items():
                self._step_gpu(key, spec, state, dt)
            self.backend.save_sim()

    def _step_gpu(self, key: str, spec: dict, state: dict, dt: float) -> None:
        root = self.backend.root
        dev, hw = device_dir(root, spec), hwmon_dir(root, spec)
        sim = state["sim"]
        sim["t"] += dt
        load = _load_pattern(key, sim["t"])
        od = state["od_active"]
        level = state["perf_level"]

        if spec["od_format"] == "rdna3":
            s_min, s_max = od["sclk_min"], od["sclk_max"]
        else:
            s_min, s_max = spec["sclk_idle"], spec["sclk_boost"] + od["sclk_offset"]
        m_levels = [m for m in spec["mclk_levels"] if m <= od["mclk_max"]] or spec["mclk_levels"][:1]

        if level in ("low", "profile_min_sclk"):
            load_clk = 0.0
        elif level in ("high", "profile_peak"):
            load_clk = 1.0
        else:
            load_clk = load
        sclk = s_min + (s_max - s_min) * (load_clk ** 0.6)
        mclk = m_levels[-1] if load > 0.15 or level in ("high", "profile_peak") else m_levels[0]
        if level in ("low", "profile_min_mclk"):
            mclk = m_levels[0]

        idle = spec["power_idle"]
        scale = (sclk / spec["sclk_boost"]) * (1 + od.get("voltage_offset", 0) / 600)
        demand = idle + load * (spec["power_default"] * 1.15 - idle) * scale
        cap = state["power_cap_uw"] / 1_000_000
        power = min(demand, cap)
        if demand > cap:  # power limited: clocks drop
            sclk = s_min + (sclk - s_min) * (cap - idle) / max(demand - idle, 1)
        power += random.uniform(-2, 2)

        # Fan: custom curve (hotspot based) or the firmware's own curve with zero-RPM.
        curve = state["fan_active"]
        if all(t == 0 and p == 0 for t, p in curve):
            pwm = 0.0 if sim["junction"] < 55 else min(100.0, 25 + (sim["junction"] - 55) * 2.2)
        else:
            pwm = _interp(curve, sim["junction"])
        rpm = int(spec["fan_max_rpm"] * pwm / 100)

        target_j = 34 + power * 0.23 - pwm * 0.12
        sim["junction"] += (target_j - sim["junction"]) * min(1.0, 0.25 * max(dt, 1.0))
        sim["edge"] = sim["junction"] - 6 - power * 0.04
        sim["mem"] += (38 + power * 0.1 - sim["mem"]) * 0.15

        vram_used = int(spec["vram"] * (0.08 + 0.55 * load if key == "9070xt" else 0.05 + 0.7 * (load > 0.5)))

        s_cur = int(sclk)
        dpm = [spec["sclk_idle"], s_cur, int(s_max)]
        if s_cur <= spec["sclk_idle"] + 5:
            sclk_txt = f"0: {spec['sclk_idle']}Mhz *\n1: {int(s_max)}Mhz\n"
        elif s_cur >= s_max - 5:
            sclk_txt = f"0: {spec['sclk_idle']}Mhz\n1: {int(s_max)}Mhz *\n"
        else:
            sclk_txt = "".join(f"{i}: {v}Mhz{' *' if i == 1 else ''}\n" for i, v in enumerate(dpm))
        mclk_txt = "".join(
            f"{i}: {m}Mhz{' *' if m == mclk else ''}\n" for i, m in enumerate(spec["mclk_levels"])
        )

        _write(dev / "gpu_busy_percent", f"{int(load * 100)}\n")
        _write(dev / "pp_dpm_sclk", sclk_txt)
        _write(dev / "pp_dpm_mclk", mclk_txt)
        _write(dev / "mem_info_vram_used", f"{vram_used}\n")
        _write(hw / spec["power_attr"], f"{int(max(power, 1) * 1_000_000)}\n")
        _write(hw / "freq1_input", f"{s_cur * 1_000_000}\n")
        _write(hw / "freq2_input", f"{mclk * 1_000_000}\n")
        _write(hw / "fan1_input", f"{rpm}\n")
        for i, label in enumerate(("edge", "junction", "mem"), 1):
            _write(hw / f"temp{i}_input", f"{int(sim[label] * 1000)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m radeonpilot.emulator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="create an emulated sysfs tree")
    b.add_argument("root", type=Path)
    b.add_argument("--no-od", action="store_true", help="emulate ppfeaturemask without OverDrive")
    b.add_argument("--gpus", default="9070xt,7900xtx", help=f"comma separated subset of {','.join(SPECS)}")
    args = parser.parse_args(argv)
    gpus = [g for g in args.gpus.split(",") if g]
    unknown = [g for g in gpus if g not in SPECS]
    if unknown:
        parser.error(f"unknown GPU(s): {', '.join(unknown)}")
    build_tree(args.root, gpus, od_enabled=not args.no_od)
    print(f"emulated sysfs created at {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
