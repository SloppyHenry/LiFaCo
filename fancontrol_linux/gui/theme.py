"""App-wide styling: base layout CSS plus a colour theme (preset or custom) chosen under Settings → Appearance.

"Midnight" themes use a dark navy look with an accent gradient that is applied to curves, gauges, switches
and buttons. Classic themes colour the cards and the header bar with solid colours.
"""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from . import paint  # noqa: E402

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

SCHEMES = [("system", "System", Adw.ColorScheme.DEFAULT),
           ("light", "Light", Adw.ColorScheme.FORCE_LIGHT),
           ("dark", "Dark", Adw.ColorScheme.FORCE_DARK)]

MIDNIGHT = {"window": "#0a0e17", "view": "#0d1220", "card": "#111827", "border": "#1f2a44", "header": "#0d1322",
            "sidebar": "#0b101b", "popover": "#151d2f", "fg": "#e5e7eb", "dim": "#8b95a7"}


def _midnight(name, *accent):
    return {"name": name, "group": "midnight", "accent": list(accent), "dark": True}


def _classic(name, accent, card, header):
    return {"name": name, "group": "classic", "accent": [accent] if accent else None, "card": card,
            "header": header, "dark": False}


PALETTES = {
    "midnight_aurora": _midnight("Aurora (blue → violet)", "#3b82f6", "#8b5cf6", "#a855f7"),
    "midnight_neon": _midnight("Neon (green → lime)", "#22c55e", "#84cc16", "#a3e635"),
    "midnight_rainbow": _midnight("Rainbow", "#ef4444", "#f59e0b", "#eab308", "#22c55e", "#06b6d4", "#3b82f6",
                                  "#a855f7"),
    "midnight_sunset": _midnight("Sunset (red → orange)", "#ef4444", "#f97316"),
    "midnight_amber": _midnight("Amber (orange → yellow)", "#f97316", "#facc15"),
    "midnight_mint": _midnight("Mint (green → teal)", "#10b981", "#2dd4bf"),
    "midnight_ice": _midnight("Ice (cyan → blue)", "#22d3ee", "#3b82f6"),
    "midnight_orchid": _midnight("Orchid (violet → pink)", "#a855f7", "#ec4899"),
    "classic": _classic("Classic (blue/yellow)", "#f7cf3b", "#0b3183", "#0d3a8c"),
    "adwaita": _classic("Adwaita", None, None, None),
    "ocean": _classic("Ocean", "#5ee6f5", "#0f4c5c", "#135e70"),
    "forest": _classic("Forest", "#8ff0a4", "#1b4a30", "#1f5637"),
    "ember": _classic("Ember", "#ffb057", "#5a220f", "#6d2a12"),
    "violet": _classic("Violet", "#e3b3ff", "#3a1f6b", "#46257f"),
    "graphite": _classic("Graphite (red)", "#ff6b6b", "#34343a", "#26262b"),
    "custom": {"name": "Custom colours", "group": "custom", "dark": False},
}
DEFAULT_PALETTE = "midnight_aurora"
# Colours for individual fan and curve cards (in this order).
ITEM_COLORS = ["#3b82f6", "#22c55e", "#a855f7", "#8b5cf6", "#14b8a6", "#f59e0b", "#ec4899", "#06b6d4"]
DEFAULT_CUSTOM = {"accent": "#f7cf3b", "accent2": "#f7cf3b", "card": "#0b3183", "header": "#0d3a8c"}

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
.fc-card .fc-huge { font-size: 1.7em; font-weight: 700; }
.fc-card .fc-unit { font-size: 0.85em; opacity: 0.7; }
.fc-card .fc-percent { font-size: 1.1em; font-weight: 600; }
.fc-card-title { font-weight: 700; }
.fc-icon-bubble { min-width: 40px; min-height: 40px; border-radius: 999px;
    background-color: alpha(@accent_bg_color, 0.16); color: @accent_bg_color; }
