"""Configuration and profile storage (JSON)."""

import copy
import json
import os
import re
import tempfile
import uuid

from .curves import CURVE_TYPES, MIX_FUNCTIONS, TEMP_CURVES
from .sensors import CUSTOM_TYPES, file_path_allowed

CONFIG_VERSION = 2
MAX_RPM = 20000.0
MAX_RESPONSE = 300.0

DEFAULT_SETTINGS = {
    "interval": 1.0,
    "safety_temp": 95.0,
    "liquidctl": True,
    "thermaltake": False,
}

DEFAULT_CONTROL = {
    "name": "",
    "enabled": False,
    "mode": "curve",
    "curve": None,
    "manual_percent": 50.0,
    "fan": None,
    "min_percent": 0.0,
    "max_percent": 100.0,
    "start_percent": 35.0,
    "stop_percent": 0.0,
    "offset": 0.0,
    "step_up": 0.0,
    "step_down": 0.0,
    "avoid": [],
    "force_apply": True,
    "calibration": None,
}

_HYSTERESIS = {"hysteresis_up": 2.0, "hysteresis_down": 2.0, "response_up": 1.0, "response_down": 1.0,
               "ignore_hysteresis_at_limits": True}

CURVE_DEFAULTS = {
    "graph": {"points": [[30, 20], [50, 35], [65, 55], [75, 80], [85, 100]], "temp_axis": [10.0, 100.0],
              **_HYSTERESIS},
    "linear": {"temp_min": 40.0, "temp_max": 80.0, "speed_min": 20.0, "speed_max": 100.0, **_HYSTERESIS},
    "flat": {"value": 50.0},
    "mix": {"curves": [], "function": "max"},
    "trigger": {"idle_temp": 50.0, "load_temp": 65.0, "idle_speed": 30.0, "load_speed": 80.0,
                "response_up": 3.0, "response_down": 3.0},
    "sync": {"control": None, "offset": 0.0, "proportional": False},
    "auto": {"idle_temp": 45.0, "load_temp": 70.0, "min_speed": 20.0, "max_speed": 100.0,
             "step": 2.0, "deadband": 2.0, "response_time": 3.0},
}

CUSTOM_DEFAULTS = {
    "mix": {"sensors": [], "function": "max", "allow_missing": False},
    "average": {"sensor": None, "seconds": 30.0},
    "offset": {"sensor": None, "offset": 0.0, "proportional": False},
    "file": {"path": "/run/fancontrol-linux/sensors/example.sensor"},
}


class ConfigError(ValueError):
    pass


def config_dir():
    return os.environ.get("FANCONTROL_CONFIG_DIR", "/etc/fancontrol-linux")


def new_id():
    return uuid.uuid4().hex[:8]


def empty_config():
    return {"version": CONFIG_VERSION, "profile": "Default", "settings": dict(DEFAULT_SETTINGS),
            "curves": [], "controls": [], "custom_sensors": [], "sensor_names": {}, "hidden": []}


def new_curve(kind, name, sensors=None):
    if kind not in CURVE_TYPES:
        raise ConfigError(f"Unknown curve type: {kind}")
    curve = {"id": new_id(), "name": name, "type": kind, "unit": "percent"}
    curve.update(copy.deepcopy(CURVE_DEFAULTS[kind]))
    if kind in TEMP_CURVES:
        curve["sensors"] = list(sensors or [])
        curve["sensor_mix"] = "max"
    return curve


def new_custom_sensor(kind, name, source=None):
    if kind not in CUSTOM_TYPES:
        raise ConfigError(f"Unknown sensor type: {kind}")
    sensor = {"id": new_id(), "name": name, "type": kind}
    sensor.update(copy.deepcopy(CUSTOM_DEFAULTS[kind]))
    if source and kind == "mix":
        sensor["sensors"] = [source]
    elif source and kind in ("average", "offset"):
        sensor["sensor"] = source
    return sensor


def _num(value, lo, hi, field):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field}: number expected") from None
    if v != v or not lo <= v <= hi:
        raise ConfigError(f"{field}: must be between {lo:g} and {hi:g}")
    return v


def _migrate_curve(raw):
    raw = dict(raw)
    if "hysteresis" in raw:
        h = raw.pop("hysteresis")
        raw.setdefault("hysteresis_up", h)
        raw.setdefault("hysteresis_down", h)
    if "response_time" in raw and raw.get("type") in ("graph", "linear", "trigger"):
        r = raw.pop("response_time")
        raw.setdefault("response_up", r)
        raw.setdefault("response_down", r)
    return raw


def _migrate_control(raw):
    raw = dict(raw)
    if "zero_rpm" in raw:
        if raw.pop("zero_rpm") and not raw.get("stop_percent"):
            raw["stop_percent"] = 1.0
    return raw


