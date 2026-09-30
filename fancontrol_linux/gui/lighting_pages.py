"""Settings → LED devices (manage, search and install plugins) and the Light section (one card per device)."""

import base64

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..ipc import Client  # noqa: E402
from . import common as ui  # noqa: E402
from .util import run_async  # noqa: E402

GUIDE_URL = "https://github.com/SloppyHenry/LiFaCo-plugins/blob/main/docs/plugin-guide.md"
LIGHT_TEXT = ("Control the RGB LEDs of your mainboard, graphics card, fans, coolers and LED strips. Lighting is "
              "provided by plugins: switch on the ones you need in <b>Settings → LED devices</b>, or search the "
              "plugin catalog there.")
STATUS_TEXT = {"running": "Running", "starting": "Starting …", "stopped": "Off", "error": "Problem",
               "needs_approval": "Needs your approval"}
EFFECTS = (("static", "Static colour"), ("breathing", "Breathing"), ("rainbow", "Rainbow"),
           ("temperature", "Follows temperature"), ("off", "Off"))


def _clear(group, rows):
    for row in rows:
        group.remove(row)
    rows.clear()


class LedPage(Adw.Bin):
    """Settings → LED devices."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.slow = Client(path=win.client.path, timeout=180)     # installs download and unpack: they take a while
        self.plugins = []
        self.plugin_rows, self.catalog_rows = [], []
        self.query = ""
        self.timer = None
        self.open_rows = set()

        page = Adw.PreferencesPage()
        self.installed_group = Adw.PreferencesGroup(
            title="Plugins",
            description="Each plugin controls one kind of lighting hardware. Plugins run in their own process "
                        "without administrator rights and can only do what you allow here.")
        page.add(self.installed_group)

        search_group = Adw.PreferencesGroup(
            title="Find plugins", description="Search the plugin catalog and install what fits your hardware.")
        self.search = Gtk.SearchEntry(placeholder_text="Search, e.g. WLED, keyboard, Corsair, RAM …", hexpand=True)
        self.search.connect("search-changed", lambda e: self._search(e.get_text()))
        search_group.add(self.search)
        page.add(search_group)
        self.find_group = Adw.PreferencesGroup()     # the results, below the search field
        page.add(self.find_group)

        own = Adw.PreferencesGroup(title="Your own plugins",
                                   description="A plugin is a folder with two files. Writing one takes a few minutes.")
        add = Adw.ActionRow(title="Add a plugin from a file", subtitle="A .zip package, for example one you built yourself")
        button = Gtk.Button(label="Choose file …", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self._choose_file())
        add.add_suffix(button)
        own.add(add)
        guide = Adw.ActionRow(title="How to write a plugin",
                              subtitle="Guide with a template: create one with  lifacoctl plugin new my-plugin")
        guide_button = Gtk.Button(label="Open guide", valign=Gtk.Align.CENTER)
        guide_button.connect("clicked", lambda *_: Gtk.UriLauncher(uri=GUIDE_URL).launch(win, None, None))
        guide.add_suffix(guide_button)
        own.add(guide)
        page.add(own)
        self.set_child(page)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # --- refresh ----------------------------------------------------------------------
    def _on_map(self, *_):
        self.refresh()
        self._search(self.query)
        self.timer = GLib.timeout_add_seconds(3, self._tick)

    def _on_unmap(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def _tick(self):
        if any(p.get("status") == "starting" for p in self.plugins):
            self.refresh()
        return True

    def refresh(self, plugins=None):
        if plugins is not None:
            self._show_plugins(plugins)
            return
        run_async(lambda: self.win.client.call("plugin_list"), self._show_plugins, self._poll_error)

    def _poll_error(self, error):
        pass    # the main window already shows that the service is unreachable

    def _fail(self, error):
        self.win.toast(str(error))
        self.refresh()

    # --- installed plugins ------------------------------------------------------------
    def _signature(self, plugins):
        return [(p["id"], p.get("status"), p.get("enabled"), p.get("version"), p.get("devices")) for p in plugins]

    def _show_plugins(self, plugins):
        if self._signature(plugins) == self._signature(self.plugins) and self.plugin_rows:
            return
        self.plugins = plugins
        _clear(self.installed_group, self.plugin_rows)
        if not plugins:
            row = Adw.ActionRow(title="No plugins installed yet", subtitle="Find one below, for example WLED or OpenRGB")
            self.installed_group.add(row)
            self.plugin_rows.append(row)
        for p in plugins:
            row = (self._broken_row(p) if p.get("broken") else self._builtin_row(p) if p.get("builtin")
                   else self._plugin_row(p))
            self.installed_group.add(row)
            self.plugin_rows.append(row)

    def _broken_row(self, p):
        return Adw.ActionRow(title=p["name"], subtitle=f"Cannot be loaded: {p['error']}", css_classes=["error"])

    def _builtin_row(self, p):
        return Adw.ActionRow(title=p["name"], subtitle=GLib.markup_escape_text(
            f"Built in · {p['devices']} devices · " + p["description"]), subtitle_lines=0)

    def _plugin_row(self, p):
        status = STATUS_TEXT.get(p["status"], p["status"])
        row = Adw.ExpanderRow(title=p["name"], subtitle=f"{p['version']} · {status}"
                              + (f" · {p['devices']} devices" if p["status"] == "running" else ""))
        row.set_expanded(p["id"] in self.open_rows)
        row.connect("notify::expanded", lambda r, _p, pid=p["id"]: (self.open_rows.add(pid) if r.get_expanded()
                                                                    else self.open_rows.discard(pid)))
        switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=p["enabled"] and not p["needs_approval"])
        switch.connect("notify::active", lambda s, _p: self._toggle(p, s))
        row.add_suffix(switch)

        about = Adw.ActionRow(title="About", subtitle=GLib.markup_escape_text(
            " · ".join(x for x in (p["description"], f"by {p['author']}" if p["author"] else "", p["license"]) if x)),
            subtitle_lines=0)
        row.add_row(about)
        allowed = Adw.ActionRow(title="This plugin may", subtitle=GLib.markup_escape_text("\n".join(
            "• " + line for line in p["permission_lines"])), subtitle_lines=0)
        row.add_row(allowed)
        if p["status"] == "error" and p["error"]:
            row.add_row(Adw.ActionRow(title="Problem", subtitle=GLib.markup_escape_text(p["error"]), subtitle_lines=0,
                                      css_classes=["error"]))
        if p["missing_commands"]:
            row.add_row(Adw.ActionRow(title="Missing program", css_classes=["warning"], subtitle_lines=0,
                                      subtitle="Needs: " + ", ".join(p["missing_commands"])))

        values = dict(p["settings"])
        for s in p["settings_schema"]:
            row.add_row(self._setting_row(s, values))
        actions = Adw.ActionRow(title="")
        if p["settings_schema"]:
            save = Gtk.Button(label="Save settings", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            save.connect("clicked", lambda *_: run_async(
                lambda: self.win.client.call("plugin_settings", id=p["id"], settings=values),
                lambda plugins: (self.win.toast("Settings saved"), self.refresh(plugins)), self._fail))
            actions.add_suffix(save)
        if p["enabled"] and not p["needs_approval"]:
            restart = Gtk.Button(label="Restart", valign=Gtk.Align.CENTER)
            restart.connect("clicked", lambda *_: run_async(
                lambda: self.win.client.call("plugin_restart", id=p["id"]), self.refresh, self._fail))
            actions.add_suffix(restart)
        remove = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        remove.connect("clicked", lambda *_: self.win.confirm(
            f"Remove {p['name']}?", "The plugin and its stored data are deleted. Lighting settings for its devices "
            "are forgotten.", "Remove", lambda: run_async(
                lambda: self.win.client.call("plugin_remove", id=p["id"]), self._removed, self._fail), destructive=True))
        actions.add_suffix(remove)
        row.add_row(actions)
        return row

    def _removed(self, plugins):
        self.refresh(plugins)
        self._search(self.query)

    def _setting_row(self, s, values):
        key, value = s["key"], values.get(s["key"], s["default"])
        subtitle = s.get("help") or None
        if s["type"] == "switch":
            row = Adw.SwitchRow(title=s["label"], subtitle=subtitle or "", active=bool(value))
            row.connect("notify::active", lambda r, _p: values.__setitem__(key, r.get_active()))
        elif s["type"] == "number":
            adj = Gtk.Adjustment(lower=s["min"], upper=s["max"], step_increment=s["step"], page_increment=s["step"] * 10,
                                 value=float(value))
            row = Adw.SpinRow(title=s["label"], subtitle=subtitle or "", adjustment=adj)
            row.connect("notify::value", lambda r, _p: values.__setitem__(key, r.get_value()))
        elif s["type"] == "choice":
            row = Adw.ComboRow(title=s["label"], subtitle=subtitle or "", model=Gtk.StringList.new(s["choices"]))
            row.set_selected(max(0, s["choices"].index(value)) if value in s["choices"] else 0)
            row.connect("notify::selected", lambda r, _p: values.__setitem__(key, s["choices"][r.get_selected()]))
        else:
            row = Adw.PasswordEntryRow(title=s["label"]) if s["type"] == "password" else Adw.EntryRow(title=s["label"])
            row.set_text(str(value))
            row.connect("changed", lambda r: values.__setitem__(key, r.get_text()))
        return row

    def _toggle(self, p, switch):
        active = switch.get_active()
        if active == (p["enabled"] and not p["needs_approval"]):
            return
        if active and p["needs_approval"]:
            self._ask_permission(p)
        else:
            run_async(lambda: self.win.client.call("plugin_enable", id=p["id"], enabled=active),
                      self.refresh, self._fail)

    def _ask_permission(self, p):
        dialog = Adw.AlertDialog(heading=f"Allow {p['name']}?",
                                 body="This plugin asks for permission to:\n\n"
                                      + "\n".join("• " + line for line in p["permission_lines"])
                                      + "\n\nOnly allow what fits what the plugin is supposed to do.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("allow", "Allow and switch on")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def answered(_d, response):
            if response == "allow":
                run_async(lambda: self.win.client.call("plugin_enable", id=p["id"], enabled=True, approve=True),
                          self.refresh, self._fail)
            else:
                self.plugins = []      # forces the switch back to its real state
                self.refresh()
        dialog.connect("response", answered)
        dialog.present(self.win)

    # --- catalog ----------------------------------------------------------------------
    def _search(self, query):
        self.query = query
        run_async(lambda: self.win.client.call("plugin_catalog", query=query),
                  lambda data: self._show_catalog(query, data), lambda e: self._show_catalog(query, None, str(e)))

    def _show_catalog(self, query, data, error=None):
        if query != self.query:
            return
        _clear(self.find_group, self.catalog_rows)
        if error:
            row = Adw.ActionRow(title="The catalog cannot be reached", subtitle=GLib.markup_escape_text(error),
                                subtitle_lines=0)
            retry = Gtk.Button(label="Try again", valign=Gtk.Align.CENTER)
            retry.connect("clicked", lambda *_: run_async(
                lambda: self.win.client.call("plugin_catalog", query=query, refresh=True),
                lambda d: self._show_catalog(query, d), lambda e: self._show_catalog(query, None, str(e))))
            row.add_suffix(retry)
            self.find_group.add(row)
            self.catalog_rows.append(row)
            return
        if not data["plugins"]:
            row = Adw.ActionRow(title="Nothing found", subtitle="Try another word, or clear the search to see everything")
            self.find_group.add(row)
            self.catalog_rows.append(row)
        for e in data["plugins"]:
            info = e["description"] + ("\n" + ", ".join(e["tags"][:6]) if e["tags"] else "")
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"{e['name']}  {e['version']}"),
                                subtitle=GLib.markup_escape_text(info), subtitle_lines=3)
            if e["installed"] and not e["update"]:
                label = Gtk.Label(label="Installed", css_classes=["dim-label"], valign=Gtk.Align.CENTER)
                row.add_suffix(label)
            else:
                button = Gtk.Button(label="Update" if e["update"] else "Install", valign=Gtk.Align.CENTER,
                                    css_classes=["suggested-action"])
                button.connect("clicked", lambda b, pid=e["id"], name=e["name"]: self._install(b, pid, name))
                row.add_suffix(button)
            self.find_group.add(row)
            self.catalog_rows.append(row)

    def _install(self, button, pid, name):
        button.set_sensitive(False)
        button.set_label("Installing …")

        def done(plugins):
            self.win.toast(f"{name} installed – switch it on under Plugins")
            self.plugins = []
            self.refresh(plugins)
            self._search(self.query)
        run_async(lambda: self.slow.call("plugin_install", id=pid), done, lambda e: (self._fail(e), self._search(self.query)))

    # --- upload -----------------------------------------------------------------------
    def _choose_file(self):
        dialog = Gtk.FileDialog(title="Choose a plugin package")
        zips = Gtk.FileFilter(name="Plugin packages (*.zip)")
        zips.add_pattern("*.zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(zips)
        dialog.set_filters(filters)
        dialog.open(self.win, None, self._file_chosen)

    def _file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            ok, data, _etag = file.load_contents(None)
        except GLib.Error:
            return
        if not ok:
            return
        payload = base64.b64encode(bytes(data)).decode()
        self.win.confirm(
            "Install this plugin?",
            f"{file.get_basename()} was not checked by anyone. A plugin can control your lighting hardware and, "
            "if you allow it, use the network. Only install plugins from people you trust. You will be asked again "
            "before it is switched on.", "Install",
            lambda: run_async(lambda: self.slow.call("plugin_install_file", data=payload),
                              lambda plugins: (self.win.toast("Plugin installed – switch it on under Plugins"),
                                               self.refresh(plugins)), self._fail))


def _rgba(color):
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = color[0] / 255.0, color[1] / 255.0, color[2] / 255.0, 1.0
    return rgba


def _rgb(rgba):
    return [round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)]


class DeviceCard(Gtk.Box):
    """Effect controls for one LED device. The card is the source of truth while the user edits."""

    def __init__(self, win, device):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=ui.CARD_WIDTH,
                         valign=Gtk.Align.START, css_classes=["card", "fc-card"])
        self.win, self.device = win, device
        self.modes = {m["name"]: m for m in device["modes"]}
        saved = device.get("effect") or {}
        self.pending = None

        head = Gtk.Box(spacing=8)
        head.append(ui.icon_bubble("fc-light-symbolic"))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label=device["name"], xalign=0, ellipsize=3, css_classes=["fc-card-title"]))
        names.append(ui.caption(f"{device['plugin_name']} · {device['leds']} LEDs"))
        head.append(names)
        identify = Gtk.Button(icon_name="find-location-symbolic", css_classes=["flat", "circular"],
                              valign=Gtk.Align.CENTER, tooltip_text="Blink this device to find it",
                              sensitive=device["direct"])
        identify.connect("clicked", lambda *_: run_async(
            lambda: win.client.call("light_identify", device=device["key"]), None, lambda e: win.toast(str(e))))
        head.append(identify)
        self.append(head)

        # Effect choice. Keys: release, a software effect, or "hw:<mode name>".
        self.choices = [("release", "Not controlled")]
        if device["direct"]:
            self.choices += list(EFFECTS)
        self.choices += [(f"hw:{m['name']}", f"{m['name']}  (device)") for m in device["modes"]]
        self.dropdown = Gtk.DropDown.new_from_strings([label for _k, label in self.choices])
        self.dropdown.set_enable_search(len(self.choices) > 12)
        if len(self.choices) > 12:
            self.dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.append(self.dropdown)

        self.colors = [Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
                       for _ in range(3)]
        for i, b in enumerate(self.colors):
            saved_colors = saved.get("colors") or ([saved["color"]] if saved.get("color") else [])
            b.set_rgba(_rgba(saved_colors[i] if i < len(saved_colors) else [(255, 80, 0), (0, 120, 255), (0, 220, 90)][i]))
            b.connect("notify::rgba", lambda *_: self._changed())
        self.color_row = Gtk.Box(spacing=8)
        self.color_row.append(ui.caption("Colour"))
        for b in self.colors:
            self.color_row.append(b)
        self.append(self.color_row)

        self.brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.brightness.set_value(saved.get("brightness", 100))
        self.speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.speed.set_value(saved.get("speed", 50))
        self.brightness_box = ui.labeled("Brightness", self.brightness)
        self.speed_box = ui.labeled("Speed", self.speed)
        for s in (self.brightness, self.speed):
            s.connect("value-changed", lambda *_: self._changed())
        self.append(self.brightness_box)
        self.append(self.speed_box)

        temps = win.status["temps"] if win.status else {}
        self.sensor_ids = [""] + list(temps)
        self.sensor = Gtk.DropDown.new_from_strings(["Hottest sensor"] + [win.display_name(s) for s in temps])
        try:
            self.sensor.set_selected(self.sensor_ids.index(saved.get("sensor", "")))
        except ValueError:
            pass
        self.sensor.connect("notify::selected", lambda *_: self._changed())
        self.sensor_box = ui.labeled("Temperature source (blue when cool, red when hot)", self.sensor)
        self.append(self.sensor_box)

        self.error = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, css_classes=["error"], visible=False)
        self.append(self.error)

        key = "release"
        if saved.get("type") == "hardware":
            key = f"hw:{saved['mode']}"
        elif saved.get("type"):
            key = saved["type"]
        keys = [k for k, _l in self.choices]
        self.dropdown.set_selected(keys.index(key) if key in keys else 0)
        self.dropdown.connect("notify::selected", lambda *_: self._changed())
        self._layout()
        self.set_error(device.get("error"))

    def set_error(self, text):
        self.error.set_visible(bool(text))
        self.error.set_label(text or "")

    def _kind(self):
        return self.choices[self.dropdown.get_selected()][0]

    def _layout(self):
        kind = self._kind()
        mode = self.modes.get(kind[3:]) if kind.startswith("hw:") else None
        colors = 1 if kind in ("static", "breathing") else (mode["colors"] if mode else 0)
        for i, b in enumerate(self.colors):
            b.set_visible(i < colors)
        self.color_row.set_visible(colors > 0)
        self.brightness_box.set_visible(kind in ("static", "breathing", "rainbow", "temperature")
                                        or bool(mode and mode["brightness"]))
        self.speed_box.set_visible(kind in ("breathing", "rainbow") or bool(mode and mode["speed"]))
        self.sensor_box.set_visible(kind == "temperature")

    def effect(self):
        kind = self._kind()
        b, s = self.brightness.get_value(), self.speed.get_value()
        if kind == "release":
            return None
        if kind == "off":
            return {"type": "off"}
        if kind == "static":
            return {"type": "static", "color": _rgb(self.colors[0].get_rgba()), "brightness": b}
        if kind == "breathing":
            return {"type": "breathing", "color": _rgb(self.colors[0].get_rgba()), "speed": s, "brightness": b}
        if kind == "rainbow":
            return {"type": "rainbow", "speed": s, "brightness": b}
        if kind == "temperature":
            return {"type": "temperature", "sensor": self.sensor_ids[self.sensor.get_selected()], "brightness": b}
        mode = self.modes[kind[3:]]
        return {"type": "hardware", "mode": mode["name"], "speed": s, "brightness": b,
                "colors": [_rgb(self.colors[i].get_rgba()) for i in range(min(3, mode["colors"]))]}

    def _changed(self):
        self._layout()
        if self.pending:
            GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(250, self._send)

    def _send(self):
        self.pending = None
        effect = self.effect()
        run_async(lambda: self.win.client.call("light_set", device=self.device["key"], effect=effect),
                  lambda _d: self.set_error(""), lambda e: self.set_error(str(e)))
        return False


class LightSection(Gtk.Box):
    """The Light section of the overview."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.signature = None
        self.cards = {}
        self.timer = None
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self._show_empty(None)

    def _on_map(self, *_):
        self.refresh()
        if not self.timer:
            self.timer = GLib.timeout_add_seconds(3, lambda: (self.refresh(), True)[1])

    def _on_unmap(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def refresh(self):
        if not self.win.connected:
            return

        def fetch():
            return self.win.client.call("light_devices"), self.win.client.call("plugin_list")
        run_async(fetch, self._show, lambda e: None)

    def _show(self, result):
        devices, plugins = result
        signature = (tuple((d["key"], d["name"], d["leds"], len(d["modes"]), d["direct"]) for d in devices),
                     any(p.get("enabled") for p in plugins))
        if signature == self.signature:
            for d in devices:                      # only refresh error texts, never the controls being edited
                card = self.cards.get(d["key"])
                if card:
                    card.set_error(d.get("error"))
            return
        self.signature = signature
        child = self.get_first_child()
        while child:
            self.remove(child)
            child = self.get_first_child()
        self.cards = {}
        if not devices:
            self._show_empty(plugins)
            return
        flow = ui.CardFlow()
        for d in devices:
            card = DeviceCard(self.win, d)
            self.cards[d["key"]] = card
            flow.append(card)
        self.append(flow)

    def _show_empty(self, plugins):
        if plugins and any(p.get("enabled") for p in plugins):
            title, text = "No LED devices found", ("The enabled plugins did not find any devices. Check their settings "
                                                   "under <b>Settings → LED devices</b>.")
        else:
            title, text = "No lighting plugin is switched on", LIGHT_TEXT
        self.append(ui.notice(title, "fc-light-symbolic", text, ("Set up LED devices", lambda: self.win.navigate("leds"))))
