"""Drawing helpers shared by the graph, ring gauge and sparkline: the theme's accent gradient."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Graphene, Gsk  # noqa: E402

# Set by the theme: list of Gdk.RGBA (1 = solid accent, more = gradient). None = desktop accent colour.
gradient = None


def rgba(red, green, blue, alpha=1.0):
    # Gdk.RGBA(red=...) ignores its arguments in PyGObject < 3.52, so set the fields explicitly.
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = red, green, blue, alpha
    return color


def parse(color):
    c = Gdk.RGBA()
    c.parse(color)
    return c


def with_alpha(c, alpha):
    return rgba(c.red, c.green, c.blue, alpha)


def colors():
    if gradient:
        return gradient
    style = Adw.StyleManager.get_default()
    if hasattr(style, "get_accent_color_rgba"):
        return [style.get_accent_color_rgba()]
    return [rgba(0.21, 0.52, 0.89)]


def color_at(t):
    """Colour of the gradient at position t (0..1)."""
    stops = colors()
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    f = t - i
    a, b = stops[i], stops[i + 1]
    return rgba(a.red + (b.red - a.red) * f, a.green + (b.green - a.green) * f,
                a.blue + (b.blue - a.blue) * f, a.alpha + (b.alpha - a.alpha) * f)


def _stops(alpha):
    result = []
    items = colors()
    for i, c in enumerate(items):
        stop = Gsk.ColorStop()
        stop.offset = i / (len(items) - 1) if len(items) > 1 else 0.0
        stop.color = with_alpha(c, c.alpha * alpha)
        result.append(stop)
    if len(result) == 1:
        second = Gsk.ColorStop()
        second.offset, second.color = 1.0, result[0].color
        result.append(second)
    return result


def _horizontal(snap, x, y, w, h, alpha):
    snap.append_linear_gradient(Graphene.Rect().init(x, y, w, h), Graphene.Point().init(x, y),
                                Graphene.Point().init(x + w, y), _stops(alpha))


def stroke(snap, path, width, bounds, alpha=1.0, dash=None):
    """Stroke a path with the gradient running left to right across bounds (x, y, w, h)."""
    s = Gsk.Stroke.new(width)
    s.set_line_cap(Gsk.LineCap.ROUND)
    s.set_line_join(Gsk.LineJoin.ROUND)
    if dash:
        s.set_dash(dash)
    snap.push_stroke(path, s)
    _horizontal(snap, *bounds, alpha)
    snap.pop()


def fill(snap, path, bounds, alpha=1.0, fade=True):
    """Fill a path with the gradient; with fade the fill becomes transparent towards the bottom."""
    x, y, w, h = bounds
    snap.push_fill(path, Gsk.FillRule.WINDING)
    if fade:
        # push_mask records the mask first (opaque at the top, transparent at the bottom), then the source.
        snap.push_mask(Gsk.MaskMode.ALPHA)
        snap.append_linear_gradient(Graphene.Rect().init(x, y, w, h), Graphene.Point().init(x, y),
                                    Graphene.Point().init(x, y + h),
                                    [_stop(0.0, rgba(0, 0, 0, 1.0)), _stop(1.0, rgba(0, 0, 0, 0.0))])
        snap.pop()
        _horizontal(snap, x, y, w, h, alpha)
        snap.pop()
    else:
        _horizontal(snap, x, y, w, h, alpha)
    snap.pop()


def _stop(offset, color):
    stop = Gsk.ColorStop()
    stop.offset, stop.color = offset, color
    return stop
