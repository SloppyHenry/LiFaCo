"""Settings → LED devices (manage, search and install plugins) and the Light section (one card per device)."""

import base64

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from ..ipc import Client  # noqa: E402
from . import common as ui  # noqa: E402
from .util import c_to_disp, disp_to_c, run_async, temp_unit  # noqa: E402

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
        behaviour = Adw.PreferencesGroup(title="Behaviour")
        self.off_row = Adw.SwitchRow(title="Turn lights off when LiFaCo stops",
                                     subtitle="Otherwise the devices keep their last colours")
        self.off_row.connect("notify::active", self._off_on_exit_changed)
        self.loading_settings = True
        behaviour.add(self.off_row)
        page.add(behaviour)
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
    def _off_on_exit_changed(self, row, _p):
        if not self.loading_settings:
            active = row.get_active()
            run_async(lambda: self.win.client.call("light_settings", off_on_exit=active), None,
                      lambda e: self.win.toast(str(e)))

    def _load_settings(self, settings):
        self.loading_settings = True
        self.off_row.set_active(bool(settings.get("off_on_exit")))
        self.loading_settings = False

    def _on_map(self, *_):
        self.refresh()
        run_async(lambda: self.win.client.call("light_settings"), self._load_settings, self._poll_error)
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


TYPE_ICONS = {"mainboard": "computer-symbolic", "gpu": "fc-gpu-symbolic", "ram": "media-flash-symbolic",
              "keyboard": "input-keyboard-symbolic", "mouse": "input-mouse-symbolic",
              "headset": "audio-headphones-symbolic", "cooler": "fc-fan-symbolic", "fan": "fc-fan-symbolic",
              "strip": "fc-light-symbolic", "case": "computer-symbolic", "other": "fc-light-symbolic"}
RAINBOW = [(255, 0, 0), (255, 200, 0), (0, 220, 60), (0, 200, 255), (60, 80, 255), (200, 0, 255), (255, 0, 120)]


def preview_stops(effect):
    """Colours (evenly spaced) that stand for an effect in the small preview bar; None = not controlled."""
    if not effect:
        return None
    kind = effect["type"]
    bright = effect.get("brightness", 100) / 100.0

    def dim(c):
        return tuple(int(v * bright) for v in c)
    if kind == "off":
        return [(30, 34, 46)]
    if kind in ("static", "breathing"):
        return [dim(effect["color"])]
    if kind == "rainbow":
        return [dim(c) for c in RAINBOW]
    if kind == "temperature":
        return [dim(c) for _t, c in effect.get("stops") or [[0, (0, 90, 255)], [1, (0, 220, 90)], [2, (255, 170, 0)],
                                                            [3, (255, 30, 0)]]]
    colors = effect.get("colors") or []
    return [dim(c) for c in colors] if colors else [dim(c) for c in RAINBOW]


class GradientBar(Gtk.Widget):
    """A thin rounded bar showing the colours of an effect (a solid colour, a rainbow, a temperature gradient …)."""

    HEIGHT = 7

    def __init__(self):
        super().__init__(hexpand=True, valign=Gtk.Align.CENTER)
        self.set_size_request(40, self.HEIGHT)
        self.stops = None

    def set_effect(self, effect):
        self.stops = preview_stops(effect)
        self.queue_draw()

    def do_measure(self, orientation, _for_size):
        size = self.HEIGHT if orientation == Gtk.Orientation.VERTICAL else 40
        return size, size, -1, -1

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.HEIGHT
        rect = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, height / 2)
        snap.push_rounded_clip(rounded)
        if not self.stops:
            snap.append_color(Gdk.RGBA(red=0.5, green=0.55, blue=0.65, alpha=0.25), rect)
        elif len(self.stops) == 1:
            snap.append_color(_rgba(self.stops[0]), rect)
        else:
            stops = []
            for i, c in enumerate(self.stops):
                stop = Gsk.ColorStop()
                stop.offset = i / (len(self.stops) - 1)
                stop.color = _rgba(c)
                stops.append(stop)
            snap.append_linear_gradient(rect, Graphene.Point().init(0, 0), Graphene.Point().init(width, 0), stops)
        snap.pop()


