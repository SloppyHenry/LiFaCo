"""Shared building blocks for the card-based pages."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from .util import c_to_disp, delta_to_disp, disp_to_c, disp_to_delta, temp_unit  # noqa: E402

CARD_WIDTH = 300
CARD_MAX_WIDTH = 300


def status_page(title, description, icon="dialog-information-symbolic", button=None):
    page = Adw.StatusPage(title=title, description=description, icon_name=icon)
    if button:
        label, callback = button
        b = Gtk.Button(label=label, halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        b.connect("clicked", lambda *_: callback())
        page.set_child(b)
    return page


def section(title, help_text):
    """Title row (+ help popover) and a flow grid for cards."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    head = Gtk.Box(spacing=8)
    head.append(Gtk.Label(label=title, css_classes=["fc-page-title"]))
    help_label = Gtk.Label(label=help_text, wrap=True, max_width_chars=52, xalign=0,
                           margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
    head.append(Gtk.MenuButton(child=Gtk.Label(label="?"), css_classes=["circular", "flat"],
                               valign=Gtk.Align.CENTER, popover=Gtk.Popover(child=help_label),
                               tooltip_text="Help"))
    box.append(head)
    flow = CardFlow()
    box.append(flow)
    return box, flow


class CardFlow(Gtk.FlowBox):
    """Card grid where each row is as tall as its tallest card.

    A homogeneous FlowBox would give every cell the height of the tallest card on the page, so expanding
    one card would add space below all of them. Instead every card is clamped to the same width.
    """

    def __init__(self):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=12,
                         row_spacing=12, min_children_per_line=1, max_children_per_line=8,
                         valign=Gtk.Align.START, css_classes=["fc-cards"])

    def append(self, widget):
        widget.set_size_request(CARD_WIDTH, -1)
        clamp = Adw.Clamp(child=widget, maximum_size=CARD_MAX_WIDTH, tightening_threshold=CARD_MAX_WIDTH,
                          valign=Gtk.Align.START)
        super().append(clamp)


def scroller(child):
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=16, margin_bottom=96,
                    margin_start=16, margin_end=16)
    for c in child if isinstance(child, (list, tuple)) else [child]:
        outer.append(c)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sw.set_child(outer)
    return sw


def grid_page(title, help_text):
    box, flow = section(title, help_text)
    return scroller(box), flow


def fab(tooltip, entries):
    """Round + button with a popover; entries: [(icon, label, callback)]."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                  margin_start=6, margin_end=6)
    popover = Gtk.Popover(child=box, position=Gtk.PositionType.TOP)
    for icon, label, callback in entries:
        b = Gtk.Button(child=Adw.ButtonContent(icon_name=icon, label=label, halign=Gtk.Align.START),
                       css_classes=["flat"])
        b.connect("clicked", lambda _b, cb=callback: (popover.popdown(), cb()))
        box.append(b)
    return Gtk.MenuButton(icon_name="list-add-symbolic", popover=popover, halign=Gtk.Align.END,
                          valign=Gtk.Align.END, margin_end=24, margin_bottom=24,
                          css_classes=["fc-fab", "suggested-action"], tooltip_text=tooltip)


def caption(text):
    # A bounded natural width keeps long captions from widening every card in the grid.
    return Gtk.Label(label=text, css_classes=["fc-caption"], xalign=0, wrap=True, max_width_chars=30)


def labeled(text, widget):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    box.append(caption(text))
    box.append(widget)
    return box


def spin(value, lo, hi, step, apply, digits=0):
    s = Gtk.SpinButton.new_with_range(lo, hi, step)
    s.set_digits(digits)
    s.set_value(value)
    s.set_hexpand(True)
    s.connect("value-changed", lambda w: apply(w.get_value()))
    return s


def temp_spin(value_c, lo_c, hi_c, apply, delta=False, digits=0):
    to_disp, from_disp = (delta_to_disp, disp_to_delta) if delta else (c_to_disp, disp_to_c)
    return spin(round(to_disp(value_c), 1), to_disp(lo_c), to_disp(hi_c), 0.5 if digits else 1,
                lambda v: apply(round(from_disp(v), 2)), digits)


def tlabel(text):
    """Caption text with the temperature unit appended."""
    return f"{text} {temp_unit()}"


def dropdown(labels, selected, apply):
    dd = Gtk.DropDown(model=Gtk.StringList.new(labels), hexpand=True)
    dd.set_selected(max(0, selected))
    dd.connect("notify::selected", lambda d, _p: apply(d.get_selected()))
    return dd


def switch_line(text, active, apply, tooltip=None):
    box = Gtk.Box(spacing=8)
    box.append(Gtk.Label(label=text, hexpand=True, xalign=0, wrap=True, max_width_chars=20))
    sw = Gtk.Switch(active=bool(active), valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    sw.connect("notify::active", lambda s, _p: apply(s.get_active()))
    box.append(sw)
    return box


def name_entry(text, placeholder, apply):
    entry = Gtk.Entry(text=text, placeholder_text=placeholder, hexpand=True, css_classes=["fc-name"])
    entry.connect("changed", lambda e: apply(e.get_text().strip()))
    return entry


def card_menu(card, items):
    """⋮ menu for a card; items: [(label, callback)]."""
    group = Gio.SimpleActionGroup()
    menu = Gio.Menu()
    for i, (label, callback) in enumerate(items):
        action = Gio.SimpleAction.new(f"a{i}", None)
        action.connect("activate", lambda *_a, cb=callback: cb())
        group.add_action(action)
        menu.append(label, f"card.a{i}")
    card.insert_action_group("card", group)
    return Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, css_classes=["flat", "circular"],
                          valign=Gtk.Align.CENTER, tooltip_text="More")


def card(hidden=False):
    classes = ["card", "fc-card"] + (["fc-hidden"] if hidden else [])
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=CARD_WIDTH,
                   valign=Gtk.Align.START, css_classes=classes)


def card_header(c, icon, entry, menu_items, icon_tooltip=None, on_icon=None):
    top = Gtk.Box(spacing=8)
    image = Gtk.Image(icon_name=icon, pixel_size=28, tooltip_text=icon_tooltip)
    if on_icon:
        button = Gtk.Button(child=image, css_classes=["flat"], tooltip_text=icon_tooltip)
        button.connect("clicked", lambda *_: on_icon())
        top.append(button)
    else:
        top.append(image)
    top.append(entry)
    top.append(card_menu(c, menu_items))
    c.append(top)


def picker_list(card_box, win, selected, choices, on_change, empty_text):
    """Editable list with an 'add' dropdown and × buttons; choices: [(id, label)]."""
    labels = dict(choices)
    remaining = [(i, l) for i, l in choices if i not in selected]
    if remaining:
        def add(i):
            if i > 0:
                on_change(selected + [remaining[i - 1][0]])
        card_box.append(dropdown(["Add …"] + [l for _i, l in remaining], 0, add))
    for item in selected:
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label=labels.get(item, f"{item} (fehlt)"), xalign=0, hexpand=True, ellipsize=3))
        remove = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                            tooltip_text="Remove")
        remove.connect("clicked", lambda _b, x=item: on_change([s for s in selected if s != x]))
        row.append(remove)
        card_box.append(row)
    if not selected:
        card_box.append(caption(empty_text))


def hidden_menu_item(win, item_id):
    hidden = item_id in win.config["hidden"]
    return ("Show" if hidden else "Hide", lambda: win.toggle_hidden(item_id))
