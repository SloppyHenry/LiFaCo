import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .. import config as cfgmod  # noqa: E402
from ..curves import TEMP_CURVES  # noqa: E402
from . import common as ui  # noqa: E402
from . import paint  # noqa: E402
from .graph_editor import GraphEditor, preview_points  # noqa: E402
from .util import (combo_row, delta_to_disp, entry_row, fmt_pct, fmt_temp, spin_row, switch_row,  # noqa: E402
                   temp_spin_row, temp_unit, value_label)

TYPE_NAMES = {
    "graph": "Graph",
    "linear": "Linear",
    "flat": "Flat",
    "mix": "Mix",
    "trigger": "Trigger (idle/load)",
    "sync": "Sync (follows a control)",
    "auto": "Auto (target temperature)",
}
TYPE_ICONS = {
    "graph": "fc-curve-graph-symbolic",
    "linear": "fc-curve-linear-symbolic",
    "flat": "fc-curve-flat-symbolic",
    "mix": "fc-curve-mix-symbolic",
    "trigger": "fc-curve-trigger-symbolic",
    "sync": "fc-curve-sync-symbolic",
    "auto": "fc-curve-auto-symbolic",
}
TYPE_HELP = {
    "graph": "Freely defined curve: each point sets the speed for a temperature. "
             "In between the speed is interpolated linearly; outside, the first or last point applies.",
    "linear": "Straight line: below the min temperature the min speed applies, above the max temperature "
              "the max speed, linear in between.",
    "flat": "Always returns the same value, regardless of temperatures.",
    "mix": "Combines several curves: maximum, minimum, average, sum or subtraction "
           "(first curve minus all others).",
    "trigger": "Two states: idle and load. When the temperature reaches the load temperature, the fan jumps "
               "to the load speed and stays there until the temperature drops below the idle temperature again. "
               "Response time up/down: how long the state must persist.",
    "sync": "Takes the percentage of another control – plus an offset in percentage points, or "
            "proportionally (e.g. +20 % → 50 % becomes 60 %).",
    "auto": "Regulates on its own: above the load temperature (plus deadband) the speed rises by "
            "“Step %/s”, below it falls again – finding the lowest speed that holds the target temperature. "
            "Below the idle temperature the min speed applies.",
}
HYST_HELP = ("Hysteresis: the curve only reacts once the temperature has changed by at least this amount "
             "(separately for rising ↑ and falling ↓). Response time: how long the change must persist.")
SENSOR_MIX = [("max", "Maximum"), ("min", "Minimum"), ("avg", "Average")]
CURVE_MIX = [("max", "Maximum"), ("min", "Minimum"), ("avg", "Average"), ("sum", "Sum"), ("sub", "Subtract")]
CURVE_TILE_WIDTH = 250
CURVES_HELP = (
    "Curves calculate the fan speed in percent – or, in RPM mode, a speed that calibrated fans then "
    "target. A curve can be assigned to several fans.\n\n"
    "Use + to add curves. Clicking a card's icon explains the curve type, "
    "“Edit” opens the large editor with all options."
)


def _speed_fmt(curve, value):
    if value is None:
        return "–"
    return f"{value:.0f} RPM" if curve.get("unit") == "rpm" else fmt_pct(value)


def _speed_top(curve):
    return cfgmod.MAX_RPM if curve.get("unit") == "rpm" else 100


def _speed_step(curve):
    return 50 if curve.get("unit") == "rpm" else 1


def _speed_caption(curve, text):
    return f"{text} {'RPM' if curve.get('unit') == 'rpm' else '%'}"


def hysteresis_summary(curve):
    unit = temp_unit()
    return (f"↑ {delta_to_disp(curve['hysteresis_up']):g} {unit} · {curve['response_up']:g} s   "
            f"↓ {delta_to_disp(curve['hysteresis_down']):g} {unit} · {curve['response_down']:g} s")


class CurvesPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.live = {}
        self.dialog = None

    def rebuild(self):
        win = self.win
        self.live = {}
        if win.config is None:
            self.set_child(ui.notice("Connecting to the service …", "network-transmit-receive-symbolic"))
            return
        flow = ui.CardFlow(width=CURVE_TILE_WIDTH)
        visible = [c for c in win.config["curves"] if win.is_visible_item(c["id"])]
        for index, curve in enumerate(visible):
            flow.append(self._tile(curve, index))
        flow.append(self._new_tile())
        self.set_child(flow)
        self.update_live()

    def add(self, kind):
        win = self.win
        if win.config is None:
            return
        n = sum(1 for c in win.config["curves"] if c["type"] == kind) + 1
        curve = cfgmod.new_curve(kind, f"{TYPE_NAMES[kind].split(' (')[0]} {n}", win.default_temp_sensors())
        if kind == "sync" and win.status and win.status["pwms"]:
            curve["control"] = next(iter(win.status["pwms"]))
        win.config["curves"].append(curve)
        win.config_changed(rebuild={"curves", "controls"})

    def _duplicate(self, curve):
        dup = copy.deepcopy(curve)
        dup["id"] = cfgmod.new_id()
        dup["name"] = f"{curve['name']} (copy)"
        self.win.config["curves"].append(dup)
        self.win.config_changed(rebuild={"curves", "controls"})

    def delete(self, curve, after=None):
        win = self.win
        users = [win.display_name(c["id"]) for c in win.config["controls"]
                 if c["curve"] == curve["id"] and c["mode"] == "curve"]
        users += [c["name"] for c in win.config["curves"] if curve["id"] in c.get("curves", [])]
        body = "The curve will be removed."
        if users:
            body += "\n\nUsed by: " + ", ".join(users) + ".\nAffected fans return to automatic control."

        def confirmed():
            cid = curve["id"]
            win.config["curves"] = [c for c in win.config["curves"] if c["id"] != cid]
            for c in win.config["curves"]:
                if cid in c.get("curves", []):
                    c["curves"].remove(cid)
            for ctl in win.config["controls"]:
                if ctl["curve"] == cid:
                    ctl["curve"] = None
            if after:
                after()
            win.config_changed(rebuild={"curves", "controls"})
        win.confirm(f"Delete “{curve['name']}”?", body, "Delete", confirmed, destructive=True)

    def open_editor(self, curve):
        self.dialog = CurveEditorDialog(self.win, curve, self)
        self.dialog.present(self.win)

    def show_help(self, curve):
        text = TYPE_HELP[curve["type"]]
        if curve["type"] in ("graph", "linear"):
            text += "\n\n" + HYST_HELP
        dialog = Adw.AlertDialog(heading=TYPE_NAMES[curve["type"]], body=text)
        dialog.add_response("ok", "OK")
        dialog.present(self.win)

    # --- tiles --------------------------------------------------------
    def _users(self, curve):
        return [self.win.display_name(x["id"]).rpartition(": ")[2] for x in self.win.config["controls"]
                if x["curve"] == curve["id"] and x["mode"] == "curve"]

    def _tile(self, curve, index):
        win = self.win
        palette = paint.item(index)
        tile = ui.card(hidden=curve["id"] in win.config["hidden"], color_index=index)
        tile.add_css_class("fc-tile")
        row = Gtk.Box(spacing=12)
        preview = preview_points(curve)
        if preview:
            points, temp_range, rpm = preview
            icon = GraphEditor(points, editable=False, compact=True, temp_range=temp_range, rpm=rpm,
                               palette=palette)
            icon.set_size_request(48, 34)
            icon.set_hexpand(False)
            icon.set_valign(Gtk.Align.CENTER)
            row.append(icon)
        else:
            row.append(ui.icon_bubble(TYPE_ICONS[curve["type"]]))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label=curve["name"], xalign=0, ellipsize=3, css_classes=["fc-card-title"]))
        users = self._users(curve)
        sub = Gtk.Label(label=", ".join(users) if users else TYPE_NAMES[curve["type"]], xalign=0, ellipsize=3,
                        css_classes=["fc-caption"],
                        tooltip_text=TYPE_NAMES[curve["type"]] + (" · used by " + ", ".join(users) if users else ""))
        names.append(sub)
        row.append(names)
        value = Gtk.Label(css_classes=["fc-caption", "numeric"], valign=Gtk.Align.CENTER)
        row.append(value)
        row.append(ui.card_menu(tile, [("Edit …", lambda: self.open_editor(curve)),
                                       ("Duplicate", lambda: self._duplicate(curve)),
                                       ("Help", lambda: self.show_help(curve)),
                                       ui.hidden_menu_item(win, curve["id"]),
                                       ("Delete", lambda: self.delete(curve))]))
        row.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["fc-dim-icon"]))
        tile.append(row)
        def clicked(gesture, _n, x, y):
            target = gesture.get_widget().pick(x, y, Gtk.PickFlags.DEFAULT)
            # Clicks on the ⋮ menu belong to the menu, not to the tile.
            if target is None or target.get_ancestor(Gtk.MenuButton) is None:
                self.open_editor(curve)
        click = Gtk.GestureClick()
        click.connect("released", clicked)
        tile.add_controller(click)
        tile.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        tile.set_tooltip_text(f"{TYPE_NAMES[curve['type']]} – click to edit")
        self.live[curve["id"]] = (value, curve)
        return tile

    def _new_tile(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover = Gtk.Popover(child=box)
        for kind, name in TYPE_NAMES.items():
            b = Gtk.Button(child=Adw.ButtonContent(icon_name=TYPE_ICONS[kind], label=name, halign=Gtk.Align.START),
                           css_classes=["flat"])
            b.connect("clicked", lambda _b, k=kind: (popover.popdown(), self.add(k)))
            box.append(b)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER)
        content.append(Gtk.Image(icon_name="list-add-symbolic", pixel_size=18))
        content.append(Gtk.Label(label="Create new curve", css_classes=["fc-caption"]))
        return Gtk.MenuButton(child=content, popover=popover, css_classes=["flat", "fc-new-tile"],
                              tooltip_text="Add a curve")

    def update_live(self):
        st = self.win.status
        if not st:
            return
        for cid, (value, curve) in self.live.items():
            info = st["curves"].get(cid) or {}
            value.set_label("100 %" if info.get("failsafe") else _speed_fmt(curve, info.get("output")))
        if self.dialog:
            self.dialog.update_live()


