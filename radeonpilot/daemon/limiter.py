"""Software power limiter for targets below the driver's power1_cap_min.

The kernel rejects power caps below power1_cap_min, so for lower targets the
hardware cap stays at that minimum and this loop lowers clocks while the
measured power is above the target, and gives them back when there is
headroom. Two stages:

1. core clock ceiling (RDNA3: OD sclk_max, RDNA4: OD sclk_offset) - always;
2. base-clock pin (power_dpm_force_performance_level = profile_standard) -
   only if the user opted in, only once the core is at its floor, and only
   while the GPU is under load. This level pins GFX to the board's base clock
   but also pins FCLK/SoC clock to their minimum and turns off GFX deep sleep,
   ULV and GPO, so it is released as soon as the GPU goes idle;
3. memory clock ceiling (OD mclk_max, stepping through the real DPM levels) -
   only if the user opted in, and only once the earlier stages are exhausted.

Clocks are given back in reverse order (memory first).

It is written so that it never keeps an idle GPU awake:

* ``power/runtime_status`` (a PCI attribute that never touches the GPU) is
  checked first; a runtime-suspended GPU is not read at all.
* While the GPU is awake but idle, samples are spaced further apart than the
  kernel's 5 s autosuspend delay, because every amdgpu sysfs read restarts it.
* Nothing is ever written while the GPU is idle; a clock ceiling that was
  lowered under load simply stays until the next load (it does not affect
  idle power states).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..sysfs import AUTOSUSPEND_DELAY_S, GpuInfo, is_asleep, read_stats

log = logging.getLogger(__name__)

TICK_S = 1.0
IDLE_SAMPLE_S = AUTOSUSPEND_DELAY_S + 3.0  # > autosuspend delay: lets the GPU suspend
IDLE_BUSY_PERCENT = 10
# Clocks are only given back while the GPU is this busy: under light load the
# power is low because there is little work, not because there is headroom,
# and raising the ceiling then just causes an overshoot at the next spike.
RAISE_BUSY_PERCENT = 80
MIN_WRITE_INTERVAL_S = 2.0
OVER = 1.03   # act when power > target * OVER
UNDER = 0.92  # give clocks back when power < target * UNDER
RAISE_STEP_MHZ = 50
MIN_STEP_MHZ = 25
MAX_STEP_MHZ = 200
# Memory steps are coarse (a whole DPM level), so it takes more headroom and a
# hold time before one is given back - otherwise it would oscillate.
MEM_UNDER = 0.82
MEM_HOLD_S = 10.0
MEM_KEY = "mclk_max"
PERF_KEY = "power_dpm_force_performance_level"
PIN_LEVEL = "profile_standard"
PIN_HOLD_S = 15.0


@dataclass
class LimitState:
    target_uw: int
    key: str          # "sclk_max" (RDNA3) or "sclk_offset" (RDNA4)
    base: int         # the user's own value for key
    current: int      # value currently applied
    floor: int        # lowest value the driver allows for key
    next_sample: float = 0.0
    last_write: float = 0.0
    idle_samples: int = 0
    last_power_w: float | None = None
    status: str = "starting"  # starting / sleeping / idle / ok / limiting / floor
    # Memory stage (None = not allowed: memory stays at the user's value).
    mem_levels: list[int] | None = None  # usable mclk_max values, ascending, floor first
    mem_base: int = 0
    mem_current: int = 0
    mem_lowered_at: float = float("-inf")
    # Base-clock pin stage (None = not allowed).
    pin_base_level: str | None = None  # the user's performance level, restored on release
    pinned: bool = False
    pinned_at: float = float("-inf")
    pin_ratio: float = 1.0  # power just before pinning / power while pinned (headroom estimate)
    power_before_pin: float | None = None

    @property
    def mem_floor(self) -> int | None:
        return self.mem_levels[0] if self.mem_levels else None

    def to_dict(self) -> dict:
        return {
            "target_w": self.target_uw / 1_000_000,
            "key": self.key,
            "base": self.base,
            "current": self.current,
            "floor": self.floor,
            "status": self.status,
            "last_power_w": self.last_power_w,
            # Every allowed stage is at its floor: nothing more can be lowered.
            "at_floor": self.current <= self.floor
            and (self.pin_base_level is None or self.pinned)
            and (self.mem_levels is None or self.mem_current <= self.mem_floor),
            "base_clock_pin": None
            if self.pin_base_level is None
            else {"active": self.pinned, "restore_level": self.pin_base_level},
            "memory": None
            if self.mem_levels is None
            else {"base": self.mem_base, "current": self.mem_current, "floor": self.mem_floor},
        }


def memory_levels(dpm_levels: list[int], base: int, mclk_min: int | None) -> list[int]:
    """mclk_max values the memory stage may use: real DPM levels between the floor and base.

    The floor is the second-lowest DPM level (the lowest one is the idle state),
    and never below the user's mclk_min.
    """
    levels = sorted(set(dpm_levels))
    if not levels:
        return [base]
    floor = levels[1] if len(levels) > 2 else levels[0]
    floor = max(floor, mclk_min or 0)
    usable = [lv for lv in levels if floor <= lv < base]
    return usable + [base]


def next_value(st: LimitState, power_w: float, busy_percent: int = 100) -> int:
    """Control law: proportional step down, fixed step up, dead band in between."""
    target_w = st.target_uw / 1_000_000
    if power_w > target_w * OVER:
        excess = (power_w - target_w) / target_w
        step = int(round(excess * 600 / MIN_STEP_MHZ)) * MIN_STEP_MHZ
        step = max(MIN_STEP_MHZ, min(MAX_STEP_MHZ, step))
        return max(st.floor, st.current - step)
    if power_w < target_w * UNDER and st.current < st.base and busy_percent >= RAISE_BUSY_PERCENT:
        return min(st.base, st.current + RAISE_STEP_MHZ)
    return st.current


def decide(st: LimitState, power_w: float, busy_percent: int, now: float) -> tuple[str, int | str] | None:
    """Pick the next (key, value) to write, or None. key is an OD key or PERF_KEY."""
    target_w = st.target_uw / 1_000_000
    if power_w > target_w * OVER:
        if st.current > st.floor:
            return st.key, next_value(st, power_w, busy_percent)
        if st.pin_base_level is not None and not st.pinned:
            return PERF_KEY, PIN_LEVEL
        if st.mem_levels and st.mem_current > st.mem_floor:
            lower = [lv for lv in st.mem_levels if lv < st.mem_current]
            return MEM_KEY, lower[-1]
        return None
    if busy_percent < RAISE_BUSY_PERCENT:
        return None
    if st.mem_levels and st.mem_current < st.mem_base:
        # Memory is given back first; the core stays at its floor until then.
        if power_w < target_w * MEM_UNDER and now - st.mem_lowered_at >= MEM_HOLD_S:
            higher = [lv for lv in st.mem_levels if lv > st.mem_current]
            return MEM_KEY, higher[0]
        return None
    if st.pinned:
        # Release only if the unpinned power (estimated) would fit, after a hold time.
        if power_w * st.pin_ratio < target_w * UNDER and now - st.pinned_at >= PIN_HOLD_S:
            return PERF_KEY, st.pin_base_level
        return None
    new = next_value(st, power_w, busy_percent)
    return (st.key, new) if new != st.current else None


class SoftPowerLimiter:
    def __init__(self, controller, clock=time.monotonic) -> None:
        self.controller = controller
        self.clock = clock
        self.states: dict[str, LimitState] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="soft-power-limiter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("limiter tick failed")

    # ------------------------------------------------------------ state

    def get(self, pci: str) -> LimitState | None:
        with self.lock:
            return self.states.get(pci)

    def set(self, pci: str, state: LimitState) -> None:
        with self.lock:
            self.states[pci] = state

    def drop(self, pci: str) -> LimitState | None:
        with self.lock:
            return self.states.pop(pci, None)

    # ------------------------------------------------------------ loop

    def tick(self) -> None:
        now = self.clock()
        with self.lock:
            due = [(pci, st) for pci, st in self.states.items() if now >= st.next_sample]
        if not due:
            return
        gpus = {g.pci_address: g for g in self.controller.discover()}
        for pci, st in due:
            gpu = gpus.get(pci)
            if gpu is None:
                st.status = "missing"
                st.next_sample = now + IDLE_SAMPLE_S
                continue
            self._sample(gpu, st, now)

    def _sample(self, gpu: GpuInfo, st: LimitState, now: float) -> None:
        if is_asleep(gpu):
            st.status = "sleeping"
            st.idle_samples = 0
            st.next_sample = now + TICK_S  # runtime_status is free to read
            return
        stats = read_stats(gpu)
        power, busy = stats.power_w, stats.busy_percent
        st.last_power_w = power
        target_w = st.target_uw / 1_000_000
        if power is None or busy is None:
            st.next_sample = now + IDLE_SAMPLE_S
            return
        if busy < IDLE_BUSY_PERCENT and power <= target_w:
            if st.pinned:
                # The one write allowed at idle: leave profile_standard so GFX deep
                # sleep / ULV / GPO and the normal idle clocks come back.
                self._write(gpu, st, PERF_KEY, st.pin_base_level, power, now)
            # Idle: never write, and back off so the GPU can autosuspend.
            st.idle_samples += 1
            st.status = "idle"
            st.next_sample = now + (TICK_S if st.idle_samples < 3 else IDLE_SAMPLE_S)
            return
        st.idle_samples = 0
        st.next_sample = now + TICK_S
        if st.pinned and st.power_before_pin and now - st.pinned_at >= 2 * TICK_S:
            st.pin_ratio = max(1.0, st.power_before_pin / max(power, 1.0))
            st.power_before_pin = None
        step = decide(st, power, busy, now)
        over = power > target_w * OVER
        lowered = st.current < st.base or st.pinned or (st.mem_levels is not None and st.mem_current < st.mem_base)
        st.status = "floor" if over and step is None else ("limiting" if lowered or over else "ok")
        if step is None or now - st.last_write < MIN_WRITE_INTERVAL_S:
            return
        key, value = step
        log.debug("%s: %.0f W (target %.0f W): %s -> %s", gpu.pci_address, power, target_w, key, value)
        self._write(gpu, st, key, value, power, now)

    def _write(self, gpu: GpuInfo, st: LimitState, key: str, value, power: float, now: float) -> None:
        if key == PERF_KEY:
            if not self.controller.limiter_set_perf(gpu, value):
                return
            st.pinned = value == PIN_LEVEL
            if st.pinned:
                st.pinned_at = now
                st.power_before_pin = power
            st.last_write = now
            return
        if self.controller.limiter_write(gpu, key, value):
            if key == MEM_KEY:
                if value < st.mem_current:
                    st.mem_lowered_at = now
                st.mem_current = value
            else:
                st.current = value
            st.last_write = now