.fc-dim-icon { opacity: 0.55; }
.fc-detail-row { min-height: 32px; }
.fc-detail-label { opacity: 0.85; }
.fc-detail-value { font-weight: 600; }
.fc-details { border-top: 1px solid alpha(currentColor, 0.1); padding-top: 8px; }
.fc-details-toggle { font-size: 0.85em; padding: 2px 6px; min-height: 24px; }
.fc-chart-placeholder { min-height: 60px; }
.fc-tile { padding: 10px 12px; }
.fc-new-tile { border: 1px dashed alpha(currentColor, 0.3); border-radius: 12px; min-height: 58px; }
.fc-notice { padding: 16px; }
.fc-section-title { font-size: 1.45em; font-weight: 800; }
.fc-section-subtitle { opacity: 0.6; }
.fc-app-title { font-weight: 800; font-size: 1.05em; }
.fc-app-subtitle { font-size: 0.78em; opacity: 0.6; }
.fc-stat-value { font-weight: 700; font-size: 0.95em; }
.fc-stat-caption { font-size: 0.72em; opacity: 0.6; }
.fc-stat { padding: 0 10px; border-left: 1px solid alpha(currentColor, 0.12); }
.fc-nav { padding: 12px; }
.fc-nav-header { padding: 6px 4px 14px 4px; }
.fc-nav-list row { padding: 12px 10px; border-radius: 12px; margin: 3px 0; }
.fc-nav-list row:selected { background-color: alpha(@accent_bg_color, 0.18);
    box-shadow: inset 0 0 0 1px alpha(@accent_bg_color, 0.45); }
.fc-nav-list row image { -gtk-icon-size: 20px; }
.fc-nav-profile { border-radius: 12px; padding: 8px 10px; border: 1px solid alpha(currentColor, 0.12); }
.fc-settings-bar { padding: 8px 16px; border-bottom: 1px solid alpha(currentColor, 0.1); }
flowbox.fc-cards > flowboxchild { padding: 0; background: none; }
.fc-chip { padding: 2px 8px; border-radius: 999px; background: alpha(currentColor, 0.1); }
"""

# Variables used by libadwaita >= 1.6 (GTK >= 4.16); older versions use the @define-color names.
_ADW_VARS = {"window_bg_color": "--window-bg-color", "window_fg_color": "--window-fg-color",
             "view_bg_color": "--view-bg-color", "view_fg_color": "--view-fg-color",
             "headerbar_bg_color": "--headerbar-bg-color", "headerbar_fg_color": "--headerbar-fg-color",
             "headerbar_backdrop_color": "--headerbar-backdrop-color",
             "card_bg_color": "--card-bg-color", "card_fg_color": "--card-fg-color",
             "sidebar_bg_color": "--sidebar-bg-color", "sidebar_fg_color": "--sidebar-fg-color",
             "popover_bg_color": "--popover-bg-color", "popover_fg_color": "--popover-fg-color",
             "dialog_bg_color": "--dialog-bg-color", "dialog_fg_color": "--dialog-fg-color",
             "accent_bg_color": "--accent-bg-color", "accent_color": "--accent-color",
             "accent_fg_color": "--accent-fg-color"}


def _hex_to_rgba(color):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba


def _text_on(color):
    c = _hex_to_rgba(color)
    luminance = 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue
    return "#1b1b1f" if luminance > 0.55 else "#ffffff"


def palette(prefs):
    """The active theme as a dict: name, group, accent (list of colours or None), card, header, dark …"""
    key = prefs.get("palette") or DEFAULT_PALETTE
    if key == "custom":
        custom = dict(DEFAULT_CUSTOM, **(prefs.get("custom_colors") or {}))
        accent = [custom["accent"]] if custom["accent2"] == custom["accent"] else [custom["accent"], custom["accent2"]]
        return {"name": PALETTES["custom"]["name"], "group": "custom", "accent": accent, "card": custom["card"],
                "header": custom["header"], "dark": False}
    return PALETTES.get(key, PALETTES[DEFAULT_PALETTE])


def gradient_css(colors, angle=90):
    if len(colors) == 1:
        return colors[0]
    return f"linear-gradient({angle}deg, {', '.join(colors)})"


def _named_colors(values):
    """@define-color lines plus, on GTK >= 4.16, the matching libadwaita CSS variables."""
    lines = [f"@define-color {name} {value};" for name, value in values.items()]
    if (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16):
        props = " ".join(f"{_ADW_VARS[name]}: {value};" for name, value in values.items() if name in _ADW_VARS)
        lines.append(f":root {{ {props} }}")
    return "\n".join(lines)


def build_css(pal):
    accent = pal.get("accent")
    css = []
    named = {}
    if accent:
        mid = accent[len(accent) // 2]
        named.update(accent_bg_color=mid, accent_color=mid, accent_fg_color=_text_on(mid))
    if pal.get("dark"):
        m = MIDNIGHT
        named.update(window_bg_color=m["window"], window_fg_color=m["fg"], view_bg_color=m["view"],
                     view_fg_color=m["fg"], headerbar_bg_color=m["header"], headerbar_fg_color=m["fg"],
                     headerbar_backdrop_color=m["header"], card_bg_color=m["card"], card_fg_color=m["fg"],
                     sidebar_bg_color=m["sidebar"], sidebar_fg_color=m["fg"], popover_bg_color=m["popover"],
                     popover_fg_color=m["fg"], dialog_bg_color=m["card"], dialog_fg_color=m["fg"])
    if named:
        css.append(_named_colors(named))
    if pal.get("dark"):
        m = MIDNIGHT
        css.append(f"""
