"""Control loop: sensors -> custom sensors -> curves -> per-control tuning -> fan outputs."""

import math
import time

from .curves import CurveEvaluator, interpolate, referenced_sensors
from .sensors import CustomSensors

KICK_SECONDS = 2.0
REASSERT_SECONDS = 5.0
RESCAN_SECONDS = 60.0
STALL_SECONDS = 3.0
BOOST_STEP = 5.0
BOOST_EVERY = 2.0
OVERRIDE_TOLERANCE = 2.5


def map_output(ctl, curve_value):
    """Turn a curve result into the percentage for one control (offset, stop %, limits, avoid ranges)."""
    target = curve_value + ctl.get("offset", 0.0)
    stop = ctl.get("stop_percent", 0.0)
    if stop > 0 and target < stop:
        return 0.0
    lo, hi = ctl.get("min_percent", 0.0), ctl.get("max_percent", 100.0)
    target = max(lo, min(hi, target))
    for a, b in ctl.get("avoid", []):
        if a < target < b:
            candidates = [e for e in (a, b) if lo <= e <= hi and not (e == 0 and stop <= 0)]
            if candidates:
                target = min(candidates, key=lambda e: (abs(e - target), -e))
    return float(target)


def rate_limit(previous, target, ctl, dt):
    if previous is None:
        return target
    if target > previous and ctl.get("step_up", 0) > 0:
        return min(target, previous + ctl["step_up"] * dt)
    if target < previous and ctl.get("step_down", 0) > 0:
        return max(target, previous - ctl["step_down"] * dt)
    return target


def rpm_to_percent(calibration, rpm):
    """Percent needed for an RPM target, from the calibration sweep. None if not calibrated."""
    if not calibration or math.isinf(rpm):
        return None if not calibration else math.inf
    curve = sorted(calibration["rpm_curve"])
    if rpm <= 0:
        return 0.0
    spinning = [(p, r) for p, r in curve if r > 0]
    if not spinning:
        return None
    if rpm >= max(r for _p, r in spinning):
        return 100.0
    # Monotonic rpm -> percent lookup (ignoring the flat stopped region).
    pairs, best = [], -1
    for p, r in spinning:
        if r > best:
            pairs.append((r, p))
            best = r
    return interpolate(pairs, rpm)


class ControlState:
    def __init__(self):
        self.applied = None
        self.written = None
        self.last_write = 0.0
        self.kick_until = 0.0
        self.target = None
        self.error = None
        self.overridden = False
        self.stall_since = None
        self.boost = 0.0
        self.last_boost = 0.0
        self.identify_until = 0.0


class Calibration:
    DESCEND = list(range(100, -1, -5))
    ASCEND = list(range(5, 55, 5))

    def __init__(self, control_id, fan_hint, settle=4.0):
        self.control_id = control_id
        self.fan_hint = fan_hint
        self.plan = [(100, settle + 2, "down")]
        self.plan += [(p, settle, "down") for p in self.DESCEND[1:]]
        self.plan += [(0, settle + 2, "rest")]
        self.plan += [(p, settle, "up") for p in self.ASCEND]
        self.index = 0
        self.step_started = None
        self.records = []
        self.result = None
        self.error = None
        self.running = True
        self.run = time.time()

    @property
    def current_percent(self):
        return self.plan[min(self.index, len(self.plan) - 1)][0]

    def progress(self):
        return self.index / len(self.plan)

    def step(self, now, fans):
        if self.step_started is None:
            self.step_started = now
            return
        pct, hold, phase = self.plan[self.index]
        if now - self.step_started < hold:
            return
        self.records.append((phase, pct, dict(fans)))
        self.index += 1
        self.step_started = now
        if self.index >= len(self.plan):
            self.running = False
            self._analyze()

    def _analyze(self):
        down = [(pct, fans) for phase, pct, fans in self.records if phase == "down"]
        up = [(pct, fans) for phase, pct, fans in self.records if phase == "up"]
        full, lowest = down[0][1], down[-1][1]

        fan = self.fan_hint if self.fan_hint in full else None
        if fan is None:
            deltas = {f: (full.get(f) or 0) - (lowest.get(f) or 0) for f in full}
            if deltas:
                best = max(deltas, key=deltas.get)
                if deltas[best] > 100:
                    fan = best
        if fan is None or not full.get(fan):
            self.error = "Kein Drehzahlsensor hat auf diese Steuerung reagiert."
            return

        curve = [[pct, fans.get(fan) or 0] for pct, fans in down]
        max_rpm = full.get(fan)
        spinning = [(pct, rpm) for pct, rpm in curve if rpm > 0]
        stops = curve[-1][1] == 0
        tolerance = max(30, 0.03 * max_rpm)
        # Below this point the speed no longer changes (fans with a hardware minimum).
        flat_from = min(p for p, _r in spinning) if spinning else 100
        for pct, rpm in spinning:
            lower = [r for p, r in spinning if p < pct]
            if lower and all(abs(r - rpm) <= tolerance for r in lower):
                flat_from = pct
                break
        start = next((pct for pct, fans in up if (fans.get(fan) or 0) > 0), None)
        if stops:
            lowest_spin = min(p for p, _r in spinning) if spinning else 100
            suggested_min = min(100, max(lowest_spin + 5, flat_from if flat_from > lowest_spin else 0))
            suggested_start = min(100, (start if start is not None else lowest_spin) + 5)
            suggested_stop = float(lowest_spin)
        else:
            suggested_min = flat_from if flat_from < 100 and flat_from > min(p for p, _r in curve) else 0
            suggested_start = 0
            suggested_stop = 0.0
        self.result = {
            "fan": fan,
            "max_rpm": max_rpm,
            "min_rpm": min(r for _p, r in spinning) if spinning else 0,
            "rpm_curve": curve,
            "can_stop": stops,
            "suggested_min_percent": float(suggested_min),
            "suggested_start_percent": float(suggested_start),
            "suggested_stop_percent": suggested_stop,
        }

    def status(self):
        return {
            "control": self.control_id,
            "run": self.run,
            "running": self.running,
            "progress": round(self.progress(), 3),
            "percent": self.current_percent if self.running else None,
            "result": self.result,
            "error": self.error,
        }


