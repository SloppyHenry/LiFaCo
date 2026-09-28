import copy
import json
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID  # noqa: E402
from .. import config as cfgmod  # noqa: E402
from ..ipc import Client, DaemonUnavailable  # noqa: E402
from .controls import ControlsPage  # noqa: E402
from .curves import CurvesPage  # noqa: E402
from .misc_pages import AboutPage, DesignPage, SettingsPage, TrayPage  # noqa: E402
from .sensors_page import SensorsPage  # noqa: E402
from .theme import Theme  # noqa: E402
from .tray import Tray  # noqa: E402
from .util import GuiPrefs, fmt_pct, fmt_rpm, fmt_temp, run_async, set_fahrenheit, c_to_disp  # noqa: E402

SERVICE = "fancontrol-linux.service"
CPU_PATTERNS = ["tctl", "package id", "tdie", "cpu"]
GPU_PATTERNS = ["nvidia", "amdgpu: edge", "amdgpu: junction", "gpu"]
GPU_DRIVERS = ("amdgpu", "nouveau", "radeon", "nvidia")
_UNSET = object()
SERVICE_FILES = ("/etc/systemd/system/fancontrol-linux.service", "/etc/init.d/fancontrol-linux",
                 "/etc/sv/fancontrol-linux/run")


def _service_missing():
    return not any(os.path.exists(p) for p in SERVICE_FILES)
