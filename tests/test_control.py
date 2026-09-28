import pytest

from radeonpilot import control
from radeonpilot.control import ValidationError
from radeonpilot.sysfs import discover_gpus

RDNA3_OD = """OD_SCLK:
0: 500Mhz
1: 2500Mhz
OD_MCLK:
0: 97Mhz
1: 1250MHz
OD_VDDGFX_OFFSET:
0mV
OD_RANGE:
SCLK:     500Mhz       3000Mhz
MCLK:      97Mhz       1500Mhz
VDDGFX_OFFSET:    -450mv          0mv
"""

RDNA4_OD = """OD_SCLK_OFFSET:
0Mhz
OD_MCLK:
0: 97Mhz
1: 1258MHz
OD_VDDGFX_OFFSET:
0mV
OD_RANGE:
SCLK_OFFSET:    -500Mhz       1000Mhz
MCLK:      97Mhz       1500Mhz
VDDGFX_OFFSET:    -200mv          0mv
"""

FAN = """OD_FAN_CURVE:
0: 0C 0%
1: 0C 0%
2: 0C 0%
3: 0C 0%
4: 0C 0%
OD_RANGE:
FAN_CURVE(hotspot temp): 25C 100C
FAN_CURVE(fan speed): 15% 100%
"""


def test_parse_rdna3():
    od = control.parse_od(RDNA3_OD)
    assert od.values == {"sclk_min": 500, "sclk_max": 2500, "mclk_min": 97, "mclk_max": 1250, "voltage_offset": 0}
    assert od.ranges["SCLK"] == control.Range(500, 3000)
    assert od.ranges["VDDGFX_OFFSET"] == control.Range(-450, 0)
    assert not od.uses_sclk_offset
    assert set(od.supported()) == {"sclk_min", "sclk_max", "mclk_min", "mclk_max", "voltage_offset"}


def test_parse_rdna4():
    od = control.parse_od(RDNA4_OD)
    assert od.values["sclk_offset"] == 0
    assert od.ranges["SCLK_OFFSET"] == control.Range(-500, 1000)
    assert od.uses_sclk_offset
    assert "sclk_max" not in od.supported()


def test_parse_garbage():
    assert control.parse_od("") is None
    assert control.parse_od("nonsense\n") is None
    assert control.parse_fan_curve("OD_FAN_CURVE:\n0: 0C 0%\n") is None  # no range


def test_parse_fan():
    fc = control.parse_fan_curve(FAN)
    assert len(fc.points) == 5 and fc.is_driver_default
    assert fc.temp_range == control.Range(25, 100) and fc.pwm_range == control.Range(15, 100)


def test_validate_od():
    od = control.parse_od(RDNA3_OD)
    assert control.validate_od(od, {"sclk_max": 2700, "voltage_offset": -50}) == {"sclk_max": 2700, "voltage_offset": -50}
    for bad in ({"sclk_max": 3001}, {"voltage_offset": 10}, {"sclk_min": 2600},  # min > max
                {"sclk_offset": 100}, {"bogus": 1}, {"sclk_max": "2700"}, {"sclk_max": True},
                {"sclk_max": 2700.5}, {}):
        with pytest.raises(ValidationError):
            control.validate_od(od, bad)
    assert control.od_commands({"sclk_min": 600, "sclk_max": 2700, "mclk_max": 1300, "voltage_offset": -50}) == [
        "s 0 600", "s 1 2700", "m 1 1300", "vo -50"]
    od4 = control.parse_od(RDNA4_OD)
    assert control.od_commands(control.validate_od(od4, {"sclk_offset": -200})) == ["s -200"]
    with pytest.raises(ValidationError):
        control.validate_od(od4, {"sclk_offset": 1001})
    with pytest.raises(ValidationError):
        control.validate_od(None, {"sclk_offset": 0})


def test_validate_fan():
    fc = control.parse_fan_curve(FAN)
    good = [[30, 20], [50, 30], [65, 50], [80, 75], [95, 100]]
    assert control.validate_fan_curve(fc, good) == [tuple(p) for p in good]
    bad_curves = [
        good[:4],
        [[20, 20]] + good[1:],                     # temp below range
        [[30, 10]] + good[1:],                     # pwm below range
        [[50, 20], [50, 30]] + good[2:],           # temps not increasing
        [[30, 50], [50, 30]] + good[2:],           # pwm decreasing
    ]
    for bad in bad_curves:
        with pytest.raises(ValidationError):
            control.validate_fan_curve(fc, bad)
    control.validate_fan_curve(fc, control.suggested_fan_curve(fc))


def test_validate_power():
    p = control.PowerCap(300_000_000, 274_000_000, 334_000_000, 304_000_000)
    assert control.validate_power_cap(p, 280) == 280_000_000
    for bad in (273, 335, "300", None, float("nan")):
        with pytest.raises(ValidationError):
            control.validate_power_cap(p, bad)
    with pytest.raises(ValidationError):
        control.validate_power_cap(control.PowerCap(1, None, None, None), 1)


def test_perf_level():
    assert control.validate_perf_level("manual") == "manual"
    with pytest.raises(ValidationError):
        control.validate_perf_level("turbo")


def test_state_od_enabled(emu_root):
    for gpu in discover_gpus(emu_root):
        st = control.read_control_state(gpu, emu_root)
        assert st.od_available and st.od and st.fan_curve and st.power.max_w
        assert st.perf_level == "auto"


def test_state_od_disabled(tmp_path):
    from radeonpilot.emulator import build_tree

    root = build_tree(tmp_path, od_enabled=False)
    gpu = discover_gpus(root)[0]
    st = control.read_control_state(gpu, root)
    assert not st.od_available
    assert "ppfeaturemask" in st.od_block_reason and "0xfff7bfff" in st.od_block_reason
    assert st.od is None and st.fan_curve is None
    assert st.power is not None  # power cap does not need OverDrive