def _finite(value):
    return None if value is None or math.isinf(value) else value


class Engine:
    def __init__(self, hardware, config, clock=time.monotonic):
        self.hw = hardware
        self.clock = clock
        self.config = config
        self.evaluator = CurveEvaluator()
        self.custom = CustomSensors()
        self.controls: dict[str, ControlState] = {}
        self.calibration: Calibration | None = None
        self.last_tick = None
        self.last_rescan = clock()
        self.safety_active = False
        self.status_cache = {}
        self.support = hardware.info() if hasattr(hardware, "info") else []

    def set_config(self, config):
        self.config = config
        self.evaluator.reset(config["curves"])
        self.custom.reset(config["custom_sensors"])
        if hasattr(self.hw, "configure"):
            self.hw.configure(config["settings"])
            self.support = self.hw.info()

    def rescan(self, full=True):
        self.hw.scan(full=full)
        self.last_rescan = self.clock()
        if hasattr(self.hw, "info"):
            self.support = self.hw.info()

    def start_calibration(self, control_id, settle=4.0):
        if control_id not in self.hw.pwms:
            raise ValueError("Unbekannte Steuerung")
        if self.calibration and self.calibration.running:
            raise ValueError("Es läuft bereits eine Kalibrierung")
        ctl = next((c for c in self.config["controls"] if c["id"] == control_id), None)
        hint = (ctl or {}).get("fan") or self.hw.pwms[control_id].default_fan
        self.calibration = Calibration(control_id, hint, settle)

    def cancel_calibration(self):
        if self.calibration and self.calibration.running:
            self.calibration.running = False
            self.calibration.error = "Abgebrochen"

    def identify(self, control_id, seconds=10.0):
        if control_id not in self.hw.pwms:
            raise ValueError("Unbekannte Steuerung")
        self.controls.setdefault(control_id, ControlState()).identify_until = self.clock() + seconds

    def _write(self, pwm, state, percent, now, force_apply=True):
        state.error = None
        try:
            pwm.take_control()
            if pwm.pwm_path is not None and state.written is not None:
                current = pwm.read_percent()
                state.overridden = current is not None and abs(current - state.written) > OVERRIDE_TOLERANCE
                if state.overridden and force_apply:
                    state.written = None
            if state.written != percent or now - state.last_write >= REASSERT_SECONDS:
                pwm.write_percent(percent)
                state.written, state.last_write = percent, now
        except OSError as e:
            state.error = f"{e.strerror or e} ({pwm.id})"

    def _control_value(self, ctl, outputs, curve_units, state):
        """Percent requested by the control's source, or inf for failsafe, or None for 'not controlled'."""
        if ctl["mode"] == "manual":
            return ctl["manual_percent"]
        if ctl["curve"] is None:
            return None
        value = outputs.get(ctl["curve"], math.inf)
        if curve_units.get(ctl["curve"]) == "rpm":
            converted = rpm_to_percent(ctl.get("calibration"), value)
            if converted is None:
                state.error = "RPM-Kurve braucht eine Kalibrierung dieser Steuerung"
                return math.inf
            return converted
        return value

    def tick(self):
        now = self.clock()
        dt = 0.0 if self.last_tick is None else max(0.0, now - self.last_tick)
        self.last_tick = now
        if now - self.last_rescan >= RESCAN_SECONDS:
            self.rescan(full=False)

        cfg = self.config
        temps = self.hw.read_temps()
        fans = self.hw.read_fans()
        custom = self.custom.evaluate(cfg["custom_sensors"], temps, now)
        all_temps = {**temps, **custom}
        previous = {}
        if any(c["type"] == "sync" for c in cfg["curves"]):
            for pid, pwm in self.hw.pwms.items():
                st = self.controls.get(pid)
                # Outputs left to the firmware still have a measurable duty cycle to follow.
                previous[pid] = st.applied if st and st.applied is not None else pwm.read_percent()
        outputs = self.evaluator.evaluate(cfg["curves"], all_temps, now, previous)
        curve_units = {c["id"]: c.get("unit", "percent") for c in cfg["curves"]}

        safety_temp = cfg["settings"].get("safety_temp", 0)
        watched = referenced_sensors(cfg["curves"])
        self.safety_active = bool(safety_temp) and any(
            all_temps.get(s) is not None and all_temps[s] >= safety_temp for s in watched)

        cal = self.calibration
        if cal and cal.running:
            if self.safety_active:
                cal.running = False
                cal.error = "Abgebrochen: Sicherheitstemperatur erreicht"
            else:
                cal.step(now, fans)

        configured = {c["id"]: c for c in cfg["controls"]}
        for pid, pwm in self.hw.pwms.items():
            state = self.controls.setdefault(pid, ControlState())
            ctl = configured.get(pid)

            if cal and cal.running and cal.control_id == pid:
                state.target = state.applied = float(cal.current_percent)
                self._write(pwm, state, state.target, now)
                continue
            if now < state.identify_until:
                state.target = state.applied = 100.0
                self._write(pwm, state, 100.0, now)
                continue

            value = None
            if ctl and ctl["enabled"]:
                state.error = None
                value = self._control_value(ctl, outputs, curve_units, state)
            if value is None:
                if pwm.controlled:
                    pwm.restore()
                state.applied = state.written = state.target = None
                state.overridden, state.boost, state.stall_since = False, 0.0, None
                continue

            error = state.error
            target = 100.0 if self.safety_active else map_output(ctl, value)
            state.target = target
            applied = target if self.safety_active else rate_limit(state.applied, target, ctl, dt)
            if state.applied is not None and state.applied <= 0 < applied:
                state.kick_until = now + KICK_SECONDS
            if now < state.kick_until and applied > 0:
                applied = max(applied, ctl["start_percent"])
            applied = self._stall_boost(ctl, pwm, state, applied, fans, now)
            state.applied = applied
            self._write(pwm, state, round(applied, 1), now, ctl["force_apply"])
            state.error = state.error or error

        self.status_cache = self._build_status(all_temps, custom, fans, outputs)
        return self.status_cache

    def _stall_boost(self, ctl, pwm, state, applied, fans, now):
        """Progressively raise the speed when a calibrated fan does not start spinning."""
        fan = ctl.get("fan") or pwm.default_fan
        calibrated = ctl.get("calibration") and any(r > 0 for _p, r in ctl["calibration"]["rpm_curve"])
        rpm = fans.get(fan) if fan else None
        if not calibrated or rpm is None or applied <= 0 or now < state.kick_until:
            state.stall_since, state.boost = None, 0.0
            return applied
        if rpm > 0:
            state.stall_since, state.boost = None, 0.0
            return applied
        if state.stall_since is None:
            state.stall_since = now
        elif now - state.stall_since >= STALL_SECONDS and now - state.last_boost >= BOOST_EVERY:
            state.boost = min(100.0, state.boost + BOOST_STEP)
            state.last_boost = now
        if state.boost:
            return max(applied, min(100.0, ctl["start_percent"] + state.boost))
        return applied

    def _build_status(self, temps, custom, fans, outputs):
        labels = self.hw.temp_labels()
        custom_names = {f"custom:{s['id']}": s["name"] for s in self.config["custom_sensors"]}
        names = dict(self.config.get("sensor_names", {}))
        names.update({c["id"]: c["name"] for c in self.config["controls"] if c.get("name")})
        now = self.clock()
        pwms = {}
        for pid, pwm in self.hw.pwms.items():
            state = self.controls.get(pid) or ControlState()
            pwms[pid] = {
                "label": pwm.label,
                "name": names.get(pid, ""),
                "percent": pwm.read_percent(),
                "controlled": pwm.controlled,
                "target": _finite(state.target),
                "default_fan": pwm.default_fan,
                "error": state.error,
                "overridden": state.overridden,
                "boost": state.boost,
                "identifying": now < state.identify_until,
                "kind": type(pwm).__name__,
            }
        temp_status = {}
        for sid, v in temps.items():
            if sid in custom:
                temp_status[sid] = {"label": custom_names.get(sid, sid), "name": "", "value": v, "custom": True}
            else:
                temp_status[sid] = {"label": labels.get(sid, sid), "name": names.get(sid, ""), "value": v}
        return {
            "profile": self.config.get("profile"),
            "safety_active": self.safety_active,
            "temps": temp_status,
            "fans": {sid: {"label": self.hw.fans[sid].label, "name": names.get(sid, ""), "value": v}
                     for sid, v in fans.items() if sid in self.hw.fans},
            "pwms": pwms,
            "curves": {cid: {"output": _finite(out), "failsafe": math.isinf(out),
                             "temp": self.evaluator.states[cid].temp}
                       for cid, out in outputs.items() if cid in self.evaluator.states},
            "calibration": self.calibration.status() if self.calibration else None,
            "support": self.support,
        }

    def shutdown(self):
        self.hw.restore_all()
