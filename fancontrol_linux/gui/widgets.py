"""Small custom-drawn widgets."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Graphene, Gsk, Gtk  # noqa: E402

from . import paint  # noqa: E402


class Swatch(Gtk.Widget):
    """Preview of a theme: a gradient bar for gradient themes, dots for solid colours."""

    def __init__(self, colors, gradient=False):
        super().__init__(valign=Gtk.Align.CENTER)
        self.colors, self.gradient = colors, gradient
        width = 84 if gradient else 22 * len(colors) + 4 * (len(colors) - 1)
        self.set_size_request(width, 22)

    def do_snapshot(self, snap):
        parsed = [paint.parse(c) for c in self.colors]
        if self.gradient:
            rect = Graphene.Rect().init(0, 0, self.get_width(), 22)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, 11)
            snap.push_rounded_clip(rounded)
            stops = []
            for i, c in enumerate(parsed):
                stop = Gsk.ColorStop()
                stop.offset = i / (len(parsed) - 1) if len(parsed) > 1 else 0.0
                stop.color = c
                stops.append(stop)
            if len(stops) == 1:
                snap.append_color(parsed[0], rect)
            else:
                snap.append_linear_gradient(rect, Graphene.Point().init(0, 0),
                                            Graphene.Point().init(self.get_width(), 0), stops)
            snap.pop()
            return
        for i, c in enumerate(parsed):
            rect = Graphene.Rect().init(i * 26, 0, 22, 22)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, 11)
            snap.push_rounded_clip(rounded)
            snap.append_color(c, rect)
            snap.pop()
