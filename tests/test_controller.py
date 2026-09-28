import json

import pytest

from radeonpilot.control import ValidationError
from radeonpilot.daemon.controller import ControlError
from tests.conftest import RX7900XTX, RX9070XT


def od_writes(backend):
    return [t for a, t in backend.writes if a == "pp_od_clk_voltage"]


def test_power_cap(controller, backend):
    st = controller.set_power_cap(RX9070XT, 280)
    assert st["power"]["current_w"] == 280
    with pytest.raises(ValidationError):
        controller.set_power_cap(RX9070XT, 335)
    with pytest.raises(ValidationError):
        controller.set_power_cap(RX9070XT, 100)
    assert [t for a, t in backend.writes if a == "power1_cap"] == ["280000000"]  # rejected values never written


def test_perf_level(controller):
    assert controller.set_perf_level(RX7900XTX, "high")["perf_level"] == "high"
    with pytest.raises(ValidationError):
        controller.set_perf_level(RX7900XTX, "../../etc/passwd")


def test_od_rdna3(controller, backend):
    st = controller.set_od(RX7900XTX, {"sclk_max": 2700, "mclk_max": 1300, "voltage_offset": -50})
    assert st["od"]["values"]["sclk_max"] == 2700
    assert od_writes(backend) == ["s 1 2700", "m 1 1300", "vo -50", "c"]
    with pytest.raises(ValidationError):
        controller.set_od(RX7900XTX, {"sclk_max": 3100})
    with pytest.raises(ValidationError):
        controller.set_od(RX7900XTX, {"sclk_offset": 100})  # RDNA3 has no offset


def test_od_rdna4(controller, backend):
    st = controller.set_od(RX9070XT, {"sclk_offset": -150, "voltage_offset": -60})
    assert st["od"]["values"]["sclk_offset"] == -150
    assert od_writes(backend) == ["s -150", "vo -60", "c"]
    with pytest.raises(ValidationError):
        controller.set_od(RX9070XT, {"voltage_offset": -201})


def test_unknown_gpu(controller):
    with pytest.raises(ValidationError):
        controller.set_power_cap("0000:ff:00.0", 280)
    with pytest.raises(ValidationError):
        controller.set_power_cap(None, 280)


def test_write_failure_resets_power(controller, backend):
    controller.set_power_cap(RX9070XT, 280)
    backend.fail.add("power1_cap")
    with pytest.raises(ControlError) as exc:
        controller.set_power_cap(RX9070XT, 290)
    # Reset of the same attribute also fails -> reported.
    assert "リセットにも失敗" in str(exc.value)
    backend.fail.clear()


def test_commit_failure_resets_od(controller, backend):
    controller.set_od(RX7900XTX, {"sclk_max": 2700})
    backend.fail.add("pp_od_clk_voltage:c")
    with pytest.raises(ControlError) as exc:
        controller.set_od(RX7900XTX, {"sclk_max": 2800})
    assert "デフォルトに戻しました" in str(exc.value) and "リセットにも失敗" not in str(exc.value)
    assert od_writes(backend)[-1] == "r"
    backend.fail.clear()
    assert controller.state(RX7900XTX)["od"]["values"]["sclk_max"] == 2500  # default


def test_readback_mismatch_resets(controller, backend, monkeypatch):
    # Simulate a driver that silently ignores the value.
    real_write = backend.write

    def lying_write(path, text):
        if path.name == "power1_cap" and text != "304000000":
            return
        real_write(path, text)

    monkeypatch.setattr(backend, "write", lying_write)
    with pytest.raises(ControlError) as exc:
        controller.set_power_cap(RX9070XT, 280)
    assert "読み戻し" in str(exc.value)
    assert controller.state(RX9070XT)["power"]["current_w"] == 304


def test_fan_curve(controller, backend):
    pts = [[35, 20], [50, 30], [65, 50], [80, 75], [95, 100]]
    st = controller.set_fan_curve(RX9070XT, pts)
    assert st["fan_curve"]["points"] == pts and not st["fan_curve"]["driver_default"]
    with pytest.raises(ValidationError):
        controller.set_fan_curve(RX9070XT, [[35, 20]] * 5)
    st = controller.reset_fan_curve(RX9070XT)
    assert st["fan_curve"]["driver_default"]


def test_reset_all(controller):
    controller.set_power_cap(RX7900XTX, 380)
    controller.set_perf_level(RX7900XTX, "high")
    controller.set_od(RX7900XTX, {"sclk_max": 2800})
    controller.set_fan_curve(RX7900XTX, [[35, 20], [50, 30], [65, 50], [80, 75], [95, 100]])
    st = controller.reset(RX7900XTX)
    assert st["power"]["current_w"] == 339
    assert st["perf_level"] == "auto"
    assert st["od"]["values"]["sclk_max"] == 2500
    assert st["fan_curve"]["driver_default"]