class CurveEditorDialog(Adw.Dialog):
    """Full editor for one curve. Changes apply live; “Cancel” restores the state from before opening."""

    def __init__(self, win, curve, page):
        super().__init__(title=curve["name"], content_width=780, content_height=780)
        self.win, self.curve, self.page = win, curve, page
        self.snapshot = copy.deepcopy(curve)
        self.graph = None
        self.points_list = None
        self.connect("closed", self._on_closed)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._cancel())
        done = Gtk.Button(label="Done", css_classes=["suggested-action"])
        done.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        header.pack_end(done)
        self.view = Adw.ToolbarView()
        self.view.add_top_bar(header)
        self.view.set_content(self._build())
        self.set_child(self.view)

    def _cancel(self):
        self.curve.clear()
        self.curve.update(self.snapshot)
        self.win.config_changed()
        self.close()

    def _on_closed(self, *_):
        self.page.dialog = None
        self.win.rebuild({"curves", "controls"})

    def _rebuild(self):
        self.view.set_content(self._build())

    def _build(self):
        win, curve = self.win, self.curve
        changed = win.config_changed
        page = Adw.PreferencesPage()
        kind = curve["type"]
        rpm = curve.get("unit") == "rpm"
        top, step = _speed_top(curve), _speed_step(curve)

        def setter(key):
            def apply(value):
                curve[key] = value
                changed()
            return apply

        general = Adw.PreferencesGroup(title=TYPE_NAMES[kind], description=TYPE_HELP[kind])
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete curve",
                            valign=Gtk.Align.CENTER, css_classes=["flat", "destructive-action"])
        delete.connect("clicked", lambda *_: self.page.delete(curve, after=self.close))
        general.set_header_suffix(delete)

        def rename(text):
            if text:
                curve["name"] = text
                self.set_title(text)
                changed(rebuild={"controls"})
        general.add(entry_row("Name", curve["name"], rename))
        if kind != "sync":
            def set_unit(i):
                new = ("percent", "rpm")[i]
                if new == curve.get("unit", "percent"):
                    return
                factor = 20.0 if new == "rpm" else 1 / 20.0
                curve["unit"] = new
                for key in ("speed_min", "speed_max", "value", "idle_speed", "load_speed", "min_speed", "max_speed", "step"):
                    if key in curve:
                        curve[key] = round(min(cfgmod.MAX_RPM if new == "rpm" else 100, curve[key] * factor), 1)
                if "points" in curve:
                    for p in curve["points"]:
                        p[1] = round(min(cfgmod.MAX_RPM if new == "rpm" else 100, p[1] * factor), 1)
                changed()
                GLib.idle_add(lambda: (self._rebuild(), False)[1])
            general.add(combo_row("Unit", ["Percent", "Speed (RPM)"], 1 if rpm else 0, set_unit,
                                  subtitle="RPM curves need calibrated fans"))
        live_row = Adw.ActionRow(title="Current")
        self.live_label = value_label()
        live_row.add_suffix(self.live_label)
        general.add(live_row)
        page.add(general)

        if kind == "graph":
            group = Adw.PreferencesGroup(title="Curve points",
                                         description="Drag points · double-click adds a point · right-click removes it")
            self.graph = GraphEditor(curve["points"], self._graph_changed, temp_range=curve["temp_axis"], rpm=rpm)
            group.add(Gtk.Frame(child=self.graph, css_classes=["card"]))
            page.add(group)
            page.add(self._points_group())
            axis = Adw.PreferencesGroup(title="Temperature range of the axis")

            def set_axis(index):
                def apply(value):
                    lo, hi = curve["temp_axis"]
                    new = [value, hi] if index == 0 else [lo, value]
                    if new[1] - new[0] >= 10:
                        curve["temp_axis"] = new
                        self.graph.set_range(new)
                        changed()
                return apply
            axis.add(temp_spin_row("From", curve["temp_axis"][0], -50, 140, set_axis(0)))
            axis.add(temp_spin_row("To", curve["temp_axis"][1], -40, 150, set_axis(1)))
            page.add(axis)
        elif kind == "linear":
            preview = [[curve["temp_min"], curve["speed_min"]], [curve["temp_max"], curve["speed_max"]]]
            group = Adw.PreferencesGroup(title="Values")
            self.graph = GraphEditor(preview, editable=False, rpm=rpm)

            def lin(key, idx):
                def apply(value):
                    curve[key] = value
                    preview[idx[0]][idx[1]] = value
                    self.graph.refresh()
                    changed()
                return apply
            group.add(temp_spin_row("Temperature min", curve["temp_min"], -20, 150, lin("temp_min", (0, 0))))
            group.add(spin_row(_speed_caption(curve, "Speed at min"), curve["speed_min"], 0, top, step, lin("speed_min", (0, 1))))
            group.add(temp_spin_row("Temperature max", curve["temp_max"], -20, 150, lin("temp_max", (1, 0))))
            group.add(spin_row(_speed_caption(curve, "Speed at max"), curve["speed_max"], 0, top, step, lin("speed_max", (1, 1))))
            group.add(Gtk.Frame(child=self.graph, css_classes=["card"], margin_top=12))
            page.add(group)
        elif kind == "flat":
            group = Adw.PreferencesGroup(title="Value")
            group.add(spin_row(_speed_caption(curve, "Speed"), curve["value"], 0, top, step, setter("value")))
            page.add(group)
        elif kind == "mix":
            page.add(self._mix_group())
        elif kind == "sync":
            group = Adw.PreferencesGroup(title="Source")
            pwms = list((win.status or {}).get("pwms", {}))
            if curve["control"] and curve["control"] not in pwms:
                pwms.append(curve["control"])
            group.add(combo_row("Control", [win.display_name(p) for p in pwms] or ["–"],
                                pwms.index(curve["control"]) if curve["control"] in pwms else 0,
                                lambda i: pwms and setter("control")(pwms[i])))
            group.add(spin_row("Offset", curve["offset"], -100, 100, 0.5, setter("offset"), digits=1,
                               subtitle="Percentage points, or percent of the source value with “Proportional”"))
            group.add(switch_row("Proportional", curve["proportional"], setter("proportional")))
            page.add(group)
        elif kind == "trigger":
            group = Adw.PreferencesGroup(title="Thresholds")
            group.add(temp_spin_row("Idle temperature", curve["idle_temp"], -20, 150, setter("idle_temp"),
                                    subtitle="Below this: idle speed"))
            group.add(spin_row(_speed_caption(curve, "Idle speed"), curve["idle_speed"], 0, top, step, setter("idle_speed")))
            group.add(temp_spin_row("Load temperature", curve["load_temp"], -20, 150, setter("load_temp"),
                                    subtitle="From here: load speed"))
            group.add(spin_row(_speed_caption(curve, "Load speed"), curve["load_speed"], 0, top, step, setter("load_speed")))
            group.add(spin_row("Response time up s", curve["response_up"], 0, 300, 0.5, setter("response_up"),
                               digits=1, subtitle="How long the load temperature must be held"))
            group.add(spin_row("Response time down s", curve["response_down"], 0, 300, 0.5, setter("response_down"),
                               digits=1, subtitle="How long the temperature must stay below the idle temperature"))
            page.add(group)
        elif kind == "auto":
            group = Adw.PreferencesGroup(title="Regulation")
            group.add(temp_spin_row("Idle temperature", curve["idle_temp"], -20, 150, setter("idle_temp"),
                                    subtitle="Below this: min speed"))
            group.add(temp_spin_row("Load temperature (target)", curve["load_temp"], -20, 150, setter("load_temp"),
                                    subtitle="This temperature is held"))
            group.add(spin_row(_speed_caption(curve, "Min. speed"), curve["min_speed"], 0, top, step, setter("min_speed")))
            group.add(spin_row(_speed_caption(curve, "Max. speed"), curve["max_speed"], 0, top, step, setter("max_speed")))
            group.add(spin_row(_speed_caption(curve, "Step") + "/s", curve["step"], 0.1, 100 if not rpm else 2000, 0.5,
                               setter("step"), digits=1, subtitle="How fast the speed is adjusted"))
            group.add(temp_spin_row("Deadband", curve["deadband"], 0, 20, setter("deadband"), delta=True, digits=1,
                                    subtitle="Within ± deadband around the target the speed stays the same"))
            group.add(spin_row("Response time s", curve["response_time"], 0, 300, 0.5, setter("response_time"), digits=1))
            page.add(group)

        if kind in TEMP_CURVES:
            page.add(self._sensor_group())

        if kind in ("graph", "linear"):
            group = Adw.PreferencesGroup(title="Hysteresis", description=HYST_HELP)
            group.add(temp_spin_row("Hysteresis ↑", curve["hysteresis_up"], 0, 50, setter("hysteresis_up"),
                                    delta=True, digits=1, subtitle="For rising temperature"))
            group.add(spin_row("Response time ↑ s", curve["response_up"], 0, 300, 0.5, setter("response_up"), digits=1))
            group.add(temp_spin_row("Hysteresis ↓", curve["hysteresis_down"], 0, 50, setter("hysteresis_down"),
                                    delta=True, digits=1, subtitle="For falling temperature"))
            group.add(spin_row("Response time ↓ s", curve["response_down"], 0, 300, 0.5, setter("response_down"), digits=1))
            group.add(switch_row("Ignore hysteresis at the limits", curve["ignore_hysteresis_at_limits"],
                                 setter("ignore_hysteresis_at_limits"),
                                 subtitle="Minimum and maximum are reached immediately"))
            page.add(group)
        self.update_live()
        return page

    # --- graph points ---------------------------------------------------
    def _graph_changed(self):
        self.win.config_changed()
        self._fill_points()

    def _points_group(self):
        group = Adw.PreferencesGroup(title="Points as numbers")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add point",
                         css_classes=["flat"], valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self._add_point())
        group.set_header_suffix(add)
        self.points_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        # Keep the order consistent after typing a temperature past a neighbour.
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._resort())
        self.points_list.add_controller(focus)
        group.add(self.points_list)
        self._fill_points()
        return group

    def _add_point(self):
        pts = self.curve["points"]
        last = pts[-1] if pts else [40, 30]
        pts.append([min(150, last[0] + 5), last[1]])
        pts.sort()
        self.graph.refresh()
        self._graph_changed()

    def _fill_points(self):
        if self.points_list is None:
            return
        while (row := self.points_list.get_row_at_index(0)) is not None:
            self.points_list.remove(row)
        curve = self.curve
        top, step = _speed_top(curve), _speed_step(curve)
        for i, point in enumerate(curve["points"]):
            box = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6, margin_start=12, margin_end=6)
            box.append(Gtk.Label(label=f"{i + 1}.", width_chars=3, xalign=0))

            def set_temp(value, p=point):
                p[0] = value
                self.graph.refresh()
                self.win.config_changed()

            def set_speed(value, p=point):
                p[1] = value
                self.graph.refresh()
                self.win.config_changed()
            box.append(ui.temp_spin(point[0], -50, 150, set_temp))
            box.append(Gtk.Label(label=temp_unit()))
            box.append(ui.spin(point[1], 0, top, step, set_speed))
            box.append(Gtk.Label(label="RPM" if curve.get("unit") == "rpm" else "%"))
            remove = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Remove point",
                                sensitive=len(curve["points"]) > 2)
            remove.connect("clicked", lambda _b, p=point: self._remove_point(p))
            box.append(remove)
            self.points_list.append(box)

    def _resort(self):
        pts = self.curve["points"]
        if pts != sorted(pts):
            pts.sort()
            self.graph.refresh()
            self._fill_points()

    def _remove_point(self, point):
        if len(self.curve["points"]) > 2:
            self.curve["points"].remove(point)
            self.graph.refresh()
            self._graph_changed()

    # --- groups ---------------------------------------------------------
    def _sensor_group(self):
        win, curve = self.win, self.curve
        group = Adw.PreferencesGroup(title="Temperature source")
        temps = win.status["temps"] if win.status else {}
        expander = Adw.ExpanderRow(title="Sensors", expanded=not curve["sensors"])
        known = list(temps) + [s for s in curve["sensors"] if s not in temps]

        def summary():
            names = [win.display_name(s) for s in curve["sensors"]]
            expander.set_subtitle(", ".join(names) if names else "No sensor selected – fans run at 100 %")

        def toggle(sid, active):
            if active and sid not in curve["sensors"]:
                curve["sensors"].append(sid)
            elif not active and sid in curve["sensors"]:
                curve["sensors"].remove(sid)
            summary()
            win.config_changed()

        for sid in known:
            row = Adw.ActionRow(title=win.display_name(sid), subtitle=sid)
            check = Gtk.CheckButton(active=sid in curve["sensors"], valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda c, s=sid: toggle(s, c.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            value = temps.get(sid, {}).get("value") if sid in temps else None
            row.add_suffix(Gtk.Label(label=fmt_temp(value) if sid in temps else "not present",
                                     css_classes=["numeric", "dim-label"]))
            expander.add_row(row)
        summary()
        group.add(expander)
        keys = [k for k, _ in SENSOR_MIX]
        group.add(combo_row("With several sensors", [n for _, n in SENSOR_MIX],
                            keys.index(curve["sensor_mix"]) if curve["sensor_mix"] in keys else 0,
                            lambda i: (curve.__setitem__("sensor_mix", keys[i]), win.config_changed())))
        return group

    def _mix_group(self):
        win, curve = self.win, self.curve
        group = Adw.PreferencesGroup(title="Combine curves",
                                     description="With “Subtract” the order matters: first curve minus all others.")
        keys = [k for k, _ in CURVE_MIX]
        group.add(combo_row("Function", [n for _, n in CURVE_MIX],
                            keys.index(curve["function"]) if curve["function"] in keys else 0,
                            lambda i: (curve.__setitem__("function", keys[i]), win.config_changed())))

        def toggle(cid, active):
            if active and cid not in curve["curves"]:
                curve["curves"].append(cid)
            elif not active and cid in curve["curves"]:
                curve["curves"].remove(cid)
            win.config_changed()

        others = [c for c in win.config["curves"] if c["id"] != curve["id"]]
        if not others:
            group.add(Adw.ActionRow(title="Add more curves first."))
        for other in others:
            row = Adw.ActionRow(title=other["name"], subtitle=TYPE_NAMES[other["type"]])
            check = Gtk.CheckButton(active=other["id"] in curve["curves"], valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda c, i=other["id"]: toggle(i, c.get_active()))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            group.add(row)
        return group

    def update_live(self):
        st = self.win.status
        if not st:
            return
        info = st["curves"].get(self.curve["id"])
        if not info:
            self.live_label.set_label("not calculated yet")
            if self.graph:
                self.graph.set_live(None, None)
            return
        temp, out = info.get("temp"), info.get("output")
        out_text = "100 % (failsafe)" if info.get("failsafe") else _speed_fmt(self.curve, out)
        self.live_label.set_label(out_text if temp is None else f"{fmt_temp(temp)}  →  {out_text}")
        if self.graph:
            self.graph.set_live(temp, out)
