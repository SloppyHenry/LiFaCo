"""AMD RDNA3/RDNA4 GPUs: fan control through the overdrive fan curve (pwm1 is read-only on these cards).

Needs overdrive enabled (kernel parameter amdgpu.ppfeaturemask with bit 14 set, e.g. 0xffffffff).
"""

import os
import re

POINTS = 5


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def curve_path(hwmon_dir):
    return os.path.join(hwmon_dir, "device", "gpu_od", "fan_ctrl", "fan_curve")


def gfx_major(hwmon_dir):
    """Graphics IP major version (11 = RDNA3, 12 = RDNA4), or None if unknown."""
    text = _read(os.path.join(hwmon_dir, "device", "ip_discovery", "die", "0", "GC", "0", "major"))
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def parse_ranges(text):
    """(temp_lo, temp_hi, speed_lo, speed_hi) from the OD_RANGE section."""
    temps = re.search(r"FAN_CURVE\(hotspot temp\):\s*(-?\d+)C\s+(-?\d+)C", text or "")
    speeds = re.search(r"FAN_CURVE\(fan speed\):\s*(\d+)%\s+(\d+)%", text or "")
    t = (int(temps.group(1)), int(temps.group(2))) if temps else (25, 100)
    s = (int(speeds.group(1)), int(speeds.group(2))) if speeds else (15, 100)
    return (*t, *s)


class AmdOdOutput:
    """Flat overdrive curve: all points at the requested speed, so the GPU holds that speed."""

    pwm_path = None  # the driver interpolates, so read-back differs from the request: no override check
    enable_path = None

    def __init__(self, sid, label, hwmon_dir, default_fan):
        self.id, self.label, self.default_fan = sid, label, default_fan
        self.curve = curve_path(hwmon_dir)
        self.pwm_read = os.path.join(hwmon_dir, "pwm1")
        self.t_lo, self.t_hi, self.s_lo, self.s_hi = parse_ranges(_read(self.curve))
        self._taken = False
        self._last = None

    @property
    def controlled(self):
        return self._taken

    def read_percent(self):
        raw = _read(self.pwm_read)
        try:
            return round(int(raw) * 100 / 255, 1) if raw else None
        except ValueError:
            return None

    def take_control(self):
        self._taken = True

    def _send(self, line):
        with open(self.curve, "w") as f:
            f.write(line + "\n")

    def write_percent(self, percent):
        speed = int(round(max(self.s_lo, min(self.s_hi, percent))))
        if speed == self._last:
            return
        step = (self.t_hi - self.t_lo) / (POINTS - 1)
        for i in range(POINTS):
            self._send(f"{i} {int(round(self.t_lo + i * step))} {speed}")
        self._send("c")
        self._last = speed

    def restore(self):
        if not self._taken:
            return
        try:
            self._send("r")
            self._send("c")
        except OSError:
            pass
        self._taken = False
        self._last = None


def od_status(hwmon_dir):
    """None if not an RDNA3+ card, else "active" or "disabled"."""
    if os.path.exists(curve_path(hwmon_dir)):
        return "active"
    major = gfx_major(hwmon_dir)
    if major is not None and major >= 11:
        return "disabled"
    return None
