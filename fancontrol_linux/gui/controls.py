"""Fans section: one card per fan output with live values, the curve chart and fine tuning."""

import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import config as cfgmod  # noqa: E402
from . import common as ui  # noqa: E402
from . import paint  # noqa: E402
from .graph_editor import GraphEditor, preview_points  # noqa: E402
from .util import fmt_pct, fmt_temp  # noqa: E402

CONTROLS_HELP = (
    "Each card is a fan output (mainboard, AMD or NVIDIA graphics card).\n\n"
    "• Switch: let LiFaCo control the fan (off = BIOS/driver).\n"
    "• The chart shows the assigned curve; the dot is the current operating point.\n"
    "• Show details: curve, current temperature, target, minimum and maximum speed, plus more settings\n"
    "   – Step up/down: maximum change per second\n"
    "   – Start %: short boost when the fan starts from standstill\n"
    "   – Stop %: if the curve asks for less, the fan is switched off (0 = never)\n"
    "   – Offset: added to the curve; Force: take control back when another program interferes\n"
    "• “?” next to the percentage: another program or the BIOS is changing the fan.\n"
    "• ⋮ → Calibrate measures start/stop point and the speed curve (needed for RPM curves).\n"
    "• ⋮ → Identify spins the fan at 100 % for 10 s so you can find it in the case."
)
NO_FANS_HINT = (
    "No controllable fans were found.\n"
    "• Run <tt>sudo sensors-detect</tt> (package lm-sensors) and load the suggested kernel modules, "
    "e.g. <tt>nct6775</tt> or <tt>it87</tt> – the installer does this for you.\n"
    "• Some mainboards need the kernel parameter <tt>acpi_enforce_resources=lax</tt>.\n"
    "• ThinkPads: <tt>options thinkpad_acpi fan_control=1</tt>. Dell: module <tt>dell-smm-hwmon</tt>.\n"
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


def _detail_row(icon, text, widget):
    row = Gtk.Box(spacing=10, css_classes=["fc-detail-row"])
    row.append(Gtk.Image(icon_name=icon, css_classes=["fc-dim-icon"]))
    row.append(Gtk.Label(label=text, xalign=0, hexpand=True, css_classes=["fc-detail-label"]))
    row.append(widget)
    return row


def _value_label():
    return Gtk.Label(xalign=1, css_classes=["numeric", "fc-detail-value"])


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
            self.set_child(ui.notice("Connecting to the service …", "network-transmit-receive-symbolic"))
            return
        pwms = win.status["pwms"]
        if not pwms:
            self.set_child(ui.notice("No controllable fans found", "dialog-warning-symbolic", NO_FANS_HINT,
                                     ("Search again", win.rescan)))
            return
        flow = ui.CardFlow()
        visible = [(pid, info) for pid, info in pwms.items() if win.is_visible_item(pid)]
        for index, (pid, info) in enumerate(visible):
            flow.append(self._card(pid, info, index))
        for ctl in [c for c in win.config["controls"] if c["id"] not in pwms]:
            flow.append(self._missing_card(ctl))
        self.set_child(flow)
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

    def _card(self, pid, info, index):
        win = self.win
        ctl = win.find_control(pid) or dict(copy.deepcopy(cfgmod.DEFAULT_CONTROL), id=pid)
        palette = paint.item(index)

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

        device, _sep, channel = info["label"].rpartition(": ")
        own_name = win.display_name(pid)
        custom = own_name != info["label"]
        title = own_name if custom else (channel or info["label"])

        def rename():
            win.prompt_text("Rename fan", title, "Rename",
                            lambda text: setter("name", rebuild={"controls", "sensors", "curves"})(text))

        c = ui.card(hidden=pid in win.config["hidden"], color_index=index)
        menu = [("Rename …", rename),
                ("Calibrate …", lambda: win.calibration_clicked(pid)),
                ("Identify (100 % for 10 s)", lambda: win.identify(pid)),
                ui.hidden_menu_item(win, pid),
                ("Reset fine tuning", reset)]
        if ctl.get("calibration"):
            menu.append(("Delete calibration", clear_calibration))

        top = Gtk.Box(spacing=10)
        top.append(ui.icon_bubble("fc-fan-symbolic"))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label=title, xalign=0, ellipsize=3, css_classes=["fc-card-title"]))
        subtitle = channel if custom and channel else device
        names.append(Gtk.Label(label=subtitle or "", xalign=0, ellipsize=3, css_classes=["fc-caption"],
                               tooltip_text=info["label"]))
        top.append(names)
        switch = Gtk.Switch(active=ctl["enabled"], valign=Gtk.Align.CENTER,
                            tooltip_text="Control this fan (off = BIOS/driver)")
        switch.connect("notify::active", lambda s, _p: setter("enabled")(s.get_active()))
        top.append(switch)
        top.append(ui.card_menu(c, menu))
        c.append(top)

        values = Gtk.Box(spacing=4)
        big = Gtk.Label(css_classes=["fc-huge", "numeric"], xalign=0, tooltip_text=calibration_text(ctl))
        unit = Gtk.Label(css_classes=["fc-unit"], xalign=0, valign=Gtk.Align.END, margin_bottom=4)
        pct = Gtk.Label(css_classes=["fc-percent", "numeric"], xalign=1, hexpand=True, valign=Gtk.Align.END,
                        margin_bottom=4)
        values.append(big)
        values.append(unit)
        values.append(pct)
        c.append(values)

        curve = win.find_curve(ctl["curve"]) if ctl["mode"] == "curve" else None
        if ctl["mode"] == "manual":
            preview = ([[20, ctl["manual_percent"]], [90, ctl["manual_percent"]]], (20, 90), False)
        else:
            preview = preview_points(curve) if curve else None
        chart = None
        if preview:
            points, temp_range, rpm = preview
            chart = GraphEditor(points, editable=False, mini_axes=True, temp_range=temp_range, rpm=rpm,
                                palette=palette)
            c.append(chart)
        else:
            text = ("No curve assigned – choose one under “Show details”" if not curve else
                    f"{curve['name']}: {'mix of curves' if curve['type'] == 'mix' else 'follows another fan'}")
            c.append(Gtk.Label(label=text, wrap=True, xalign=0, max_width_chars=30,
                               css_classes=["fc-caption", "fc-chart-placeholder"]))
        state = ui.caption("")
        c.append(state)
        progress = Gtk.ProgressBar(visible=False)
        c.append(progress)

        # --- details -------------------------------------------------------
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["fc-details"])
        curves = win.config["curves"]
        choices = [("none", None), ("manual", None)] + [("curve", cv["id"]) for cv in curves]
        labels = ["No curve", "Manual"] + [cv["name"] for cv in curves]
        selected = 1 if ctl["mode"] == "manual" else next(
            (i for i, ch in enumerate(choices) if ch == ("curve", ctl["curve"])), 0)

        def choose(i):
            kind, cid = choices[i]
            target = win.ensure_control(pid)
            target["mode"] = "manual" if kind == "manual" else "curve"
            if kind != "manual":
                target["curve"] = cid
            win.config_changed(rebuild={"controls", "curves"})
        curve_dd = ui.dropdown(labels, selected, choose)
        curve_dd.set_hexpand(False)
        details.append(_detail_row("fc-curve-symbolic", "Curve", curve_dd))
        if ctl["mode"] == "manual":
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            scale.set_value(ctl["manual_percent"])
            scale.set_draw_value(True)
            scale.set_format_value_func(lambda _s, v: f"{v:.0f} %")
            scale.connect("value-changed", lambda s: setter("manual_percent", {"controls"})(round(s.get_value())))
            details.append(ui.labeled("Manual speed", scale))
        temp_value, target_value = _value_label(), _value_label()
        details.append(_detail_row("fc-thermometer-symbolic", "Current temperature", temp_value))
        details.append(_detail_row("fc-gauge-symbolic", "Target speed", target_value))
        min_spin = ui.spin(ctl["min_percent"], 0, 100, 1, setter("min_percent"))
        max_spin = ui.spin(ctl["max_percent"], 0, 100, 1, setter("max_percent"))
        for s in (min_spin, max_spin):
            s.set_hexpand(False)
        details.append(_detail_row("fc-fan-symbolic", "Min. speed %", min_spin))
        details.append(_detail_row("fc-fan-symbolic", "Max. speed %", max_spin))

        grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True, margin_top=6)
        fields = [
            ("Step up %/s", "step_up", 0, 100, 0),
            ("Step down %/s", "step_down", 0, 100, 0),
            ("Start %", "start_percent", 0, 100, 0),
            ("Stop %", "stop_percent", 0, 100, 0),
            ("Offset %", "offset", -100, 100, 1),
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
        more = Gtk.Expander(label="More settings", child=grid, css_classes=["fc-more"])
        details.append(more)

        revealer = Gtk.Revealer(child=details, reveal_child=pid in self.expanded)
        c.append(revealer)
        toggle_content = Adw.ButtonContent(icon_name="pan-down-symbolic", label="Show details")
        toggle = Gtk.ToggleButton(child=toggle_content, active=pid in self.expanded, halign=Gtk.Align.START,
                                  css_classes=["flat", "fc-details-toggle"])

        def on_toggle(button):
            open_ = button.get_active()
            revealer.set_reveal_child(open_)
            toggle_content.set_icon_name("pan-up-symbolic" if open_ else "pan-down-symbolic")
            toggle_content.set_label("Hide details" if open_ else "Show details")
            (self.expanded.add if open_ else self.expanded.discard)(pid)
        toggle.connect("toggled", on_toggle)
        on_toggle(toggle)
        c.append(toggle)

        self.live[pid] = (big, unit, pct, chart, state, progress, temp_value, target_value)
        return c

    def update_live(self):
        st = self.win.status
        if not st:
            return
        cal = st.get("calibration")
        for pid, (big, unit, pct, chart, state, progress, temp_value, target_value) in self.live.items():
            info = st["pwms"].get(pid)
            if not info:
                continue
            ctl = self.win.find_control(pid) or {}
            fan = ctl.get("fan") or info.get("default_fan")
            rpm = st["fans"].get(fan, {}).get("value") if fan else None
            if fan:
                big.set_label("–" if rpm is None else str(rpm))
                unit.set_label("RPM")
                text = fmt_pct(info["percent"]).replace(" ", "")
            else:
                big.set_label(fmt_pct(info["percent"]).replace(" %", ""))
                unit.set_label("%")
                text = ""
            if info.get("overridden"):
                text += " ?"
            pct.set_label(text)
            pct.set_tooltip_text("Another program or the BIOS is changing this fan"
                                 if info.get("overridden") else None)
            curve_info = (st["curves"].get(ctl.get("curve")) or {}) if ctl.get("mode", "curve") == "curve" else {}
            temp = curve_info.get("temp")
            temp_value.set_label(fmt_temp(temp) if temp is not None else "–")
            target_value.set_label(fmt_pct(info["target"]))
            if chart is not None:
                if ctl.get("mode") == "manual":
                    chart.set_live(None, None)
                else:
                    chart.set_live(temp, curve_info.get("output"))
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