def test_profiles(controller, tmp_path):
    controller.set_power_cap(RX9070XT, 290)
    controller.set_od(RX9070XT, {"sclk_offset": 100})
    controller.save_profile(RX9070XT, "OC")
    controller.set_boot_profile(RX9070XT, "OC")
    cfg = json.loads((tmp_path / "etc/config.json").read_text())
    entry = cfg["gpus"][RX9070XT]
    assert entry["device_id"] == "1002:7550" and entry["boot_profile"] == "OC"
    assert entry["profiles"]["OC"]["power_cap_w"] == 290
    assert entry["profiles"]["OC"]["od"]["sclk_offset"] == 100
    assert "fan_curve" not in entry["profiles"]["OC"]  # driver default curve is not stored

    controller.reset(RX9070XT)
    st = controller.apply_profile(RX9070XT, "OC")
    assert st["power"]["current_w"] == 290 and st["od"]["values"]["sclk_offset"] == 100

    for bad in ("", " x", "a/b", "x" * 65, None, "\n"):
        with pytest.raises(ValidationError):
            controller.save_profile(RX9070XT, bad)
    with pytest.raises(ValidationError):
        controller.set_boot_profile(RX9070XT, "nope")
    controller.delete_profile(RX9070XT, "OC")
    assert controller.list_profiles(RX9070XT) == {"profiles": {}, "boot_profile": None}


def test_boot_apply(controller, tmp_path):
    controller.set_power_cap(RX7900XTX, 360)
    controller.save_profile(RX7900XTX, "boot")
    controller.set_boot_profile(RX7900XTX, "boot")
    controller.reset(RX7900XTX)
    controller.apply_boot_profiles(wait_seconds=0)
    assert controller.state(RX7900XTX)["power"]["current_w"] == 360


def test_boot_apply_skips_swapped_gpu(controller, tmp_path):
    controller.set_power_cap(RX7900XTX, 360)
    controller.save_profile(RX7900XTX, "boot")
    controller.set_boot_profile(RX7900XTX, "boot")
    controller.reset(RX7900XTX)
    path = tmp_path / "etc/config.json"
    cfg = json.loads(path.read_text())
    cfg["gpus"][RX7900XTX]["device_id"] = "1002:7550"
    path.write_text(json.dumps(cfg))
    controller.apply_boot_profiles(wait_seconds=0)
    assert controller.state(RX7900XTX)["power"]["current_w"] == 339


def test_invalid_profile_writes_nothing(controller, backend, tmp_path):
    path = tmp_path / "etc/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "gpus": {RX9070XT: {
        "device_id": "1002:7550", "boot_profile": "bad",
        "profiles": {"bad": {"power_cap_w": 290, "od": {"sclk_offset": 5000}}}}}}))
    backend.writes.clear()
    with pytest.raises(ValidationError):
        controller.apply_profile(RX9070XT, "bad")
    controller.apply_boot_profiles(wait_seconds=0)
    assert backend.writes == []


def test_profile_failure_resets_everything_touched(controller, backend):
    controller.set_power_cap(RX9070XT, 290)
    controller.set_od(RX9070XT, {"sclk_offset": 100})
    controller.save_profile(RX9070XT, "p")
    controller.reset(RX9070XT)
    backend.fail.add("pp_od_clk_voltage:c")
    with pytest.raises(ControlError):
        controller.apply_profile(RX9070XT, "p")
    backend.fail.clear()
    st = controller.state(RX9070XT)
    assert st["power"]["current_w"] == 304 and st["od"]["values"]["sclk_offset"] == 0


def test_corrupt_config(controller, tmp_path):
    path = tmp_path / "etc/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert controller.list_profiles(RX9070XT)["profiles"] == {}
    assert list(path.parent.glob("config.json.broken-*"))


def test_od_disabled(tmp_path):
    from radeonpilot.daemon.controller import Controller
    from radeonpilot.daemon.profiles import ProfileStore
    from radeonpilot.emulator import EmulatedBackend, build_tree

    root = build_tree(tmp_path / "s", od_enabled=False)
    c = Controller(EmulatedBackend(root), root, ProfileStore(tmp_path / "c.json"))
    with pytest.raises(ValidationError) as exc:
        c.set_od(RX9070XT, {"sclk_offset": 0})
    assert "ppfeaturemask" in str(exc.value)
    assert c.set_power_cap(RX9070XT, 280)["power"]["current_w"] == 280
    c.reset(RX9070XT)  # must not fail when OD is unavailable
