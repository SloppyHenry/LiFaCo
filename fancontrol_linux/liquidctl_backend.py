"""AIO coolers and smart fan hubs through liquidctl (NZXT, Corsair Hydro/Commander, ASUS Ryujin, MSI …).

Devices that have a kernel hwmon driver (nzxt-kraken3, nzxt-smart2, corsair-cpro, aquacomputer_d5next …) are
already handled by hwmon; liquidctl covers the rest. Only used when the liquidctl Python package is installed.
"""

import logging
import re
import time

log = logging.getLogger("fancontrol-linuxd")
CACHE_SECONDS = 0.5
# Speed a device is left at when the daemon stops and the device has no automatic mode we can return to.
FALLBACK_PROFILE = [(20, 30), (30, 50), (40, 80), (50, 100)]


def available():
    try:
        import liquidctl  # noqa: F401
    except ImportError:
        return False
    return True


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def channel_for(key):
    """liquidctl channel name from a status key: 'Fan 2 speed' -> 'fan2', 'Pump speed' -> 'pump'."""
    m = re.match(r"^(fan|pump)\s*(\d*)\s+(speed|duty)$", key.strip().lower())
    return (m.group(1) + m.group(2)) if m else None


class _Device:
    def __init__(self, dev, key):
        self.dev = dev
        self.key = key
        self.name = dev.description
        self.status = []
        self.read_at = 0.0

    def get_status(self):
        now = time.monotonic()
        if now - self.read_at >= CACHE_SECONDS:
            try:
                self.status = self.dev.get_status()
            except Exception as e:  # USB errors must not stop the control loop
                log.warning("liquidctl %s: %s", self.name, e)
                self.status = []
            self.read_at = now
        return self.status

    def value(self, key):
        for k, v, _unit in self.get_status():
            if k == key:
                return v
        return None


class LiquidTemp:
    def __init__(self, sid, label, device, key):
        self.id, self.label, self.device, self.key = sid, label, device, key

    def read(self):
        v = self.device.value(self.key)
        return None if v is None else float(v)


class LiquidFan:
    def __init__(self, sid, label, device, key):
        self.id, self.label, self.device, self.key = sid, label, device, key

    def read(self):
        v = self.device.value(self.key)
        return None if v is None else int(v)


class LiquidOutput:
    pwm_path = None
    enable_path = None

    def __init__(self, sid, label, device, channel, duty_key, default_fan):
        self.id, self.label, self.device, self.channel = sid, label, device, channel
        self.duty_key, self.default_fan = duty_key, default_fan
        self._taken = False
        self._last = None

    @property
    def controlled(self):
        return self._taken

    def read_percent(self):
        if self.duty_key:
            v = self.device.value(self.duty_key)
            if v is not None:
                return float(v)
        return self._last

    def take_control(self):
        self._taken = True

    def write_percent(self, percent):
        duty = int(round(max(0, min(100, percent))))
        try:
            self.device.dev.set_fixed_speed(self.channel, duty)
        except Exception as e:
            raise OSError(f"liquidctl: {e}") from e
        self._last = duty

    def restore(self):
        if not self._taken:
            return
        try:
            self.device.dev.set_speed_profile(self.channel, FALLBACK_PROFILE)
        except Exception:
            try:
                self.device.dev.set_fixed_speed(self.channel, 100)
            except Exception:
                pass
        self._taken = False


class LiquidctlBackend:
    def __init__(self):
        self.devices: list[_Device] = []
        self.error = None

    def close(self):
        for d in self.devices:
            try:
                d.dev.disconnect()
            except Exception:
                pass
        self.devices = []

    def scan(self, temps, fans, outputs, finder=None):
        self.close()
        self.error = None
        try:
            if finder is None:
                from liquidctl import find_liquidctl_devices as finder
            found = list(finder())
        except Exception as e:
            self.error = str(e)
            return
        seen = {}
        for dev in found:
            try:
                dev.connect()
                dev.initialize()
            except Exception as e:
                log.warning("liquidctl %s could not be initialized: %s", getattr(dev, "description", "?"), e)
                continue
            base = _slug(dev.description)
            seen[base] = seen.get(base, 0) + 1
            key = f"liquidctl:{base}#{seen[base]}" if seen[base] > 1 else f"liquidctl:{base}"
            device = _Device(dev, key)
            self.devices.append(device)
            status = device.get_status()
            duty_keys = {channel_for(k): k for k, _v, u in status if u == "%" and channel_for(k)}
            for k, _v, unit in status:
                slug = _slug(k)
                if unit == "°C":
                    temps[f"{key}:{slug}"] = LiquidTemp(f"{key}:{slug}", f"{dev.description}: {k}", device, k)
                elif unit == "rpm":
                    fan_id = f"{key}:{slug}"
                    fans[fan_id] = LiquidFan(fan_id, f"{dev.description}: {k}", device, k)
                    channel = channel_for(k)
                    if channel:
                        out_id = f"{key}:{channel}"
                        outputs[out_id] = LiquidOutput(out_id, f"{dev.description}: {channel}", device, channel,
                                                       duty_keys.get(channel), fan_id)

    def info(self):
        if self.error:
            return f"Error: {self.error}"
        if not self.devices:
            return "No supported devices found"
        return ", ".join(d.name for d in self.devices)
