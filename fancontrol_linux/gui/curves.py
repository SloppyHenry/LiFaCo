import copy

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .. import config as cfgmod  # noqa: E402
from ..curves import TEMP_CURVES  # noqa: E402
from . import common as ui  # noqa: E402
from .graph_editor import GraphEditor  # noqa: E402
from .util import (combo_row, delta_to_disp, entry_row, fmt_pct, fmt_temp, spin_row, switch_row,  # noqa: E402
                   temp_spin_row, temp_unit, value_label)

TYPE_NAMES = {
    "graph": "Grafische Kurve",
    "linear": "Linear",
    "flat": "Fester Wert",
    "mix": "Mix aus Kurven",
    "trigger": "Auslöser (Leerlauf/Last)",
    "sync": "Sync (folgt Steuerung)",
    "auto": "Auto (Zieltemperatur)",
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
    "graph": "Frei definierbare Kurve: Jeder Punkt legt fest, welche Drehzahl bei welcher Temperatur gilt. "
             "Dazwischen wird linear gerechnet, außerhalb gilt der erste bzw. letzte Punkt.",
    "linear": "Gerade Linie: unterhalb der Min-Temperatur gilt die Min-Drehzahl, oberhalb der Max-Temperatur "
              "die Max-Drehzahl, dazwischen linear.",
    "flat": "Liefert immer den gleichen Wert, unabhängig von Temperaturen.",
    "mix": "Kombiniert mehrere Kurven: Maximum, Minimum, Durchschnitt, Summe oder Subtraktion "
           "(erste Kurve minus alle weiteren).",
    "trigger": "Zwei Zustände: Leerlauf und Last. Erreicht die Temperatur die Last-Temperatur, springt der Lüfter "
               "auf die Last-Drehzahl und bleibt dort, bis die Leerlauf-Temperatur wieder unterschritten wird. "
               "Reaktionszeit hoch/runter: so lange muss der Zustand anhalten.",
    "sync": "Übernimmt den Prozentwert einer anderen Steuerung – plus Offset in Prozentpunkten, oder "
            "proportional (z. B. +20 % → 50 % wird zu 60 %).",
    "auto": "Regelt selbstständig: Liegt die Temperatur über der Last-Temperatur (plus Totband), wird die Drehzahl "
            "um „Schritt %/s“ erhöht, darunter wieder gesenkt – so wird die niedrigste Drehzahl gefunden, die die "
            "Zieltemperatur hält. Unter der Leerlauf-Temperatur gilt die Min-Drehzahl.",
}
HYST_HELP = ("Hysterese: Die Kurve reagiert erst, wenn sich die Temperatur um mindestens diesen Wert geändert hat "
             "(getrennt für steigend ↑ und fallend ↓). Reaktionszeit: So lange muss die Änderung anhalten.")
