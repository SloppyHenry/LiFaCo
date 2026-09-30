"""Built-in lighting provider for liquidctl devices (AIO coolers, fan hubs, LED controllers).

liquidctl devices are already open in the service for fan control, and two users of one USB device would disturb each
other, so lighting uses the same device objects (with their lock) instead of a separate plugin process. This code is
part of LiFaCo and runs in the service; it is not a downloadable plugin.

Every colour channel of a device becomes one zone with one "LED"; each frame sets the channel to that colour with the
mode "fixed". Only volatile settings are sent (liquidctl's 'non_volatile' option is never used).
"""

import logging
import re
import sys

log = logging.getLogger("fancontrol-linuxd")
PROVIDER_ID = "liquidctl"
MIN_INTERVAL = 0.5       # seconds between colour updates of one device: USB round trips are slow
# Drivers that do not describe their colour channels but are documented to use one channel called "led".
SINGLE_LED_DRIVERS = ("HydroPlatinum", "Modern690Lc", "Legacy690Lc", "Hydro690Lc")


def channels(dev):
    """Names of the colour channels of a liquidctl device (empty if lighting is not known for it)."""
    found = getattr(dev, "_color_channels", None)
    if not found:
        found = getattr(sys.modules.get(type(dev).__module__), "_COLOR_CHANNELS", None)
    if isinstance(found, dict) and found:
        names = [str(c) for c in found if c != "sync"]
        return names or [str(c) for c in found]
    if type(dev).__name__ in SINGLE_LED_DRIVERS:
        return ["led"]
    return []


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class LiquidctlLighting:
    def __init__(self, get_devices):
        self.get_devices = get_devices           # -> list of liquidctl_backend._Device (changes on rescan)
        self.sent = {}                           # (device id, channel) -> last colour

    def devices(self):
        """Device descriptions in the format the lighting manager uses for plugin devices."""
        out = []
        for d in self.get_devices():
            names = channels(d.dev)
            if not names:
                continue
            status_keys = " ".join(k.lower() for k, _v, _u in d.status)
            out.append({"id": d.key.split(":", 1)[-1], "name": d.name, "vendor": "",
                        "type": "cooler" if "pump" in status_keys else "fan",
                        "zones": [{"name": n, "leds": 1} for n in names], "modes": [], "leds": len(names),
                        "direct": True, "min_interval": MIN_INTERVAL})
        return out

    def _find(self, device_id):
        for d in self.get_devices():
            if d.key.split(":", 1)[-1] == device_id:
                return d
        raise RuntimeError("The device is no longer available")

    def set_colors(self, device_id, colors):
        d = self._find(device_id)
        names = channels(d.dev)
        errors = []
        for name, color in zip(names, colors, strict=False):
            color = [int(c) for c in color]
            if self.sent.get((device_id, name)) == color:
                continue
            try:
                with d.lock:
                    d.dev.set_color(name, "fixed", [color])
            except Exception as e:  # noqa: BLE001 – a driver problem is shown on the device card
                errors.append(f"{name}: {e}")
                continue
            self.sent[(device_id, name)] = color
        if errors:
            raise RuntimeError("; ".join(errors))
