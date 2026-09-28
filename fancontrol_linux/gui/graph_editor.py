"""Interactive fan curve graph: drag points, double-click to add, right-click to remove."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk  # noqa: E402

from .util import c_to_disp, temp_unit  # noqa: E402

FULL_MARGINS = (54, 16, 14, 32)
COMPACT_MARGINS = (2, 2, 8, 2)
HIT_RADIUS = 10
MIN_POINTS = 2

# Set by the theme so graphs follow the app accent instead of the desktop accent.
accent_override = None


def rgba(red, green, blue, alpha=1.0):
    # Gdk.RGBA(red=...) ignores its arguments in PyGObject < 3.52, so set the fields explicitly.
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = red, green, blue, alpha
    return color


def _rgba(c, alpha):
    return rgba(c.red, c.green, c.blue, alpha)


def _line_path(points):
    b = Gsk.PathBuilder.new()
    b.move_to(*points[0])
    for p in points[1:]:
        b.line_to(*p)
    return b


def _circle(x, y, r):
    b = Gsk.PathBuilder.new()
    b.add_circle(Graphene.Point().init(x, y), r)
    return b.to_path()


class GraphEditor(Gtk.Widget):
    def __init__(self, points, on_changed=None, editable=True, compact=False, temp_range=None, rpm=False):
        super().__init__()
        self.rpm = rpm
        self.fixed_range = temp_range
        self.compact = compact
        self.margins = COMPACT_MARGINS if compact else FULL_MARGINS
        self.points = points  # list of [temp, speed], edited in place
        self.on_changed = on_changed
        self.editable = editable
        self.temp_range = (10.0, 100.0)
        self.live = None  # (temp, output)
        self.drag_index = None
        self.hover_index = None
        self._drag_origin = None
        self.set_size_request(-1, 70 if compact else 300)
        self.set_hexpand(True)
        self._fit_range()

        if editable:
            click = Gtk.GestureClick(button=0)
            click.connect("pressed", self._on_click)
            self.add_controller(click)
            drag = Gtk.GestureDrag(button=1)
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update)
            drag.connect("drag-end", self._on_drag_end)
            self.add_controller(drag)
            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._on_motion)
            motion.connect("leave", lambda *_: self._set_hover(None))
            self.add_controller(motion)
            self.set_tooltip_text("Punkte ziehen · Doppelklick: Punkt hinzufügen · Rechtsklick: Punkt entfernen")

    def _fit_range(self):
        temps = [p[0] for p in self.points]
        lo, hi = self.fixed_range or (10.0, 100.0)
        self.temp_range = (min([lo] + temps), max([hi] + temps))
        speeds = [p[1] for p in self.points]
        self.y_max = 100.0 if not self.rpm else max(1000.0, (max(speeds or [0]) * 1.25) // 500 * 500 + 500)

    def set_range(self, temp_range):
        self.fixed_range = temp_range
        self._fit_range()
        self.queue_draw()

    def refresh(self):
        self._fit_range()
        self.queue_draw()

    def _fmt_y(self, v):
        return f"{v:.0f} RPM" if self.rpm else f"{v:.0f} %"

    def _fmt_t(self, t, digits=0):
        return f"{c_to_disp(t):.{digits}f} {temp_unit()}"

    def set_live(self, temp, output):
        live = None if temp is None or output is None else (temp, output)
        if live != self.live:
            self.live = live
            self.queue_draw()

    # --- geometry -------------------------------------------------------
    def _plot_rect(self):
        w, h = self.get_width(), self.get_height()
        ml, mr, mt, mb = self.margins
        return ml, mt, max(1, w - ml - mr), max(1, h - mt - mb)

    def _to_screen(self, temp, speed):
        x, y, w, h = self._plot_rect()
        t0, t1 = self.temp_range
        return x + (temp - t0) / (t1 - t0) * w, y + h - speed / self.y_max * h

    def _from_screen(self, sx, sy):
        x, y, w, h = self._plot_rect()
        t0, t1 = self.temp_range
        return t0 + (sx - x) / w * (t1 - t0), (y + h - sy) / h * self.y_max

    def _hit(self, sx, sy):
        best, best_d = None, HIT_RADIUS ** 2
        for i, (t, s) in enumerate(self.points):
            px, py = self._to_screen(t, s)
            d = (px - sx) ** 2 + (py - sy) ** 2
            if d <= best_d:
                best, best_d = i, d
        return best

    # --- interaction ----------------------------------------------------
    def _set_hover(self, index):
        if index != self.hover_index:
            self.hover_index = index
            self.queue_draw()

    def _on_motion(self, _ctl, x, y):
        if self.drag_index is None:
            self._set_hover(self._hit(x, y))

    def _on_click(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        index = self._hit(x, y)
        if button == 3 and index is not None and len(self.points) > MIN_POINTS:
            del self.points[index]
            self.hover_index = None
            self._changed()
        elif button == 1 and n_press == 2 and index is None:
            t, s = self._from_screen(x, y)
            t0, t1 = self.temp_range
            if t0 <= t <= t1 and 0 <= s <= self.y_max:
                self.points.append([round(t), round(s, -1) if self.rpm else round(s)])
                self.points.sort()
                self._changed()

    def _on_drag_begin(self, _gesture, x, y):
        self.drag_index = self._hit(x, y)
        self._drag_origin = (x, y)

    def _on_drag_update(self, _gesture, dx, dy):
        i = self.drag_index
        if i is None:
            return
        t, s = self._from_screen(self._drag_origin[0] + dx, self._drag_origin[1] + dy)
        lo = self.points[i - 1][0] + 1 if i > 0 else self.temp_range[0]
        hi = self.points[i + 1][0] - 1 if i + 1 < len(self.points) else self.temp_range[1]
        self.points[i][0] = float(max(lo, min(hi, round(t))))
        self.points[i][1] = float(max(0, min(self.y_max, round(s, -1) if self.rpm else round(s))))
        self.queue_draw()

    def _on_drag_end(self, *_):
        if self.drag_index is not None:
            self.drag_index = None
            self._changed()

    def _changed(self):
        self._fit_range()
        self.queue_draw()
        self.on_changed()

    # --- drawing --------------------------------------------------------
    def _text(self, snap, text, x, y, color, anchor="left"):
        layout = self.create_pango_layout(text)
        w, h = layout.get_pixel_size()
        dx = {"left": 0, "center": -w / 2, "right": -w}[anchor]
        snap.save()
        snap.translate(Graphene.Point().init(x + dx, y - h / 2))
        snap.append_layout(layout, color)
        snap.restore()
        return w, h

    def do_snapshot(self, snap):
        fg = self.get_color()
        style = Adw.StyleManager.get_default()
        if accent_override is not None:
            accent = accent_override
        elif hasattr(style, "get_accent_color_rgba"):
            accent = style.get_accent_color_rgba()
        else:
            accent = rgba(0.21, 0.52, 0.89, 1)
        x, y, w, h = self._plot_rect()
        t0, t1 = self.temp_range
        grid, dim = _rgba(fg, 0.12), _rgba(fg, 0.6)

        for i in range(6):
            if self.compact:
                break
            value = self.y_max * i / 5
            _, sy = self._to_screen(t0, value)
            snap.append_color(grid, Graphene.Rect().init(x, sy, w, 1))
            self._text(snap, self._fmt_y(value).replace(" RPM", ""), x - 8, sy, dim, "right")
        step = 10
        t = (int(t0) // step + (1 if t0 % step else 0)) * step
        while t <= t1 and not self.compact:
            sx, _ = self._to_screen(t, 0)
            snap.append_color(grid, Graphene.Rect().init(sx, y, 1, h))
            self._text(snap, f"{c_to_disp(t):.0f}°", sx, y + h + 14, dim, "center")
            t += step

        if not self.points:
            return
        pts = sorted(self.points)
        line = [(t0, pts[0][1])] + [tuple(p) for p in pts] + [(t1, pts[-1][1])]
        screen = [self._to_screen(*p) for p in line]

        area = _line_path(screen)
        area.line_to(screen[-1][0], y + h)
        area.line_to(screen[0][0], y + h)
        area.close()
        snap.append_fill(area.to_path(), Gsk.FillRule.WINDING, _rgba(accent, 0.25 if self.compact else 0.15))
        snap.append_stroke(_line_path(screen).to_path(), Gsk.Stroke.new(2 if self.compact else 2.5), _rgba(accent, 1))

        if self.compact:
            if self.live:
                temp, out = self.live
                lx, ly = self._to_screen(max(t0, min(t1, temp)), min(out, self.y_max))
                snap.append_fill(_circle(lx, ly, 4), Gsk.FillRule.WINDING, _rgba(accent, 1))
                snap.append_stroke(_circle(lx, ly, 4), Gsk.Stroke.new(1.5), _rgba(fg, 0.9))
            return

        label = None
        for i, (pt, ps) in enumerate(self.points):
            px, py = self._to_screen(pt, ps)
            active = i in (self.hover_index, self.drag_index)
            dot = _circle(px, py, 7 if active else 5)
            snap.append_fill(dot, Gsk.FillRule.WINDING, _rgba(accent, 1))
            snap.append_stroke(dot, Gsk.Stroke.new(1.5), rgba(1, 1, 1, 0.9))
            if active:
                label = (px, py, f"{self._fmt_t(pt)} → {self._fmt_y(ps)}")

        if self.live:
            temp, out = self.live
            red = rgba(0.9, 0.3, 0.2, 0.85)
            lx, _ = self._to_screen(max(t0, min(t1, temp)), 0)
            _, ly = self._to_screen(temp, min(out, self.y_max))
            stroke = Gsk.Stroke.new(1.5)
            stroke.set_dash([4, 4])
            snap.append_stroke(_line_path([(lx, y), (lx, y + h)]).to_path(), stroke, red)
            snap.append_fill(_circle(lx, ly, 5), Gsk.FillRule.WINDING, red)
            if label is None:
                label = (lx, ly, f"{self._fmt_t(temp, 1)} → {self._fmt_y(out)}")

        if label:
            self._label(snap, *label)

    def _label(self, snap, px, py, text):
        layout = self.create_pango_layout(text)
        tw, th = layout.get_pixel_size()
        bx = min(max(px + 10, self.margins[0]), self.get_width() - tw - 14)
        by = max(py - th - 16, 2)
        bg = Gsk.RoundedRect()
        bg.init_from_rect(Graphene.Rect().init(bx - 6, by - 3, tw + 12, th + 6), 6)
        snap.push_rounded_clip(bg)
        snap.append_color(rgba(0, 0, 0, 0.75), Graphene.Rect().init(bx - 6, by - 3, tw + 12, th + 6))
        snap.pop()
        snap.save()
        snap.translate(Graphene.Point().init(bx, by))
        snap.append_layout(layout, rgba(1, 1, 1, 1))
        snap.restore()