class StopsDialog:
    """Edit the colour gradient of a 'follows temperature / fan speed' effect."""

    def __init__(self, win, stops, is_fan, on_done):
        self.win, self.is_fan, self.on_done = win, is_fan, on_done
        self.stops = [[t, list(c)] for t, c in stops]
        self.dialog = Adw.Dialog(title="Colour gradient", content_width=440)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=16,
                      margin_start=16, margin_end=16)
        self.preview = GradientBar()
        box.append(self.preview)
        self.group = Adw.PreferencesGroup(
            description="Below the first point the first colour is shown, above the last point the last colour. "
                        "Between points the colour blends smoothly.")
        box.append(self.group)
        self.rows = []
        add = Gtk.Button(label="Add a point", halign=Gtk.Align.START)
        add.connect("clicked", lambda *_: self._add())
        box.append(add)
        done = Gtk.Button(label="Done", halign=Gtk.Align.END, css_classes=["suggested-action", "pill"])
        done.connect("clicked", lambda *_: self._finish())
        box.append(done)
        self.dialog.set_child(box)
        self._fill()

    def _fill(self):
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        self.stops.sort(key=lambda s: s[0])
        for i, (t, color) in enumerate(self.stops):
            unit = "%" if self.is_fan else temp_unit()
            shown = t if self.is_fan else c_to_disp(t)
            adj = Gtk.Adjustment(lower=0 if self.is_fan else -20, upper=100 if self.is_fan else 250, step_increment=1,
                                 page_increment=10, value=round(shown))
            row = Adw.SpinRow(title=f"At {unit}", adjustment=adj)
            row.connect("changed", lambda r, i=i: self._set_stop(i, r.get_value()))
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
            button.set_rgba(_rgba(color))
            button.connect("notify::rgba", lambda b, _p, i=i: self._set_color(i, _rgb(b.get_rgba())))
            row.add_suffix(button)
            remove = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                                valign=Gtk.Align.CENTER, sensitive=len(self.stops) > 2, tooltip_text="Remove")
            remove.connect("clicked", lambda *_, i=i: self._remove(i))
            row.add_suffix(remove)
            self.group.add(row)
            self.rows.append(row)
        self._preview()

    def _preview(self):
        self.preview.set_effect({"type": "temperature", "stops": sorted(self.stops, key=lambda s: s[0])})

    def _set_stop(self, i, value):
        self.stops[i][0] = float(value) if self.is_fan else disp_to_c(value)
        self._preview()

    def _set_color(self, i, color):
        self.stops[i][1] = color
        self._preview()

    def _add(self):
        if len(self.stops) < 8:
            self.stops.append([self.stops[-1][0] + 5, list(self.stops[-1][1])])
            self._fill()

    def _remove(self, i):
        if len(self.stops) > 2:
            del self.stops[i]
            self._fill()

    def _finish(self):
        self.dialog.close()
        self.on_done(sorted(self.stops, key=lambda s: s[0]))

    def present(self):
        self.dialog.present(self.win)


class DeviceCard(Gtk.Box):
    """One LED device as a tile: icon, name, power switch and a preview of its colours; the chevron opens the
    settings. The tile is the source of truth while the user edits; the server state is applied when idle."""

    def __init__(self, win, device):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=ui.CARD_WIDTH,
                         valign=Gtk.Align.START, css_classes=["card", "fc-card"])
        self.win, self.device = win, device
        self.modes = {m["name"]: m for m in device["modes"]}
        saved = device.get("effect") or {}
        self.pending = None
        self.loading = True
        self.stops = saved.get("stops") if saved.get("type") == "temperature" else None

        head = Gtk.Box(spacing=8)
        head.append(ui.icon_bubble(TYPE_ICONS.get(device["type"], "fc-light-symbolic")))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label=device["name"], xalign=0, ellipsize=3, css_classes=["fc-card-title"]))
        self.subtitle = ui.caption("")
        names.append(self.subtitle)
        head.append(names)
        self.power = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Lights on / off")
        self.power.connect("notify::active", lambda s, _p: self._power(s.get_active()))
        head.append(self.power)
        self.append(head)

        self.bar = GradientBar()
        self.append(self.bar)
        foot = Gtk.Box(spacing=4)
        self.summary = ui.caption("")
        self.summary.set_hexpand(True)
        foot.append(self.summary)
        identify = Gtk.Button(icon_name="find-location-symbolic", css_classes=["flat", "circular"],
                              tooltip_text="Blink this device to find it", sensitive=device["direct"])
        identify.connect("clicked", lambda *_: run_async(
            lambda: win.client.call("light_identify", device=device["key"]), None, lambda e: win.toast(str(e))))
        foot.append(identify)
        self.chevron = Gtk.ToggleButton(icon_name="pan-down-symbolic", css_classes=["flat", "circular"],
                                        tooltip_text="Settings")
        foot.append(self.chevron)
        self.append(foot)

        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.chevron.connect("toggled", lambda b: self.revealer.set_reveal_child(b.get_active()))
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.revealer.set_child(self.controls)
        self.append(self.revealer)
        self._build_controls(saved)

        self.apply_state(device)
        self.loading = False

    # --- controls ---------------------------------------------------------------------
    def _build_controls(self, saved):
        d, win = self.device, self.win
        self.choices = [("release", "Not controlled")]
        if d["direct"]:
            self.choices += list(EFFECTS)
        self.choices += [(f"hw:{m['name']}", f"{m['name']}  (device)") for m in d["modes"]]
        self.dropdown = Gtk.DropDown.new_from_strings([label for _k, label in self.choices])
        self.dropdown.set_enable_search(len(self.choices) > 12)
        if len(self.choices) > 12:
            self.dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.controls.append(self.dropdown)

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
        self.controls.append(self.color_row)

        self.brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.brightness.set_value(saved.get("brightness", 100))
        self.speed.set_value(saved.get("speed", 50))
        self.brightness_box = ui.labeled("Brightness", self.brightness)
        self.speed_box = ui.labeled("Speed", self.speed)
        for s in (self.brightness, self.speed):
            s.connect("value-changed", lambda *_: self._changed())
        self.controls.append(self.brightness_box)
        self.controls.append(self.speed_box)

        status = win.status or {}
        temps, pwms = status.get("temps", {}), status.get("pwms", {})
        self.sources = [""] + list(temps) + [f"fan:{p}" for p in pwms]
        labels = ["Hottest sensor"] + [win.display_name(t) for t in temps] + [
            "Fan speed: " + (p_info.get("name") or p_info.get("label") or p) for p, p_info in pwms.items()]
        self.source = Gtk.DropDown.new_from_strings(labels)
        self.source.connect("notify::selected", lambda *_: self._source_changed())
        self.edit_stops = Gtk.Button(label="Edit colour gradient …", halign=Gtk.Align.START)
        self.edit_stops.connect("clicked", lambda *_: self._open_stops())
        self.source_box = ui.labeled("Follows", self.source)
        self.controls.append(self.source_box)
        self.controls.append(self.edit_stops)

        self.error = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, css_classes=["error"], visible=False)
        self.controls.append(self.error)
        self.dropdown.connect("notify::selected", lambda *_: self._changed())

    def apply_state(self, device):
        """Show what the service says (also after a change made elsewhere, for example the tray)."""
        self.loading = True
        self.device = device
        effect = device.get("effect")
        saved = effect or {}
        key = "release"
        if saved.get("type") == "hardware":
            key = f"hw:{saved['mode']}"
        elif saved.get("type"):
            key = saved["type"]
        keys = [k for k, _l in self.choices]
        self.dropdown.set_selected(keys.index(key) if key in keys else 0)
        if saved.get("type") == "temperature":
            self.stops = saved.get("stops")
            try:
                self.source.set_selected(self.sources.index(saved.get("sensor", "")))
            except ValueError:
                pass
        for i, c in enumerate((saved.get("colors") or ([saved["color"]] if saved.get("color") else []))[:3]):
            self.colors[i].set_rgba(_rgba(c))
        if "brightness" in saved:
            self.brightness.set_value(saved["brightness"])
        if "speed" in saved:
            self.speed.set_value(saved["speed"])
        self.power.set_active(bool(device.get("on")))
        self.bar.set_effect(effect)
        zones = len(device["zones"])
        self.subtitle.set_label(f"{device['plugin_name']} · {device['leds']} LEDs"
                                + (f" · {zones} zones" if zones > 1 else ""))
        labels = dict(self.choices)
        self.summary.set_label(labels.get(key, "Not controlled") if effect else "Not controlled")
        self.set_error(device.get("error"))
        self._layout()
        self.loading = False

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
        self.source_box.set_visible(kind == "temperature")
        self.edit_stops.set_visible(kind == "temperature")

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
            effect = {"type": "temperature", "sensor": self.sources[self.source.get_selected()], "brightness": b}
            if self.stops:
                effect["stops"] = self.stops
            return effect
        mode = self.modes[kind[3:]]
        return {"type": "hardware", "mode": mode["name"], "speed": s, "brightness": b,
                "colors": [_rgb(self.colors[i].get_rgba()) for i in range(min(3, mode["colors"]))]}

    # --- user actions -----------------------------------------------------------------
    def _power(self, on):
        if self.loading:
            return
        run_async(lambda: self.win.client.call("light_power", device=self.device["key"], on=on),
                  lambda _d: self.win.light_refresh(), lambda e: (self.win.toast(str(e)), self.win.light_refresh()))

    def _source_changed(self):
        if self.loading:
            return
        self.stops = None            # a new source has its own scale (°C or %), so start from its default gradient
        self._changed()

    def _open_stops(self):
        is_fan = self.sources[self.source.get_selected()].startswith("fan:")
        current = self.stops or (effects_default_stops(is_fan))
        StopsDialog(self.win, current, is_fan, self._stops_done).present()

    def _stops_done(self, stops):
        self.stops = stops
        self._changed()

    def _changed(self):
        if self.loading:
            return
        self._layout()
        self.bar.set_effect(self.effect())
        if self.pending:
            GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(250, self._send)

    def _send(self):
        self.pending = None
        effect = self.effect()
        run_async(lambda: self.win.client.call("light_set", device=self.device["key"], effect=effect),
                  lambda _d: (self.set_error(""), self.win.light_refresh()), lambda e: self.set_error(str(e)))
        return False


def effects_default_stops(is_fan):
    from ..lighting import effects
    return effects.FAN_STOPS if is_fan else effects.DEFAULT_STOPS


class StatTile(Gtk.Box):
    """A small tile of the overview: icon, big number and what it counts."""

    def __init__(self, icon, label):
        super().__init__(spacing=10, css_classes=["card", "fc-card"], margin_top=2, margin_bottom=2)
        self.append(ui.icon_bubble(icon))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.value = Gtk.Label(xalign=0, css_classes=["fc-card-title"])
        box.append(self.value)
        box.append(ui.caption(label))
        self.append(box)
        self.set_margin_end(0)

    def set(self, text):
        self.value.set_label(str(text))


class LightSection(Gtk.Box):
    """The Light section of the overview: a summary and one tile per LED device."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.win = win
        self.signature = None
        self.cards = {}
        self.timer = None
        win.light_refresh = self.refresh
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

        self.overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["card", "fc-card"])
        top = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(Gtk.Label(label="Overview", xalign=0, css_classes=["fc-card-title"]))
        titles.append(ui.caption("Your lighting at a glance"))
        top.append(titles)
        all_on = Gtk.Button(label="All on", valign=Gtk.Align.CENTER)
        all_off = Gtk.Button(label="All off", valign=Gtk.Align.CENTER)
        all_on.connect("clicked", lambda *_: self._all(True))
        all_off.connect("clicked", lambda *_: self._all(False))
        top.append(all_on)
        top.append(all_off)
        self.overview.append(top)
        stats = Gtk.Box(spacing=12, homogeneous=True)
        self.stat_devices = StatTile("fc-light-symbolic", "Online")
        self.stat_zones = StatTile("weather-clear-symbolic", "Zones active")
        self.stat_profile = StatTile("fc-profile-symbolic", "Profile")
        for t in (self.stat_devices, self.stat_zones, self.stat_profile):
            stats.append(t)
        self.overview.append(stats)
        self.holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.append(self.holder)

    def _on_map(self, *_):
        self.refresh()
        if not self.timer:
            self.timer = GLib.timeout_add_seconds(3, lambda: (self.refresh(), True)[1])

    def _on_unmap(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def _all(self, on):
        run_async(lambda: self.win.client.call("light_power_all", on=on), lambda _r: self.refresh(),
                  lambda e: self.win.toast(str(e)))

    def refresh(self):
        if not self.win.connected:
            return

        def fetch():
            return self.win.client.call("light_devices"), self.win.client.call("plugin_list")
        run_async(fetch, self._show, lambda e: None)

    def _clear(self):
        child = self.holder.get_first_child()
        while child:
            self.holder.remove(child)
            child = self.holder.get_first_child()
        self.cards = {}

    def _show(self, result):
        devices, plugins = result
        tray = getattr(self.win, "tray", None)
        if tray:
            tray.set_lights(bool(devices))
        active = sum(len(d["zones"]) for d in devices if d.get("on"))
        self.stat_devices.set(f"{len(devices)} devices")
        self.stat_zones.set(f"{active} zones")
        self.stat_profile.set((self.win.config or {}).get("profile") or "–")
        signature = (tuple((d["key"], d["name"], d["leds"], len(d["modes"]), d["direct"]) for d in devices),
                     any(p.get("enabled") for p in plugins))
        if signature == self.signature:
            for d in devices:                      # apply changes made elsewhere, but never while the user is editing
                card = self.cards.get(d["key"])
                if card and not card.pending:
                    card.apply_state(d)
            return
        self.signature = signature
        self._clear()
        if not devices:
            if plugins and any(p.get("enabled") for p in plugins):
                title, text = "No LED devices found", ("The enabled plugins did not find any devices. Check their "
                                                       "settings under <b>Settings → LED devices</b>.")
            else:
                title, text = "No lighting plugin is switched on", LIGHT_TEXT
            self.holder.append(ui.notice(title, "fc-light-symbolic", text,
                                         ("Set up LED devices", lambda: self.win.navigate("leds"))))
            return
        self.holder.append(self.overview)
        self.holder.append(Gtk.Label(label="Devices", xalign=0, css_classes=["fc-page-title"], margin_top=8))
        flow = ui.CardFlow()
        for d in devices:
            card = DeviceCard(self.win, d)
            self.cards[d["key"]] = card
            flow.append(card)
        self.holder.append(flow)
