import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk  # noqa: E402

from .. import APP_ID, __version__  # noqa: E402
from . import theme as thememod  # noqa: E402
from .util import combo_row, spin_row, switch_row, temp_spin_row  # noqa: E402

AUTOSTART_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                              "autostart", f"{APP_ID}.desktop")
TRAY_DEFAULT_COLOR = "#ffffff"


def _hex(rgba):
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue)))


def _rgba(color):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba


class Swatch(Gtk.Widget):
    def __init__(self, colors):
        super().__init__(valign=Gtk.Align.CENTER)
        self.colors = colors
        self.set_size_request(22 * len(colors) + 4 * (len(colors) - 1), 22)

    def do_snapshot(self, snap):
        for i, color in enumerate(self.colors):
            rect = Graphene.Rect().init(i * 26, 0, 22, 22)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, 11)
            snap.push_rounded_clip(rounded)
            snap.append_color(_rgba(color), rect)
            snap.pop()


def _color_button(color, on_change):
    button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), rgba=_rgba(color),
                                   valign=Gtk.Align.CENTER)
    button.connect("notify::rgba", lambda b, _p: on_change(_hex(b.get_rgba())))
    return button


class DesignPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.build()

    def build(self):
        prefs = self.win.prefs
        page = Adw.PreferencesPage()

        general = Adw.PreferencesGroup(title="General")
        keys = [k for k, _n, _s in thememod.SCHEMES]
        current = prefs.get("theme")

        def set_scheme(i):
            prefs.set("theme", keys[i])
            self.win.apply_theme()
        general.add(combo_row("Light / dark", [n for _k, n, _s in thememod.SCHEMES],
                              keys.index(current) if current in keys else 0, set_scheme))
        general.add(combo_row("Temperature unit", ["Celsius (°C)", "Fahrenheit (°F)"],
                              1 if prefs.get("fahrenheit") else 0, self.win.set_fahrenheit))
        page.add(general)

        palettes = Adw.PreferencesGroup(title="Colour theme", description="Accent colour, cards and header bar")
        active = prefs.get("palette") or "classic"
        first = None
        custom = dict(thememod.DEFAULT_CUSTOM, **(prefs.get("custom_colors") or {}))
        for key, (name, accent, card, header) in thememod.PALETTES.items():
            row = Adw.ActionRow(title=name)
            check = Gtk.CheckButton(active=key == active, valign=Gtk.Align.CENTER)
            if first is None:
                first = check
            else:
                check.set_group(first)
            check.connect("toggled", lambda c, k=key: c.get_active() and self._set_palette(k))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if key == "custom":
                row.add_suffix(Swatch([custom["accent"], custom["card"], custom["header"]]))
            elif accent:
                row.add_suffix(Swatch([accent, card, header]))
            else:
                row.set_subtitle("System colours")
            palettes.add(row)
        page.add(palettes)

        custom_group = Adw.PreferencesGroup(title="Custom colours",
                                            description="Changing a colour here switches to “Custom colours”")
        for key, title in (("accent", "Accent colour"), ("card", "Cards"), ("header", "Header bar")):
            row = Adw.ActionRow(title=title)
            row.add_suffix(_color_button(custom[key], lambda color, k=key: self._set_custom(k, color)))
            custom_group.add(row)
        reset = Gtk.Button(label="Reset", css_classes=["flat"], valign=Gtk.Align.CENTER)
        reset.connect("clicked", lambda *_: self._reset_custom())
        custom_group.set_header_suffix(reset)
        page.add(custom_group)
        self.set_child(page)

    def _reset_custom(self):
        self.win.prefs.set("custom_colors", {})
        self.win.apply_theme()
        self.build()

    def _set_palette(self, key):
        self.win.prefs.set("palette", key)
        self.win.apply_theme()

    def _set_custom(self, key, color):
        colors = dict(self.win.prefs.get("custom_colors") or {})
        colors[key] = color
        self.win.prefs.set("custom_colors", colors)
        was_custom = self.win.prefs.get("palette") == "custom"
        self.win.prefs.set("palette", "custom")
        self.win.apply_theme()
        if not was_custom:
            self.build()


class TrayPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def rebuild(self):
        win, prefs = self.win, self.win.prefs
        page = Adw.PreferencesPage()
        general = Adw.PreferencesGroup(
            title="Tray", description="Icons in the system tray of the panel (KDE, Xfce, Cinnamon, "
                                      "GNOME with the AppIndicator extension …)")
        general.add(switch_row("Show tray icon", prefs.get("tray_enabled"), win.set_tray_enabled,
                               subtitle="Closing the window keeps Linux FanControl running in the tray"))
        general.add(switch_row("Start in the tray at login", os.path.exists(AUTOSTART_FILE), self._set_autostart,
                               subtitle="The service controls the fans anyway – this only affects the user interface"))
        page.add(general)

        icons = Adw.PreferencesGroup(title="Values as tray icons",
                                     description="Every selected sensor gets its own icon showing its value")
        entries = {e["id"]: e for e in prefs.get("tray_sensors") or []}
        st = win.status or {"temps": {}, "pwms": {}, "fans": {}}
        items = [(sid, "temps") for sid in st["temps"]] + [(sid, "pwms") for sid in st["pwms"]] \
            + [(sid, "fans") for sid in st["fans"]]
        if not items:
            icons.add(Adw.ActionRow(title="No sensors (service not connected?)"))
        for sid, kind in items:
            unit = {"temps": "Temperature", "pwms": "Speed %", "fans": "RPM"}[kind]
            row = Adw.ActionRow(title=win.display_name(sid), subtitle=unit)
            entry = entries.get(sid)
            row.add_suffix(_color_button(entry["color"] if entry else TRAY_DEFAULT_COLOR,
                                         lambda color, s=sid: self._set_color(s, color)))
            switch = Gtk.Switch(active=entry is not None, valign=Gtk.Align.CENTER)
            switch.connect("notify::active", lambda sw, _p, s=sid: self._toggle(s, sw.get_active()))
            row.add_suffix(switch)
            icons.add(row)
        page.add(icons)
        self.set_child(page)

    def _entries(self):
        return [dict(e) for e in self.win.prefs.get("tray_sensors") or []]

    def _toggle(self, sid, active):
        entries = [e for e in self._entries() if e["id"] != sid]
        if active:
            entries.append({"id": sid, "color": TRAY_DEFAULT_COLOR})
        self.win.prefs.set("tray_sensors", entries)
        self.win.update_tray()

    def _set_color(self, sid, color):
        entries = self._entries()
        for e in entries:
            if e["id"] == sid:
                e["color"] = color
        self.win.prefs.set("tray_sensors", entries)
        self.win.update_tray(force=True)

    def _set_autostart(self, enabled):
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
            with open(AUTOSTART_FILE, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Linux FanControl (tray)\n"
                        f"Exec=fancontrol-linux --hidden\nIcon={APP_ID}\nX-GNOME-Autostart-enabled=true\n")
            if not self.win.prefs.get("tray_enabled"):
                self.win.set_tray_enabled(True)
                self.rebuild()
        else:
            try:
                os.unlink(AUTOSTART_FILE)
            except FileNotFoundError:
                pass


class SettingsPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def rebuild(self):
        win = self.win
        page = Adw.PreferencesPage()
        if win.config:
            settings = win.config["settings"]
            group = Adw.PreferencesGroup(title="Control", description="Applies to the background service")

            def set_setting(key):
                def apply(value):
                    settings[key] = value
                    win.config_changed()
                return apply
            group.add(spin_row("Update interval s", settings["interval"], 0.2, 10, 0.1,
                               set_setting("interval"), digits=1))
            group.add(temp_spin_row("Safety temperature", settings["safety_temp"], 0, 150,
                                    set_setting("safety_temp"),
                                    subtitle="If a sensor in use reaches this value, all fans run at 100 % (0 = off)"))
            page.add(group)

        support = Adw.PreferencesGroup(
            title="Hardware support",
            description="Which device groups were detected and what may still be missing")
        icons = {"ok": "object-select-symbolic", "warn": "dialog-warning-symbolic", "off": "list-remove-symbolic"}
        for row_info in (win.status or {}).get("support", []):
            row = Adw.ActionRow(title=row_info["name"], subtitle=row_info["detail"], subtitle_lines=3)
            row.add_prefix(Gtk.Image(icon_name=icons.get(row_info["state"], "dialog-information-symbolic")))
            if row_info.get("hint"):
                row.set_tooltip_text(row_info["hint"])
                row.set_subtitle(f"{row_info['detail']}\n→ {row_info['hint']}")
            support.add(row)
        if win.config:
            settings = win.config["settings"]

            def set_backend(key):
                def apply(value):
                    settings[key] = value
                    win.config_changed()
                return apply
            support.add(switch_row("Use liquidctl", settings.get("liquidctl", True), set_backend("liquidctl"),
                                   subtitle="AIO liquid coolers and smart hubs (NZXT, Corsair, ASUS, MSI …)"))
            support.add(switch_row("Thermaltake controllers (experimental)", settings.get("thermaltake", False),
                                   set_backend("thermaltake"),
                                   subtitle="Riing/G3 controllers over USB – untested, use at your own risk"))
        page.add(support)

        view = Adw.PreferencesGroup(title="View")
        view.add(switch_row("Show hidden cards", win.prefs.get("show_hidden"), win.set_show_hidden))
        page.add(view)

        hw = Adw.PreferencesGroup(title="Hardware and configuration")
        for title, subtitle, label, callback in (
                ("Rescan hardware", "After loading new drivers or plugging in devices",
                 "Rescan", win.rescan),
                ("Setup assistant", "Create default curves and assign them to all fans",
                 "Start", win.setup_wizard),
                ("Keyboard shortcuts", "Overview of all shortcuts", "Show", win.show_shortcuts)):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_a, cb=callback: cb())
            row.add_suffix(button)
            hw.add(row)
        page.add(hw)

        service = Adw.PreferencesGroup(title="Service")
        state = "connected" if win.connected else "unreachable"
        service.add(Adw.ActionRow(title="fancontrol-linux.service", subtitle=f"Status: {state}"))
        page.add(service)
        self.set_child(page)


class AboutPage(Adw.Bin):
    def __init__(self, win):
        super().__init__()
        page = Adw.StatusPage(icon_name=APP_ID, title="Linux FanControl",
                              description=f"Version {__version__}\n\nFan control for Linux via hwmon, NVML and liquidctl – "
                                          "curves, custom sensors, profiles, calibration, tray and background service.\n"
                                          "Inspired by FanControl for Windows, developed independently.")
        button = Gtk.Button(label="License and details", halign=Gtk.Align.CENTER, css_classes=["pill"])
        button.connect("clicked", lambda *_: Adw.AboutDialog(
            application_name="Linux FanControl", application_icon=APP_ID, version=__version__,
            comments="Fan control for Linux via hwmon, NVML and liquidctl.", license_type=Gtk.License.MIT_X11).present(win))
        page.set_child(button)
        self.set_child(page)
