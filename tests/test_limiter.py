"""Software power limiter and deep-idle behaviour, against the emulator."""

import pytest

from radeonpilot import control, sysfs
from radeonpilot.control import ValidationError
from radeonpilot.daemon import limiter as limiter_mod
from radeonpilot.daemon.controller import ControlError
from radeonpilot.emulator import Simulator
from tests.conftest import RX7900XTX, RX9070XT


@pytest.fixture
def clock(controller):
    t = [1000.0]
    controller.limiter.clock = lambda: t[0]
    return t


def run(controller, backend, clock, seconds):
    sim = Simulator(backend)
    samples = []
    for _ in range(seconds):
        sim.step(1.0)
        clock[0] += 1.0
        controller.limiter.tick()
        gpu = {g.pci_address: g for g in controller.discover()}
        samples.append({pci: sysfs.read_stats(g) for pci, g in gpu.items()})
    return samples


def test_validate_power_target():
    p = control.PowerCap(304_000_000, 274_000_000, 334_000_000, 304_000_000)
    od = control.parse_od("OD_SCLK_OFFSET:\n0Mhz\nOD_RANGE:\nSCLK_OFFSET: -500Mhz 1000Mhz\n")
    assert control.validate_power_target(p, 280, od) == (280_000_000, False)
    assert control.validate_power_target(p, 182.4, od) == (182_400_000, True)  # -40 %
    for bad in (121, 335, None, "182"):
        with pytest.raises(ValidationError):
            control.validate_power_target(p, bad, od)
    with pytest.raises(ValidationError) as exc:
        control.validate_power_target(p, 182, None)  # below min needs OverDrive
    assert "OverDrive" in str(exc.value)


def test_below_min_is_rejected_by_plain_power_cap(controller):
    with pytest.raises(ValidationError):
        controller.set_power_cap(RX9070XT, 182)


def test_rdna4_minus_40(controller, backend, clock):
    st = controller.set_power_target(RX9070XT, 182)
    assert st["power"]["current_w"] == 274  # hardware cap at the driver minimum
    assert st["soft_limit"]["key"] == "sclk_offset" and st["soft_limit"]["floor"] == -500
    samples = run(controller, backend, clock, 90)
    lim = controller.limiter.get(RX9070XT)
    offsets = [int(t.split()[1]) for a, t in backend.writes if a == "pp_od_clk_voltage" and t.startswith("s ")]
    assert offsets and min(offsets) < 0  # clock offset was lowered under load
    powers = [s[RX9070XT].power_w for s in samples[-45:] if s[RX9070XT].busy_percent > 50]
    # Either held near the target on average, or honestly reporting that the clock floor was hit.
    assert lim.status == "floor" or sum(powers) / len(powers) < 182 * 1.08
    assert controller.state(RX9070XT)["od"]["values"]["sclk_offset"] == 0  # user's value shown
    power_writes = [t for a, t in backend.writes if a == "power1_cap"]
    assert power_writes == ["274000000"]


def test_rdna3_minus_40_holds_target(controller, backend, clock):
    controller.set_power_target(RX7900XTX, 203)
    samples = run(controller, backend, clock, 160)
    lim = controller.limiter.get(RX7900XTX)
    assert lim.key == "sclk_max" and lim.floor == 500
    busy = [s[RX7900XTX].power_w for s in samples[-60:]
            if not s[RX7900XTX].asleep and s[RX7900XTX].busy_percent > 90]
    assert busy and sum(busy) / len(busy) < 203 * 1.06


def test_no_reads_while_suspended_and_no_writes_while_idle(controller, backend, clock, monkeypatch):
    controller.set_power_target(RX7900XTX, 203)
    reads = []
    real = limiter_mod.read_stats
    monkeypatch.setattr(limiter_mod, "read_stats", lambda g: reads.append(clock[0]) or real(g))
    sim = Simulator(backend)
    asleep_reads = 0
    for _ in range(120):
        sim.step(1.0)
        clock[0] += 1.0
        gpu = next(g for g in controller.discover() if g.pci_address == RX7900XTX)
        before = len(reads)
        writes_before = len(backend.writes)
        was_asleep = sysfs.is_asleep(gpu)
        controller.limiter.tick()
        if was_asleep:
            asleep_reads += len(reads) - before
            assert len(backend.writes) == writes_before
    assert asleep_reads == 0
    lim = controller.limiter.get(RX7900XTX)
    assert lim.status in ("sleeping", "idle", "limiting", "ok", "floor")


