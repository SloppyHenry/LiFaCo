"""Settings → LED devices (manage, search and install plugins) and the Light section (one card per device)."""

import base64
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from ..ipc import Client  # noqa: E402
from . import common as ui  # noqa: E402
from .util import c_to_disp, disp_to_c, run_async, temp_unit  # noqa: E402

EDIT_QUIET = 3.0            # seconds after an edit in which refreshes leave a card alone (sliders being dragged)
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
        busy = any(p.get("status") == "starting" or any(h["installing"] for h in p.get("helpers", []))
                   for p in self.plugins)
        if busy or any(p.get("helpers") for p in self.plugins):     # also notice a server that was started meanwhile
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
        self.plugins = []
        self.refresh()
        self._search(self.query)          # what is installed may have changed anyway (for example after a timeout)

    # --- installed plugins ------------------------------------------------------------
    def _signature(self, plugins):
        return [(p["id"], p.get("status"), p.get("enabled"), p.get("version"), p.get("devices"),
                 str(p.get("device_names")), str(p.get("helpers"))) for p in plugins]

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
        if p["status"] == "stopped" and not p["enabled"]:
            row.add_row(Adw.ActionRow(title="Switched off", subtitle="Switch the plugin on with the switch above. "
                                      "It then looks for its devices; they appear in the Light section.",
                                      subtitle_lines=0, css_classes=["warning"]))
        if p["status"] == "running":
            names = p.get("device_names") or []
            row.add_row(Adw.ActionRow(title=f"Found {len(names)} devices" if names else "No devices found yet",
                                      subtitle=GLib.markup_escape_text(", ".join(names)) if names else
                                      "Check the settings below, or search again", subtitle_lines=0))
        if p["status"] == "error" and p["error"]:
            row.add_row(Adw.ActionRow(title="Problem", subtitle=GLib.markup_escape_text(p["error"]), subtitle_lines=0,
                                      css_classes=["error"]))
        for h in p.get("helpers", []):
            row.add_row(self._helper_row(p, h))
        if p["status"] == "running":
            rescan = Adw.ActionRow(title="Search the hardware again",
                                   subtitle="After plugging in a device or changing something in the BIOS")
            button = Gtk.Button(label="Search", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda b: self._hardware_rescan(b, p))
            rescan.add_suffix(button)
            row.add_row(rescan)
        for h in p.get("helpers", []):
            if h.get("manual_devices") and h["installed"]:
                manual = Adw.ActionRow(
                    title="Devices you add yourself",
                    subtitle=f"Network lamps, LED controllers and LED strips that {h['name']} cannot find by itself",
                    subtitle_lines=0)
                button = Gtk.Button(label="Manage …", valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda *_, hid=h["id"], name=h["name"]: ManualDevicesDialog(
                    self.win, self.slow, hid, name, self.refresh).present())
                manual.add_suffix(button)
                row.add_row(manual)
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
                lambda: self.slow.call("plugin_settings", id=p["id"], settings=values),
                lambda plugins: (self.win.toast("Settings saved"), self.refresh(plugins)), self._fail))
            actions.add_suffix(save)
        if p["enabled"] and not p["needs_approval"]:
            restart = Gtk.Button(label="Restart", valign=Gtk.Align.CENTER)
            restart.connect("clicked", lambda *_: run_async(
                lambda: self.slow.call("plugin_restart", id=p["id"]), self.refresh, self._fail))
            actions.add_suffix(restart)
        remove = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        remove.connect("clicked", lambda *_: self.win.confirm(
            f"Remove {p['name']}?", "The plugin and its stored data are deleted. Lighting settings for its devices "
            "are forgotten.", "Remove", lambda: run_async(
                lambda: self.slow.call("plugin_remove", id=p["id"]), self._removed, self._fail), destructive=True))
        actions.add_suffix(remove)
        row.add_row(actions)
        return row

    def _helper_row(self, p, h):
        """What the plugin needs (for example the OpenRGB program): status, and a button to install it."""
        if h["installing"]:
            row = Adw.ActionRow(title=f"Installing {h['name']} …", subtitle="This can take a minute")
            row.add_suffix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
            return row
        if h["server_running"]:
            return Adw.ActionRow(title=f"{h['name']} is running", subtitle="The plugin can connect to it")
        row = Adw.ActionRow(title=f"{h['name']} " + ("server is not running" if h["installed"] else "is not installed"),
                            subtitle=GLib.markup_escape_text(h["error"] or h["hint"]), subtitle_lines=0,
                            css_classes=["error"] if h["error"] else [])
        if not h["installed"] and h["can_install"]:
            button = Gtk.Button(label=f"Install {h['name']} …", valign=Gtk.Align.CENTER,
                                css_classes=["suggested-action"])
            button.connect("clicked", lambda *_: self._ask_install_helper(p, h))
            row.add_suffix(button)
        return row

    def _hardware_rescan(self, button, p):
        button.set_sensitive(False)
        button.set_label("Searching …")

        def done(result):
            self.win.toast(f"{p['name']}: {result['devices']} devices – more may appear in a few seconds")
            self.win.light_refresh()
            self.plugins = []
            self.refresh()
        run_async(lambda: self.slow.call("plugin_hardware_rescan", id=p["id"]), done, self._fail)

    def _check_helpers(self, plugins, pid):
        """After installing a plugin: if it needs software that is missing, ask right away whether to install it."""
        for pl in plugins:
            if pl["id"] != pid:
                continue
            missing = [h for h in pl.get("helpers", []) if not h["installed"] and h["can_install"]]
            if any(not h["server_running"] for h in pl.get("helpers", [])):
                self.open_rows.add(pid)             # show what the plugin still needs
                self.plugins = []
                self.refresh()
            if missing:
                self._ask_install_helper(pl, missing[0])

    def _ask_install_helper(self, p, h):
        dialog = Adw.AlertDialog(
            heading=f"Install {h['name']}?",
            body=f"The {p['name']} plugin needs the {h['name']} program. LiFaCo installs it with administrator rights: from your "
                 "distribution's package repository, or, if it is not packaged there, the official release from "
                 "the OpenRGB project (codeberg.org/OpenRGB). No boot service is set up: LiFaCo starts the server "
                 "only while the plugin is switched on.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def answered(_d, response):
            if response == "install":
                run_async(lambda: self.win.client.call("helper_install", id=h["id"]),
                          lambda _s: self.refresh(), self._fail)
        dialog.connect("response", answered)
        dialog.present(self.win)

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
            run_async(lambda: self.slow.call("plugin_enable", id=p["id"], enabled=active),
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
                run_async(lambda: self.slow.call("plugin_enable", id=p["id"], enabled=True, approve=True),
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
            self._check_helpers(plugins, pid)
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
                                               self.refresh(plugins), self._check_helpers(plugins, _plugin_id(plugins))),
                              self._fail))


