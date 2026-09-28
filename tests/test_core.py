import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from fancontrol_linux import config as cfgmod
from fancontrol_linux.curves import CurveEvaluator, interpolate
from fancontrol_linux.engine import Calibration, Engine, map_output, rate_limit, rpm_to_percent
from fancontrol_linux.sensors import CustomSensors
from fancontrol_linux.hwmon import Hardware
import fake_hwmon


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _file(root, rel):
    return os.path.join(root, rel)


def _read(root, rel):
    with open(_file(root, rel)) as f:
        return f.read().strip()


def _pwm(percent):
    return str(int(round(percent * 255 / 100)))


def _set(root, rel, value):
    with open(_file(root, rel), "w") as f:
        f.write(str(value))


class CurveTests(unittest.TestCase):
    def test_interpolate(self):
        pts = [[30, 20], [70, 100]]
        self.assertEqual(interpolate(pts, 10), 20)
        self.assertEqual(interpolate(pts, 50), 60)
        self.assertEqual(interpolate(pts, 90), 100)

    def test_missing_sensor_is_failsafe(self):
        c = cfgmod.new_curve("graph", "g", ["x"])
        out = CurveEvaluator().evaluate([c], {"x": None}, 0)
        self.assertTrue(math.isinf(out[c["id"]]))

    def test_hysteresis_and_response_time(self):
        c = cfgmod.new_curve("linear", "l", ["x"])
        c.update(temp_min=40, temp_max=80, speed_min=0, speed_max=100, hysteresis_up=3, hysteresis_down=3,
                 response_up=2, response_down=2)
        ev = CurveEvaluator()
        self.assertEqual(ev.evaluate([c], {"x": 60}, 0)[c["id"]], 50)
        self.assertEqual(ev.evaluate([c], {"x": 62}, 1)[c["id"]], 50)  # inside deadband
        self.assertEqual(ev.evaluate([c], {"x": 64}, 2)[c["id"]], 50)  # not held long enough
        self.assertEqual(ev.evaluate([c], {"x": 64}, 4)[c["id"]], 60)
        # Crossing the maximum ignores hysteresis.
        self.assertEqual(ev.evaluate([c], {"x": 90}, 4.1)[c["id"]], 100)

    def test_mix_and_cycle(self):
        a = cfgmod.new_curve("flat", "a"); a["value"] = 30
        b = cfgmod.new_curve("flat", "b"); b["value"] = 70
        m = cfgmod.new_curve("mix", "m"); m["curves"] = [a["id"], b["id"]]; m["function"] = "avg"
        out = CurveEvaluator().evaluate([a, b, m], {}, 0)
        self.assertEqual(out[m["id"]], 50)
        m2 = cfgmod.new_curve("mix", "m2"); m3 = cfgmod.new_curve("mix", "m3")
        m2["curves"], m3["curves"] = [m3["id"]], [m2["id"]]
        out = CurveEvaluator().evaluate([m2, m3], {}, 0)
        self.assertTrue(math.isinf(out[m2["id"]]))

    def test_trigger(self):
        c = cfgmod.new_curve("trigger", "t", ["x"])
        c.update(idle_temp=50, load_temp=65, idle_speed=30, load_speed=80, response_up=0, response_down=0)
        ev = CurveEvaluator()
        self.assertEqual(ev.evaluate([c], {"x": 60}, 0)[c["id"]], 30)
        self.assertEqual(ev.evaluate([c], {"x": 66}, 1)[c["id"]], 80)
        self.assertEqual(ev.evaluate([c], {"x": 55}, 2)[c["id"]], 80)
        self.assertEqual(ev.evaluate([c], {"x": 50}, 3)[c["id"]], 30)


class ControlMappingTests(unittest.TestCase):
    def ctl(self, **kw):
        c = dict(cfgmod.DEFAULT_CONTROL)
        c.update(kw)
        return c

    def test_limits_and_zero_rpm(self):
        self.assertEqual(map_output(self.ctl(min_percent=20), 5), 20)
        self.assertEqual(map_output(self.ctl(min_percent=20, stop_percent=10), 8), 0)
        self.assertEqual(map_output(self.ctl(min_percent=20, stop_percent=10), 12), 20)
        self.assertEqual(map_output(self.ctl(max_percent=80), math.inf), 80)
        self.assertEqual(map_output(self.ctl(max_percent=80), 95), 80)
        self.assertEqual(map_output(self.ctl(offset=10), 50), 60)

    def test_avoid(self):
        c = self.ctl(avoid=[[40, 50]])
        self.assertEqual(map_output(c, 42), 40)
        self.assertEqual(map_output(c, 48), 50)
        self.assertEqual(map_output(c, 45), 50)

    def test_rate_limit(self):
        c = self.ctl(step_up=10, step_down=5)
        self.assertEqual(rate_limit(20, 80, c, 1.0), 30)
        self.assertEqual(rate_limit(80, 20, c, 2.0), 70)


