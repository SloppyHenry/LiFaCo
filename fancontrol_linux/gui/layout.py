"""Window layout: the scrolling main view with its sections, the side menu and the settings view."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Graphene, Gtk  # noqa: E402

from .. import APP_ID, __version__  # noqa: E402
from . import common as ui  # noqa: E402

NAV_ITEMS = (("fans", "Fans", "fc-fan-symbolic"), ("curves", "Curves", "fc-curve-symbolic"),
             ("light", "Light", "fc-light-symbolic"), ("settings", "Settings", "fc-settings-symbolic"))
MAIN_SECTIONS = ("fans", "curves", "light")


class MainView(Gtk.ScrolledWindow):
    """One scrolling page with the sections Fans, Curves and Light."""

    def __init__(self, sections, on_section):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True)
        self.on_section = on_section
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=36, margin_top=24, margin_bottom=48,
                               margin_start=24, margin_end=24)
        self.sections = {}
        for name, title, subtitle, help_text, widget in sections:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            head = Gtk.Box(spacing=8)
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            titles.append(Gtk.Label(label=title, xalign=0, css_classes=["fc-section-title"]))
            titles.append(Gtk.Label(label=subtitle, xalign=0, css_classes=["fc-section-subtitle"]))
            head.append(titles)
            if help_text:
                label = Gtk.Label(label=help_text, wrap=True, max_width_chars=56, xalign=0,
                                  margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
                head.append(Gtk.MenuButton(child=Gtk.Label(label="?"), css_classes=["circular", "flat"],
                                           valign=Gtk.Align.START, popover=Gtk.Popover(child=label),
                                           tooltip_text="Help"))
            box.append(head)
            box.append(widget)
            self.content.append(box)
            self.sections[name] = box
        self.set_child(self.content)
        self.animation = None
        self.requested = None   # section chosen in the menu; stays highlighted until the user scrolls
        self.target = 0.0
        self.get_vadjustment().connect("value-changed", self._scrolled)

    def _offset(self, name):
        ok, point = self.sections[name].compute_point(self.content, Graphene.Point().init(0, 0))
        return point.y if ok else 0

    def scroll_to(self, name):
        adj = self.get_vadjustment()
        target = max(0.0, min(self._offset(name) - 12, adj.get_upper() - adj.get_page_size()))
        if self.animation:
            self.animation.pause()
        self.requested, self.target = name, target
        self.animation = Adw.TimedAnimation.new(self, adj.get_value(), target, 350,
                                                Adw.PropertyAnimationTarget.new(adj, "value"))
        self.animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self.animation.play()

    def _scrolled(self, adj):
        if self.requested:
            playing = self.animation and self.animation.get_state() == Adw.AnimationState.PLAYING
            if playing or abs(adj.get_value() - self.target) < 1:
                self.on_section(self.requested)
                return
            self.requested = None
        position = adj.get_value() + 80
        current = MAIN_SECTIONS[0]
        for name in MAIN_SECTIONS:
            if self._offset(name) <= position:
                current = name
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 4:
            current = MAIN_SECTIONS[-1]
        self.on_section(current)


class NavPanel(Gtk.Box):
    """Side menu (right): Fans, Curves, Light scroll the main view, Settings opens the settings view."""

    def __init__(self, win, profile_popover):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["fc-nav"], width_request=250)
        self.win = win
        self._updating = False
        header = Gtk.Box(spacing=10, css_classes=["fc-nav-header"])
        header.append(Gtk.Image(icon_name=APP_ID, pixel_size=40))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label="LiFaCo", xalign=0, css_classes=["fc-app-title"]))
        names.append(Gtk.Label(label=f"v{__version__}", xalign=0, css_classes=["fc-app-subtitle"]))
        header.append(names)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           valign=Gtk.Align.CENTER, tooltip_text="Hide menu")
        close.connect("clicked", lambda *_: win.set_nav_visible(False))
        header.append(close)
        self.append(header)

        self.list = Gtk.ListBox(css_classes=["navigation-sidebar", "fc-nav-list"])
        self.rows = {}
        for name, title, icon in NAV_ITEMS:
            box = Gtk.Box(spacing=14)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            box.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["fc-dim-icon"]))
            row = Gtk.ListBoxRow(child=box)
            row.nav_name = name
            self.list.append(row)
            self.rows[name] = row
        self.list.connect("row-activated", lambda _l, row: win.navigate(row.nav_name))
        self.append(self.list)
        self.append(Gtk.Box(vexpand=True))

        content = Gtk.Box(spacing=10)
        content.append(ui.icon_bubble("fc-profile-symbolic", 16))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.profile_label = Gtk.Label(label="Profile", xalign=0, ellipsize=3, css_classes=["fc-card-title"])
        labels.append(self.profile_label)
        labels.append(Gtk.Label(label="Active profile", xalign=0, css_classes=["fc-stat-caption"]))
        content.append(labels)
        content.append(Gtk.Image(icon_name="pan-down-symbolic", css_classes=["fc-dim-icon"]))
        self.profile_button = Gtk.MenuButton(child=content, popover=profile_popover, direction=Gtk.ArrowType.UP,
                                             css_classes=["flat", "fc-nav-profile"], tooltip_text="Profiles")
        self.append(self.profile_button)

    def highlight(self, name):
        row = self.rows.get(name)
        if row and self.list.get_selected_row() is not row:
            self.list.select_row(row)

    def set_profile(self, name):
        self.profile_label.set_label(name)


class SettingsView(Gtk.Box):
    """Separate settings view with its own category list and a back button."""

    def __init__(self, win, pages):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        bar = Gtk.Box(spacing=12, css_classes=["fc-settings-bar"])
        back = Gtk.Button(child=Adw.ButtonContent(icon_name="go-previous-symbolic", label="Back"),
                          css_classes=["flat"], tooltip_text="Back to the overview")
        back.connect("clicked", lambda *_: win.close_settings())
        bar.append(back)
        bar.append(Gtk.Label(label="Settings", css_classes=["fc-section-title"], xalign=0))
        self.append(bar)

        body = Gtk.Box(vexpand=True)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], width_request=220)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True)
        self.rows = {}
        for name, title, icon, widget in pages:
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row = Gtk.ListBoxRow(child=box)
            row.page_name = name
            self.list.append(row)
            self.rows[name] = row
            self.stack.add_named(widget, name)
        self.list.connect("row-selected", self._selected)
        body.append(self.list)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self.stack)
        self.append(body)
        self.select(pages[0][0])

    def _selected(self, _list, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)
            self.win.update_visible()

    def select(self, name):
        if name in self.rows:
            self.list.select_row(self.rows[name])

    def has(self, name):
        return name in self.rows
