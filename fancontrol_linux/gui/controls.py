import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import config as cfgmod  # noqa: E402
from . import common as ui  # noqa: E402
from .util import fmt_pct, fmt_rpm  # noqa: E402

CONTROLS_HELP = (
    "Each card is a fan output (mainboard, AMD or NVIDIA graphics card).\n\n"
    "• Switch: let this program control the fan (off = BIOS/driver).\n"
    "• Curve: determines the speed. “Manual” sets a fixed value with a slider.\n"
    "• Arrow: fine tuning\n"
    "   – Step up/down: maximum change per second\n"
    "   – Start %: short boost when the fan starts from standstill\n"
    "   – Stop %: if the curve asks for less, the fan is switched off (0 = never)\n"
    "   – Minimum/Maximum %: hard limits, Offset: added to the curve\n"
    "   – Force: take control back immediately when another program interferes\n"
    "• “?” next to the percentage: another program or the BIOS is changing the fan.\n"
    "• ⋮ → Calibrate measures start/stop point and the speed curve (needed for RPM curves).\n"
    "• ⋮ → Identify spins the fan at 100 % for 10 s so you can find it in the case."
)
NO_FANS_HINT = (
    "No controllable fans were found.\n\n"
    "• Run <tt>sudo sensors-detect</tt> (package lm-sensors) and load the suggested "
    "kernel modules, e.g. <tt>nct6775</tt> or <tt>it87</tt> – the installer does this for you.\n"
    "• Some mainboards need the kernel parameter <tt>acpi_enforce_resources=lax</tt>.\n"
    "• ThinkPads: <tt>options thinkpad_acpi fan_control=1</tt>.\n"
    "• Dell: module <tt>dell-smm-hwmon</tt>.\n"
    "• AMD graphics cards (<tt>amdgpu</tt>) and NVIDIA cards (proprietary driver) are detected automatically."
)
TUNING_KEYS = ("min_percent", "max_percent", "start_percent", "stop_percent", "offset", "step_up",
               "step_down", "avoid", "force_apply")


def calibration_text(ctl):
    cal = (ctl or {}).get("calibration")
    if not cal:
        return "Not calibrated"
    rpms = [r for _p, r in cal["rpm_curve"] if r > 0]
    if not rpms:
        return "Calibrated (no speed measured)"
    return f"Calibrated: {min(rpms)}–{max(rpms)} RPM"


class ControlsPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.live = {}
        self.expanded = set()

    def rebuild(self):
        win = self.win
        self.live = {}
        if win.status is None or win.config is None:
            self.set_child(ui.status_page("Connecting to the service …", "", "network-transmit-receive-symbolic"))
            return
        pwms = win.status["pwms"]
        if not pwms:
            self.set_child(ui.status_page("No controllable fans found", NO_FANS_HINT,
                                          "dialog-warning-symbolic", ("Search again", win.rescan)))
            return
        page, flow = ui.grid_page("Controls", CONTROLS_HELP)
        for pid, info in pwms.items():
            if win.is_visible_item(pid):
                flow.append(self._card(pid, info))
        for ctl in [c for c in win.config["controls"] if c["id"] not in pwms]:
            flow.append(self._missing_card(ctl))
        self.set_child(page)
        self.update_live()

    def _missing_card(self, ctl):
        c = ui.card()
        top = Gtk.Box(spacing=8)
        top.append(Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=24))
        top.append(Gtk.Label(label=ctl["name"] or ctl["id"], hexpand=True, xalign=0, ellipsize=3,
                             css_classes=["heading"]))
        c.append(top)
        c.append(ui.caption("Hardware not found"))
        remove = Gtk.Button(label="Remove from configuration", css_classes=["flat"])
        remove.connect("clicked", lambda *_: self._remove(ctl["id"]))
        c.append(remove)
        return c

    def _remove(self, cid):
        self.win.config["controls"] = [c for c in self.win.config["controls"] if c["id"] != cid]
        self.win.config_changed(rebuild={"controls"})

    def _card(self, pid, info):
        win = self.win
        ctl = win.find_control(pid) or dict(copy.deepcopy(cfgmod.DEFAULT_CONTROL), id=pid)

        def setter(key, rebuild=None):
            def apply(value):
                win.ensure_control(pid)[key] = value
                win.config_changed(rebuild=rebuild)
            return apply

        def reset():
            c = win.ensure_control(pid)
            for key in TUNING_KEYS:
                c[key] = copy.deepcopy(cfgmod.DEFAULT_CONTROL[key])
            win.config_changed(rebuild={"controls"})

        def clear_calibration():
            win.ensure_control(pid)["calibration"] = None
            win.config_changed(rebuild={"controls"})

        c = ui.card(hidden=pid in win.config["hidden"])
        menu = [("Calibrate …", lambda: win.calibration_clicked(pid)),
                ("Identify (100 % for 10 s)", lambda: win.identify(pid)),
                ui.hidden_menu_item(win, pid),
                ("Reset fine tuning", reset)]
        if ctl.get("calibration"):
            menu.append(("Delete calibration", clear_calibration))
        ui.card_header(c, "fc-gauge-symbolic",
                       ui.name_entry(win.display_name(pid), info["label"],
                                     setter("name", rebuild={"sensors", "curves"})), menu)

        row = Gtk.Box(spacing=12)
        switch = Gtk.Switch(active=ctl["enabled"], valign=Gtk.Align.END,
                            tooltip_text="Control this fan (off = BIOS/driver)")
        switch.connect("notify::active", lambda s, _p: setter("enabled")(s.get_active()))
        row.append(switch)
        curves = win.config["curves"]
        choices = [("none", None), ("manual", None)] + [("curve", cv["id"]) for cv in curves]
        labels = ["No curve", "Manual"] + [cv["name"] for cv in curves]
        if ctl["mode"] == "manual":
            selected = 1
        else:
            selected = next((i for i, ch in enumerate(choices) if ch == ("curve", ctl["curve"])), 0)

        def choose(i):
            kind, cid = choices[i]
            target = win.ensure_control(pid)
            target["mode"] = "manual" if kind == "manual" else "curve"
            if kind != "manual":
                target["curve"] = cid
            win.config_changed(rebuild={"controls", "curves"})
        row.append(ui.labeled("Curve", ui.dropdown(labels, selected, choose)))
        c.append(row)

        if ctl["mode"] == "manual":
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            scale.set_value(ctl["manual_percent"])
            scale.set_draw_value(True)
            scale.set_format_value_func(lambda _s, v: f"{v:.0f} %")
            scale.connect("value-changed", lambda s: setter("manual_percent")(round(s.get_value())))
            c.append(ui.labeled("Manual speed", scale))

        values = Gtk.Box(spacing=8)
        pct = Gtk.Label(css_classes=["fc-big", "numeric"], xalign=0, hexpand=True)
        rpm = Gtk.Label(css_classes=["fc-big", "numeric"], xalign=1, tooltip_text=calibration_text(ctl))
        toggle = Gtk.ToggleButton(icon_name="pan-down-symbolic", active=pid in self.expanded,
                                  css_classes=["flat", "circular"], valign=Gtk.Align.CENTER,
                                  tooltip_text="Fine tuning")
        values.append(pct)
        values.append(rpm)
        values.append(toggle)
        c.append(values)
        state = ui.caption("")
        c.append(state)
        progress = Gtk.ProgressBar(visible=False)
        c.append(progress)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True)
        fields = [
            ("Step up %/s", "step_up", 0, 100, 0),
            ("Step down %/s", "step_down", 0, 100, 0),
            ("Start %", "start_percent", 0, 100, 0),
            ("Stop %", "stop_percent", 0, 100, 0),
            ("Offset %", "offset", -100, 100, 1),
            ("Minimum %", "min_percent", 0, 100, 0),
            ("Maximum %", "max_percent", 0, 100, 0),
        ]
        for i, (text, key, lo, hi, digits) in enumerate(fields):
            step = 0.5 if digits else 1
            grid.attach(ui.labeled(text, ui.spin(ctl[key], lo, hi, step, setter(key), digits)), i % 2, i // 2, 1, 1)
        rows = len(fields) // 2 + 1
        grid.attach(ui.switch_line("Force", ctl["force_apply"], setter("force_apply"),
                                   "Take control back immediately when another program interferes"),
                    0, rows, 2, 1)

        fans = win.status["fans"]
        fan_ids = [None] + list(fans)
        current_fan = ctl["fan"] or info.get("default_fan")
        grid.attach(ui.labeled("Speed sensor", ui.dropdown(
            ["None"] + [win.display_name(f) for f in fans],
            fan_ids.index(current_fan) if current_fan in fan_ids else 0,
            lambda i: setter("fan")(fan_ids[i]))), 0, rows + 1, 2, 1)

        avoid = Gtk.Entry(text=", ".join(f"{a:g}-{b:g}" for a, b in ctl["avoid"]),
                          placeholder_text="e.g. 40-50, 70-75",
                          tooltip_text="Ranges the fan should never run in (e.g. because it hums)")

        def set_avoid(*_):
            try:
                ranges = []
                for part in filter(None, (p.strip() for p in avoid.get_text().split(","))):
                    a, b = (float(x) for x in part.split("-"))
                    ranges.append([a, b])
            except ValueError:
                win.toast("Avoid ranges: format 40-50, 70-75")
                return
            if ranges != win.ensure_control(pid)["avoid"]:
                setter("avoid")(ranges)
        avoid.connect("activate", set_avoid)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", set_avoid)
        avoid.add_controller(focus)
        grid.attach(ui.labeled("Avoid ranges (%)", avoid), 0, rows + 2, 2, 1)
        grid.attach(ui.caption(calibration_text(ctl)), 0, rows + 3, 2, 1)

        revealer = Gtk.Revealer(child=grid, reveal_child=pid in self.expanded)

        def on_toggle(button):
            revealer.set_reveal_child(button.get_active())
            button.set_icon_name("pan-up-symbolic" if button.get_active() else "pan-down-symbolic")
            (self.expanded.add if button.get_active() else self.expanded.discard)(pid)
        toggle.connect("toggled", on_toggle)
        on_toggle(toggle)
        c.append(revealer)

        self.live[pid] = (pct, rpm, state, progress)
        return c

    def update_live(self):
        st = self.win.status
        if not st:
            return
        cal = st.get("calibration")
        for pid, (pct, rpm, state, progress) in self.live.items():
            info = st["pwms"].get(pid)
            if not info:
                continue
            ctl = self.win.find_control(pid) or {}
            fan = ctl.get("fan") or info.get("default_fan")
            text = fmt_pct(info["percent"])
            if info.get("overridden"):
                text += " ?"
            pct.set_label(text)
            pct.set_tooltip_text("Another program or the BIOS is changing this fan"
                                 if info.get("overridden") else None)
            rpm.set_label(fmt_rpm(st["fans"].get(fan, {}).get("value")) if fan else "")
            if info.get("error"):
                message = f"Error: {info['error']}"
            elif info.get("identifying"):
                message = "Identify: 100 %"
            elif st.get("safety_active") and info["controlled"]:
                message = "Safety temperature reached – 100 %"
            elif info["controlled"]:
                message = f"Controlled · target {fmt_pct(info['target'])}"
                if info.get("boost"):
                    message += f" · start assist +{info['boost']:.0f} %"
            else:
                message = "Automatic (BIOS/driver)"
            running = bool(cal and cal["running"] and cal["control"] == pid)
            progress.set_visible(running)
            if running:
                progress.set_fraction(cal["progress"])
                message = f"Calibrating … PWM {cal['percent']} %  (⋮ → Calibrate cancels)"
            state.set_label(message)
