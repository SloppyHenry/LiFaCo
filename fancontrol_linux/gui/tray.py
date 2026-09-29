"""Tray icons via the StatusNotifierItem D-Bus protocol (KDE, Xfce, Cinnamon, GNOME + AppIndicator …).

One main icon (open window, switch profile, quit) plus optional icons that show a sensor value as text.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .. import APP_ID  # noqa: E402
from .theme import ICON_DIR  # noqa: E402

WATCHER = "org.kde.StatusNotifierWatcher"
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/org/fancontrol_linux/Menu"
ICON_SIZES = (24, 48)

SNI_XML = f"""
<node><interface name="{SNI_IFACE}">
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewToolTip"/><signal name="NewTitle"/>
  <signal name="NewStatus"><arg type="s"/></signal>
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
</interface></node>"""

MENU_XML = f"""
<node><interface name="{MENU_IFACE}">
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{{sv}})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
    <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{{sv}})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
</interface></node>"""


class _Item:
    def __init__(self, path, title, icon_name="", pixmaps=None, tooltip=("", "")):
        self.path = path
        self.title = title
        self.icon_name = icon_name
        self.pixmaps = pixmaps or []
        self.tooltip = tooltip
        self.reg_id = None
        self.key = None  # what is currently drawn, to skip redundant redraws


class Tray:
    def __init__(self, on_activate, on_quit, on_profile):
        self.on_activate, self.on_quit, self.on_profile = on_activate, on_quit, on_profile
        self.bus = None
        self.items: dict[str, _Item] = {}
        self.profiles, self.active_profile = [], None
        self.revision = 1
        self.menu_reg = None
        self.watch_id = None
        self.renderer = None
        self.sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self.menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

    # --- lifecycle ------------------------------------------------------
    def start(self):
        if self.bus:
            return True
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.menu_reg = self.bus.register_object(MENU_PATH, self.menu_info, self._menu_call, self._menu_prop, None)
        except GLib.Error:
            self.bus = None
            return False
        self._add_item("main", _Item("/org/fancontrol_linux/Tray/main", "LiFaCo", icon_name=APP_ID,
                                     tooltip=("LiFaCo", "")))
        self.watch_id = Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                         lambda *_: self._register_all(), None)
        return True

    def stop(self):
        if self.renderer is not None:
            # A realized renderer that is garbage-collected aborts the process (GTK assertion).
            self.renderer.unrealize()
            self.renderer = None
        if not self.bus:
            return
        for item in list(self.items.values()):
            self.bus.unregister_object(item.reg_id)
        self.items.clear()
        if self.menu_reg:
            self.bus.unregister_object(self.menu_reg)
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
        self.bus = self.menu_reg = self.watch_id = None

    def _add_item(self, key, item):
        item.reg_id = self.bus.register_object(item.path, self.sni_info,
                                               lambda *a, it=item: self._sni_call(it, *a),
                                               lambda *a, it=item: self._sni_prop(it, *a), None)
        self.items[key] = item
        self._register(item)

    def _remove_item(self, key):
        item = self.items.pop(key)
        self.bus.unregister_object(item.reg_id)

    def _register(self, item):
        self.bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (item.path,)), None, Gio.DBusCallFlags.NONE, 2000, None, None, None)

    def _register_all(self):
        for item in self.items.values():
            self._register(item)

    def _emit(self, item, signal):
        if self.bus:
            self.bus.emit_signal(None, item.path, SNI_IFACE, signal, None)

    # --- StatusNotifierItem ---------------------------------------------
    def _sni_call(self, item, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        invocation.return_value(None)

    def _sni_prop(self, item, _conn, _sender, _path, _iface, prop):
        values = {
            "Category": ("s", "Hardware"),
            "Id": ("s", f"fancontrol-linux-{item.path.rsplit('/', 1)[-1]}"),
            "Title": ("s", item.title),
            "Status": ("s", "Active"),
            "WindowId": ("i", 0),
            "IconName": ("s", item.icon_name),
            "IconPixmap": ("a(iiay)", item.pixmaps),
            "IconThemePath": ("s", ICON_DIR),
            "OverlayIconName": ("s", ""),
            "AttentionIconName": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", (item.icon_name, item.pixmaps, item.tooltip[0], item.tooltip[1])),
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
        }
        sig, value = values[prop]
        return GLib.Variant(sig, value)

    # --- menu -----------------------------------------------------------
    def _menu_items(self):
        items = {
            0: ({"children-display": GLib.Variant("s", "submenu")}, [1, 2, 3, 4, 5]),
            1: ({"label": GLib.Variant("s", "Open LiFaCo")}, []),
            2: ({"type": GLib.Variant("s", "separator")}, []),
            3: ({"label": GLib.Variant("s", "Profile"), "children-display": GLib.Variant("s", "submenu"),
                 "enabled": GLib.Variant("b", bool(self.profiles))}, [100 + i for i in range(len(self.profiles))]),
            4: ({"type": GLib.Variant("s", "separator")}, []),
            5: ({"label": GLib.Variant("s", "Quit (fan control keeps running)")}, []),
        }
        for i, name in enumerate(self.profiles):
            items[100 + i] = ({"label": GLib.Variant("s", name.replace("_", "__")),
                               "toggle-type": GLib.Variant("s", "radio"),
                               "toggle-state": GLib.Variant("i", 1 if name == self.active_profile else 0)}, [])
        return items

    def _layout(self, items, item_id):
        props, children = items[item_id]
        return (item_id, props, [GLib.Variant("(ia{sv}av)", self._layout(items, c)) for c in children])

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        items = self._menu_items()
        if method == "GetLayout":
            parent = params.unpack()[0]
            parent = parent if parent in items else 0
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(items, parent))))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or list(items)
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, items[i][0]) for i in ids if i in items],)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = items.get(item_id, ({}, []))[0].get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event = params.unpack()[:2]
            if event == "clicked":
                GLib.idle_add(lambda: (self._clicked(item_id), False)[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in params.unpack()[0]:
                if event == "clicked":
                    GLib.idle_add(lambda i=item_id: (self._clicked(i), False)[1])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _menu_prop(self, _conn, _sender, _path, _iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [ICON_DIR])}[prop]

    def _clicked(self, item_id):
        if item_id == 1:
            self.on_activate()
        elif item_id == 5:
            self.on_quit()
        elif item_id >= 100 and item_id - 100 < len(self.profiles):
            self.on_profile(self.profiles[item_id - 100])

    def set_profiles(self, profiles, active):
        if (profiles, active) != (self.profiles, self.active_profile):
            self.profiles, self.active_profile = list(profiles), active
            self.revision += 1
            if self.bus:
                self.bus.emit_signal(None, MENU_PATH, MENU_IFACE, "LayoutUpdated",
                                     GLib.Variant("(ui)", (self.revision, 0)))

    # --- content --------------------------------------------------------
    def set_main_tooltip(self, title, text):
        item = self.items.get("main")
        if item and item.tooltip != (title, text):
            item.tooltip = (title, text)
            self._emit(item, "NewToolTip")

    def set_value_icons(self, entries, force=False):
        """entries: [(id, text_on_icon, color, tooltip_title, tooltip_text)]"""
        if not self.bus:
            return
        wanted = {f"v:{e[0]}": e for e in entries}
        for key in [k for k in self.items if k.startswith("v:") and k not in wanted]:
            self._remove_item(key)
        for index, (key, (sid, text, color, title, tip)) in enumerate(wanted.items()):
            item = self.items.get(key)
            if item is None:
                safe = "".join(ch if ch.isalnum() else "_" for ch in sid)
                item = _Item(f"/org/fancontrol_linux/Tray/v_{safe}", title)
                self._add_item(key, item)
            if item.tooltip != (title, tip):
                item.tooltip = (title, tip)
                self._emit(item, "NewToolTip")
            draw_key = (text, color)
            if force or item.key != draw_key:
                item.key = draw_key
                item.pixmaps = [self._render(text, color, size) for size in ICON_SIZES]
                self._emit(item, "NewIcon")

    def _render(self, text, color, size):
        if self.renderer is None:
            self.renderer = Gsk.CairoRenderer.new()
            self.renderer.realize_for_display(Gdk.Display.get_default())
            self.text_widget = Gtk.Label()
        rgba = Gdk.RGBA()
        rgba.parse(color)
        layout = self.text_widget.create_pango_layout(text)
        px = size * 0.72
        while True:
            desc = Pango.FontDescription.from_string("Sans Bold")
            desc.set_absolute_size(px * Pango.SCALE)
            layout.set_font_description(desc)
            w, h = layout.get_pixel_size()
            if w <= size or px <= 6:
                break
            px *= 0.9
        snap = Gtk.Snapshot()
        snap.translate(Graphene.Point().init((size - w) / 2, (size - h) / 2))
        snap.append_layout(layout, rgba)
        texture = self.renderer.render_texture(snap.to_node(), Graphene.Rect().init(0, 0, size, size))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.A8R8G8B8)
        data, stride = downloader.download_bytes()
        raw = data.get_data()
        if stride != size * 4:
            raw = b"".join(raw[r * stride:r * stride + size * 4] for r in range(size))
        return (size, size, bytes(raw))
