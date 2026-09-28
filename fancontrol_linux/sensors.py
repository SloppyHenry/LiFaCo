"""Custom temperature sensors built from other sensors or files: mix, time average, offset, file."""

import collections
import os

from .curves import combine

CUSTOM_TYPES = ("mix", "average", "offset", "file")
PREFIX = "custom:"
DEFAULT_FILE_DIRS = ("/sys/", "/run/fancontrol-linux/sensors/", "/var/lib/fancontrol-linux/sensors/")


def file_sensor_dirs():
    extra = [d for d in os.environ.get("FANCONTROL_FILE_SENSOR_DIRS", "").split(":") if d]
    return tuple(DEFAULT_FILE_DIRS) + tuple(os.path.join(os.path.realpath(d), "") for d in extra)


def file_path_allowed(path):
    """The daemon runs as root, so file sensors are limited to a few directories."""
    real = os.path.realpath(path)
    return os.path.isabs(path) and any(real.startswith(d) for d in file_sensor_dirs())


def read_file_sensor(path):
    if not file_path_allowed(path):
        return None
    try:
        with open(os.path.realpath(path)) as f:
            value = float(f.readline().strip().replace(",", "."))
    except (OSError, ValueError):
        return None
    # hwmon-style files report millidegrees.
    return value / 1000.0 if abs(value) >= 1000 else value


class CustomSensors:
    def __init__(self):
        self.history: dict[str, collections.deque] = {}

    def reset(self, sensors):
        ids = {s["id"] for s in sensors}
        self.history = {k: v for k, v in self.history.items() if k in ids}

    def evaluate(self, sensors, temps, now):
        """Returns {"custom:<id>": value or None}; custom sensors may use other custom sensors."""
        by_id = {PREFIX + s["id"]: s for s in sensors}
        results = {}

        def value_of(sid, stack):
            if sid not in by_id:
                return temps.get(sid)
            if sid in results:
                return results[sid]
            if sid in stack:
                return None
            v = self._one(by_id[sid], lambda other: value_of(other, stack | {sid}), now)
            results[sid] = v
            return v

        for sid in by_id:
            value_of(sid, frozenset())
        return results

    def _one(self, sensor, value_of, now):
        kind = sensor["type"]
        if kind == "file":
            return read_file_sensor(sensor.get("path", ""))
        if kind == "mix":
            values = [value_of(s) for s in sensor.get("sensors", [])]
            if not values or (any(v is None for v in values) and not sensor.get("allow_missing")):
                return None
            return combine(values, sensor.get("function", "max"))
        source = value_of(sensor.get("sensor"))
        if kind == "offset":
            if source is None:
                return None
            offset = sensor.get("offset", 0.0)
            return source * (1 + offset / 100) if sensor.get("proportional") else source + offset
        if kind == "average":
            hist = self.history.setdefault(sensor["id"], collections.deque())
            if source is not None:
                hist.append((now, source))
            window = sensor.get("seconds", 30.0)
            while hist and now - hist[0][0] > window:
                hist.popleft()
            return sum(v for _t, v in hist) / len(hist) if hist else None
        return None
