"""Software effects: LiFaCo calculates the colour of every LED and sends the frames to the plugin.

An effect is a small dict, stored per device:
  {"type": "off"}
  {"type": "static", "color": [r, g, b], "brightness": 100}
  {"type": "breathing", "color": [r, g, b], "speed": 50, "brightness": 100}
  {"type": "rainbow", "speed": 50, "brightness": 100}
  {"type": "temperature", "sensor": "<sensor id or ''>", "stops": [[30, [0,0,255]], ...], "brightness": 100}
  {"type": "hardware", "mode": "<mode name>", "colors": [[r,g,b], ...], "speed": 50, "brightness": 100}
"""

import colorsys
import math

EFFECT_TYPES = ("off", "static", "breathing", "rainbow", "temperature", "hardware")
ANIMATED = ("breathing", "rainbow", "temperature")
DEFAULT_STOPS = [[30, [0, 90, 255]], [50, [0, 220, 90]], [65, [255, 170, 0]], [80, [255, 30, 0]]]
FAN_STOPS = [[0, [0, 90, 255]], [40, [0, 220, 90]], [70, [255, 170, 0]], [100, [255, 30, 0]]]   # fan speed in %
FAN_PREFIX = "fan:"      # a temperature effect whose "sensor" starts with this follows a fan output (percent)


class EffectError(ValueError):
    pass


SYNC_DEFAULT = {"type": "rainbow", "speed": 50.0, "brightness": 100.0}


def normalize_sync(raw):
    """Sync mode: one effect for every device that takes LiFaCo's colours. Hardware effects cannot be shared."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        effect = normalize(raw.get("effect"))
        if effect["type"] == "hardware":
            raise EffectError("hardware")
    except EffectError:
        effect = dict(SYNC_DEFAULT)
    return {"on": bool(raw.get("on")), "effect": effect, "resume": bool(raw.get("resume"))}


def _color(value, field="color"):
    try:
        r, g, b = (int(value[i]) for i in range(3))
    except (TypeError, ValueError, IndexError, KeyError):
        raise EffectError(f"{field}: three numbers (red, green, blue) expected") from None
    if not all(0 <= c <= 255 for c in (r, g, b)):
        raise EffectError(f"{field}: values must be between 0 and 255")
    return [r, g, b]


def _percent(value, field, default=100):
    try:
        v = float(default if value is None else value)
    except (TypeError, ValueError):
        raise EffectError(f"{field}: number expected") from None
    if not 0 <= v <= 100:
        raise EffectError(f"{field}: must be between 0 and 100")
    return v


def normalize(raw):
    if not isinstance(raw, dict) or raw.get("type") not in EFFECT_TYPES:
        raise EffectError("Unknown effect")
    kind = raw["type"]
    out = {"type": kind}
    if kind == "off":
        return out
    out["brightness"] = _percent(raw.get("brightness"), "brightness")
    if kind in ("static", "breathing"):
        out["color"] = _color(raw.get("color", [255, 255, 255]))
    if kind in ("breathing", "rainbow", "hardware"):
        out["speed"] = _percent(raw.get("speed"), "speed", 50)
    if kind == "temperature":
        out["sensor"] = str(raw.get("sensor") or "")
        stops = raw.get("stops") or (FAN_STOPS if out["sensor"].startswith(FAN_PREFIX) else DEFAULT_STOPS)
        try:
            parsed = sorted(([float(t), _color(c, "stop")] for t, c in stops), key=lambda s: s[0])
        except (TypeError, ValueError):
            raise EffectError("stops: list of [temperature, [r, g, b]] expected") from None
        if not 2 <= len(parsed) <= 8:
            raise EffectError("stops: between 2 and 8 colour points are needed")
        out["stops"] = parsed
    if kind == "hardware":
        out["mode"] = str(raw.get("mode") or "")[:80]
        out["colors"] = [_color(c) for c in (raw.get("colors") or [])][:8]
        if not out["mode"]:
            raise EffectError("hardware effect: mode is missing")
    return out


def is_animated(effect):
    return effect["type"] in ANIMATED


def _scale(rgb, factor):
    return (int(rgb[0] * factor + 0.5), int(rgb[1] * factor + 0.5), int(rgb[2] * factor + 0.5))


def gradient(stops, value):
    """Colour at `value` on a list of [position, [r, g, b]] stops (clamped at both ends)."""
    if value <= stops[0][0]:
        return tuple(stops[0][1])
    for (t0, c0), (t1, c1) in zip(stops, stops[1:], strict=False):
        if value <= t1:
            f = (value - t0) / (t1 - t0) if t1 > t0 else 1.0
            return tuple(int(a + (b - a) * f + 0.5) for a, b in zip(c0, c1, strict=True))
    return tuple(stops[-1][1])


def render(effect, count, t, temps=None):
    """List of `count` (r, g, b) tuples for time t (seconds). Not for 'hardware' effects."""
    kind = effect["type"]
    if kind == "off":
        return [(0, 0, 0)] * count
    bright = effect.get("brightness", 100) / 100.0
    if kind == "static":
        return [_scale(effect["color"], bright)] * count
    if kind == "breathing":
        period = 8.0 - 7.0 * effect["speed"] / 100.0          # 8 s (slow) … 1 s (fast)
        level = 0.5 - 0.5 * math.cos(2 * math.pi * t / period)
        return [_scale(effect["color"], bright * (0.04 + 0.96 * level))] * count
    if kind == "rainbow":
        cycle = 20.0 - 18.0 * effect["speed"] / 100.0          # seconds per full colour cycle
        base = (t / cycle) % 1.0
        return [_scale(tuple(int(c * 255 + 0.5) for c in colorsys.hsv_to_rgb((base + i / max(count, 1)) % 1.0, 1, 1)),
                       bright) for i in range(count)]
    if kind == "temperature":
        value = (temps or {}).get(effect["sensor"])
        if value is None:
            value = effect["stops"][0][0]
        return [_scale(gradient(effect["stops"], value), bright)] * count
    raise EffectError(f"{kind} cannot be rendered")