window.background, .fc-body {{ background-color: {m['window']}; color: {m['fg']}; }}
.fc-rail {{ background-color: {m['sidebar']}; }}
.fc-card {{ background-color: {m['card']}; color: {m['fg']}; border: 1px solid {m['border']}; border-radius: 14px;
    box-shadow: none; }}
.fc-card .fc-caption {{ color: {m['dim']}; opacity: 1; }}
.fc-card entry:not(.fc-name), .fc-card spinbutton, .fc-card dropdown > button {{ background-color: alpha(white, 0.04);
    border: 1px solid {m['border']}; color: {m['fg']}; }}
.fc-header {{ background-color: {m['header']}; color: {m['fg']}; box-shadow: inset 0 -1px {m['border']}; }}""")
    else:
        if pal.get("card"):
            fg = _text_on(pal["card"])
            css.append(f".fc-card {{ background-color: {pal['card']}; color: {fg}; }}\n"
                       f".fc-card entry, .fc-card spinbutton, .fc-card dropdown button {{ color: {fg}; }}")
        if pal.get("header"):
            fg = _text_on(pal["header"])
            css.append(f".fc-header {{ background-color: {pal['header']}; color: {fg}; }}\n"
                       f".fc-header button, .fc-header label {{ color: {fg}; }}")
    if pal.get("item_colors"):
        for i, color in enumerate(pal["item_colors"]):
            css.append(f".fc-color-{i} .fc-icon-bubble {{ background-color: alpha({color}, 0.16); color: {color}; }}")
    if accent and len(accent) > 1:
        grad = gradient_css(accent)
        css.append(f"""
switch:checked, button.suggested-action, .fc-fab, progressbar > trough > progress,
scale > trough > highlight, checkbutton check:checked {{ background-image: {grad}; border-color: transparent; }}""")
    return "\n".join(css)


class Theme:
    def __init__(self):
        display = Gdk.Display.get_default()
        # Our own icons first, so an older installed copy of the app icon cannot shadow them.
        icons = Gtk.IconTheme.get_for_display(display)
        icons.set_search_path([ICON_DIR] + [p for p in icons.get_search_path() or [] if p != ICON_DIR])
        self.base = Gtk.CssProvider()
        self.base.load_from_string(BASE_CSS)
        self.palette = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self.palette, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.header_widgets = []

    def apply(self, prefs):
        pal = dict(palette(prefs))
        if prefs.get("item_colors") is not False:
            pal["item_colors"] = ITEM_COLORS
        if pal.get("dark"):
            scheme = Adw.ColorScheme.FORCE_DARK  # the midnight look is dark by design
        else:
            scheme = next((s for k, _n, s in SCHEMES if k == prefs.get("theme")), Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)
        self.palette.load_from_string(build_css(pal))
        header = pal.get("dark") or pal.get("header")
        for widget in self.header_widgets:
            if header:
                widget.add_css_class("fc-header")
            else:
                widget.remove_css_class("fc-header")
        paint.gradient = [_hex_to_rgba(c) for c in pal["accent"]] if pal.get("accent") else None
        paint.item_colors = [_hex_to_rgba(c) for c in pal["item_colors"]] if pal.get("item_colors") else None