class ManualDevicesDialog:
    """Devices a helper's server cannot find by itself (OpenRGB: E1.31, DDP, serial LED strips, network lamps)."""

    def __init__(self, win, client, hid, name, on_changed):
        self.win, self.client, self.hid, self.name, self.on_changed = win, client, hid, name, on_changed
        self.config = None
        self.rows = []
        self.dialog = Adw.Dialog(title=f"{name} devices", content_width=520, content_height=620)
        self.nav = Adw.NavigationView()
        self.dialog.set_child(self.nav)

        page = Adw.PreferencesPage()
        self.note = Adw.PreferencesGroup()
        page.add(self.note)
        self.note_row = None
        self.group = Adw.PreferencesGroup(
            title="Added by hand",
            description=f"{name} finds mainboards, graphics cards, RAM, keyboards and mice by itself. Devices on the "
                        "network or on a serial port have to be entered here. Changes restart the server; that takes "
                        "a few seconds.")
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text="Add a device")
        add.connect("clicked", lambda *_: self._open_form())
        self.group.set_header_suffix(add)
        page.add(self.group)
        self.nav.add(self._page(page, f"{name} devices", "list"))
        self._load()

    @staticmethod
    def _page(content, title, tag):
        view = Adw.ToolbarView(content=content)
        view.add_top_bar(Adw.HeaderBar())
        return Adw.NavigationPage(child=view, title=title, tag=tag)

    def present(self):
        self.dialog.present(self.win)

    def _load(self):
        run_async(lambda: self.client.call("helper_devices", id=self.hid), self._show, self._error)

    def _error(self, error):
        self.win.toast(str(error))

    def _show(self, config):
        self.config = config
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        if self.note_row:
            self.note.remove(self.note_row)
            self.note_row = None
        text = config.get("problem") or config.get("error") or config.get("note")
        if text:
            self.note_row = Adw.ActionRow(title="Note", subtitle=GLib.markup_escape_text(text), subtitle_lines=0,
                                          css_classes=["warning"])
            self.note.add(self.note_row)
        labels = {t["id"]: t["label"].split(" (")[0] for t in config["types"]}
        if not config["devices"]:
            row = Adw.ActionRow(title="No devices added", subtitle="Add one with the + button")
            self.group.add(row)
            self.rows.append(row)
        for i, d in enumerate(config["devices"]):
            where = d.get("ip") or d.get("port") or ""
            detail = " · ".join(str(x) for x in (labels.get(d["type"], d["type"]), where,
                                                 f"{d['num_leds']} LEDs" if d.get("num_leds") else "") if x)
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.get("name") or labels.get(d["type"], "Device")),
                                subtitle=GLib.markup_escape_text(detail))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["flat"], tooltip_text="Remove")
            remove.connect("clicked", lambda *_, i=i: self._save(
                [x for j, x in enumerate(self.config["devices"]) if j != i]))
            row.add_suffix(remove)
            self.group.add(row)
            self.rows.append(row)

    def _save(self, devices):
        self.dialog.set_sensitive(False)

        def done(config):
            self.dialog.set_sensitive(True)
            self._show(config)
            self.win.toast(f"{self.name} restarted with the new devices" if not config.get("problem")
                           else config["problem"])
            self.on_changed()
            self.win.light_refresh()

        def failed(error):
            self.dialog.set_sensitive(True)
            self._error(error)
        run_async(lambda: self.client.call("helper_devices", id=self.hid, devices=devices), done, failed)

    # --- the form for a new device --------------------------------------------------------
    def _open_form(self):
        if not self.config:
            return
        types = self.config["types"]
        page = Adw.PreferencesPage()
        kind_group = Adw.PreferencesGroup()
        kind = Adw.ComboRow(title="Kind of device", model=Gtk.StringList.new([t["label"] for t in types]))
        kind_group.add(kind)
        page.add(kind_group)
        fields_group = Adw.PreferencesGroup()
        page.add(fields_group)
        values, field_rows = {}, []

        def build(*_):
            for r in field_rows:
                fields_group.remove(r)
            field_rows.clear()
            values.clear()
            t = types[kind.get_selected()]
            values["type"] = t["id"]
            for f in t["fields"]:
                r = self._field_row(f, values)
                fields_group.add(r)
                field_rows.append(r)
        kind.connect("notify::selected", build)
        build()

        add = Gtk.Button(label="Add device", halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"],
                         margin_top=12)

        def submit(*_):
            self.nav.pop()
            self._save(list(self.config["devices"]) + [dict(values)])
        add.connect("clicked", submit)
        button_group = Adw.PreferencesGroup()
        button_group.add(add)
        page.add(button_group)
        self.nav.push(self._page(page, "Add a device", "add"))

    def _field_row(self, f, values):
        key = f["key"]
        values[key] = f["default"]
        if f["kind"] == "int":
            adj = Gtk.Adjustment(lower=f["min"], upper=f["max"], step_increment=1, page_increment=10,
                                 value=f["default"])
            row = Adw.SpinRow(title=f["label"], adjustment=adj)
            row.connect("notify::value", lambda r, _p: values.__setitem__(key, int(r.get_value())))
        elif f["kind"] == "switch":
            row = Adw.SwitchRow(title=f["label"], active=bool(f["default"]))
            row.connect("notify::active", lambda r, _p: values.__setitem__(key, r.get_active()))
        elif f["kind"] in ("choice", "serial"):
            choices = f["choices"] if f["kind"] == "choice" else (self.config["serial_ports"] or ["/dev/ttyUSB0"])
            row = Adw.ComboRow(title=f["label"], model=Gtk.StringList.new(choices))
            if f["kind"] == "serial" and not self.config["serial_ports"]:
                row.set_subtitle("No serial port found – plug the device in and open this dialog again")
            values[key] = choices[0]
            row.connect("notify::selected", lambda r, _p: values.__setitem__(key, choices[r.get_selected()]))
        else:
            row = Adw.EntryRow(title=f["label"])
            row.connect("changed", lambda r: values.__setitem__(key, r.get_text().strip()))
        return row


def _plugin_id(plugins):
    """Which plugin was just installed from a file: the one that is new or needs setup (best effort)."""
    for p in plugins:
        if p.get("helpers") and any(not h["installed"] for h in p["helpers"]):
            return p["id"]
    return ""


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
    allow_release = True           # "Not controlled" in the effect list
    """One LED device as a tile: icon, name, power switch and a preview of its colours; the chevron opens the
    settings. The tile is the source of truth while the user edits; the server state is applied when idle."""

    def __init__(self, win, device):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=ui.CARD_WIDTH,
                         valign=Gtk.Align.START, css_classes=["card", "fc-card"])
        self.win, self.device = win, device
        self.modes = {m["name"]: m for m in device["modes"]}
        saved = device.get("effect") or {}
        self.pending = None
        self.edited = 0.0                    # when the user last changed something on this card
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
        self.identify_button = identify
        self.chevron = Gtk.ToggleButton(icon_name="pan-down-symbolic", css_classes=["flat", "circular"],
                                        tooltip_text="Settings")
        foot.append(self.chevron)
        self.append(foot)

        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        opened = getattr(win, "light_open", set())       # stays open when the cards are rebuilt
        self.chevron.connect("toggled", lambda b: (
            self.revealer.set_reveal_child(b.get_active()),
            (opened.add if b.get_active() else opened.discard)(device["key"])))
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.revealer.set_child(self.controls)
        self.append(self.revealer)
        self._build_controls(saved)
        self._build_zone_sizes()
        if device["key"] in opened:
            self.chevron.set_active(True)

        self.apply_state(device)
        self.loading = False

    def _build_zone_sizes(self):
        """Addressable headers: the user says how many LEDs are connected (the controller cannot know)."""
        zones = [(i, z) for i, z in enumerate(self.device["zones"]) if "min_leds" in z]
        if not zones:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=4)
        box.append(Gtk.Label(label="Connected LEDs", xalign=0, css_classes=["heading"]))
        box.append(ui.caption("How many LEDs hang on each header. Count them or look at the product data; "
                              "too many does no harm."))
        for i, z in zones:
            line = Gtk.Box(spacing=8)
            line.append(Gtk.Label(label=z["name"], xalign=0, hexpand=True, ellipsize=3))
            spin = Gtk.SpinButton.new_with_range(z["min_leds"], z["max_leds"], 1)
            spin.set_value(z["leds"])
            spin.set_valign(Gtk.Align.CENTER)
            spin.connect("value-changed", lambda b, i=i: self._zone_size_changed(i, b.get_value_as_int()))
            line.append(spin)
            box.append(line)
        self.controls.append(box)
        self.resize_pending = None

    def _zone_size_changed(self, zone, leds):
        self.edited = time.monotonic()
        if self.resize_pending:
            GLib.source_remove(self.resize_pending)

        def send():
            self.resize_pending = None
            run_async(lambda: self.win.client.call("light_resize_zone", device=self.device["key"], zone=zone,
                                                   leds=leds),
                      lambda _d: self.win.light_refresh(), lambda e: self.set_error(str(e)))
            return False
        self.resize_pending = GLib.timeout_add(900, send)

    # --- controls ---------------------------------------------------------------------
    def _build_controls(self, saved):
        d, win = self.device, self.win
        self.choices = [("release", "Not controlled")] if self.allow_release else []
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
        self.bar.set_effect(device.get("shown", effect))
        zones = len(device["zones"])
        self.subtitle.set_label(f"{device['plugin_name']} · {device['leds']} LEDs"
                                + (f" · {zones} zones" if zones > 1 else ""))
        labels = dict(self.choices)
        self.summary.set_label(labels.get(key, "Not controlled") if effect else "Not controlled")
        self.set_error(device.get("error"))
        synced = bool(device.get("synced"))
        self.controls.set_sensitive(not synced)        # sync mode decides; the own effect comes back afterwards
        self.power.set_sensitive(not synced)
        if synced:
            self.power.set_active(True)
            self.summary.set_label("In sync with all devices")
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
        self.edited = time.monotonic()
        if self.pending:
            GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(250, self._send)

    def _send(self):
        self.pending = None
        effect = self.effect()
        run_async(lambda: self.win.client.call("light_set", device=self.device["key"], effect=effect),
                  lambda _d: (self.set_error(""), self.win.light_refresh()), lambda e: self.set_error(str(e)))
        return False


class SyncCard(DeviceCard):
    """Sync mode: one effect for all devices, edited with the same controls as a device. Its switch turns sync on."""

    allow_release = False

    def __init__(self, win, sync):
        super().__init__(win, self.pseudo(sync))
        self.identify_button.set_visible(False)

    @staticmethod
    def pseudo(sync):
        return {"key": "sync", "name": "Sync: all devices", "type": "other", "direct": True, "modes": [], "zones": [],
                "leds": 0, "plugin_name": "", "effect": sync["effect"], "on": sync["on"], "error": "",
                "devices": sync["devices"]}

    def apply_state(self, device):
        super().apply_state(device)
        n = device.get("devices", 0)
        self.subtitle.set_label(f"Same effect on {n} devices" if device["on"] else "Off – every device has its own")
        if not device["on"]:
            self.summary.set_label("Switch on to control all devices together")

    def _power(self, on):
        if self.loading:
            return
        run_async(lambda: self.win.client.call("light_sync", on=on),
                  lambda _d: self.win.light_refresh(), lambda e: (self.win.toast(str(e)), self.win.light_refresh()))

    def _send(self):
        self.pending = None
        effect = self.effect()
        run_async(lambda: self.win.client.call("light_sync", effect=effect),
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
        win.light_open = set()           # keys of the cards whose settings are open
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
            return (self.win.client.call("light_devices"), self.win.client.call("plugin_list"),
                    self.win.client.call("light_sync"))
        run_async(fetch, self._show, lambda e: None)

    def _clear(self):
        child = self.holder.get_first_child()
        while child:
            self.holder.remove(child)
            child = self.holder.get_first_child()
        self.cards = {}

    def _show(self, result):
        devices, plugins, sync = result
        tray = getattr(self.win, "tray", None)
        if tray:
            tray.set_lights(bool(devices))
        active = sum(len(d["zones"]) for d in devices if d.get("on") or d.get("synced"))
        self.stat_devices.set(f"{len(devices)} devices")
        self.stat_zones.set(f"{active} zones")
        self.stat_profile.set((self.win.config or {}).get("profile") or "–")
        signature = (tuple((d["key"], d["name"], d["leds"], len(d["modes"]), d["direct"]) for d in devices),
                     any(p.get("enabled") for p in plugins))
        if signature == self.signature:
            for d in devices + [SyncCard.pseudo(sync)]:   # changes made elsewhere, but never while editing
                card = self.cards.get(d["key"])
                if card and not card.pending and time.monotonic() - card.edited > EDIT_QUIET:
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
        if any(d["direct"] for d in devices):
            self.cards["sync"] = SyncCard(self.win, sync)
            flow.append(self.cards["sync"])
        for d in devices:
            card = DeviceCard(self.win, d)
            self.cards[d["key"]] = card
            flow.append(card)
        self.holder.append(flow)
