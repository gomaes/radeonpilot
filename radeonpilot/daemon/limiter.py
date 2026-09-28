"""Software power limiter for targets below the driver's power1_cap_min.

The kernel rejects power caps below power1_cap_min, so for lower targets the
hardware cap stays at that minimum and this loop lowers the core clock
ceiling (RDNA3: OD sclk_max, RDNA4: OD sclk_offset) while the measured power
is above the target, and gives it back when there is headroom.

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

    def to_dict(self) -> dict:
        return {
            "target_w": self.target_uw / 1_000_000,
            "key": self.key,
            "base": self.base,
            "current": self.current,
            "floor": self.floor,
            "status": self.status,
            "last_power_w": self.last_power_w,
        }


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
            # Idle: never write, and back off so the GPU can autosuspend.
            st.idle_samples += 1
            st.status = "idle"
            st.next_sample = now + (TICK_S if st.idle_samples < 3 else IDLE_SAMPLE_S)
            return
        st.idle_samples = 0
        st.next_sample = now + TICK_S
        new = next_value(st, power, busy)
        over = power > target_w * OVER
        st.status = "floor" if over and new == st.floor == st.current else ("limiting" if st.current < st.base or over else "ok")
        if new == st.current or now - st.last_write < MIN_WRITE_INTERVAL_S:
            return
        log.debug("%s: %.0f W (target %.0f W): %s %d -> %d", gpu.pci_address, power, target_w, st.key, st.current, new)
        if self.controller.limiter_write(gpu, st.key, new):
            st.current = new
            st.last_write = now
