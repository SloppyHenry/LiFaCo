"""EXPERIMENTAL: Thermaltake Riing/G3 fan controllers over hidraw (protocol as used by linux_thermaltake_rgb).

Off by default – enable it in the settings. Untested on real hardware.
"""

import os
import select
import time

VENDOR = 0x264A
PRODUCT_RANGES = [(0x1F41, 0x1F51), (0x1FA5, 0x1FB5), (0x2135, 0x2145), (0x2260, 0x2263)]
PORTS = 5
PACKET = 64
INIT = [0xFE, 0x33]
SET_FAN = [0x32, 0x51]
GET_FAN = [0x33, 0x51]


def hidraw_root():
    return os.environ.get("FANCONTROL_HIDRAW_ROOT", "/sys/class/hidraw")


def dev_root():
    return os.environ.get("FANCONTROL_DEV_ROOT", "/dev")


def find_controllers():
    """[(hidraw name, product id)] of supported controllers."""
    found = []
    try:
        entries = sorted(os.listdir(hidraw_root()))
    except OSError:
        return found
    for name in entries:
        try:
            with open(os.path.join(hidraw_root(), name, "device", "uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                _bus, vendor, product = line[7:].split(":")
                vendor, product = int(vendor, 16), int(product, 16)
                if vendor == VENDOR and any(lo <= product <= hi for lo, hi in PRODUCT_RANGES):
                    found.append((name, product))
    return found


class Controller:
    def __init__(self, path, key):
        self.path, self.key = path, key
        self.fd = os.open(path, os.O_RDWR)
        self._cache = {}
        self.send(INIT)

    def close(self):
        os.close(self.fd)

    def send(self, data):
        payload = bytes([0x00] + data + [0x00] * (PACKET - len(data)))
        os.write(self.fd, payload)

    def request(self, data, timeout=0.3):
        self.send(data)
        ready, _w, _x = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, PACKET)

    def fan(self, port):
        """(percent, rpm) or None. Cached so one cycle asks the device once per port, and a silent
        device is not asked again for a while (each unanswered request blocks for the timeout)."""
        now = time.monotonic()
        cached = self._cache.get(port)
        if cached and now < cached[0]:
            return cached[1]
        reply = self.request(GET_FAN + [port])
        if not reply or len(reply) < 7:
            self._cache[port] = (now + 5.0, None)
            return None
        value = (reply[4], reply[5] | (reply[6] << 8))
        self._cache[port] = (now + 0.5, value)
        return value

    def set_fan(self, port, percent):
        self.send(SET_FAN + [port, 0x01, int(round(max(0, min(100, percent))))])


class TtFan:
    def __init__(self, sid, label, ctl, port):
        self.id, self.label, self.ctl, self.port = sid, label, ctl, port

    def read(self):
        data = self.ctl.fan(self.port)
        return None if data is None else data[1]


class TtOutput:
    pwm_path = None
    enable_path = None

    def __init__(self, sid, label, ctl, port, default_fan):
        self.id, self.label, self.ctl, self.port, self.default_fan = sid, label, ctl, port, default_fan
        self._taken = False

    @property
    def controlled(self):
        return self._taken

    def read_percent(self):
        data = self.ctl.fan(self.port)
        return None if data is None else float(data[0])

    def take_control(self):
        self._taken = True

    def write_percent(self, percent):
        self.ctl.set_fan(self.port, percent)

    def restore(self):
        # The controller has no automatic mode: leave the fans at a safe speed.
        if self._taken:
            try:
                self.ctl.set_fan(self.port, 100)
            except OSError:
                pass
        self._taken = False


class ThermaltakeBackend:
    def __init__(self):
        self.controllers = []
        self.error = None

    def close(self):
        for c in self.controllers:
            try:
                c.close()
            except OSError:
                pass
        self.controllers = []

    def scan(self, fans, outputs):
        self.close()
        self.error = None
        for i, (name, product) in enumerate(find_controllers(), start=1):
            key = f"thermaltake:{product:04x}#{i}"
            try:
                ctl = Controller(os.path.join(dev_root(), name), key)
            except OSError as e:
                self.error = f"{name}: {e}"
                continue
            self.controllers.append(ctl)
            for port in range(1, PORTS + 1):
                fan_id = f"{key}:fan{port}"
                fans[fan_id] = TtFan(fan_id, f"Thermaltake {i}: Fan {port}", ctl, port)
                out_id = f"{key}:pwm{port}"
                outputs[out_id] = TtOutput(out_id, f"Thermaltake {i}: Port {port}", ctl, port, fan_id)

    def info(self):
        if self.error:
            return f"Error: {self.error}"
        return f"{len(self.controllers)} controller(s)" if self.controllers else "No controllers found"
