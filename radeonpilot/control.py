"""Control attributes: parsing, capability detection and request validation.

This module is shared by the GUI (to display current values and the ranges
the driver allows) and by the privileged daemon, which re-reads the ranges
from the driver and validates every request with these functions before it
writes anything. Nothing in here writes to sysfs.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .sysfs import GpuInfo, read_int, read_text, sysfs_root

PPFEATUREMASK_PATH = "sys/module/amdgpu/parameters/ppfeaturemask"
PP_OVERDRIVE_MASK = 0x4000
OD_ENABLE_MASK = "0xffffffff"

# Values accepted by power_dpm_force_performance_level.
PERF_LEVELS = (
    "auto",
    "low",
    "high",
    "manual",
    "profile_standard",
    "profile_min_sclk",
    "profile_min_mclk",
    "profile_peak",
)

# OverDrive request keys -> (OD_RANGE key, label, unit)
OD_FIELDS = {
    "sclk_min": ("SCLK", "コアクロック下限", "MHz"),
    "sclk_max": ("SCLK", "コアクロック上限", "MHz"),
    "sclk_offset": ("SCLK_OFFSET", "コアクロック オフセット", "MHz"),
    "mclk_min": ("MCLK", "メモリクロック下限", "MHz"),
    "mclk_max": ("MCLK", "メモリクロック上限", "MHz"),
    "voltage_offset": ("VDDGFX_OFFSET", "電圧オフセット", "mV"),
}

ATTR_POWER_CAP = "power1_cap"
ATTR_PERF_LEVEL = "power_dpm_force_performance_level"
ATTR_OD = "pp_od_clk_voltage"
ATTR_FAN_CURVE = "fan_curve"
FAN_CURVE_RELPATH = "gpu_od/fan_ctrl/fan_curve"

WRITABLE_ATTRS = frozenset({ATTR_POWER_CAP, ATTR_PERF_LEVEL, ATTR_OD, ATTR_FAN_CURVE})


class ValidationError(ValueError):
    """A request was rejected before anything was written."""


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int

    def __contains__(self, value: int) -> bool:
        return self.lo <= value <= self.hi


# ---------------------------------------------------------------- parsing

_SECTION_RE = re.compile(r"^(OD_[A-Z_]+):$")
_RANGE_RE = re.compile(r"^(.+?):\s*(-?\d+)\s*[a-z%]*\s+(-?\d+)\s*[a-z%]*$", re.IGNORECASE)
_LEVEL_RE = re.compile(r"^(\d+):\s*(-?\d+)\s*mhz$", re.IGNORECASE)
_SINGLE_RE = re.compile(r"^(-?\d+)\s*(mhz|mv)$", re.IGNORECASE)
_FAN_POINT_RE = re.compile(r"^(\d+):\s*(-?\d+)\s*C\s+(-?\d+)\s*%$", re.IGNORECASE)


@dataclass
class OdState:
    """Parsed pp_od_clk_voltage (RDNA3: min/max clocks, RDNA4: clock offset)."""

    values: dict[str, int] = field(default_factory=dict)
    ranges: dict[str, Range] = field(default_factory=dict)

    @property
    def uses_sclk_offset(self) -> bool:
        return "sclk_offset" in self.values

    def range_for(self, key: str) -> Range | None:
        return self.ranges.get(OD_FIELDS[key][0])

    def supported(self) -> list[str]:
        """Request keys the driver exposes *and* reports a range for."""
        return [k for k in OD_FIELDS if k in self.values and self.range_for(k) is not None]


def parse_od(text: str | None) -> OdState | None:
    if not text:
        return None
    od = OdState()
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        if section == "OD_RANGE":
            m = _RANGE_RE.match(line)
            if m:
                od.ranges[m.group(1).strip().upper()] = Range(int(m.group(2)), int(m.group(3)))
        elif section in ("OD_SCLK", "OD_MCLK"):
            m = _LEVEL_RE.match(line)
            if m and m.group(1) in ("0", "1"):
                prefix = "sclk" if section == "OD_SCLK" else "mclk"
                od.values[f"{prefix}_{'min' if m.group(1) == '0' else 'max'}"] = int(m.group(2))
        elif section in ("OD_SCLK_OFFSET", "OD_VDDGFX_OFFSET"):
            m = _SINGLE_RE.match(line)
            if m:
                key = "sclk_offset" if section == "OD_SCLK_OFFSET" else "voltage_offset"
                od.values[key] = int(m.group(1))
    if not od.values:
        return None
    return od


@dataclass
class FanCurve:
    points: list[tuple[int, int]]  # (temperature °C, fan speed %)
    temp_range: Range
    pwm_range: Range

    @property
    def is_driver_default(self) -> bool:
        """All-zero points mean the firmware's automatic curve is in use."""
        return all(t == 0 and p == 0 for t, p in self.points)


