"""Validated, fail-safe control operations.

Every operation follows the same sequence:

1. re-read the current state and the ranges the driver reports,
2. validate the whole request (nothing is written if anything is invalid),
3. write,
4. read back and compare,
5. on any write error or readback mismatch, reset the affected settings to
   the driver defaults and report the failure.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from .. import control
from ..control import (
    ATTR_OD,
    ATTR_PERF_LEVEL,
    ATTR_POWER_CAP,
    ControlState,
    ValidationError,
)
from ..sysfs import GpuInfo, discover_gpus
from .profiles import ProfileStore

log = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "perf_level": "パフォーマンスレベル",
    "power_cap": "電力上限",
    "od": "クロック/電圧 (OverDrive)",
    "fan_curve": "ファンカーブ",
}

_PROFILE_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,64}$")


class ControlError(Exception):
    """A write failed; the affected settings were reset to their defaults."""


class Controller:
    def __init__(self, backend, root: Path, store: ProfileStore) -> None:
        self.backend = backend
        self.root = Path(root)
        self.store = store
        self.lock = threading.RLock()

    # ------------------------------------------------------------ helpers

    def _gpu(self, pci) -> GpuInfo:
        if not isinstance(pci, str):
            raise ValidationError("GPU の PCI アドレスが指定されていません")
        for gpu in discover_gpus(self.root):
            if gpu.pci_address == pci:
                return gpu
        raise ValidationError(f"GPU {pci} が見つかりません")

    def _state(self, gpu: GpuInfo) -> ControlState:
        return control.read_control_state(gpu, self.root)

    def _write(self, gpu: GpuInfo, attr: str, text: str) -> None:
        if attr == ATTR_POWER_CAP:
            if gpu.hwmon_path is None:
                raise OSError("hwmon が見つかりません")
            path = gpu.hwmon_path / attr
        elif attr == control.ATTR_FAN_CURVE:
            path = control.fan_curve_path(gpu)
        else:
            path = gpu.device_path / attr
        self.backend.write(path, text)

    # ------------------------------------------------------------ resets

    def _reset_category(self, gpu: GpuInfo, category: str) -> None:
        """Restore one category to the driver default. Raises OSError on failure."""
        if category == "perf_level":
            self._write(gpu, ATTR_PERF_LEVEL, "auto")
        elif category == "power_cap":
            power = self._state(gpu).power
            if power is None:
                return
            if power.default_uw is None:
                raise OSError("power1_cap_default が無いためデフォルト値が分かりません")
            self._write(gpu, ATTR_POWER_CAP, str(power.default_uw))
        elif category == "od":
            if self._state(gpu).od is None:
                return
            # On SMU13/14 (RDNA3/4) "r" restores the defaults *and* commits them, so no
            # separate "c" is needed - which keeps the reset working when commit is broken.
            self._write(gpu, ATTR_OD, "r")
        elif category == "fan_curve":
            if self._state(gpu).fan_curve is None:
                return
            self._write(gpu, control.ATTR_FAN_CURVE, "r")

    def _reset_categories(self, gpu: GpuInfo, categories) -> list[str]:
        errors = []
        for category in categories:
            try:
                self._reset_category(gpu, category)
            except OSError as exc:
                log.error("%s: reset of %s failed: %s", gpu.pci_address, category, exc)
                errors.append(f"{CATEGORY_LABELS[category]}: {exc}")
        return errors

    def _fail(self, gpu: GpuInfo, categories, exc: Exception) -> ControlError:
        log.error("%s: write failed (%s); resetting %s", gpu.pci_address, exc, ", ".join(categories))
        errors = self._reset_categories(gpu, categories)
        labels = "、".join(CATEGORY_LABELS[c] for c in categories)
        msg = f"書き込みに失敗しました: {exc}\n安全のため {labels} をデフォルトに戻しました。"
        if errors:
            msg += "\nただしリセットにも失敗しました: " + " / ".join(errors) + "\n再起動を推奨します。"
        return ControlError(msg)

    # ------------------------------------------------------------ apply primitives
    # These assume validated input and raise OSError on failure/mismatch.

    def _apply_perf_level(self, gpu, level: str) -> None:
        self._write(gpu, ATTR_PERF_LEVEL, level)
        now = self._state(gpu).perf_level
        if now != level:
            raise OSError(f"読み戻し値が一致しません（{now!r} ≠ {level!r}）")

    def _apply_power_cap(self, gpu, uw: int) -> None:
        self._write(gpu, ATTR_POWER_CAP, str(uw))
        power = self._state(gpu).power
        # The driver stores whole watts.
        if power is None or power.current_uw is None or abs(power.current_uw - uw) >= 1_000_000:
            got = None if power is None else power.current_w
            raise OSError(f"読み戻し値が一致しません（{got} W）")

    def _apply_od(self, gpu, values: dict[str, int]) -> None:
        for cmd in control.od_commands(values):
            self._write(gpu, ATTR_OD, cmd)
        self._write(gpu, ATTR_OD, "c")
        od = self._state(gpu).od
        mismatched = [k for k, v in values.items() if od is None or od.values.get(k) != v]
        if mismatched:
            raise OSError(f"読み戻し値が一致しません（{', '.join(mismatched)}）")

    def _apply_fan_curve(self, gpu, points: list[tuple[int, int]]) -> None:
        for i, (temp, pwm) in enumerate(points):
            self._write(gpu, control.ATTR_FAN_CURVE, f"{i} {temp} {pwm}")
        self._write(gpu, control.ATTR_FAN_CURVE, "c")
        curve = self._state(gpu).fan_curve
        if curve is None or curve.points != list(points):
            raise OSError("ファンカーブの読み戻し値が一致しません")

    # ------------------------------------------------------------ commands

    def gpus(self) -> list[dict]:
        return [
            {
                "pci": g.pci_address,
                "card": g.card,
                "name": g.name,
                "device_id": g.vk_device_select,
            }
            for g in discover_gpus(self.root)
        ]

    def state(self, pci) -> dict:
        return self._state(self._gpu(pci)).to_dict()

    def set_perf_level(self, pci, level) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            level = control.validate_perf_level(level)
            try:
                self._apply_perf_level(gpu, level)
            except OSError as exc:
                raise self._fail(gpu, ["perf_level"], exc) from None
            return self.state(pci)

    def set_power_cap(self, pci, watts) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            uw = control.validate_power_cap(self._state(gpu).power, watts)
            try:
                self._apply_power_cap(gpu, uw)
            except OSError as exc:
                raise self._fail(gpu, ["power_cap"], exc) from None
            return self.state(pci)

    def set_od(self, pci, values) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            st = self._state(gpu)
            if not st.od_available:
                raise ValidationError(st.od_block_reason)
            values = control.validate_od(st.od, values)
            try:
                self._apply_od(gpu, values)
            except OSError as exc:
                raise self._fail(gpu, ["od"], exc) from None
            return self.state(pci)

    def set_fan_curve(self, pci, points) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            st = self._state(gpu)
            if not st.od_available:
                raise ValidationError(st.od_block_reason)
            points = control.validate_fan_curve(st.fan_curve, points)
            try:
                self._apply_fan_curve(gpu, points)
            except OSError as exc:
                raise self._fail(gpu, ["fan_curve"], exc) from None
            return self.state(pci)

    def reset_fan_curve(self, pci) -> dict:
        return self._reset(pci, ["fan_curve"])

    def reset_od(self, pci) -> dict:
        return self._reset(pci, ["od"])

    def reset(self, pci) -> dict:
        return self._reset(pci, ["perf_level", "power_cap", "od", "fan_curve"])

    def _reset(self, pci, categories) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            errors = self._reset_categories(gpu, categories)
            if errors:
                raise ControlError("デフォルトへのリセットに失敗しました: " + " / ".join(errors))
            return self.state(pci)

    def reset_all_gpus(self) -> list[str]:
        errors = []
        for info in self.gpus():
            try:
                self.reset(info["pci"])
            except (ControlError, ValidationError) as exc:
                errors.append(f"{info['pci']}: {exc}")
        return errors

    # ------------------------------------------------------------ settings / profiles

    def snapshot(self, gpu: GpuInfo) -> dict:
        st = self._state(gpu)
        settings: dict = {}
        if st.perf_level in control.PERF_LEVELS:
            settings["perf_level"] = st.perf_level
        if st.power is not None and st.power.current_uw is not None:
            settings["power_cap_w"] = st.power.current_uw // 1_000_000
        if st.od is not None:
            settings["od"] = {k: st.od.values[k] for k in st.od.supported()}
        if st.fan_curve is not None and not st.fan_curve.is_driver_default:
            settings["fan_curve"] = [list(p) for p in st.fan_curve.points]
        return settings

    def _validate_settings(self, gpu: GpuInfo, settings) -> dict:
        if not isinstance(settings, dict):
            raise ValidationError("プロファイルの形式が不正です")
        unknown = set(settings) - {"perf_level", "power_cap_w", "od", "fan_curve"}
        if unknown:
            raise ValidationError(f"プロファイルに不明な項目があります: {', '.join(sorted(unknown))}")
        st = self._state(gpu)
        out: dict = {}
        if settings.get("perf_level") is not None:
            out["perf_level"] = control.validate_perf_level(settings["perf_level"])
        if settings.get("power_cap_w") is not None:
            out["power_cap_uw"] = control.validate_power_cap(st.power, settings["power_cap_w"])
        if settings.get("od") or settings.get("fan_curve"):
            if not st.od_available:
                raise ValidationError(
                    "プロファイルにクロック/電圧/ファンカーブが含まれていますが、OverDrive が無効です: "
                    + st.od_block_reason
                )
        if settings.get("od"):
            out["od"] = control.validate_od(st.od, settings["od"])
        if settings.get("fan_curve"):
            out["fan_curve"] = control.validate_fan_curve(st.fan_curve, settings["fan_curve"])
        return out

    def apply_settings(self, gpu: GpuInfo, settings) -> None:
        with self.lock:
            valid = self._validate_settings(gpu, settings)  # all-or-nothing validation
            applied: list[str] = []
            steps = (
                ("perf_level", "perf_level", self._apply_perf_level),
                ("power_cap_uw", "power_cap", self._apply_power_cap),
                ("od", "od", self._apply_od),
                ("fan_curve", "fan_curve", self._apply_fan_curve),
            )
            for key, category, fn in steps:
                if key not in valid:
                    continue
                applied.append(category)
                try:
                    fn(gpu, valid[key])
                except OSError as exc:
                    # Leave the GPU in a known state: everything this profile touched goes back to default.
                    raise self._fail(gpu, applied, exc) from None

    def list_profiles(self, pci) -> dict:
        gpu = self._gpu(pci)
        entry = self.store.load()["gpus"].get(gpu.pci_address, {})
        return {
            "profiles": entry.get("profiles", {}),
            "boot_profile": entry.get("boot_profile"),
        }

    @staticmethod
    def _check_name(name) -> str:
        if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name) or name != name.strip():
            raise ValidationError("プロファイル名は 1〜64 文字で、制御文字・/・\\ を含めないでください")
        return name

    def save_profile(self, pci, name) -> dict:
        """Store the settings that are *currently applied* on the GPU."""
        with self.lock:
            gpu = self._gpu(pci)
            name = self._check_name(name)
            data = self.store.load()
            entry = self.store.gpu_entry(data, gpu.pci_address, gpu.vk_device_select)
            entry["profiles"][name] = self.snapshot(gpu)
            self.store.save(data)
            return self.list_profiles(pci)

    def delete_profile(self, pci, name) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            data = self.store.load()
            entry = data["gpus"].get(gpu.pci_address)
            if not entry or name not in entry.get("profiles", {}):
                raise ValidationError(f"プロファイル {name!r} はありません")
            del entry["profiles"][name]
            if entry.get("boot_profile") == name:
                entry["boot_profile"] = None
            self.store.save(data)
            return self.list_profiles(pci)

    def set_boot_profile(self, pci, name) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            data = self.store.load()
            entry = self.store.gpu_entry(data, gpu.pci_address, gpu.vk_device_select)
            if name is not None and name not in entry["profiles"]:
                raise ValidationError(f"プロファイル {name!r} はありません")
            entry["boot_profile"] = name
            self.store.save(data)
            return self.list_profiles(pci)

    def apply_profile(self, pci, name) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            profiles = self.list_profiles(pci)["profiles"]
            if name not in profiles:
                raise ValidationError(f"プロファイル {name!r} はありません")
            self.apply_settings(gpu, profiles[name])
            return self.state(pci)

    def apply_boot_profiles(self, wait_seconds: float = 30.0) -> None:
        """Apply each GPU's boot profile. Waits for GPUs that are not up yet."""
        data = self.store.load()
        pending = {
            pci: entry
            for pci, entry in data["gpus"].items()
            if isinstance(entry, dict) and entry.get("boot_profile")
        }
        deadline = time.monotonic() + wait_seconds
        while pending:
            found = {g.pci_address: g for g in discover_gpus(self.root)}
            for pci in [p for p in pending if p in found]:
                entry = pending.pop(pci)
                gpu = found[pci]
                name = entry["boot_profile"]
                if entry.get("device_id") != gpu.vk_device_select:
                    log.warning(
                        "%s: boot profile %r was saved for %s but the GPU is now %s; skipping",
                        pci, name, entry.get("device_id"), gpu.vk_device_select,
                    )
                    continue
                settings = entry.get("profiles", {}).get(name)
                if settings is None:
                    log.warning("%s: boot profile %r does not exist", pci, name)
                    continue
                try:
                    self.apply_settings(gpu, settings)
                    log.info("%s: applied boot profile %r", pci, name)
                except (ValidationError, ControlError) as exc:
                    log.error("%s: boot profile %r not applied: %s", pci, name, exc)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        for pci in pending:
            log.warning("%s: GPU not found; boot profile not applied", pci)