PAGES = (
    ("controls", "Steuerungen", "fc-gauge-symbolic"),
    ("curves", "Kurven", "fc-curve-symbolic"),
    ("sensors", "Sensoren", "fc-thermometer-symbolic"),
    ("design", "Design", "fc-palette-symbolic"),
    ("tray", "Tray", "fc-eye-symbolic"),
    ("settings", "Einstellungen", "fc-settings-symbolic"),
    ("about", "Über", "fc-about-symbolic"),
)
SHORTCUTS = [
    ("<Control>1 … <Control>7", "Seite wechseln"),
    ("<Control>r", "Hardware neu erkennen"),
    ("<Control>n", "Neue grafische Kurve"),
    ("<Control>h", "Ausgeblendete Kacheln anzeigen/verbergen"),
    ("<Control>p", "Profile öffnen"),
    ("<Control>o", "Konfigurationsdatei öffnen"),
    ("<Control><Shift>s", "Konfiguration als Datei speichern"),
    ("<Control>i", "Aus Datei importieren"),
    ("F1", "Tastenkürzel anzeigen"),
    ("<Control>w", "Fenster schließen (läuft im Tray weiter)"),
    ("<Control>q", "Beenden"),
]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="FanControl", default_width=1180, default_height=800)
        self.app = app
        self.prefs = GuiPrefs()
        set_fahrenheit(self.prefs.get("fahrenheit"))
        self.client = Client(timeout=3)
        self.config = None
        self.status = None
        self.connected = None
        self.polling = False
        self.push_source = None
        self.hw_signature = None
        self.setup_prompted = False
        self.shown_calibration = _UNSET
        self.profiles_checked = 0
        self.theme = Theme()
        self.tray = None
        self.held = False

        self.controls_page = ControlsPage(self)
        self.curves_page = CurvesPage(self)
        self.sensors_page = SensorsPage(self)
        self.design_page = DesignPage(self)
        self.tray_page = TrayPage(self)
        self.settings_page = SettingsPage(self)
        widgets = {"controls": self.controls_page, "curves": self.curves_page, "sensors": self.sensors_page,
                   "design": self.design_page, "tray": self.tray_page, "settings": self.settings_page,
                   "about": AboutPage(self)}

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        self.rail = Gtk.ListBox(css_classes=["navigation-sidebar", "fc-rail"], width_request=92)
        for name, title, icon in PAGES:
            self.stack.add_named(widgets[name], name)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER))
            row = Gtk.ListBoxRow(child=box, tooltip_text=title)
            row.page_name = name
            self.rail.append(row)
        self.rail.connect("row-selected", lambda _l, row: row and self.stack.set_visible_child_name(row.page_name))
        self.rail.select_row(self.rail.get_row_at_index(0))

        header = Adw.HeaderBar(show_title=False)
        title = Gtk.Box(spacing=10, margin_start=6)
        title.append(Gtk.Image(icon_name="fc-fan-symbolic", pixel_size=22))
        title.append(Gtk.Label(label="Fan Control", css_classes=["title"]))
        header.pack_start(title)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Neue leere Konfiguration", "win.new-config")
        section.append("Konfigurationsdatei öffnen …", "win.open-config")
        section.append("Konfiguration speichern unter …", "win.save-config")
        section.append("Aus Datei importieren …", "win.import-config")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Einrichtungsassistent", "win.setup")
        section.append("Hardware neu erkennen", "win.rescan")
        section.append("Tastenkürzel", "win.shortcuts")
        section.append("Beenden", "win.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Menü"))

        self.profile_content = Adw.ButtonContent(icon_name="fc-profile-symbolic", label="Profil")
        self.profile_popover = Gtk.Popover()
        self.profile_popover.connect("show", lambda *_: self._fill_profiles())
        self.profile_button = Gtk.MenuButton(child=self.profile_content, popover=self.profile_popover,
                                             tooltip_text="Profile (Strg+P)")
        header.pack_end(self.profile_button)
        self.hidden_toggle = Gtk.ToggleButton(icon_name="fc-eye-symbolic", active=bool(self.prefs.get("show_hidden")),
                                              tooltip_text="Ausgeblendete Kacheln anzeigen (Strg+H)")
        self.hidden_toggle.connect("toggled", lambda b: self.set_show_hidden(b.get_active()))
        header.pack_end(self.hidden_toggle)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Hardware neu erkennen (Strg+R)")
        refresh.connect("clicked", lambda *_: self.rescan())
        header.pack_end(refresh)
        self.theme.header_widgets.append(header)
        self.apply_theme()
        self._setup_actions()

        self.banner = Adw.Banner(button_label="Dienst starten")
        self.banner.connect("button-clicked", lambda *_: self.start_service())
        self.safety_banner = Adw.Banner(title="Sicherheitstemperatur erreicht – alle gesteuerten Lüfter laufen auf 100 %")

        body = Gtk.Box()
        body.append(self.rail)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.toasts = Adw.ToastOverlay(child=self.stack, hexpand=True)
        body.append(self.toasts)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.add_top_bar(self.safety_banner)
        view.set_content(body)
        self.set_content(view)

        self.connect("close-request", self._on_close)
        if self.prefs.get("tray_enabled"):
            self._start_tray()
        self.rebuild()
        self._poll()
        self.poll_source = GLib.timeout_add(1000, self._poll)

    def _setup_actions(self):
        actions = {
            "rescan": (self.rescan, ["<Control>r"]),
            "setup": (self.setup_wizard, []),
            "shortcuts": (self.show_shortcuts, ["F1"]),
            "quit": (self.quit_app, ["<Control>q"]),
            "close": (self.close, ["<Control>w"]),
            "new-curve": (lambda: (self.show_page("curves"), self.curves_page.add("graph")), ["<Control>n"]),
            "toggle-hidden": (lambda: self.hidden_toggle.set_active(not self.hidden_toggle.get_active()),
                              ["<Control>h"]),
            "profiles": (lambda: self.profile_button.popup(), ["<Control>p"]),
            "new-config": (self.new_config, []),
            "open-config": (self.open_config, ["<Control>o"]),
            "save-config": (self.save_config, ["<Control><Shift>s"]),
            "import-config": (self.import_config, ["<Control>i"]),
        }
        for i, (name, _t, _i) in enumerate(PAGES):
            actions[f"page-{i + 1}"] = (lambda n=name: self.show_page(n), [f"<Control>{i + 1}"])
        for name, (callback, accels) in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=callback: cb())
            self.add_action(action)
            if accels:
                self.app.set_accels_for_action(f"win.{name}", accels)

    # --- data helpers ---------------------------------------------------
    def find_control(self, pid):
        if not self.config:
            return None
        return next((c for c in self.config["controls"] if c["id"] == pid), None)

    def ensure_control(self, pid):
        ctl = self.find_control(pid)
        if ctl is None:
            ctl = copy.deepcopy(cfgmod.DEFAULT_CONTROL)
            ctl["id"] = pid
            self.config["controls"].append(ctl)
        return ctl

    def find_curve(self, cid):
        if not self.config or cid is None:
            return None
        return next((c for c in self.config["curves"] if c["id"] == cid), None)

    def display_name(self, sid):
        ctl = self.find_control(sid)
        if ctl and ctl["name"]:
            return ctl["name"]
        if self.config:
            if self.config["sensor_names"].get(sid):
                return self.config["sensor_names"][sid]
            if sid and sid.startswith("custom:"):
                sensor = next((s for s in self.config["custom_sensors"] if "custom:" + s["id"] == sid), None)
                if sensor:
                    return sensor["name"]
        if self.status:
            for key in ("temps", "fans", "pwms"):
                if sid in self.status[key]:
                    return self.status[key][sid]["label"]
        return sid

    def is_visible_item(self, item_id):
        return item_id not in (self.config or {}).get("hidden", []) or bool(self.prefs.get("show_hidden"))

    def toggle_hidden(self, item_id):
        hidden = self.config["hidden"]
        if item_id in hidden:
            hidden.remove(item_id)
        else:
            hidden.append(item_id)
            if not self.prefs.get("show_hidden"):
                self.toast("Ausgeblendet – über das Augen-Symbol oben wieder sichtbar machen")
        self.config_changed(rebuild={"controls", "curves", "sensors"})

    def set_show_hidden(self, show):
        self.prefs.set("show_hidden", bool(show))
        if self.hidden_toggle.get_active() != bool(show):
            self.hidden_toggle.set_active(bool(show))
        self.rebuild({"controls", "curves", "sensors", "settings"})

    def set_fahrenheit(self, index):
        self.prefs.set("fahrenheit", index == 1)
        set_fahrenheit(index == 1)
        self.rebuild()
        self.update_tray(force=True)

    def _pick_temp(self, patterns):
        temps = (self.status or {}).get("temps", {})
        for pat in patterns:
            for sid, info in temps.items():
                if not info.get("custom") and pat in info["label"].lower() and info["value"] is not None:
                    return sid
        return None

    def default_temp_sensors(self):
        sid = self._pick_temp(CPU_PATTERNS) or next(iter((self.status or {}).get("temps", {})), None)
        return [sid] if sid else []

    # --- updates --------------------------------------------------------
    def rebuild(self, pages=None):
        pages = pages or {"controls", "curves", "sensors", "settings", "tray"}
        if "controls" in pages:
            self.controls_page.rebuild()
        if "curves" in pages:
            self.curves_page.rebuild()
        if "sensors" in pages:
            self.sensors_page.rebuild()
        if "settings" in pages:
            self.settings_page.rebuild()
        if "tray" in pages:
            self.tray_page.rebuild()
        self._update_profile_label()

    def _update_profile_label(self):
        self.profile_content.set_label((self.config or {}).get("profile") or "Profil")

    def config_changed(self, rebuild=None):
        if self.config is None:
            return
        if rebuild:
            GLib.idle_add(lambda: (self.rebuild(rebuild), False)[1])
        if self.push_source:
            GLib.source_remove(self.push_source)
        self.push_source = GLib.timeout_add(400, self._push)

    def _push(self):
        if self.push_source:
            GLib.source_remove(self.push_source)
        self.push_source = None
        cfg = copy.deepcopy(self.config)
        run_async(lambda: self.client.call("set_config", config=cfg),
                  on_error=lambda e: self.toast(f"Nicht übernommen: {e}"))
        return False

    def _poll(self):
        if self.polling:
            return True
        self.polling = True
        need_config = self.config is None
        now = GLib.get_monotonic_time()
        need_profiles = self.tray is not None and now - self.profiles_checked > 15_000_000
        if need_profiles:
            self.profiles_checked = now

        def fetch():
            status = self.client.call("status")
            cfg = self.client.call("get_config") if need_config else None
            profiles = self.client.call("list_profiles") if need_profiles else None
            return status, cfg, profiles

        run_async(fetch, self._on_status, self._on_poll_error)
        return True

    def _on_status(self, result):
        self.polling = False
        status, cfg, profiles = result if len(result) == 3 else (*result, None)
        if not self.connected:
            self.connected = True
            self.banner.set_revealed(False)
            self.settings_page.rebuild()
        rebuild = cfg is not None
        if cfg is not None:
            self.config = cfg
        self.status = status
        signature = tuple(tuple(sorted(status[k])) for k in ("temps", "fans", "pwms"))
        if signature != self.hw_signature:
            self.hw_signature = signature
            rebuild = True
        if rebuild:
            self.rebuild()
        self.controls_page.update_live()
        self.curves_page.update_live()
        self.sensors_page.update_live()
        self.safety_banner.set_revealed(bool(status.get("safety_active")))
        if profiles is not None and self.tray:
            self.tray.set_profiles(profiles["profiles"], profiles["active"])
        self.update_tray()
        self._check_calibration(status.get("calibration"))
        self._maybe_prompt_setup()

    def _on_poll_error(self, error):
        self.polling = False
        if self.connected is not False:
            self.connected = False
            self.status = None
            self.config = None
            self.hw_signature = None
            self.rebuild()
        self.safety_banner.set_revealed(False)
        message = str(error)
        self.banner.set_title(message)
        if not isinstance(error, DaemonUnavailable) or "Berechtigung" in message:
            label = None
        elif _service_missing() and os.environ.get("APPIMAGE"):
            label = "Dienst installieren"
            message = "Der Hintergrunddienst ist noch nicht eingerichtet."
            self.banner.set_title(message)
        else:
            label = "Dienst starten"
        self.banner.set_button_label(label)
        self.banner.set_revealed(True)
        if self.tray:
            self.tray.set_main_tooltip("FanControl", "Dienst nicht erreichbar")

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(message)), timeout=5))

    def confirm(self, heading, body, action_label, callback, destructive=False):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Abbrechen")
        dialog.add_response("ok", action_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                       else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _d, r: callback() if r == "ok" else None)
        dialog.present(self)

    # --- tray -----------------------------------------------------------
    def _start_tray(self):
        self.tray = Tray(on_activate=self.present_window, on_quit=self.quit_app, on_profile=self._load_profile)
        if not self.tray.start():
            self.tray = None
            return
        if not self.held:
            self.app.hold()
            self.held = True
        self.profiles_checked = 0

    def set_tray_enabled(self, enabled):
        self.prefs.set("tray_enabled", bool(enabled))
        if enabled and not self.tray:
            self._start_tray()
            self.update_tray(force=True)
        elif not enabled and self.tray:
            self.tray.stop()
            self.tray = None
            if self.held:
                self.app.release()
                self.held = False

    def update_tray(self, force=False):
        if not self.tray or not self.status:
            return
        st = self.status
        lines = []
        for pid, info in st["pwms"].items():
            if info["controlled"]:
                ctl = self.find_control(pid) or {}
                fan = ctl.get("fan") or info.get("default_fan")
                rpm = st["fans"].get(fan, {}).get("value") if fan else None
                lines.append(f"{self.display_name(pid)}: {fmt_pct(info['percent'])}"
                             + (f" · {fmt_rpm(rpm)}" if rpm is not None else ""))
        if st.get("safety_active"):
            lines.insert(0, "⚠ Sicherheitstemperatur erreicht")
        self.tray.set_main_tooltip(f"FanControl – {st.get('profile') or ''}",
                                   "\n".join(lines) or "Keine Lüfter gesteuert")
        entries = []
        for entry in self.prefs.get("tray_sensors") or []:
            sid = entry["id"]
            if sid in st["temps"]:
                value = st["temps"][sid]["value"]
                text = "–" if value is None else f"{c_to_disp(value):.0f}"
                tip = fmt_temp(value)
            elif sid in st["pwms"]:
                value = st["pwms"][sid]["percent"]
                text = "–" if value is None else f"{value:.0f}"
                tip = fmt_pct(value)
            elif sid in st["fans"]:
                value = st["fans"][sid]["value"]
                text = "–" if value is None else (f"{value / 1000:.1f}k" if value >= 1000 else f"{value}")
                tip = fmt_rpm(value)
            else:
                continue
            entries.append((sid, text, entry.get("color", "#ffffff"), self.display_name(sid), tip))
        self.tray.set_value_icons(entries, force=force)

    def present_window(self):
        self.set_visible(True)
        self.present()

    def quit_app(self):
        self._flush()
        if self.tray:
            self.tray.stop()
        self.app.quit()

    # --- actions --------------------------------------------------------
    def show_page(self, name):
        for i in range(len(PAGES)):
            row = self.rail.get_row_at_index(i)
            if row and row.page_name == name:
                self.rail.select_row(row)

    def start_service(self):
        if _service_missing() and os.environ.get("APPIMAGE"):
            self._install_from_appimage()
            return

        def work():
            res = subprocess.run(["pkexec", "systemctl", "start", SERVICE], capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(res.stderr.strip() or "Start fehlgeschlagen")
        run_async(work, lambda _: self.toast("Dienst gestartet"), lambda e: self.toast(f"Dienst: {e}"))

    def _install_from_appimage(self):
        """Run the AppImage's own installer as root; pkexec shows the password prompt."""
        def work():
            cmd = ["pkexec", "/usr/bin/env", "APPIMAGE_EXTRACT_AND_RUN=1", os.environ["APPIMAGE"], "--install", "--yes"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                lines = (res.stderr or res.stdout).strip().splitlines()
                raise RuntimeError(lines[-1] if lines else "abgebrochen")
            return res.stdout

        def done(_output):
            self.toast("Installiert – bitte einmal ab- und wieder anmelden, falls der Zugriff noch verweigert wird")
        self.toast("Installation läuft … (dauert etwa eine Minute)")
        run_async(work, done, lambda e: self.toast(f"Installation fehlgeschlagen: {e}"))

    def rescan(self):
        run_async(lambda: self.client.call("rescan"), lambda st: (self._on_status((st, None, None)),
                                                                  self.toast("Hardware neu erkannt")),
                  lambda e: self.toast(str(e)))

    def identify(self, pid):
        run_async(lambda: self.client.call("identify", control=pid, seconds=10),
                  lambda _: self.toast(f"{self.display_name(pid)} läuft 10 Sekunden auf 100 %"),
                  lambda e: self.toast(str(e)))

    def calibration_clicked(self, pid):
        cal = (self.status or {}).get("calibration")
        if cal and cal["running"] and cal["control"] == pid:
            run_async(lambda: self.client.call("cancel_calibration"), None, lambda e: self.toast(str(e)))
            return

        def start():
            run_async(lambda: self.client.call("calibrate", control=pid),
                      lambda _: self.toast("Kalibrierung gestartet"), lambda e: self.toast(str(e)))
        self.confirm(f"{self.display_name(pid)} kalibrieren?",
                     "Der Lüfter wird etwa 2 Minuten lang schrittweise von 100 % bis 0 % und wieder hoch gefahren, "
                     "um Anlauf- und Stopp-Punkt sowie die Drehzahlkurve zu messen. Bei Erreichen der "
                     "Sicherheitstemperatur wird abgebrochen.",
                     "Kalibrieren", start)

    def _check_calibration(self, cal):
        key = None if not cal or cal["running"] else cal["run"]
        if self.shown_calibration is _UNSET:
            # Results that existed before this window connected are not shown again.
            self.shown_calibration = key
            return
        if key is None or key == self.shown_calibration:
            return
        self.shown_calibration = key
        pid = cal["control"]
        if cal["error"]:
            if cal["error"] != "Abgebrochen":
                self.toast(f"Kalibrierung: {cal['error']}")
            return
        r = cal["result"]
        table = "\n".join(f"{pct:>3} %  →  {rpm} RPM" for pct, rpm in r["rpm_curve"][::2])
        body = (f"Drehzahlsensor: {self.display_name(r['fan'])}\n"
                f"Bereich: {r['min_rpm']}–{r['max_rpm']} RPM\n")
        if r["can_stop"]:
            body += (f"\nEmpfohlen: Minimum {r['suggested_min_percent']:.0f} %, "
                     f"Anlauf {r['suggested_start_percent']:.0f} %, Stop {r['suggested_stop_percent']:.0f} %\n")
        elif r["suggested_min_percent"]:
            body += (f"\nUnter {r['suggested_min_percent']:.0f} % ändert sich die Drehzahl nicht mehr – "
                     "empfohlenes Minimum.\n")
        else:
            body += "\nDer Lüfter stoppt auch bei 0 % nicht – keine Mindestwerte nötig.\n"
        body += "\nDie Messung wird gespeichert (nötig für RPM-Kurven und die Anlaufhilfe).\n\n" + table

        def apply():
            ctl = self.ensure_control(pid)
            ctl["fan"] = r["fan"]
            ctl["calibration"] = {"rpm_curve": r["rpm_curve"]}
            ctl["min_percent"] = r["suggested_min_percent"]
            if r["can_stop"]:
                ctl["start_percent"] = r["suggested_start_percent"]
            ctl["max_percent"] = max(ctl["max_percent"], ctl["min_percent"])
            self.config_changed(rebuild={"controls"})
        self.confirm(f"Kalibrierung abgeschlossen: {self.display_name(pid)}", body, "Übernehmen", apply)

    def _maybe_prompt_setup(self):
        if self.setup_prompted or self.prefs.get("setup_done") or not self.config or not self.status:
            return
        self.setup_prompted = True
        if self.config["curves"] or not self.status["pwms"]:
            return
        self.setup_wizard()

    def setup_wizard(self):
        if not self.config or not self.status:
            self.toast("Keine Verbindung zum Dienst")
            return
        pwms = self.status["pwms"]
        cpu = self._pick_temp(CPU_PATTERNS)
        gpu = self._pick_temp(GPU_PATTERNS)
        lines = [f"• {len(pwms)} steuerbare Lüfter gefunden",
                 f"• CPU-Temperatur: {self.display_name(cpu) if cpu else 'nicht gefunden'}",
                 f"• GPU-Temperatur: {self.display_name(gpu) if gpu else 'nicht gefunden'}"]
        body = ("Der Assistent legt Standardkurven an und weist sie allen Lüftern zu. "
                "GPU-Lüfter folgen der GPU-Kurve, alle anderen der CPU-Kurve.\n\n" + "\n".join(lines) +
                "\n\nDanach kannst du alles anpassen. Tipp: Kalibriere anschließend jeden Lüfter.")
        if not pwms:
            body = "Es wurden keine steuerbaren Lüfter gefunden. Hinweise dazu stehen auf der Seite „Steuerungen“."

        dialog = Adw.AlertDialog(heading="Einrichtung", body=body)
        dialog.add_response("later", "Später")
        if pwms:
            dialog.add_response("setup", "Automatisch einrichten")
            dialog.set_response_appearance("setup", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("setup")

        def on_response(_d, response):
            self.prefs.set("setup_done", True)
            if response == "setup":
                self._auto_setup(cpu, gpu)
        dialog.connect("response", on_response)
        dialog.present(self)

    def _auto_setup(self, cpu, gpu):
        cfg = self.config
        cpu_sensors = [cpu] if cpu else self.default_temp_sensors()
        cpu_curve = cfgmod.new_curve("graph", "CPU", cpu_sensors)
        cfg["curves"].append(cpu_curve)
        gpu_curve = None
        if gpu and gpu != cpu:
            gpu_curve = cfgmod.new_curve("graph", "GPU", [gpu])
            gpu_curve["points"] = [[35, 25], [55, 35], [70, 60], [80, 85], [88, 100]]
            cfg["curves"].append(gpu_curve)
        for pid in self.status["pwms"]:
            if pid.rsplit(":", 1)[-1].startswith("pump"):
                continue  # AIO pumps keep their own setting unless the user assigns a curve
            ctl = self.ensure_control(pid)
            is_gpu = pid.split("@")[0].split(":")[0] in GPU_DRIVERS
            ctl["curve"] = (gpu_curve if is_gpu and gpu_curve else cpu_curve)["id"]
            ctl["mode"] = "curve"
            ctl["enabled"] = True
        self.config_changed(rebuild={"controls", "curves"})
        self.toast("Eingerichtet – die Lüfter werden jetzt gesteuert")

    def show_shortcuts(self):
        group = Adw.PreferencesGroup()
        for accel, text in SHORTCUTS:
            row = Adw.ActionRow(title=text)
            keys = Gtk.ShortcutLabel(accelerator=accel.split(" … ")[0], valign=Gtk.Align.CENTER) \
                if " … " not in accel else Gtk.Label(label="Strg + 1 … 7", css_classes=["dim-label"])
            row.add_suffix(keys)
            group.add(row)
        page = Adw.PreferencesPage()
        page.add(group)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        Adw.Dialog(title="Tastenkürzel", child=view, content_width=460, content_height=620).present(self)

    # --- configuration files --------------------------------------------
    def _json_filter(self):
        f = Gtk.FileFilter(name="FanControl-Konfiguration (JSON)")
        f.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        return store

    def _read_file(self, file):
        with open(file.get_path()) as f:
            return cfgmod.normalize(json.load(f))

    def new_config(self):
        def do():
            cfg = cfgmod.empty_config()
            cfg["profile"] = "Neu"
            if self.config:
                cfg["settings"] = dict(self.config["settings"])
                cfg["sensor_names"] = dict(self.config["sensor_names"])
            run_async(lambda: self.client.call("set_config", config=cfg),
                      lambda c: self._profile_loaded(c, "Leere Konfiguration angelegt – alle Lüfter in Automatik"),
                      lambda e: self.toast(str(e)))
        self.confirm("Neue leere Konfiguration?",
                     "Alle Kurven, Steuerungen und eigenen Sensoren der aktiven Konfiguration werden verworfen "
                     "(gespeicherte Profile bleiben erhalten). Alle Lüfter gehen zurück in die Automatik.",
                     "Neu anlegen", do, destructive=True)

    def open_config(self):
        dialog = Gtk.FileDialog(title="Konfiguration öffnen", filters=self._json_filter())

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                cfg = self._read_file(file)
            except (OSError, ValueError) as e:
                self.toast(f"Datei ungültig: {e}")
                return
            cfg["profile"] = file.get_basename().rsplit(".", 1)[0]
            run_async(lambda: self.client.call("set_config", config=cfg),
                      lambda c: self._profile_loaded(c, f"„{file.get_basename()}“ geladen"),
                      lambda e: self.toast(str(e)))
        dialog.open(self, None, done)

    def save_config(self):
        if not self.config:
            return
        name = f"{self.config.get('profile') or 'fancontrol'}.json"
        dialog = Gtk.FileDialog(title="Konfiguration speichern", initial_name=name, filters=self._json_filter())

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return
            try:
                with open(file.get_path(), "w") as f:
                    json.dump(self.config, f, indent=2, ensure_ascii=False)
            except OSError as e:
                self.toast(f"Speichern fehlgeschlagen: {e}")
                return
            self.toast(f"Gespeichert: {file.get_path()}")
        dialog.save(self, None, done)

    def import_config(self):
        if not self.config:
            return
        dialog = Gtk.FileDialog(title="Aus Konfiguration importieren", filters=self._json_filter())

        def done(d, result):
            try:
                file = d.open_finish(result)
            except GLib.Error:
                return
            try:
                self._import_dialog(self._read_file(file))
            except (OSError, ValueError) as e:
                self.toast(f"Datei ungültig: {e}")
        dialog.open(self, None, done)

    def _import_dialog(self, other):
        """Pick curves, custom sensors and control settings from another configuration."""
        listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        choices = []
        for kind, items, label in (("curve", other["curves"], "Kurve"),
                                   ("sensor", other["custom_sensors"], "Eigener Sensor"),
                                   ("control", other["controls"], "Steuerung")):
            for item in items:
                title = item.get("name") or self.display_name(item["id"])
                row = Adw.ActionRow(title=title, subtitle=label)
                check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
                row.add_prefix(check)
                row.set_activatable_widget(check)
                listbox.append(row)
                choices.append((kind, item, check))
        if not choices:
            self.toast("Die Datei enthält nichts zum Importieren")
            return
        scroll = Gtk.ScrolledWindow(child=listbox, min_content_height=260, max_content_height=420,
                                    propagate_natural_height=True)
        dialog = Adw.AlertDialog(heading="Importieren",
                                 body="Kurven und Sensoren werden hinzugefügt, Steuerungen mit gleicher Hardware "
                                      "übernehmen die Einstellungen aus der Datei.", extra_child=scroll)
        dialog.add_response("cancel", "Abbrechen")
        dialog.add_response("ok", "Importieren")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def respond(_d, response):
            if response != "ok":
                return
            curve_ids = {c["id"] for c in self.config["curves"]}
            sensor_ids = {s["id"] for s in self.config["custom_sensors"]}
            count = 0
            for kind, item, check in choices:
                if not check.get_active():
                    continue
                item = copy.deepcopy(item)
                if kind == "curve":
                    if item["id"] in curve_ids:
                        item["name"] += " (importiert)"
                        item["id"] = cfgmod.new_id()
                    self.config["curves"].append(item)
                elif kind == "sensor":
                    if item["id"] in sensor_ids:
                        item["name"] += " (importiert)"
                        item["id"] = cfgmod.new_id()
                    self.config["custom_sensors"].append(item)
                else:
                    self.config["controls"] = [c for c in self.config["controls"] if c["id"] != item["id"]]
                    self.config["controls"].append(item)
                count += 1
            self.config_changed(rebuild={"controls", "curves", "sensors"})
            self.toast(f"{count} Einträge importiert")
        dialog.connect("response", respond)
        dialog.present(self)

    # --- profiles -------------------------------------------------------
    def _fill_profiles(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8, width_request=300)
        listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(Gtk.Label(label="Profile", css_classes=["heading"], halign=Gtk.Align.START))
        box.append(listbox)
        entry = Gtk.Entry(placeholder_text="Name für neues Profil", hexpand=True)
        save = Gtk.Button(label="Speichern", css_classes=["suggested-action"])
        row = Gtk.Box(spacing=6)
        row.append(entry)
        row.append(save)
        box.append(row)
        files = Gtk.Box(spacing=6, homogeneous=True)
        for label, callback in (("Datei öffnen …", self.open_config), ("Speichern unter …", self.save_config)):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.connect("clicked", lambda _b, cb=callback: (self.profile_popover.popdown(), cb()))
            files.append(b)
        box.append(files)
        self.profile_popover.set_child(box)

        def do_save(*_):
            name = entry.get_text().strip()
            if not name or self.config is None:
                return
            cfg = copy.deepcopy(self.config)

            def work():
                self.client.call("set_config", config=cfg)
                return self.client.call("save_profile", name=name)
            run_async(work, lambda c: self._profile_loaded(c, f"Profil „{name}“ gespeichert"),
                      lambda e: self.toast(str(e)))
            self.profile_popover.popdown()
            self.profiles_checked = 0
        save.connect("clicked", do_save)
        entry.connect("activate", do_save)

        def fill(data):
            if not data["profiles"]:
                listbox.append(Adw.ActionRow(title="Noch keine Profile gespeichert"))
            for name in data["profiles"]:
                r = Adw.ActionRow(title=name, activatable=True)
                if name == data["active"]:
                    r.add_prefix(Gtk.Image(icon_name="object-select-symbolic"))
                    entry.set_text(name)
                delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"],
                                    valign=Gtk.Align.CENTER, tooltip_text="Löschen")
                delete.connect("clicked", lambda _b, n=name: self._delete_profile(n))
                r.add_suffix(delete)
                r.connect("activated", lambda _r, n=name: self._load_profile(n))
                listbox.append(r)
        run_async(lambda: self.client.call("list_profiles"), fill, lambda e: listbox.append(
            Adw.ActionRow(title="Dienst nicht erreichbar", subtitle=str(e))))

    def _load_profile(self, name):
        self.profile_popover.popdown()
        if self.push_source:
            self._push()
        run_async(lambda: self.client.call("load_profile", name=name),
                  lambda c: self._profile_loaded(c, f"Profil „{name}“ geladen"), lambda e: self.toast(str(e)))

    def _profile_loaded(self, cfg, message):
        self.config = cfg
        self.rebuild()
        self.toast(message)
        self.profiles_checked = 0

    def _delete_profile(self, name):
        self.profile_popover.popdown()
        self.confirm(f"Profil „{name}“ löschen?", "Die aktive Konfiguration bleibt unverändert.", "Löschen",
                     lambda: run_async(lambda: self.client.call("delete_profile", name=name),
                                       lambda _: self.toast(f"Profil „{name}“ gelöscht"),
                                       lambda e: self.toast(str(e))), destructive=True)

    # --- appearance / window --------------------------------------------
    def apply_theme(self):
        self.theme.apply(self.prefs)
        for page in (self.controls_page, self.curves_page):
            page.queue_draw()

    def _flush(self):
        if self.push_source:
            GLib.source_remove(self.push_source)
            self.push_source = None
            try:
                self.client.call("set_config", config=self.config)
            except Exception as e:
                print(f"Konfiguration nicht gespeichert: {e}", file=sys.stderr)

    def _on_close(self, *_):
        self._flush()
        if self.tray:
            self.set_visible(False)
            return True
        return False


class FanControlApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.start_hidden = False
        self.add_main_option("hidden", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Nur im Tray starten, ohne Fenster", None)

    def do_handle_local_options(self, options):
        self.start_hidden = options.contains("hidden")
        return -1

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = MainWindow(self)
            if self.start_hidden and win.tray:
                return
        win.present_window()


def main():
    return FanControlApp().run(sys.argv)