def test_idle_sampling_backs_off_beyond_autosuspend(controller, clock):
    controller.set_power_target(RX9070XT, 182)
    lim = controller.limiter.get(RX9070XT)
    gpu = next(g for g in controller.discover() if g.pci_address == RX9070XT)
    (gpu.device_path / "gpu_busy_percent").write_text("2\n")
    (gpu.hwmon_path / "power1_input").write_text("15000000\n")
    gaps = []
    for _ in range(6):
        controller.limiter._sample(gpu, lim, clock[0])
        gaps.append(lim.next_sample - clock[0])
        clock[0] = lim.next_sample
    assert gaps[-1] > sysfs.AUTOSUSPEND_DELAY_S
    assert lim.status == "idle"


def test_read_stats_skips_suspended_gpu(emu_root, monkeypatch):
    gpu = next(g for g in sysfs.discover_gpus(emu_root) if g.pci_address == RX7900XTX)
    (gpu.device_path / "power/runtime_status").write_text("suspended\n")
    touched = []
    real = sysfs.read_text
    monkeypatch.setattr(sysfs, "read_text", lambda p: touched.append(p.name) or real(p))
    stats = sysfs.read_stats(gpu)
    assert stats.asleep and touched == ["runtime_status"]


def test_operations_wake_a_sleeping_gpu(controller, backend):
    gpu = next(g for g in controller.discover() if g.pci_address == RX7900XTX)
    backend._state["gpus"]["7900xtx"]["sim"]["asleep"] = True
    (gpu.device_path / "power/runtime_status").write_text("suspended\n")
    assert controller.state(RX7900XTX)["power"]["current_w"] == 339
    assert not sysfs.is_asleep(gpu)
    # Profile bookkeeping does not need (or wake) the GPU.
    (gpu.device_path / "power/runtime_status").write_text("suspended\n")
    controller.list_profiles(RX7900XTX)
    assert sysfs.is_asleep(gpu)


def test_back_in_range_restores_clocks(controller, backend, clock):
    controller.set_power_target(RX7900XTX, 203)
    lim = controller.limiter.get(RX7900XTX)
    controller.limiter_write(next(g for g in controller.discover() if g.pci_address == RX7900XTX), "sclk_max", 1800)
    lim.current = 1800
    st = controller.set_power_target(RX7900XTX, 330)
    assert st["soft_limit"] is None and st["power"]["current_w"] == 330
    assert control.parse_od((next(g for g in controller.discover() if g.pci_address == RX7900XTX).device_path
                             / "pp_od_clk_voltage").read_text()).values["sclk_max"] == 2500


def test_user_od_change_updates_base(controller):
    controller.set_power_target(RX7900XTX, 203)
    controller.set_od(RX7900XTX, {"sclk_max": 2400})
    lim = controller.limiter.get(RX7900XTX)
    assert lim.base == 2400 and lim.current == 2400


def test_profile_roundtrip(controller, backend, clock):
    controller.set_power_target(RX9070XT, 182)
    run(controller, backend, clock, 20)
    controller.save_profile(RX9070XT, "eco")
    prof = controller.list_profiles(RX9070XT)["profiles"]["eco"]
    assert prof["power_target_w"] == 182 and "power_cap_w" not in prof
    assert prof["od"]["sclk_offset"] == 0  # base, not the throttled value
    controller.reset(RX9070XT)
    assert controller.limiter.get(RX9070XT) is None
    st = controller.apply_profile(RX9070XT, "eco")
    assert st["soft_limit"]["target_w"] == 182 and st["power"]["current_w"] == 274


def test_limiter_write_failure_stops_and_resets(controller, backend, clock):
    controller.set_power_target(RX9070XT, 182)
    backend.fail.add("pp_od_clk_voltage:c")
    for _ in range(120):
        run(controller, backend, clock, 1)
        if controller.limiter.get(RX9070XT) is None:
            break
    backend.fail.clear()
    assert any(a == "pp_od_clk_voltage" and t.startswith("s -") for a, t in backend.writes)  # it did try
    assert controller.limiter.get(RX9070XT) is None
    assert controller.state(RX9070XT)["od"]["values"]["sclk_offset"] == 0


def test_shutdown_restores_user_clocks(controller, backend, clock):
    controller.set_power_target(RX7900XTX, 203)
    run(controller, backend, clock, 40)
    gpu = next(g for g in controller.discover() if g.pci_address == RX7900XTX)
    controller.shutdown()
    od = control.parse_od((gpu.device_path / "pp_od_clk_voltage").read_text())
    assert od.values["sclk_max"] == 2500


def test_no_clock_raise_under_light_load():
    st = limiter_mod.LimitState(target_uw=200_000_000, key="sclk_max", base=2500, current=1800, floor=500)
    assert limiter_mod.next_value(st, 100.0, busy_percent=40) == 1800  # light load: keep the ceiling
    assert limiter_mod.next_value(st, 100.0, busy_percent=95) == 1850  # GPU-bound with headroom: raise
    assert limiter_mod.next_value(st, 260.0, busy_percent=95) < 1800   # over target: lower
    st.current = 500
    assert limiter_mod.next_value(st, 260.0) == 500                    # never below the floor