def _custom_sensor(raw, ids):
    kind = raw.get("type")
    if kind not in CUSTOM_TYPES:
        raise ConfigError(f"Unknown sensor type: {kind}")
    sid = str(raw.get("id") or new_id())
    if sid in ids:
        raise ConfigError(f"Duplicate sensor ID: {sid}")
    ids.add(sid)
    out = {"id": sid, "name": str(raw.get("name") or kind), "type": kind}
    for key, default in CUSTOM_DEFAULTS[kind].items():
        out[key] = copy.deepcopy(raw.get(key, default))
    if kind == "mix":
        out["sensors"] = [str(x) for x in out["sensors"]]
        if out["function"] not in MIX_FUNCTIONS:
            raise ConfigError(f"Invalid function: {out['function']}")
        out["allow_missing"] = bool(out["allow_missing"])
    elif kind == "average":
        out["sensor"] = str(out["sensor"]) if out["sensor"] else None
        out["seconds"] = _num(out["seconds"], 1, 3600, "time span")
    elif kind == "offset":
        out["sensor"] = str(out["sensor"]) if out["sensor"] else None
        out["offset"] = _num(out["offset"], -100, 100, "Offset")
        out["proportional"] = bool(out["proportional"])
    elif kind == "file":
        out["path"] = str(out["path"])
        if not file_path_allowed(out["path"]):
            raise ConfigError(f"File sensor '{out['name']}': only files below /run/fancontrol-linux/sensors/, "
                              "/var/lib/fancontrol-linux/sensors/ or /sys/ are allowed")
    return out


def normalize(cfg):
    """Validate a config coming from disk or a client and fill in defaults. Raises ConfigError."""
    if not isinstance(cfg, dict):
        raise ConfigError("Configuration must be an object")
    out = empty_config()
    out["profile"] = str(cfg.get("profile") or "Default")
    settings = cfg.get("settings") or {}
    out["settings"]["interval"] = _num(settings.get("interval", 1.0), 0.2, 10, "interval")
    out["settings"]["safety_temp"] = _num(settings.get("safety_temp", 95.0), 0, 150, "safety_temp")
    out["settings"]["liquidctl"] = bool(settings.get("liquidctl", True))
    out["settings"]["thermaltake"] = bool(settings.get("thermaltake", False))
    names = cfg.get("sensor_names") or {}
    out["sensor_names"] = {str(k): str(v) for k, v in names.items() if str(v).strip()}
    out["hidden"] = sorted({str(h) for h in cfg.get("hidden") or []})

    sensor_ids = set()
    for raw in cfg.get("custom_sensors") or []:
        out["custom_sensors"].append(_custom_sensor(raw, sensor_ids))

    curve_ids = set()
    for raw in cfg.get("curves") or []:
        raw = _migrate_curve(raw)
        kind = raw.get("type")
        if kind not in CURVE_TYPES:
            raise ConfigError(f"Unknown curve type: {kind}")
        cid = str(raw.get("id") or new_id())
        if cid in curve_ids:
            raise ConfigError(f"Duplicate curve ID: {cid}")
        curve_ids.add(cid)
        curve = {"id": cid, "name": str(raw.get("name") or kind), "type": kind,
                 "unit": raw.get("unit", "percent")}
        if curve["unit"] not in ("percent", "rpm"):
            raise ConfigError(f"Invalid unit: {curve['unit']}")
        top = MAX_RPM if curve["unit"] == "rpm" else 100.0
        for key, default in CURVE_DEFAULTS[kind].items():
            curve[key] = copy.deepcopy(raw.get(key, default))
        name = curve["name"]
        if kind in TEMP_CURVES:
            curve["sensors"] = [str(s) for s in raw.get("sensors") or []]
            curve["sensor_mix"] = raw.get("sensor_mix", "max")
            if curve["sensor_mix"] not in MIX_FUNCTIONS:
                raise ConfigError(f"Invalid sensor combination: {curve['sensor_mix']}")
        if kind == "graph":
            pts = [[_num(p[0], -50, 150, "curve point °C"), _num(p[1], 0, top, "curve point")]
                   for p in curve["points"]]
            if not pts:
                raise ConfigError(f"Curve '{name}' needs at least one point")
            curve["points"] = sorted(pts)
            lo, hi = (_num(v, -50, 150, "temperature range") for v in curve["temp_axis"])
            if hi - lo < 10:
                raise ConfigError(f"Curve '{name}': the temperature range must span at least 10 °C")
            curve["temp_axis"] = [lo, hi]
        if kind == "linear":
            for key in ("temp_min", "temp_max"):
                curve[key] = _num(curve[key], -50, 150, key)
            for key in ("speed_min", "speed_max"):
                curve[key] = _num(curve[key], 0, top, key)
            if curve["temp_max"] <= curve["temp_min"]:
                raise ConfigError(f"Curve '{name}': max temperature must be above min temperature")
        if kind == "flat":
            curve["value"] = _num(curve["value"], 0, top, "value")
        if kind == "mix":
            curve["curves"] = [str(c) for c in curve["curves"]]
            if curve["function"] not in MIX_FUNCTIONS:
                raise ConfigError(f"Invalid mix function: {curve['function']}")
        if kind in ("trigger", "auto"):
            for key in ("idle_temp", "load_temp"):
                curve[key] = _num(curve[key], -50, 150, key)
            if curve["load_temp"] <= curve["idle_temp"]:
                raise ConfigError(f"Curve '{name}': load temperature must be above idle temperature")
        if kind == "trigger":
            for key in ("idle_speed", "load_speed"):
                curve[key] = _num(curve[key], 0, top, key)
        if kind == "auto":
            for key in ("min_speed", "max_speed"):
                curve[key] = _num(curve[key], 0, top, key)
            if curve["max_speed"] < curve["min_speed"]:
                raise ConfigError(f"Curve '{name}': max speed must be ≥ min speed")
            curve["step"] = _num(curve["step"], 0.1, 100 if top == 100 else 2000, "step")
            curve["deadband"] = _num(curve["deadband"], 0, 20, "deadband")
        if kind == "sync":
            curve["control"] = str(curve["control"]) if curve["control"] else None
            curve["offset"] = _num(curve["offset"], -100, 100, "offset")
            curve["proportional"] = bool(curve["proportional"])
        for key in ("hysteresis_up", "hysteresis_down"):
            if key in curve:
                curve[key] = _num(curve[key], 0, 50, key)
        for key in ("response_up", "response_down", "response_time"):
            if key in curve:
                curve[key] = _num(curve[key], 0, MAX_RESPONSE, key)
        if "ignore_hysteresis_at_limits" in curve:
            curve["ignore_hysteresis_at_limits"] = bool(curve["ignore_hysteresis_at_limits"])
        out["curves"].append(curve)

    for curve in out["curves"]:
        if curve["type"] == "mix":
            curve["curves"] = [c for c in curve["curves"] if c in curve_ids and c != curve["id"]]

    control_ids = set()
    for raw in cfg.get("controls") or []:
        raw = _migrate_control(raw)
        cid = str(raw.get("id") or "")
        if not cid or cid in control_ids:
            raise ConfigError("Control with missing or duplicate ID")
        control_ids.add(cid)
        ctl = {"id": cid}
        for key, default in DEFAULT_CONTROL.items():
            ctl[key] = copy.deepcopy(raw.get(key, default))
        label = ctl["name"] or cid
        ctl["name"] = str(ctl["name"] or "")
        ctl["enabled"] = bool(ctl["enabled"])
        ctl["force_apply"] = bool(ctl["force_apply"])
        if ctl["mode"] not in ("curve", "manual"):
            raise ConfigError(f"Control '{label}': invalid mode")
        if ctl["curve"] not in curve_ids:
            ctl["curve"] = None
        for key in ("min_percent", "max_percent", "start_percent", "stop_percent", "manual_percent"):
            ctl[key] = _num(ctl[key], 0, 100, key)
        if ctl["max_percent"] < ctl["min_percent"]:
            raise ConfigError(f"Control '{label}': max % must be ≥ min %")
        ctl["offset"] = _num(ctl["offset"], -100, 100, "offset")
        ctl["step_up"] = _num(ctl["step_up"], 0, 100, "step_up")
        ctl["step_down"] = _num(ctl["step_down"], 0, 100, "step_down")
        avoid = []
        for rng in ctl["avoid"] or []:
            a, b = _num(rng[0], 0, 100, "avoid"), _num(rng[1], 0, 100, "avoid")
            if a > b:
                a, b = b, a
            if b > a:
                avoid.append([a, b])
        ctl["avoid"] = sorted(avoid)
        cal = ctl["calibration"]
        if cal is not None:
            try:
                curve = sorted([_num(p, 0, 100, "calibration %"), _num(r, 0, 100000, "calibration RPM")]
                               for p, r in cal["rpm_curve"])
            except (KeyError, TypeError, ValueError):
                raise ConfigError(f"Control '{label}': invalid calibration") from None
            ctl["calibration"] = {"rpm_curve": curve}
        out["controls"].append(ctl)
    return out


