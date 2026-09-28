import json
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def run_async(fn, on_done=None, on_error=None):
    """Run fn in a worker thread and deliver the result on the GTK main loop."""
    def deliver(cb, value):
        if cb:
            cb(value)
        return False

    def worker():
        try:
            result = fn()
        except Exception as e:  # handed to the UI as a message
            GLib.idle_add(deliver, on_error, e)
        else:
            GLib.idle_add(deliver, on_done, result)

    threading.Thread(target=worker, daemon=True).start()


def spin_row(title, value, lo, hi, step, on_change, digits=0, subtitle=None):
    adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 10, value=value)
    row = Adw.SpinRow(title=title, adjustment=adj, digits=digits)
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::value", lambda r, _p: on_change(r.get_value()))
    return row


def switch_row(title, active, on_change, subtitle=None):
    row = Adw.SwitchRow(title=title, active=bool(active))
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


def combo_row(title, labels, selected, on_change, subtitle=None):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    row.set_selected(max(0, selected))
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::selected", lambda r, _p: on_change(r.get_selected()))
    return row


def entry_row(title, text, on_apply):
    row = Adw.EntryRow(title=title, text=text or "", show_apply_button=True)
    row.connect("apply", lambda r: on_apply(r.get_text().strip()))
    return row


def value_label():
    return Gtk.Label(css_classes=["numeric", "dim-label"], valign=Gtk.Align.CENTER)


_UNIT = {"fahrenheit": False}


def set_fahrenheit(enabled):
    _UNIT["fahrenheit"] = bool(enabled)


def temp_unit():
    return "°F" if _UNIT["fahrenheit"] else "°C"


def c_to_disp(c):
    return c * 9 / 5 + 32 if _UNIT["fahrenheit"] else c


def disp_to_c(v):
    return (v - 32) * 5 / 9 if _UNIT["fahrenheit"] else v


def delta_to_disp(d):
    return d * 9 / 5 if _UNIT["fahrenheit"] else d


def disp_to_delta(v):
    return v * 5 / 9 if _UNIT["fahrenheit"] else v


def temp_spin_row(title, value_c, lo_c, hi_c, on_change, delta=False, digits=0, subtitle=None):
    """Spin row that edits a temperature (or temperature difference) in the chosen display unit."""
    to_disp, from_disp = (delta_to_disp, disp_to_delta) if delta else (c_to_disp, disp_to_c)
    step = 0.5 if digits else 1
    return spin_row(f"{title} {temp_unit()}", round(to_disp(value_c), 1), to_disp(lo_c), to_disp(hi_c), step,
                    lambda v: on_change(round(from_disp(v), 2)), digits=digits, subtitle=subtitle)


def fmt_temp(v):
    return "–" if v is None else f"{c_to_disp(v):.1f} {temp_unit()}"


def fmt_rpm(v):
    return "–" if v is None else f"{v} RPM"


def fmt_pct(v):
    return "–" if v is None else f"{v:.0f} %"


PREFS_VERSION = 2


class GuiPrefs:
    def __init__(self):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        self.path = os.path.join(base, "fancontrol-linux", "gui.json")
        self.data = {"theme": "system", "palette": None, "custom_colors": {}, "setup_done": False,
                     "fahrenheit": False, "show_hidden": False, "tray_enabled": True, "tray_sensors": [],
                     "prefs_version": PREFS_VERSION}
        try:
            with open(self.path) as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = {}
        if stored.get("prefs_version", 1) < 2 and stored.get("palette") == "classic":
            # Version 1 saved the then-default theme on every change; treat it as "not chosen".
            stored.pop("palette")
        self.data.update(stored)
        self.data["prefs_version"] = PREFS_VERSION

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass
