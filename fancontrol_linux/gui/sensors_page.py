import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import config as cfgmod  # noqa: E402
from ..sensors import file_path_allowed  # noqa: E402
from . import common as ui  # noqa: E402
from .util import entry_row, fmt_pct, fmt_rpm, fmt_temp, value_label  # noqa: E402

CUSTOM_NAMES = {
    "mix": "Mix-Sensor",
    "average": "Zeitdurchschnitt",
    "offset": "Offset-Sensor",
    "file": "Datei-Sensor",
}
CUSTOM_HELP = (
    "Eigene Sensoren sind berechnete Temperaturen, die wie normale Sensoren in Kurven verwendet werden können.\n\n"
    "• Mix: Maximum, Minimum, Durchschnitt, Summe oder Differenz mehrerer Sensoren. "
    "„Fehlende erlauben“ rechnet mit den restlichen weiter, wenn ein Sensor ausfällt.\n"
    "• Zeitdurchschnitt: glättet einen Sensor über einen Zeitraum (1–3600 s).\n"
    "• Offset: addiert einen festen Wert oder einen Prozentsatz.\n"
    "• Datei: liest die Temperatur (°C, erste Zeile) aus einer Datei. Aus Sicherheitsgründen nur aus "
    "/run/fancontrol-linux/sensors/ (für Mitglieder der Gruppe fancontrol beschreibbar), "
    "/var/lib/fancontrol-linux/sensors/ oder /sys/. So können Skripte und andere Programme Werte liefern."
)
FUNCTIONS = [("max", "Maximum"), ("min", "Minimum"), ("avg", "Durchschnitt"), ("sum", "Summe"), ("sub", "Subtrahieren")]


class SensorsPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.labels = {}
        self.custom_live = {}

    def rebuild(self):
        win = self.win
        self.labels = {}
        self.custom_live = {}
        st = win.status
        if st is None or win.config is None:
            self.set_child(ui.status_page("Verbinde mit Dienst …", "", "network-transmit-receive-symbolic"))
            return
        custom_box, flow = ui.section("Eigene Sensoren", CUSTOM_HELP)
        sensors = [s for s in win.config["custom_sensors"] if win.is_visible_item("custom:" + s["id"])]
        for sensor in sensors:
            flow.append(self._card(sensor))
        if not sensors:
            custom_box.append(ui.caption("Noch keine eigenen Sensoren – lege mit + einen an."))

        hardware = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        hardware.append(Gtk.Label(label="Hardware", css_classes=["fc-page-title"], xalign=0))
        for title, key, fmt in (("Temperaturen", "temps", fmt_temp), ("Lüfter", "fans", fmt_rpm),
                                ("Lüfterausgänge", "pwms", fmt_pct)):
            group = Adw.PreferencesGroup(title=title, description="Eigene Namen eintragen und mit ✓ übernehmen")
            items = {k: v for k, v in st[key].items() if not v.get("custom")}
            if not items:
                group.add(Adw.ActionRow(title="Keine gefunden"))
            for sid, info in items.items():
                row = entry_row(info["label"], win.config["sensor_names"].get(sid, ""),
                                lambda text, s=sid: self._rename(s, text))
                row.set_tooltip_text(f"{info['label']}\n{sid}")
                label = value_label()
                row.add_suffix(label)
                self.labels[(key, sid)] = (label, fmt)
                group.add(row)
            hardware.append(group)

        overlay = Gtk.Overlay(child=ui.scroller([custom_box, hardware]))
        overlay.add_overlay(ui.fab("Eigenen Sensor hinzufügen",
                                   [("fc-custom-sensor-symbolic", n, lambda k=k: self._add(k))
                                    for k, n in CUSTOM_NAMES.items()]))
        self.set_child(overlay)
        self.update_live()

    def _rename(self, sid, text):
        names = self.win.config["sensor_names"]
        if text:
            names[sid] = text
        else:
            names.pop(sid, None)
        self.win.config_changed(rebuild={"controls", "curves"})

    def _add(self, kind):
        win = self.win
        n = sum(1 for s in win.config["custom_sensors"] if s["type"] == kind) + 1
        source = (win.default_temp_sensors() or [None])[0]
        win.config["custom_sensors"].append(cfgmod.new_custom_sensor(kind, f"{CUSTOM_NAMES[kind]} {n}", source))
        win.config_changed(rebuild={"sensors", "curves"})

    def _delete(self, sensor):
        win = self.win
        sid = "custom:" + sensor["id"]
        users = [c["name"] for c in win.config["curves"] if sid in c.get("sensors", [])]
        users += [s["name"] for s in win.config["custom_sensors"] if sid in s.get("sensors", []) or s.get("sensor") == sid]
        body = "Der Sensor wird entfernt."
        if users:
            body += "\n\nVerwendet von: " + ", ".join(users) + ".\nDort fehlt er danach – betroffene Lüfter laufen auf 100 %."

        def confirmed():
            win.config["custom_sensors"] = [s for s in win.config["custom_sensors"] if s["id"] != sensor["id"]]
            win.config_changed(rebuild={"sensors", "curves"})
        win.confirm(f"„{sensor['name']}“ löschen?", body, "Löschen", confirmed, destructive=True)

    def _sources(self, exclude):
        temps = (self.win.status or {}).get("temps", {})
        return [(sid, self.win.display_name(sid)) for sid in temps if sid != exclude]

    def _card(self, sensor):
        win = self.win
        sid = "custom:" + sensor["id"]

        def setter(key, rebuild=None):
            def apply(value):
                sensor[key] = value
                win.config_changed(rebuild=rebuild)
            return apply

        def rename(text):
            if text:
                sensor["name"] = text
                win.config_changed(rebuild={"curves", "controls"})

        c = ui.card(hidden=sid in win.config["hidden"])
        ui.card_header(c, "fc-custom-sensor-symbolic", ui.name_entry(sensor["name"], "Name", rename),
                       [ui.hidden_menu_item(win, sid), ("Löschen", lambda: self._delete(sensor))],
                       icon_tooltip=CUSTOM_NAMES[sensor["type"]])
        value = Gtk.Label(css_classes=["fc-big", "numeric"], xalign=0)
        c.append(value)

        kind = sensor["type"]
        sources = self._sources(sid)
        if kind == "mix":
            keys = [k for k, _ in FUNCTIONS]
            c.append(ui.labeled("Funktion", ui.dropdown([n for _, n in FUNCTIONS], keys.index(sensor["function"]),
                                                        lambda i: setter("function")(keys[i]))))
            ui.picker_list(c, win, list(sensor["sensors"]), sources, setter("sensors", {"sensors"}),
                           "Keine Sensoren gewählt")
            c.append(ui.switch_line("Fehlende erlauben", sensor["allow_missing"], setter("allow_missing"),
                                    "Mit den verbleibenden Sensoren weiterrechnen, wenn einer ausfällt"))
        elif kind in ("average", "offset"):
            ids = [s for s, _l in sources]
            labels = [lbl for _s, lbl in sources]
            if sensor["sensor"] and sensor["sensor"] not in ids:
                ids.append(sensor["sensor"])
                labels.append(f"{sensor['sensor']} (fehlt)")
            if not sensor["sensor"]:
                ids, labels = [None] + ids, ["Sensor wählen …"] + labels

            def pick(i):
                if ids[i] is not None:
                    setter("sensor")(ids[i])
            c.append(ui.labeled("Quelle", ui.dropdown(labels, ids.index(sensor["sensor"]) if sensor["sensor"] in ids else 0,
                                                      pick)))
            if kind == "average":
                c.append(ui.labeled("Zeitraum s", ui.spin(sensor["seconds"], 1, 3600, 1, setter("seconds"))))
            else:
                grid = Gtk.Grid(column_spacing=10, row_spacing=8, column_homogeneous=True)
                offset_spin = (ui.spin(sensor["offset"], -100, 100, 0.5, setter("offset"), 1) if sensor["proportional"]
                               else ui.temp_spin(sensor["offset"], -100, 100, setter("offset"), delta=True, digits=1))
                grid.attach(ui.labeled("Offset %" if sensor["proportional"] else ui.tlabel("Offset"), offset_spin),
                            0, 0, 1, 1)
                grid.attach(ui.switch_line("Proportional", sensor["proportional"],
                                           setter("proportional", {"sensors"})), 1, 0, 1, 1)
                c.append(grid)
        elif kind == "file":
            entry = Gtk.Entry(text=sensor["path"], hexpand=True)

            def apply_path(*_):
                path = entry.get_text().strip()
                if path == sensor["path"]:
                    return
                if not file_path_allowed(path):
                    entry.add_css_class("error")
                    win.toast("Pfad nicht erlaubt – nur /run/fancontrol-linux/sensors/, "
                              "/var/lib/fancontrol-linux/sensors/ oder /sys/")
                    return
                entry.remove_css_class("error")
                setter("path")(path)
            entry.connect("activate", apply_path)
            focus = Gtk.EventControllerFocus()
            focus.connect("leave", apply_path)
            entry.add_controller(focus)
            c.append(ui.labeled("Datei", entry))
            c.append(ui.caption("Erste Zeile = Temperatur in °C. Erlaubt: /run/fancontrol-linux/sensors/, "
                                "/var/lib/fancontrol-linux/sensors/, /sys/"))
        self.custom_live[sid] = value
        return c

    def update_live(self):
        st = self.win.status
        if not st:
            return
        for (key, sid), (label, fmt) in self.labels.items():
            info = st[key].get(sid)
            value = None if info is None else (info["percent"] if key == "pwms" else info["value"])
            label.set_label(fmt(value))
        for sid, label in self.custom_live.items():
            info = st["temps"].get(sid)
            label.set_label(fmt_temp(info["value"]) if info and info["value"] is not None else "– (keine Daten)")
