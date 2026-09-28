import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fancontrol_linux import amdgpu, liquidctl_backend, thermaltake  # noqa: E402
from fancontrol_linux.hwmon import Hardware  # noqa: E402

OD_TEXT = """OD_FAN_CURVE:
0: 0C 0%
1: 0C 0%
2: 0C 0%
3: 0C 0%
4: 0C 0%
OD_RANGE:
FAN_CURVE(hotspot temp): 25C 100C
FAN_CURVE(fan speed): 15% 100%
"""



def _mkfile(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class AmdOdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "hwmon")
        card = os.path.join(self.tmp.name, "card0")
        hw = os.path.join(self.root, "hwmon0")
        os.makedirs(hw)
        os.symlink(card, os.path.join(hw, "device"))
        _mkfile(os.path.join(hw, "name"), "amdgpu\n")
        _mkfile(os.path.join(hw, "temp1_input"), "45000\n")
        _mkfile(os.path.join(hw, "fan1_input"), "1200\n")
        _mkfile(os.path.join(hw, "pwm1"), "102\n")
        _mkfile(os.path.join(hw, "pwm1_enable"), "2\n")
        _mkfile(os.path.join(card, "ip_discovery", "die", "0", "GC", "0", "major"), "11\n")
        self.curve = os.path.join(card, "gpu_od", "fan_ctrl", "fan_curve")
        self.hw_dir = hw

    def tearDown(self):
        self.tmp.cleanup()

    def test_rdna3_without_overdrive_is_read_only(self):
        hw = Hardware(root=self.root, nvidia=False, settings={"liquidctl": False})
        self.assertEqual(list(hw.pwms), [])
        self.assertIn("disabled", hw.amd_od.values())
        self.assertIn("RDNA3", " ".join(r["name"] for r in hw.info()))

    def test_overdrive_flat_curve(self):
        _mkfile(self.curve, OD_TEXT)
        writes = []
        orig = amdgpu.AmdOdOutput._send
        amdgpu.AmdOdOutput._send = lambda self_, line: writes.append(line)
        try:
            hw = Hardware(root=self.root, nvidia=False, settings={"liquidctl": False})
            (out,) = hw.pwms.values()
            self.assertIsInstance(out, amdgpu.AmdOdOutput)
            self.assertEqual(out.read_percent(), 40.0)
            out.take_control()
            out.write_percent(50)
            self.assertEqual(writes, ["0 25 50", "1 44 50", "2 62 50", "3 81 50", "4 100 50", "c"])
            writes.clear()
            out.write_percent(50)
            self.assertEqual(writes, [])
            out.write_percent(5)
            self.assertEqual(writes[0], "0 25 15")
            writes.clear()
            out.restore()
            self.assertEqual(writes, ["r", "c"])
        finally:
            amdgpu.AmdOdOutput._send = orig


class FakeLiquidDevice:
    description = "NZXT Kraken X (X53, X63 or X73)"

    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append("connect")

    def initialize(self):
        self.calls.append("initialize")

    def disconnect(self):
        self.calls.append("disconnect")

    def get_status(self):
        return [("Liquid temperature", 31.5, "°C"), ("Pump speed", 2100, "rpm"), ("Pump duty", 70, "%"),
                ("Fan 1 speed", 900, "rpm")]

    def set_fixed_speed(self, channel, duty):
        self.calls.append(("fixed", channel, duty))

    def set_speed_profile(self, channel, profile):
        self.calls.append(("profile", channel))