SENSOR_MIX = [("max", "Maximum"), ("min", "Minimum"), ("avg", "Durchschnitt")]
CURVE_MIX = [("max", "Maximum"), ("min", "Minimum"), ("avg", "Durchschnitt"), ("sum", "Summe"), ("sub", "Subtrahieren")]
CURVES_HELP = (
    "Kurven berechnen die Drehzahl in Prozent – oder im RPM-Modus als Drehzahl, die kalibrierte Lüfter dann "
    "anfahren. Eine Kurve kann mehreren Lüftern zugewiesen werden.\n\n"
    "Über + legst du Kurven an. Ein Klick auf das Symbol einer Kachel erklärt den Kurventyp, "
    "„Bearbeiten“ öffnet den großen Editor mit allen Optionen."
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
            self.set_child(ui.status_page("Verbinde mit Dienst …", "", "network-transmit-receive-symbolic"))
            return
        page, flow = ui.grid_page("Kurven", CURVES_HELP)
        visible = [c for c in win.config["curves"] if win.is_visible_item(c["id"])]
        for curve in visible:
            flow.append(self._card(curve))
        overlay = Gtk.Overlay(child=page)
        if not visible:
            overlay.add_overlay(Gtk.Label(label="Noch keine Kurven – lege mit + eine an.", css_classes=["dim-label"],
                                          halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER))
        overlay.add_overlay(ui.fab("Kurve hinzufügen", [(TYPE_ICONS[k], n, lambda k=k: self.add(k))
                                                        for k, n in TYPE_NAMES.items()]))
        self.set_child(overlay)
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
        dup["name"] = f"{curve['name']} (Kopie)"
        self.win.config["curves"].append(dup)
        self.win.config_changed(rebuild={"curves", "controls"})

    def delete(self, curve, after=None):
        win = self.win
        users = [win.display_name(c["id"]) for c in win.config["controls"]
                 if c["curve"] == curve["id"] and c["mode"] == "curve"]
        users += [c["name"] for c in win.config["curves"] if curve["id"] in c.get("curves", [])]
        body = "Die Kurve wird entfernt."
        if users:
            body += "\n\nVerwendet von: " + ", ".join(users) + ".\nBetroffene Lüfter gehen zurück in die Automatik."

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
        win.confirm(f"„{curve['name']}“ löschen?", body, "Löschen", confirmed, destructive=True)

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

    # --- cards ----------------------------------------------------------
    def _card(self, curve):
        win = self.win
        changed = win.config_changed

        def setter(key, rebuild=None):
            def apply(value):
                curve[key] = value
                changed(rebuild=rebuild)
            return apply

        def rename(text):
            if text:
                curve["name"] = text
                changed(rebuild={"controls"})

        c = ui.card(hidden=curve["id"] in win.config["hidden"])
        ui.card_header(c, TYPE_ICONS[curve["type"]], ui.name_entry(curve["name"], "Name", rename),
                       [("Bearbeiten …", lambda: self.open_editor(curve)),
                        ("Duplizieren", lambda: self._duplicate(curve)),
                        ui.hidden_menu_item(win, curve["id"]),
                        ("Löschen", lambda: self.delete(curve))],
                       icon_tooltip=f"{TYPE_NAMES[curve['type']]} – Hilfe", on_icon=lambda: self.show_help(curve))

        kind = curve["type"]
        top, step = _speed_top(curve), _speed_step(curve)
        if kind in TEMP_CURVES:
            c.append(self._sensor_picker(curve))
        elif kind == "mix":
            keys = [k for k, _ in CURVE_MIX]
            c.append(ui.labeled("Funktion", ui.dropdown([n for _, n in CURVE_MIX],
                                                        keys.index(curve["function"]) if curve["function"] in keys else 0,
                                                        lambda i: setter("function")(keys[i]))))
            choices = [(o["id"], o["name"]) for o in win.config["curves"] if o["id"] != curve["id"]]
            ui.picker_list(c, win, list(curve["curves"]), choices, lambda v: setter("curves", {"curves"})(v),
                           "Keine Kurven gewählt – Lüfter laufen auf 100 %")
        elif kind == "flat":
            c.append(ui.labeled(_speed_caption(curve, "Drehzahl"), ui.spin(curve["value"], 0, top, step, setter("value"))))
        elif kind == "sync":
            pwms = list((win.status or {}).get("pwms", {}))
            if curve["control"] and curve["control"] not in pwms:
                pwms.append(curve["control"])
            c.append(ui.labeled("Steuerung", ui.dropdown(
                [win.display_name(p) for p in pwms] or ["Keine Steuerungen"],
                pwms.index(curve["control"]) if curve["control"] in pwms else 0,
                lambda i: pwms and setter("control")(pwms[i]))))
            grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True)
            grid.attach(ui.labeled("Offset %", ui.spin(curve["offset"], -100, 100, 1, setter("offset"), 1)), 0, 0, 1, 1)
            grid.attach(ui.switch_line("Proportional", curve["proportional"], setter("proportional"),
                                       "Offset als Prozentsatz des Quellwerts"), 1, 0, 1, 1)
            c.append(grid)

        values = Gtk.Box(spacing=8)
        out = Gtk.Label(css_classes=["fc-big", "numeric"], xalign=0)
        temp = Gtk.Label(css_classes=["fc-caption", "numeric"], xalign=0, hexpand=True, valign=Gtk.Align.CENTER)
        values.append(out)
        values.append(temp)
        edit = Gtk.Button(label="Bearbeiten", css_classes=["flat"], valign=Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self.open_editor(curve))
        values.append(edit)
        c.append(values)

        graph = None
        rpm = curve.get("unit") == "rpm"
        if kind == "graph":
            graph = GraphEditor(curve["points"], editable=False, compact=True, temp_range=curve["temp_axis"], rpm=rpm)
            click = Gtk.GestureClick()
            click.connect("released", lambda *_: self.open_editor(curve))
            graph.add_controller(click)
            graph.set_tooltip_text("Klicken zum Bearbeiten")
            graph.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            c.append(graph)
        elif kind == "linear":
            preview = [[curve["temp_min"], curve["speed_min"]], [curve["temp_max"], curve["speed_max"]]]
            graph = GraphEditor(preview, editable=False, compact=True, rpm=rpm)
            c.append(graph)
            grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True)

            def lin(key, idx):
                def apply(value):
                    curve[key] = value
                    preview[idx[0]][idx[1]] = value
                    graph.refresh()
                    changed()
                return apply
            fields = [(ui.tlabel("Min. Temp."), "temp_min", True, (0, 0)),
                      (ui.tlabel("Max. Temp."), "temp_max", True, (1, 0)),
                      (_speed_caption(curve, "Min. Drehzahl"), "speed_min", False, (0, 1)),
                      (_speed_caption(curve, "Max. Drehzahl"), "speed_max", False, (1, 1))]
            for i, (text, key, is_temp, idx) in enumerate(fields):
                widget = (ui.temp_spin(curve[key], -20, 150, lin(key, idx)) if is_temp
                          else ui.spin(curve[key], 0, top, step, lin(key, idx)))
                grid.attach(ui.labeled(text, widget), i % 2, i // 2, 1, 1)
            c.append(grid)
        elif kind in ("trigger", "auto"):
            grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True)
            speed_keys = ("idle_speed", "load_speed") if kind == "trigger" else ("min_speed", "max_speed")
            speed_names = ("Leerlauf", "Last") if kind == "trigger" else ("Min.", "Max.")
            fields = [(ui.tlabel("Leerlauf"), "idle_temp", True),
                      (_speed_caption(curve, speed_names[0]), speed_keys[0], False),
                      (ui.tlabel("Last" if kind == "trigger" else "Ziel (Last)"), "load_temp", True),
                      (_speed_caption(curve, speed_names[1]), speed_keys[1], False)]
            for i, (text, key, is_temp) in enumerate(fields):
                widget = (ui.temp_spin(curve[key], -20, 150, setter(key)) if is_temp
                          else ui.spin(curve[key], 0, top, step, setter(key)))
                grid.attach(ui.labeled(text, widget), i % 2, i // 2, 1, 1)
            if kind == "auto":
                grid.attach(ui.labeled(_speed_caption(curve, "Schritt") + "/s",
                                       ui.spin(curve["step"], 0.1, 100 if not rpm else 2000, 0.5, setter("step"), 1)),
                            0, 2, 1, 1)
                grid.attach(ui.labeled(ui.tlabel("Totband"),
                                       ui.temp_spin(curve["deadband"], 0, 20, setter("deadband"), delta=True, digits=1)),
                            1, 2, 1, 1)
            c.append(grid)

        if kind in ("graph", "linear"):
            c.append(ui.caption("Hysterese  " + hysteresis_summary(curve)))
        elif kind == "trigger":
            c.append(ui.caption(f"Reaktionszeit ↑ {curve['response_up']:g} s  ↓ {curve['response_down']:g} s"))
        if rpm:
            c.append(ui.caption("RPM-Modus – nur für kalibrierte Lüfter"))

        users = [win.display_name(x["id"]) for x in win.config["controls"]
                 if x["curve"] == curve["id"] and x["mode"] == "curve"]
        c.append(ui.caption("Verwendet von: " + (", ".join(users) if users else "–")))
        self.live[curve["id"]] = (out, temp, graph, curve)
        return c

    def _sensor_picker(self, curve):
        win = self.win
        temps = win.status["temps"] if win.status else {}
        if len(curve["sensors"]) > 1:
            mix = dict(SENSOR_MIX).get(curve["sensor_mix"], curve["sensor_mix"])
            return ui.labeled("Temperaturquelle",
                              Gtk.Label(label=f"{len(curve['sensors'])} Sensoren ({mix}) – über „Bearbeiten“ ändern",
                                        xalign=0, wrap=True, max_width_chars=30))
        ids = list(temps)
        missing = [s for s in curve["sensors"] if s not in temps]
        labels = [win.display_name(s) for s in ids] + [f"{s} (fehlt)" for s in missing]
        ids += missing
        if not curve["sensors"]:
            ids, labels = [None] + ids, ["Sensor wählen …"] + labels
        current = curve["sensors"][0] if curve["sensors"] else None

        def pick(i):
            if ids[i] is not None:
                curve["sensors"] = [ids[i]]
                win.config_changed(rebuild={"curves"} if current is None else None)
        dd = ui.dropdown(labels, ids.index(current) if current in ids else 0, pick)
        dd.set_tooltip_text("Mehrere Sensoren kombinieren: „Bearbeiten“")
        return ui.labeled("Temperaturquelle", dd)

    def update_live(self):
        st = self.win.status
        if not st:
            return
        for cid, (out, temp, graph, curve) in self.live.items():
            info = st["curves"].get(cid) or {}
            out.set_label("100 % (Sicherheit)" if info.get("failsafe") else _speed_fmt(curve, info.get("output")))
            out.set_tooltip_text("Sensor fehlt oder Quelle nicht verfügbar – Lüfter laufen auf voller Drehzahl"
                                 if info.get("failsafe") else None)
            temp.set_label(fmt_temp(info["temp"]) if info.get("temp") is not None else "")
            if graph:
                graph.set_live(info.get("temp"), info.get("output"))
        if self.dialog:
            self.dialog.update_live()


class CurveEditorDialog(Adw.Dialog):
    """Full editor for one curve. Changes apply live; „Abbrechen“ restores the state from before opening."""

    def __init__(self, win, curve, page):
        super().__init__(title=curve["name"], content_width=780, content_height=780)
        self.win, self.curve, self.page = win, curve, page
        self.snapshot = copy.deepcopy(curve)
        self.graph = None
        self.points_list = None
        self.connect("closed", self._on_closed)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: self._cancel())
        done = Gtk.Button(label="Fertig", css_classes=["suggested-action"])
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
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Kurve löschen",
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
            general.add(combo_row("Einheit", ["Prozent", "Drehzahl (RPM)"], 1 if rpm else 0, set_unit,
                                  subtitle="RPM-Kurven brauchen kalibrierte Lüfter"))
        live_row = Adw.ActionRow(title="Aktuell")
        self.live_label = value_label()
        live_row.add_suffix(self.live_label)
        general.add(live_row)
        page.add(general)

        if kind == "graph":
            group = Adw.PreferencesGroup(title="Kurvenpunkte",
                                         description="Punkte ziehen · Doppelklick fügt einen Punkt hinzu · Rechtsklick entfernt ihn")
            self.graph = GraphEditor(curve["points"], self._graph_changed, temp_range=curve["temp_axis"], rpm=rpm)
            group.add(Gtk.Frame(child=self.graph, css_classes=["card"]))
            page.add(group)
            page.add(self._points_group())
            axis = Adw.PreferencesGroup(title="Temperaturbereich der Achse")

            def set_axis(index):
                def apply(value):
                    lo, hi = curve["temp_axis"]
                    new = [value, hi] if index == 0 else [lo, value]
                    if new[1] - new[0] >= 10:
                        curve["temp_axis"] = new
                        self.graph.set_range(new)
                        changed()
                return apply
            axis.add(temp_spin_row("Von", curve["temp_axis"][0], -50, 140, set_axis(0)))
            axis.add(temp_spin_row("Bis", curve["temp_axis"][1], -40, 150, set_axis(1)))
            page.add(axis)
        elif kind == "linear":
            preview = [[curve["temp_min"], curve["speed_min"]], [curve["temp_max"], curve["speed_max"]]]
            group = Adw.PreferencesGroup(title="Werte")
            self.graph = GraphEditor(preview, editable=False, rpm=rpm)

            def lin(key, idx):
                def apply(value):
                    curve[key] = value
                    preview[idx[0]][idx[1]] = value
                    self.graph.refresh()
                    changed()
                return apply
            group.add(temp_spin_row("Temperatur min", curve["temp_min"], -20, 150, lin("temp_min", (0, 0))))
            group.add(spin_row(_speed_caption(curve, "Drehzahl bei min"), curve["speed_min"], 0, top, step, lin("speed_min", (0, 1))))
            group.add(temp_spin_row("Temperatur max", curve["temp_max"], -20, 150, lin("temp_max", (1, 0))))
            group.add(spin_row(_speed_caption(curve, "Drehzahl bei max"), curve["speed_max"], 0, top, step, lin("speed_max", (1, 1))))
            group.add(Gtk.Frame(child=self.graph, css_classes=["card"], margin_top=12))
            page.add(group)
        elif kind == "flat":
            group = Adw.PreferencesGroup(title="Wert")
            group.add(spin_row(_speed_caption(curve, "Drehzahl"), curve["value"], 0, top, step, setter("value")))
            page.add(group)
        elif kind == "mix":
            page.add(self._mix_group())
        elif kind == "sync":
            group = Adw.PreferencesGroup(title="Quelle")
            pwms = list((win.status or {}).get("pwms", {}))
            if curve["control"] and curve["control"] not in pwms:
                pwms.append(curve["control"])
            group.add(combo_row("Steuerung", [win.display_name(p) for p in pwms] or ["–"],
                                pwms.index(curve["control"]) if curve["control"] in pwms else 0,
                                lambda i: pwms and setter("control")(pwms[i])))
            group.add(spin_row("Offset", curve["offset"], -100, 100, 0.5, setter("offset"), digits=1,
                               subtitle="Prozentpunkte, oder Prozent des Quellwerts bei „Proportional“"))
            group.add(switch_row("Proportional", curve["proportional"], setter("proportional")))
            page.add(group)
        elif kind == "trigger":
            group = Adw.PreferencesGroup(title="Schwellwerte")
            group.add(temp_spin_row("Leerlauf-Temperatur", curve["idle_temp"], -20, 150, setter("idle_temp"),
                                    subtitle="Unterhalb davon: Leerlauf-Drehzahl"))
            group.add(spin_row(_speed_caption(curve, "Leerlauf-Drehzahl"), curve["idle_speed"], 0, top, step, setter("idle_speed")))
            group.add(temp_spin_row("Last-Temperatur", curve["load_temp"], -20, 150, setter("load_temp"),
                                    subtitle="Ab hier: Last-Drehzahl"))
            group.add(spin_row(_speed_caption(curve, "Last-Drehzahl"), curve["load_speed"], 0, top, step, setter("load_speed")))
            group.add(spin_row("Reaktionszeit hoch s", curve["response_up"], 0, 300, 0.5, setter("response_up"),
                               digits=1, subtitle="So lange muss die Last-Temperatur gehalten werden"))
            group.add(spin_row("Reaktionszeit runter s", curve["response_down"], 0, 300, 0.5, setter("response_down"),
                               digits=1, subtitle="So lange muss die Leerlauf-Temperatur unterschritten sein"))
            page.add(group)
        elif kind == "auto":
            group = Adw.PreferencesGroup(title="Regelung")
            group.add(temp_spin_row("Leerlauf-Temperatur", curve["idle_temp"], -20, 150, setter("idle_temp"),
                                    subtitle="Darunter: Min-Drehzahl"))
            group.add(temp_spin_row("Last-Temperatur (Ziel)", curve["load_temp"], -20, 150, setter("load_temp"),
                                    subtitle="Diese Temperatur soll gehalten werden"))
            group.add(spin_row(_speed_caption(curve, "Min. Drehzahl"), curve["min_speed"], 0, top, step, setter("min_speed")))
            group.add(spin_row(_speed_caption(curve, "Max. Drehzahl"), curve["max_speed"], 0, top, step, setter("max_speed")))
            group.add(spin_row(_speed_caption(curve, "Schritt") + "/s", curve["step"], 0.1, 100 if not rpm else 2000, 0.5,
                               setter("step"), digits=1, subtitle="So schnell wird nachgeregelt"))
            group.add(temp_spin_row("Totband", curve["deadband"], 0, 20, setter("deadband"), delta=True, digits=1,
                                    subtitle="Innerhalb ± Totband um das Ziel bleibt die Drehzahl gleich"))
            group.add(spin_row("Reaktionszeit s", curve["response_time"], 0, 300, 0.5, setter("response_time"), digits=1))
            page.add(group)

        if kind in TEMP_CURVES:
            page.add(self._sensor_group())

        if kind in ("graph", "linear"):
            group = Adw.PreferencesGroup(title="Hysterese", description=HYST_HELP)
            group.add(temp_spin_row("Hysterese ↑", curve["hysteresis_up"], 0, 50, setter("hysteresis_up"),
                                    delta=True, digits=1, subtitle="Bei steigender Temperatur"))
            group.add(spin_row("Reaktionszeit ↑ s", curve["response_up"], 0, 300, 0.5, setter("response_up"), digits=1))
            group.add(temp_spin_row("Hysterese ↓", curve["hysteresis_down"], 0, 50, setter("hysteresis_down"),
                                    delta=True, digits=1, subtitle="Bei fallender Temperatur"))
            group.add(spin_row("Reaktionszeit ↓ s", curve["response_down"], 0, 300, 0.5, setter("response_down"), digits=1))
            group.add(switch_row("Hysterese an den Grenzen ignorieren", curve["ignore_hysteresis_at_limits"],
                                 setter("ignore_hysteresis_at_limits"),
                                 subtitle="Minimum und Maximum werden sofort erreicht"))
            page.add(group)
        self.update_live()
        return page

    # --- graph points ---------------------------------------------------
    def _graph_changed(self):
        self.win.config_changed()
        self._fill_points()

    def _points_group(self):
        group = Adw.PreferencesGroup(title="Punkte als Zahlen")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Punkt hinzufügen",
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
            remove = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Punkt entfernen",
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
        group = Adw.PreferencesGroup(title="Temperaturquelle")
        temps = win.status["temps"] if win.status else {}
        expander = Adw.ExpanderRow(title="Sensoren", expanded=not curve["sensors"])
        known = list(temps) + [s for s in curve["sensors"] if s not in temps]

        def summary():
            names = [win.display_name(s) for s in curve["sensors"]]
            expander.set_subtitle(", ".join(names) if names else "Kein Sensor gewählt – Lüfter laufen auf 100 %")

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
            row.add_suffix(Gtk.Label(label=fmt_temp(value) if sid in temps else "nicht vorhanden",
                                     css_classes=["numeric", "dim-label"]))
            expander.add_row(row)
        summary()
        group.add(expander)
        keys = [k for k, _ in SENSOR_MIX]
        group.add(combo_row("Bei mehreren Sensoren", [n for _, n in SENSOR_MIX],
                            keys.index(curve["sensor_mix"]) if curve["sensor_mix"] in keys else 0,
                            lambda i: (curve.__setitem__("sensor_mix", keys[i]), win.config_changed())))
        return group

    def _mix_group(self):
        win, curve = self.win, self.curve
        group = Adw.PreferencesGroup(title="Kurven kombinieren",
                                     description="Bei „Subtrahieren“ zählt die Reihenfolge: erste Kurve minus alle weiteren.")
        keys = [k for k, _ in CURVE_MIX]
        group.add(combo_row("Funktion", [n for _, n in CURVE_MIX],
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
            group.add(Adw.ActionRow(title="Lege zuerst weitere Kurven an."))
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
            self.live_label.set_label("noch nicht berechnet")
            if self.graph:
                self.graph.set_live(None, None)
            return
        temp, out = info.get("temp"), info.get("output")
        out_text = "100 % (Sicherheit)" if info.get("failsafe") else _speed_fmt(self.curve, out)
        self.live_label.set_label(out_text if temp is None else f"{fmt_temp(temp)}  →  {out_text}")
        if self.graph:
            self.graph.set_live(temp, out)