def _atomic_write_json(path, data, mode=0o644):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    try:
        with open(config_path()) as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        return empty_config()


def save_config(cfg):
    _atomic_write_json(config_path(), cfg)


def profiles_dir():
    return os.path.join(config_dir(), "profiles")


_PROFILE_NAME = re.compile(r"^[\w .()+-]{1,64}$")


def _profile_path(name):
    name = (name or "").strip()
    if not _PROFILE_NAME.match(name) or name.startswith("."):
        raise ConfigError("Invalid profile name (allowed: letters, digits, spaces, . ( ) + - _)")
    return os.path.join(profiles_dir(), name + ".json")


def list_profiles():
    try:
        files = os.listdir(profiles_dir())
    except FileNotFoundError:
        return []
    return sorted(f[:-5] for f in files if f.endswith(".json") and not f.startswith("."))


def save_profile(name, cfg):
    cfg = dict(cfg, profile=name.strip())
    _atomic_write_json(_profile_path(name), cfg)
    return cfg


def load_profile(name):
    try:
        with open(_profile_path(name)) as f:
            cfg = normalize(json.load(f))
    except FileNotFoundError:
        raise ConfigError(f"Profile '{name}' does not exist") from None
    cfg["profile"] = name.strip()
    return cfg


def delete_profile(name):
    try:
        os.unlink(_profile_path(name))
    except FileNotFoundError:
        raise ConfigError(f"Profile '{name}' does not exist") from None