class ConfigTests(unittest.TestCase):
    def test_normalize_rejects_bad(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.normalize({"curves": [{"type": "nope"}]})
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.normalize({"controls": [{"id": "a", "min_percent": 150}]})

    def test_profiles_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FANCONTROL_CONFIG_DIR"] = d
            try:
                cfg = cfgmod.empty_config()
                cfg["curves"].append(cfgmod.new_curve("flat", "x"))
                cfgmod.save_profile("Leise", cfgmod.normalize(cfg))
                self.assertEqual(cfgmod.list_profiles(), ["Leise"])
                self.assertEqual(cfgmod.load_profile("Leise")["curves"][0]["name"], "x")
                with self.assertRaises(cfgmod.ConfigError):
                    cfgmod.save_profile("../evil", cfg)
                cfgmod.delete_profile("Leise")
                self.assertEqual(cfgmod.list_profiles(), [])
            finally:
                del os.environ["FANCONTROL_CONFIG_DIR"]


class HardwareEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        fake_hwmon.create(self.root)
        self.hw = Hardware(root=self.root, nvidia=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan(self):
        self.assertIn("nct6798:pwm1", self.hw.pwms)
        self.assertEqual(self.hw.pwms["nct6798:pwm1"].default_fan, "nct6798:fan1")
        self.assertEqual(self.hw.temps["k10temp:temp1"].label, "k10temp: Tctl")
        _set(self.root, "hwmon1/temp1_input", 55500)
        self.assertEqual(self.hw.read_temps()["k10temp:temp1"], 55.5)

    def _engine(self, **ctl):
        curve = cfgmod.new_curve("linear", "cpu", ["k10temp:temp1"])
        curve.update(temp_min=40, temp_max=80, speed_min=0, speed_max=100, hysteresis_up=0, hysteresis_down=0,
                     response_up=0, response_down=0)
        control = {"id": "nct6798:pwm1", "enabled": True, "curve": curve["id"], "min_percent": 0}
        control.update(ctl)
        cfg = cfgmod.normalize({"curves": [curve], "controls": [control]})
        clock = FakeClock()
        return Engine(self.hw, cfg, clock=clock), clock, cfg

    def test_manual_mode_and_identify(self):
        engine, clock, cfg = self._engine(mode="manual", manual_percent=40)
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(40))
        engine.identify("nct6798:pwm2", 5)
        clock.t += 1
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm2"), "255")
        clock.t += 5
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm2_enable"), "5")

    def test_override_detection_and_force_apply(self):
        engine, clock, _ = self._engine(mode="manual", manual_percent=40)
        engine.tick()
        _set(self.root, "hwmon0/pwm1", 200)
        clock.t += 1
        status = engine.tick()
        self.assertTrue(status["pwms"]["nct6798:pwm1"]["overridden"])
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(40))

    def test_rpm_curve(self):
        cal = {"rpm_curve": [[0, 0], [20, 400], [50, 1000], [100, 2000]]}
        curve = cfgmod.new_curve("flat", "rpm")
        curve.update(unit="rpm", value=1000)
        control = {"id": "nct6798:pwm1", "enabled": True, "curve": curve["id"], "calibration": cal}
        cfg = cfgmod.normalize({"curves": [curve], "controls": [control]})
        Engine(self.hw, cfg, clock=FakeClock()).tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(50))

    def test_stall_boost(self):
        cal = {"rpm_curve": [[0, 0], [30, 500], [100, 1800]]}
        engine, clock, _ = self._engine(mode="manual", manual_percent=30, start_percent=30, calibration=cal,
                                        fan="nct6798:fan1")
        _set(self.root, "hwmon0/fan1_input", 0)
        for _ in range(6):
            engine.tick()
            clock.t += 1
        self.assertGreater(int(_read(self.root, "hwmon0/pwm1")), int(_pwm(30)))
        _set(self.root, "hwmon0/fan1_input", 700)
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(30))

    def test_sync_follows_firmware_controlled_fan(self):
        _set(self.root, "hwmon0/pwm2", 102)
        sync = cfgmod.new_curve("sync", "s")
        sync.update(control="nct6798:pwm2", offset=10)
        control = {"id": "nct6798:pwm1", "enabled": True, "curve": sync["id"]}
        cfg = cfgmod.normalize({"curves": [sync], "controls": [control]})
        Engine(self.hw, cfg, clock=FakeClock()).tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(50))

    def test_custom_sensor_drives_curve(self):
        sensor = cfgmod.new_custom_sensor("offset", "CPU+10", "k10temp:temp1")
        sensor["offset"] = 10
        curve = cfgmod.new_curve("linear", "c", ["custom:" + sensor["id"]])
        curve.update(temp_min=40, temp_max=80, speed_min=0, speed_max=100, hysteresis_up=0, hysteresis_down=0)
        control = {"id": "nct6798:pwm1", "enabled": True, "curve": curve["id"]}
        cfg = cfgmod.normalize({"custom_sensors": [sensor], "curves": [curve], "controls": [control]})
        _set(self.root, "hwmon1/temp1_input", 50000)
        status = Engine(self.hw, cfg, clock=FakeClock()).tick()
        self.assertEqual(status["temps"]["custom:" + sensor["id"]]["value"], 60)
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(50))

    def test_control_and_restore(self):
        engine, clock, cfg = self._engine()
        _set(self.root, "hwmon1/temp1_input", 60000)
        status = engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1_enable"), "1")
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), str(round(50 * 255 / 100)))
        self.assertTrue(status["pwms"]["nct6798:pwm1"]["controlled"])
        # Untouched outputs stay in automatic mode.
        self.assertEqual(_read(self.root, "hwmon0/pwm2_enable"), "5")

        cfg["controls"][0]["enabled"] = False
        engine.set_config(cfg)
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1_enable"), "5")

    def test_shutdown_restores(self):
        engine, clock, _ = self._engine()
        engine.tick()
        engine.shutdown()
        self.assertEqual(_read(self.root, "hwmon0/pwm1_enable"), "5")

    def test_safety_temperature(self):
        engine, clock, _ = self._engine(max_percent=60)
        _set(self.root, "hwmon1/temp1_input", 99000)
        status = engine.tick()
        self.assertTrue(status["safety_active"])
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), "255")

    def test_firmware_takeover_is_reverted(self):
        engine, clock, _ = self._engine()
        engine.tick()
        _set(self.root, "hwmon0/pwm1_enable", 5)
        clock.t += 1
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1_enable"), "1")

    def test_start_kick(self):
        engine, clock, _ = self._engine(stop_percent=1, start_percent=40)
        _set(self.root, "hwmon1/temp1_input", 30000)
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), "0")
        _set(self.root, "hwmon1/temp1_input", 44000)
        clock.t += 1
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(40))
        clock.t += 3
        engine.tick()
        self.assertEqual(_read(self.root, "hwmon0/pwm1"), _pwm(10))


