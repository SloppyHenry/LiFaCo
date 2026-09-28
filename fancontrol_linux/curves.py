"""Fan curve evaluation. Curves map temperatures (or other controls) to a speed in % – or in RPM when unit == "rpm"."""

import math

CURVE_TYPES = ("graph", "linear", "flat", "mix", "trigger", "sync", "auto")
MIX_FUNCTIONS = ("max", "min", "avg", "sum", "sub")
TEMP_CURVES = ("graph", "linear", "trigger", "auto")
FAILSAFE = math.inf  # "run at full speed"; controls turn it into their maximum


def interpolate(points, x):
    if not points:
        return 0.0
    pts = sorted(points)
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])


def combine(values, function):
    values = [v for v in values if v is not None]
    if not values:
        return None
    if function == "min":
        return min(values)
    if function == "avg":
        return sum(values) / len(values)
    if function == "sum":
        return sum(values)
    if function == "sub":
        return values[0] - sum(values[1:])
    return max(values)


def _static_output(curve, temp):
    if curve["type"] == "graph":
        return interpolate(curve.get("points", []), temp)
    return interpolate([
        (curve.get("temp_min", 40), curve.get("speed_min", 20)),
        (curve.get("temp_max", 80), curve.get("speed_max", 100)),
    ], temp)


def _output_range(curve):
    if curve["type"] == "graph":
        speeds = [p[1] for p in curve.get("points", [])] or [0]
        return min(speeds), max(speeds)
    lo, hi = curve.get("speed_min", 20), curve.get("speed_max", 100)
    return min(lo, hi), max(lo, hi)


class CurveState:
    def __init__(self):
        self.accepted_temp = None
        self.pending_since = None
        self.pending_dir = 0
        self.trigger_load = False
        self.auto_output = None
        self.last_time = None
        self.output = None
        self.temp = None


class CurveEvaluator:
    """Evaluates all curves for one control cycle, keeping hysteresis/auto state between cycles."""

    def __init__(self):
        self.states: dict[str, CurveState] = {}

    def reset(self, curves):
        ids = {c["id"] for c in curves}
        self.states = {cid: st for cid, st in self.states.items() if cid in ids}

    def evaluate(self, curves, temps, now, controls=None):
        """controls: {control_id: percent applied in the previous cycle} for sync curves."""
        by_id = {c["id"]: c for c in curves}
        controls = controls or {}
        results = {}

        def run(cid, stack):
            if cid in results:
                return results[cid]
            curve = by_id.get(cid)
            if curve is None or cid in stack:
                return FAILSAFE
            state = self.states.setdefault(cid, CurveState())
            out = self._evaluate_one(curve, state, temps, controls, now, lambda other: run(other, stack | {cid}))
            state.output = out
            results[cid] = out
            return out

        for cid in by_id:
            run(cid, frozenset())
        return results

    @staticmethod
    def _source_temp(curve, temps):
        sensors = curve.get("sensors", [])
        values = [temps.get(s) for s in sensors]
        if not sensors or any(v is None for v in values):
            return None
        return combine(values, curve.get("sensor_mix", "max"))

    def _evaluate_one(self, curve, state, temps, controls, now, run):
        kind = curve["type"]
        if kind == "flat":
            return float(curve.get("value", 50))
        if kind == "mix":
            values = [run(r) for r in curve.get("curves", [])]
            if not values or any(math.isinf(v) for v in values):
                return FAILSAFE
            return max(0.0, combine(values, curve.get("function", "max")))
        if kind == "sync":
            source = controls.get(curve.get("control"))
            if source is None:
                return FAILSAFE
            offset = curve.get("offset", 0.0)
            return max(0.0, source * (1 + offset / 100) if curve.get("proportional") else source + offset)

        temp = self._source_temp(curve, temps)
        state.temp = temp
        if temp is None:
            # A missing sensor must never result in a stopped fan.
            state.accepted_temp = state.pending_since = None
            return FAILSAFE
        if kind == "trigger":
            return self._trigger(curve, state, temp, now)
        if kind == "auto":
            return self._auto(curve, state, temp, now)
        return _static_output(curve, self._apply_hysteresis(curve, state, temp, now))

    @staticmethod
    def _held(state, direction, response_time, now):
        if response_time <= 0:
            return True
        if state.pending_since is None or state.pending_dir != direction:
            state.pending_since, state.pending_dir = now, direction
        return now - state.pending_since >= response_time

    def _apply_hysteresis(self, curve, state, temp, now):
        if state.accepted_temp is None:
            state.accepted_temp = temp
            return temp
        if curve.get("ignore_hysteresis_at_limits", True):
            lo, hi = _output_range(curve)
            target = _static_output(curve, temp)
            if (target >= hi or target <= lo) and target != _static_output(curve, state.accepted_temp):
                state.accepted_temp, state.pending_since = temp, None
                return temp
        delta = temp - state.accepted_temp
        direction = 1 if delta > 0 else -1
        hyst = curve.get("hysteresis_up" if direction > 0 else "hysteresis_down", 0.0)
        response = curve.get("response_up" if direction > 0 else "response_down", 0.0)
        if delta != 0 and abs(delta) >= hyst:
            if self._held(state, direction, response, now):
                state.accepted_temp, state.pending_since = temp, None
        else:
            state.pending_since = None
        return state.accepted_temp

    def _trigger(self, curve, state, temp, now):
        wants_load = temp >= curve.get("load_temp", 65) if not state.trigger_load else temp > curve.get("idle_temp", 50)
        if wants_load != state.trigger_load:
            response = curve.get("response_up" if wants_load else "response_down", 0.0)
            if self._held(state, 1 if wants_load else -1, response, now):
                state.trigger_load, state.pending_since = wants_load, None
        else:
            state.pending_since = None
        return float(curve.get("load_speed", 80) if state.trigger_load else curve.get("idle_speed", 30))

    def _auto(self, curve, state, temp, now):
        """Feedback loop: find the lowest speed that holds the load temperature."""
        lo, hi = curve.get("min_speed", 20.0), curve.get("max_speed", 100.0)
        idle, load = curve.get("idle_temp", 45.0), curve.get("load_temp", 70.0)
        deadband, step = curve.get("deadband", 2.0), curve.get("step", 2.0)
        dt = 0.0 if state.last_time is None else max(0.0, now - state.last_time)
        state.last_time = now
        if state.auto_output is None:
            state.auto_output = lo
        if temp >= load + deadband:
            if self._held(state, 1, curve.get("response_time", 0.0), now):
                state.auto_output = min(hi, state.auto_output + step * dt)
        elif temp <= load - deadband:
            if self._held(state, -1, curve.get("response_time", 0.0), now):
                state.auto_output = max(lo, state.auto_output - step * dt)
        else:
            state.pending_since = None
        if temp >= load + 10:
            state.auto_output = hi
        if temp <= idle:
            return float(lo)
        if temp < load:
            return interpolate([(idle, lo), (load, state.auto_output)], temp)
        return float(state.auto_output)


def referenced_sensors(curves):
    return {s for c in curves for s in c.get("sensors", [])}
