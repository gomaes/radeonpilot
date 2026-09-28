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
from ..sysfs import GpuInfo, discover_gpus, is_asleep, parse_dpm_levels, read_text
from .limiter import MEM_KEY, PIN_LEVEL, LimitState, SoftPowerLimiter, memory_levels
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
        self.limiter = SoftPowerLimiter(self)

    # ------------------------------------------------------------ helpers

    def discover(self) -> list[GpuInfo]:
        return discover_gpus(self.root)

    def _gpu(self, pci, wake: bool = True) -> GpuInfo:
        """Look up a GPU. With wake=True a runtime-suspended GPU is woken first,
        because amdgpu refuses sysfs reads on a suspended device."""
        if not isinstance(pci, str):
            raise ValidationError("GPU の PCI アドレスが指定されていません")
        for gpu in self.discover():
            if gpu.pci_address == pci:
                if wake and is_asleep(gpu):
                    try:
                        self.backend.wake(gpu)
                    except OSError as exc:
                        raise ValidationError(f"スリープ中の GPU を起こせませんでした: {exc}") from None
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
            limit = self.limiter.get(gpu.pci_address)
            if limit and limit.pin_base_level is not None:
                limit.pinned, limit.pin_base_level = False, "auto"
            self._write(gpu, ATTR_PERF_LEVEL, "auto")
        elif category == "power_cap":
            self._stop_limiter(gpu, restore=True)
            power = self._state(gpu).power
            if power is None:
                return
            if power.default_uw is None:
                raise OSError("power1_cap_default が無いためデフォルト値が分かりません")
            self._write(gpu, ATTR_POWER_CAP, str(power.default_uw))
        elif category == "od":
            self.limiter.drop(gpu.pci_address)  # "r" below restores the clocks anyway
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

    def limiter_status(self, pci) -> dict | None:
        """Software limiter state. Does not touch the GPU (safe to poll)."""
        if not isinstance(pci, str):
            raise ValidationError("GPU の PCI アドレスが指定されていません")
        limit = self.limiter.get(pci)
        return limit.to_dict() if limit else None

    def wake(self, pci) -> dict:
        """Wake a runtime-suspended GPU (so the GUI can read its settings)."""
        self._gpu(pci)
        return {"awake": True}

    def state(self, pci) -> dict:
        gpu = self._gpu(pci)
        data = self._state(gpu).to_dict()
        limit = self.limiter.get(gpu.pci_address)
        data["soft_limit"] = limit.to_dict() if limit else None
        if limit and data["od"]:
            # Show the user's own values, not the ones the limiter is currently applying.
            data["od"]["values"][limit.key] = limit.base
            if limit.mem_levels is not None:
                data["od"]["values"][MEM_KEY] = limit.mem_base
        if limit and limit.pin_base_level is not None:
            data["perf_level"] = limit.pin_base_level  # the user's level, not the temporary pin
        return data

    def set_perf_level(self, pci, level) -> dict:
        with self.lock:
            gpu = self._gpu(pci)
            level = control.validate_perf_level(level)
            limit = self.limiter.get(gpu.pci_address)
            if limit and limit.pin_base_level is not None:
                if level.startswith("profile_"):
                    raise ValidationError(
                        "ベースクロック固定が有効な間は profile_* レベルを指定できません（電力目標の設定で無効にしてください）"
                    )
                limit.pin_base_level = level
                limit.pinned = False
            try:
                self._apply_perf_level(gpu, level)
            except OSError as exc:
                raise self._fail(gpu, ["perf_level"], exc) from None
            return self.state(pci)

    def set_power_cap(self, pci, watts) -> dict:
        """Plain power1_cap inside the driver range (stops the software limiter)."""
        with self.lock:
            gpu = self._gpu(pci)
            uw = control.validate_power_cap(self._state(gpu).power, watts)
            try:
                self._stop_limiter(gpu, restore=True)
                self._apply_power_cap(gpu, uw)
            except OSError as exc:
                raise self._fail(gpu, ["power_cap"], exc) from None
            return self.state(pci)

    def set_power_target(self, pci, watts, allow_memory=False, allow_base_clock=False) -> dict:
        """Power target; below power1_cap_min the software limiter takes over.

        allow_base_clock: once the core clock is at its floor, pin GFX to the base
        clock (profile_standard) while the GPU is under load. Off by default.
        allow_memory: as a last stage also lower the memory clock. Off by default:
        memory stays at the user's value.
        """
        allow_memory = False if allow_memory is None else allow_memory
        allow_base_clock = False if allow_base_clock is None else allow_base_clock
        if not isinstance(allow_memory, bool) or not isinstance(allow_base_clock, bool):
            raise ValidationError("allow_memory / allow_base_clock は true/false で指定してください")
        with self.lock:
            gpu = self._gpu(pci)
            st = self._state(gpu)
            uw, soft = control.validate_power_target(st.power, watts, st.od if st.od_available else None)
            old = self.limiter.get(gpu.pci_address)
            level = old.pin_base_level if old and old.pin_base_level else st.perf_level
            if soft and allow_base_clock and (level not in control.PERF_LEVELS or level.startswith("profile_")):
                raise ValidationError(
                    f"パフォーマンスレベルが {level!r} のため、ベースクロック固定は使えません（auto などにしてください）"
                )
            try:
                if soft:
                    if st.power.current_uw != st.power.min_uw:
                        self._apply_power_cap(gpu, st.power.min_uw)
                    self._start_limiter(gpu, uw, allow_memory, allow_base_clock)
                else:
                    self._stop_limiter(gpu, restore=True)
                    self._apply_power_cap(gpu, uw)
            except OSError as exc:
                raise self._fail(gpu, ["power_cap"], exc) from None
            return self.state(pci)

    def _start_limiter(self, gpu: GpuInfo, target_uw: int, allow_memory: bool = False,
                       allow_base_clock: bool = False) -> None:
        cs = self._state(gpu)
        od = cs.od
        key = control.sclk_limit_key(od)
        if key is None:
            raise OSError("コアクロックを制御できません")
        rng = od.range_for(key)
        floor = rng.lo
        if key == "sclk_max":
            floor = max(floor, od.values.get("sclk_min", floor))
        old = self.limiter.get(gpu.pci_address)
        base = old.base if old and old.key == key else od.values[key]
        state = LimitState(target_uw=target_uw, key=key, base=base, current=od.values[key], floor=floor)
        old_mem = old.mem_levels is not None if old else False
        mem_base = old.mem_base if old_mem else od.values.get(MEM_KEY)
        if old_mem and not allow_memory and old.mem_current != old.mem_base:
            self._apply_od(gpu, {MEM_KEY: old.mem_base})  # memory stage switched off: give it back
        if allow_memory:
            if MEM_KEY not in od.supported():
                raise OSError("このGPUではメモリクロック上限を変更できません")
            levels = parse_dpm_levels(read_text(gpu.device_path / "pp_dpm_mclk"))
            state.mem_levels = memory_levels(levels, mem_base, od.values.get("mclk_min"))
            state.mem_base = mem_base
            state.mem_current = od.values[MEM_KEY]
        old_pin_level = old.pin_base_level if old else None
        user_level = old_pin_level or cs.perf_level
        if old and old.pinned and not allow_base_clock:
            self._apply_perf_level(gpu, old_pin_level)  # stage switched off: release the pin
        if allow_base_clock:
            if user_level not in control.PERF_LEVELS or user_level.startswith("profile_"):
                raise OSError(
                    f"パフォーマンスレベルが {user_level!r} のため、ベースクロック固定は使えません（auto にしてください）"
                )
            state.pin_base_level = user_level
            if old and old.pinned:
                state.pinned, state.pinned_at, state.pin_ratio = True, old.pinned_at, old.pin_ratio
        self.limiter.set(gpu.pci_address, state)
        log.info("%s: software power limit %.0f W (%s base %d, floor %d; memory stage %s)",
                 gpu.pci_address, target_uw / 1e6, key, base, floor,
                 state.mem_levels if allow_memory else "off")
        log.info("%s: base clock pin stage %s", gpu.pci_address, "on" if allow_base_clock else "off")

    def _stop_limiter(self, gpu: GpuInfo, restore: bool) -> None:
        """Stop limiting; with restore=True give the user's clock value back."""
        st = self.limiter.drop(gpu.pci_address)
        if not st or not restore:
            return
        values = {}
        if st.current != st.base:
            values[st.key] = st.base
        if st.mem_levels is not None and st.mem_current != st.mem_base:
            values[MEM_KEY] = st.mem_base
        if values:
            self._apply_od(gpu, values)
            log.info("%s: software power limit off, restored %s", gpu.pci_address, values)
        if st.pinned:
            self._apply_perf_level(gpu, st.pin_base_level)
            log.info("%s: base clock pin released (%s)", gpu.pci_address, st.pin_base_level)

    def shutdown(self) -> None:
        """Daemon exit: stop limiting and give the user's clock values back."""
        self.limiter.stop()
        with self.lock:
            for gpu in self.discover():
                if self.limiter.get(gpu.pci_address) is None:
                    continue
                try:
                    if is_asleep(gpu):
                        self.backend.wake(gpu)
                    self._stop_limiter(gpu, restore=True)
                except OSError as exc:
                    log.error("%s: could not restore clocks on exit: %s", gpu.pci_address, exc)

    def limiter_set_perf(self, gpu: GpuInfo, level: str) -> bool:
        """Limiter thread: enter/leave the base-clock pin."""
        with self.lock:
            limit = self.limiter.get(gpu.pci_address)
            if limit is None or limit.pin_base_level is None or level not in (PIN_LEVEL, limit.pin_base_level):
                return False
            try:
                self._apply_perf_level(gpu, level)
                return True
            except OSError as exc:
                self.limiter.drop(gpu.pci_address)
                self._fail(gpu, ["perf_level"], exc)  # logs and resets to auto
                return False

    def limiter_write(self, gpu: GpuInfo, key: str, value: int) -> bool:
        """Called by the limiter thread. Same validation/failure handling as any write."""
        with self.lock:
            if self.limiter.get(gpu.pci_address) is None:
                return False  # disabled meanwhile
            try:
                values = control.validate_od(self._state(gpu).od, {key: value})
                self._apply_od(gpu, values)
                return True
            except ValidationError as exc:
                log.error("%s: limiter value rejected (%s); limiter stopped", gpu.pci_address, exc)
                self._stop_limiter(gpu, restore=False)
            except OSError as exc:
                self.limiter.drop(gpu.pci_address)
                self._fail(gpu, ["od"], exc)  # logs and resets clocks to default
            return False

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
            limit = self.limiter.get(gpu.pci_address)
            if limit and limit.key in values:
                limit.base = limit.current = values[limit.key]
            if limit and limit.mem_levels is not None and MEM_KEY in values:
                levels = parse_dpm_levels(read_text(gpu.device_path / "pp_dpm_mclk"))
                limit.mem_base = limit.mem_current = values[MEM_KEY]
                limit.mem_levels = memory_levels(levels, limit.mem_base, self._state(gpu).od.values.get("mclk_min"))
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
        limit = self.limiter.get(gpu.pci_address)
        if limit:
            settings["power_target_w"] = limit.target_uw / 1_000_000
            if limit.mem_levels is not None:
                settings["power_target_allow_memory"] = True
            if limit.pin_base_level is not None:
                settings["power_target_allow_base_clock"] = True
                settings["perf_level"] = limit.pin_base_level
        elif st.power is not None and st.power.current_uw is not None:
            settings["power_cap_w"] = st.power.current_uw // 1_000_000
        if st.od is not None:
            settings["od"] = {k: st.od.values[k] for k in st.od.supported()}
            if limit:
                settings["od"][limit.key] = limit.base
                if limit.mem_levels is not None and MEM_KEY in settings["od"]:
                    settings["od"][MEM_KEY] = limit.mem_base
        if st.fan_curve is not None and not st.fan_curve.is_driver_default:
            settings["fan_curve"] = [list(p) for p in st.fan_curve.points]
        return settings

    def _validate_settings(self, gpu: GpuInfo, settings) -> dict:
        if not isinstance(settings, dict):
            raise ValidationError("プロファイルの形式が不正です")
        unknown = set(settings) - {
            "perf_level", "power_cap_w", "power_target_w", "power_target_allow_memory",
            "power_target_allow_base_clock", "od", "fan_curve",
        }
        if unknown:
            raise ValidationError(f"プロファイルに不明な項目があります: {', '.join(sorted(unknown))}")
        st = self._state(gpu)
        out: dict = {}
        if settings.get("perf_level") is not None:
            out["perf_level"] = control.validate_perf_level(settings["perf_level"])
        if settings.get("power_cap_w") is not None and settings.get("power_target_w") is not None:
            raise ValidationError("power_cap_w と power_target_w は同時に指定できません")
        if settings.get("power_cap_w") is not None:
            out["power_cap_uw"] = control.validate_power_cap(st.power, settings["power_cap_w"])
        if settings.get("power_target_w") is not None:
            uw, soft = control.validate_power_target(
                st.power, settings["power_target_w"], st.od if st.od_available else None
            )
            if soft:
                out["power_cap_uw"] = st.power.min_uw
                out["soft_target_uw"] = uw
                for name in ("power_target_allow_memory", "power_target_allow_base_clock"):
                    allow = settings.get(name, False)
                    if not isinstance(allow, bool):
                        raise ValidationError(f"{name} は true/false で指定してください")
                out["soft_allow_memory"] = settings.get("power_target_allow_memory", False)
                out["soft_allow_base_clock"] = settings.get("power_target_allow_base_clock", False)
            else:
                out["power_cap_uw"] = uw
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
            self._stop_limiter(gpu, restore=False)  # the profile sets the clocks itself
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
            if "soft_target_uw" in valid:
                try:
                    self._start_limiter(gpu, valid["soft_target_uw"], valid["soft_allow_memory"],
                                        valid["soft_allow_base_clock"])
                except OSError as exc:
                    raise self._fail(gpu, applied, exc) from None

    def list_profiles(self, pci) -> dict:
        gpu = self._gpu(pci, wake=False)
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
            gpu = self._gpu(pci, wake=False)
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
            gpu = self._gpu(pci, wake=False)
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