class NewCurveAndSensorTests(unittest.TestCase):
    def test_sub_mix_and_sync(self):
        a = cfgmod.new_curve("flat", "a"); a["value"] = 70
        b = cfgmod.new_curve("flat", "b"); b["value"] = 20
        m = cfgmod.new_curve("mix", "m"); m["curves"] = [a["id"], b["id"]]; m["function"] = "sub"
        s1 = cfgmod.new_curve("sync", "s"); s1.update(control="p1", offset=10)
        s2 = cfgmod.new_curve("sync", "s2"); s2.update(control="p1", offset=50, proportional=True)
        out = CurveEvaluator().evaluate([a, b, m, s1, s2], {}, 0, {"p1": 40})
        self.assertEqual(out[m["id"]], 50)
        self.assertEqual(out[s1["id"]], 50)
        self.assertEqual(out[s2["id"]], 60)

    def test_hysteresis_up_down(self):
        c = cfgmod.new_curve("linear", "l", ["x"])
        c.update(temp_min=0, temp_max=100, speed_min=0, speed_max=100, hysteresis_up=1, hysteresis_down=5,
                 response_up=0, response_down=0, ignore_hysteresis_at_limits=False)
        ev = CurveEvaluator()
        ev.evaluate([c], {"x": 50}, 0)
        self.assertEqual(ev.evaluate([c], {"x": 51}, 1)[c["id"]], 51)
        self.assertEqual(ev.evaluate([c], {"x": 48}, 2)[c["id"]], 51)
        self.assertEqual(ev.evaluate([c], {"x": 46}, 3)[c["id"]], 46)

    def test_auto_curve_finds_speed(self):
        c = cfgmod.new_curve("auto", "a", ["x"])
        c.update(idle_temp=40, load_temp=70, min_speed=20, max_speed=100, step=5, deadband=2, response_time=0)
        ev = CurveEvaluator()
        self.assertEqual(ev.evaluate([c], {"x": 35}, 0)[c["id"]], 20)
        for t in range(1, 5):
            out = ev.evaluate([c], {"x": 75}, t)[c["id"]]
        self.assertEqual(out, 40)
        self.assertEqual(ev.evaluate([c], {"x": 90}, 5)[c["id"]], 100)
        self.assertEqual(ev.evaluate([c], {"x": 71}, 6)[c["id"]], 100)
        self.assertLess(ev.evaluate([c], {"x": 60}, 7)[c["id"]], 100)

    def test_custom_sensors(self):
        mix = cfgmod.new_custom_sensor("mix", "m"); mix.update(sensors=["a", "b"], function="avg")
        avg = cfgmod.new_custom_sensor("average", "avg", "a"); avg["seconds"] = 10
        off = cfgmod.new_custom_sensor("offset", "o", "custom:" + mix["id"]); off.update(offset=10, proportional=True)
        cs = CustomSensors()
        r = cs.evaluate([mix, avg, off], {"a": 40, "b": 60}, 0)
        self.assertEqual(r["custom:" + mix["id"]], 50)
        self.assertAlmostEqual(r["custom:" + off["id"]], 55)
        r = cs.evaluate([mix, avg, off], {"a": 60, "b": None}, 5)
        self.assertEqual(r["custom:" + avg["id"]], 50)
        self.assertIsNone(r["custom:" + mix["id"]])
        mix["allow_missing"] = True
        self.assertEqual(cs.evaluate([mix], {"a": 60, "b": None}, 6)["custom:" + mix["id"]], 60)
        r = cs.evaluate([avg], {"a": 60}, 20)
        self.assertEqual(r["custom:" + avg["id"]], 60)

    def test_file_sensor_restricted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.sensor")
            with open(path, "w") as f:
                f.write("42.5\n")
            sensor = cfgmod.new_custom_sensor("file", "f"); sensor["path"] = path
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.normalize({"custom_sensors": [sensor]})
            os.environ["FANCONTROL_FILE_SENSOR_DIRS"] = d
            try:
                cfg = cfgmod.normalize({"custom_sensors": [sensor]})
                r = CustomSensors().evaluate(cfg["custom_sensors"], {}, 0)
                self.assertEqual(r["custom:" + sensor["id"]], 42.5)
            finally:
                del os.environ["FANCONTROL_FILE_SENSOR_DIRS"]

    def test_rpm_to_percent(self):
        cal = {"rpm_curve": [[0, 0], [10, 0], [20, 400], [30, 400], [100, 2000]]}
        self.assertEqual(rpm_to_percent(cal, 0), 0)
        self.assertEqual(rpm_to_percent(cal, 400), 20)
        self.assertEqual(rpm_to_percent(cal, 1200), 60)
        self.assertEqual(rpm_to_percent(cal, 5000), 100)
        self.assertIsNone(rpm_to_percent(None, 500))

    def test_migration(self):
        cfg = cfgmod.normalize({
            "curves": [{"id": "c", "type": "graph", "hysteresis": 3, "response_time": 2, "points": [[30, 20]]},
                       {"id": "t", "type": "trigger", "response_time": 4}],
            "controls": [{"id": "p", "zero_rpm": True}]})
        g, t = cfg["curves"]
        self.assertEqual((g["hysteresis_up"], g["hysteresis_down"], g["response_up"]), (3, 3, 2))
        self.assertEqual((t["response_up"], t["response_down"]), (4, 4))
        self.assertEqual(cfg["controls"][0]["stop_percent"], 1)
        self.assertNotIn("zero_rpm", cfg["controls"][0])