class LiquidctlTests(unittest.TestCase):
    def test_scan_and_control(self):
        dev = FakeLiquidDevice()
        backend = liquidctl_backend.LiquidctlBackend()
        temps, fans, outputs = {}, {}, {}
        backend.scan(temps, fans, outputs, finder=lambda: [dev])
        key = "liquidctl:nzxt-kraken-x-x53-x63-or-x73"
        self.assertEqual(temps[f"{key}:liquid-temperature"].read(), 31.5)
        self.assertEqual(fans[f"{key}:pump-speed"].read(), 2100)
        pump, fan = outputs[f"{key}:pump"], outputs[f"{key}:fan1"]
        self.assertEqual(pump.read_percent(), 70.0)
        self.assertEqual(pump.default_fan, f"{key}:pump-speed")
        fan.take_control()
        fan.write_percent(45.4)
        fan.restore()
        self.assertIn(("fixed", "fan1", 45), dev.calls)
        self.assertIn(("profile", "fan1"), dev.calls)
        self.assertIn("initialize", dev.calls)
        self.assertEqual(liquidctl_backend.channel_for("Fan speed"), "fan")


class FakeHydroPlatinum(FakeLiquidDevice):
    description = "Corsair Hydro H115i Pro XT"

    def initialize(self, pump_mode="balanced", **kwargs):
        self.calls.append(("init", pump_mode))

    def get_status(self):
        return [("Liquid temperature", 30.2, "°C"), ("Fan 1 speed", 800, "rpm"), ("Fan 1 duty", 40, "%"),
                ("Fan 2 speed", 810, "rpm"), ("Fan 2 duty", 40, "%"), ("Pump speed", 2200, "rpm"),
                ("Pump duty", 60, "%")]

    def set_fixed_speed(self, channel, duty):
        if channel == "pump":
            raise ValueError("unknown channel, should be one of: 'fan', 'fan1', 'fan2'")
        self.calls.append(("fixed", channel, duty))


class HydroPlatinumTests(unittest.TestCase):
    def test_pump_modes_and_fans(self):
        dev = FakeHydroPlatinum()
        temps, fans, outputs = {}, {}, {}
        liquidctl_backend.LiquidctlBackend().scan(temps, fans, outputs, finder=lambda: [dev])
        key = "liquidctl:corsair-hydro-h115i-pro-xt"
        self.assertEqual(sorted(outputs), [f"{key}:fan1", f"{key}:fan2", f"{key}:pump"])
        pump = outputs[f"{key}:pump"]
        self.assertIsInstance(pump, liquidctl_backend.LiquidPumpModeOutput)
        pump.take_control()
        for pct in (20, 30, 60, 90):
            pump.write_percent(pct)
        self.assertEqual([c for c in dev.calls if c[0] == "init"],
                         [("init", "balanced"), ("init", "quiet"), ("init", "balanced"), ("init", "extreme")])
        pump.restore()
        self.assertEqual(dev.calls[-1], ("init", "balanced"))
        outputs[f"{key}:fan2"].write_percent(55)
        self.assertIn(("fixed", "fan2", 55), dev.calls)
        self.assertEqual(temps[f"{key}:liquid-temperature"].read(), 30.2)


class ThermaltakeTests(unittest.TestCase):
    def test_detection_and_packets(self):
        with tempfile.TemporaryDirectory() as d:
            for name, hid in (("hidraw0", "0003:0000264A:00001FA5"), ("hidraw1", "0003:0000046D:0000C52B")):
                _mkfile(os.path.join(d, name, "device", "uevent"), f"DRIVER=hid-generic\nHID_ID={hid}\n")
            os.environ["FANCONTROL_HIDRAW_ROOT"] = d
            try:
                self.assertEqual(thermaltake.find_controllers(), [("hidraw0", 0x1FA5)])
            finally:
                del os.environ["FANCONTROL_HIDRAW_ROOT"]

        sent = []
        ctl = thermaltake.Controller.__new__(thermaltake.Controller)
        ctl._cache = {}
        ctl.send = lambda data: sent.append(list(data))
        ctl.request = lambda data, timeout=0.3: bytes([0x33, 0x51, 2, 0x01, 55, 0x20, 0x03] + [0] * 57)
        ctl.set_fan(2, 42)
        self.assertEqual(sent[-1], [0x32, 0x51, 2, 0x01, 42])
        self.assertEqual(ctl.fan(2), (55, 800))


if __name__ == "__main__":
    unittest.main()