def parse_fan_curve(text: str | None) -> FanCurve | None:
    if not text:
        return None
    points: dict[int, tuple[int, int]] = {}
    temp_range = pwm_range = None
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        if section == "OD_FAN_CURVE":
            m = _FAN_POINT_RE.match(line)
            if m:
                points[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
        elif section == "OD_RANGE":
            m = _RANGE_RE.match(line)
            if m:
                rng = Range(int(m.group(2)), int(m.group(3)))
                name = m.group(1).lower()
                if "temp" in name:
                    temp_range = rng
                elif "speed" in name or "pwm" in name:
                    pwm_range = rng
    if not points or temp_range is None or pwm_range is None:
        return None
    if sorted(points) != list(range(len(points))):
        return None
    return FanCurve([points[i] for i in range(len(points))], temp_range, pwm_range)


# ---------------------------------------------------------------- state

@dataclass
class PowerCap:
    current_uw: int | None
    min_uw: int | None
    max_uw: int | None
    default_uw: int | None

    @staticmethod
    def _w(uw: int | None) -> float | None:
        return None if uw is None else uw / 1_000_000

    @property
    def current_w(self) -> float | None:
        return self._w(self.current_uw)

    @property
    def min_w(self) -> float | None:
        return self._w(self.min_uw)

    @property
    def max_w(self) -> float | None:
        return self._w(self.max_uw)

    @property
    def default_w(self) -> float | None:
        return self._w(self.default_uw)


@dataclass
class ControlState:
    power: PowerCap | None
    perf_level: str | None
    ppfeaturemask: int | None
    od: OdState | None
    fan_curve: FanCurve | None
    od_block_reason: str | None

    @property
    def od_available(self) -> bool:
        return self.od_block_reason is None

    def to_dict(self) -> dict:
        return {
            "power": None
            if self.power is None
            else {
                "current_w": self.power.current_w,
                "min_w": self.power.min_w,
                "max_w": self.power.max_w,
                "default_w": self.power.default_w,
            },
            "perf_level": self.perf_level,
            "ppfeaturemask": None if self.ppfeaturemask is None else f"0x{self.ppfeaturemask:08x}",
            "od_available": self.od_available,
            "od_block_reason": self.od_block_reason,
            "od": None
            if self.od is None
            else {
                "values": dict(self.od.values),
                "ranges": {k: [r.lo, r.hi] for k, r in self.od.ranges.items()},
            },
            "fan_curve": None
            if self.fan_curve is None
            else {
                "points": [list(p) for p in self.fan_curve.points],
                "temp_range": [self.fan_curve.temp_range.lo, self.fan_curve.temp_range.hi],
                "pwm_range": [self.fan_curve.pwm_range.lo, self.fan_curve.pwm_range.hi],
                "driver_default": self.fan_curve.is_driver_default,
            },
        }


def read_ppfeaturemask(root: Path | None = None) -> int | None:
    root = root if root is not None else sysfs_root()
    return read_int(root / PPFEATUREMASK_PATH)


def od_block_reason(mask: int | None, od: OdState | None) -> str | None:
    if mask is not None and not mask & PP_OVERDRIVE_MASK:
        return (
            f"カーネルパラメータ ppfeaturemask (0x{mask:08x}) で OverDrive ビット "
            f"(0x{PP_OVERDRIVE_MASK:x}) が無効になっています。"
        )
    if od is None:
        return "ドライバが pp_od_clk_voltage を公開していません（このGPU/カーネルでは OverDrive 非対応の可能性があります）。"
    return None


def fan_curve_path(gpu: GpuInfo) -> Path:
    return gpu.device_path / FAN_CURVE_RELPATH


def read_control_state(gpu: GpuInfo, root: Path | None = None) -> ControlState:
    dev = gpu.device_path
    hw = gpu.hwmon_path
    power = None
    if hw is not None and (hw / ATTR_POWER_CAP).exists():
        power = PowerCap(
            current_uw=read_int(hw / "power1_cap"),
            min_uw=read_int(hw / "power1_cap_min"),
            max_uw=read_int(hw / "power1_cap_max"),
            default_uw=read_int(hw / "power1_cap_default"),
        )
    mask = read_ppfeaturemask(root)
    od = parse_od(read_text(dev / ATTR_OD))
    reason = od_block_reason(mask, od)
    return ControlState(
        power=power,
        perf_level=read_text(dev / ATTR_PERF_LEVEL),
        ppfeaturemask=mask,
        od=od if reason is None else None,
        fan_curve=parse_fan_curve(read_text(fan_curve_path(gpu))) if reason is None else None,
        od_block_reason=reason,
    )


# ---------------------------------------------------------------- validation

def _as_int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{what} は数値で指定してください")
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ValidationError(f"{what} は整数で指定してください")
        value = int(value)
    return value


def validate_power_cap(power: PowerCap | None, watts) -> int:
    """Return the power cap in microwatts, or raise ValidationError."""
    if power is None or power.max_uw is None or power.min_uw is None:
        raise ValidationError("このGPUは電力上限の範囲（power1_cap_min/max）を報告していません")
    if isinstance(watts, bool) or not isinstance(watts, (int, float)) or not math.isfinite(watts):
        raise ValidationError("電力上限は数値で指定してください")
    uw = int(round(watts * 1_000_000))
    if not power.min_uw <= uw <= power.max_uw:
        raise ValidationError(
            f"電力上限 {watts:g} W はドライバの報告範囲 {power.min_w:g}〜{power.max_w:g} W の外です"
        )
    return uw


def validate_perf_level(level) -> str:
    if level not in PERF_LEVELS:
        raise ValidationError(f"不正なパフォーマンスレベルです: {level!r}")
    return level


def validate_od(od: OdState | None, request) -> dict[str, int]:
    if od is None:
        raise ValidationError("このGPUでは OverDrive が利用できません")
    if not isinstance(request, dict) or not request:
        raise ValidationError("変更する値が指定されていません")
    supported = od.supported()
    result: dict[str, int] = {}
    for key, raw in request.items():
        if key not in OD_FIELDS:
            raise ValidationError(f"不明な項目です: {key!r}")
        _, label, unit = OD_FIELDS[key]
        if key not in supported:
            raise ValidationError(f"{label} はこのGPU/ドライバでは変更できません")
        value = _as_int(raw, label)
        rng = od.range_for(key)
        if value not in rng:
            raise ValidationError(
                f"{label} {value} {unit} はドライバの報告範囲 {rng.lo}〜{rng.hi} {unit} の外です"
            )
        result[key] = value
    merged = {**od.values, **result}
    for prefix, label in (("sclk", "コアクロック"), ("mclk", "メモリクロック")):
        lo, hi = merged.get(f"{prefix}_min"), merged.get(f"{prefix}_max")
        if lo is not None and hi is not None and lo > hi:
            raise ValidationError(f"{label}の下限 ({lo} MHz) が上限 ({hi} MHz) を超えています")
    return result


def od_commands(values: dict[str, int]) -> list[str]:
    """Translate validated OD values into pp_od_clk_voltage commands (without commit)."""
    cmds = []
    for key, value in values.items():
        if key == "sclk_offset":
            cmds.append(f"s {value}")
        elif key == "voltage_offset":
            cmds.append(f"vo {value}")
        else:
            letter = "s" if key.startswith("sclk") else "m"
            cmds.append(f"{letter} {0 if key.endswith('_min') else 1} {value}")
    return cmds


def validate_fan_curve(curve: FanCurve | None, points) -> list[tuple[int, int]]:
    if curve is None:
        raise ValidationError("このGPUではファンカーブ（gpu_od/fan_ctrl/fan_curve）が利用できません")
    if not isinstance(points, (list, tuple)) or len(points) != len(curve.points):
        raise ValidationError(f"ファンカーブは {len(curve.points)} 点で指定してください")
    result = []
    for i, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValidationError(f"ファンカーブの点 {i} が不正です")
        temp = _as_int(point[0], f"点{i}の温度")
        pwm = _as_int(point[1], f"点{i}の回転数")
        if temp not in curve.temp_range:
            raise ValidationError(
                f"点{i}の温度 {temp}°C は範囲 {curve.temp_range.lo}〜{curve.temp_range.hi}°C の外です"
            )
        if pwm not in curve.pwm_range:
            raise ValidationError(
                f"点{i}の回転数 {pwm}% は範囲 {curve.pwm_range.lo}〜{curve.pwm_range.hi}% の外です"
            )
        result.append((temp, pwm))
    for (t0, p0), (t1, p1) in zip(result, result[1:]):
        if t1 <= t0:
            raise ValidationError("ファンカーブの温度は点ごとに増加させてください")
        if p1 < p0:
            raise ValidationError("ファンカーブの回転数は温度が上がるにつれて下げないでください")
    return result


def suggested_fan_curve(curve: FanCurve) -> list[tuple[int, int]]:
    """A sane starting curve inside the driver's ranges (for the editor)."""
    template = [(40, 20), (55, 35), (70, 55), (85, 80), (95, 100)]
    n = len(curve.points)
    if n != len(template):
        step = (curve.temp_range.hi - curve.temp_range.lo) / max(n - 1, 1)
        template = [
            (int(curve.temp_range.lo + step * i), int(30 + 70 * i / max(n - 1, 1))) for i in range(n)
        ]
    tr, pr = curve.temp_range, curve.pwm_range
    out = []
    last_t = tr.lo - 1
    for t, p in template:
        t = min(max(t, tr.lo, last_t + 1), tr.hi)
        p = min(max(p, pr.lo), pr.hi)
        out.append((t, p))
        last_t = t
    return out