class CalibrationTests(unittest.TestCase):
    def test_analysis(self):
        cal = Calibration("p", None, settle=0)
        def rpm(p):
            return 0 if p < 20 else 400 + p * 12
        for phase_pct in cal.plan:
            pct, _, phase = phase_pct
            start = 0 if phase == "up" and pct < 30 else None
            fans = {"f1": rpm(pct) if start is None else 0, "f2": 900}
            cal.records.append((phase, pct, fans))
        cal._analyze()
        self.assertIsNone(cal.error)
        r = cal.result
        self.assertEqual(r["fan"], "f1")
        self.assertTrue(r["can_stop"])
        self.assertEqual(r["suggested_min_percent"], 25)
        self.assertEqual(r["suggested_start_percent"], 35)
        self.assertEqual(r["suggested_stop_percent"], 20)

    def test_flat_low_range(self):
        cal = Calibration("p", None, settle=0)
        for pct, _, phase in cal.plan:
            rpm = 600 if pct <= 40 else 600 + (pct - 40) * 20
            cal.records.append((phase, pct, {"f": rpm}))
        cal._analyze()
        self.assertFalse(cal.result["can_stop"])
        self.assertEqual(cal.result["suggested_min_percent"], 40)


if __name__ == "__main__":
    unittest.main()
