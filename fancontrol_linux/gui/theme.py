"""App-wide styling: base layout CSS plus a colour palette (preset or custom) chosen on the Design page."""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from . import graph_editor  # noqa: E402

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

SCHEMES = [("system", "System", Adw.ColorScheme.DEFAULT),
           ("light", "Light", Adw.ColorScheme.FORCE_LIGHT),
           ("dark", "Dark", Adw.ColorScheme.FORCE_DARK)]

# name, accent, card, header (None = libadwaita default)
PALETTES = {
    "classic": ("Classic (blue/yellow)", "#f7cf3b", "#0b3183", "#0d3a8c"),
    "adwaita": ("Adwaita", None, None, None),
    "ocean": ("Ocean", "#5ee6f5", "#0f4c5c", "#135e70"),
    "forest": ("Forest", "#8ff0a4", "#1b4a30", "#1f5637"),
    "ember": ("Ember", "#ffb057", "#5a220f", "#6d2a12"),
    "violet": ("Violet", "#e3b3ff", "#3a1f6b", "#46257f"),
    "graphite": ("Graphite (red)", "#ff6b6b", "#34343a", "#26262b"),
    "custom": ("Custom colours", None, None, None),
}
DEFAULT_CUSTOM = {"accent": "#f7cf3b", "card": "#0b3183", "header": "#0d3a8c"}

BASE_CSS = """
.fc-rail { padding: 6px 0; }
.fc-rail row { padding: 10px 2px; margin: 2px 6px; border-radius: 10px; }
.fc-rail row label { font-size: 0.82em; }
.fc-rail row image { -gtk-icon-size: 20px; }
.fc-page-title { font-size: 1.25em; font-weight: 700; }
.fc-card { border-radius: 12px; padding: 12px; }
.fc-card .fc-caption { font-size: 0.78em; opacity: 0.8; }
.fc-card .fc-big { font-size: 1.3em; font-weight: 600; }
.fc-card entry.fc-name { background: none; box-shadow: none; border-radius: 0; min-height: 28px;
    padding: 0 2px; font-weight: 600; border-bottom: 1px solid alpha(currentColor, 0.45); }
.fc-card entry.fc-name:focus-within { border-bottom: 2px solid @accent_bg_color; }
.fc-card spinbutton { min-height: 28px; }
.fc-card spinbutton text { min-width: 3em; }
.fc-fab { min-width: 56px; min-height: 56px; border-radius: 9999px; padding: 0;
    box-shadow: 0 3px 8px alpha(black, 0.35); }
.fc-fab image { -gtk-icon-size: 24px; }
.fc-hidden { opacity: 0.55; }
.fc-chip { padding: 2px 8px; border-radius: 999px; background: alpha(currentColor, 0.1); }
"""


def _hex_to_rgba(color):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba


def _text_on(color):
    c = _hex_to_rgba(color)
    luminance = 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue
    return "#1b1b1f" if luminance > 0.55 else "#ffffff"


def palette_colors(prefs):
    key = prefs.get("palette") or "classic"
    if key == "custom":
        custom = dict(DEFAULT_CUSTOM, **(prefs.get("custom_colors") or {}))
        return custom["accent"], custom["card"], custom["header"]
    _name, accent, card, header = PALETTES.get(key, PALETTES["classic"])
    return accent, card, header


class Theme:
    def __init__(self):
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(ICON_DIR)
        self.base = Gtk.CssProvider()
        self.base.load_from_string(BASE_CSS)
        self.palette = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self.palette, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.header_widgets = []

    def apply(self, prefs):
        scheme = next((s for k, _n, s in SCHEMES if k == prefs.get("theme")), Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

        accent, card, header = palette_colors(prefs)
        css = []
        if accent:
            css.append(f"@define-color accent_bg_color {accent};\n@define-color accent_color {accent};\n"
                       f"@define-color accent_fg_color {_text_on(accent)};")
        if card:
            fg = _text_on(card)
            css.append(f".fc-card {{ background-color: {card}; color: {fg}; }}\n"
                       f".fc-card entry, .fc-card spinbutton, .fc-card dropdown button {{ color: {fg}; }}")
        if header:
            fg = _text_on(header)
            css.append(f".fc-header {{ background-color: {header}; color: {fg}; }}\n"
                       f".fc-header button, .fc-header label {{ color: {fg}; }}")
        self.palette.load_from_string("\n".join(css))
        for widget in self.header_widgets:
            if header:
                widget.add_css_class("fc-header")
            else:
                widget.remove_css_class("fc-header")
        graph_editor.accent_override = _hex_to_rgba(accent) if accent else None
