#!/usr/bin/env python3
"""Generates the symbolic icons in fancontrol_linux/gui/icons (fill-only shapes, so GTK can recolor them)."""

import math
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "fancontrol_linux", "gui", "icons",
                   "hicolor", "scalable", "actions")
W = 1.6


def seg(x1, y1, x2, y2, w=W):
    length = math.hypot(x2 - x1, y2 - y1)
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    return (f'<rect x="{x1:.2f}" y="{y1 - w / 2:.2f}" width="{length:.2f}" height="{w}" '
            f'transform="rotate({angle:.2f} {x1:.2f} {y1:.2f})"/>')


def dot(x, y, r):
    return f'<circle cx="{x}" cy="{y}" r="{r}"/>'


def polyline(points, w=W):
    parts = [seg(*a, *b, w) for a, b in zip(points, points[1:])]
    parts += [dot(x, y, w / 2) for x, y in points]
    return "".join(parts)


def ring(cx, cy, r_out, r_in):
    def circ(r):
        return f"M{cx - r},{cy}a{r},{r} 0 1,0 {2 * r},0a{r},{r} 0 1,0 {-2 * r},0z"
    return f'<path fill-rule="evenodd" d="{circ(r_out)}{circ(r_in)}"/>'


def arc_band(cx, cy, r_out, r_in, start, end):
    """Filled ring sector from start to end angle (degrees, SVG orientation, clockwise)."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    large = 1 if (end - start) % 360 > 180 else 0
    (x1, y1), (x2, y2) = pt(r_out, start), pt(r_out, end)
    (x3, y3), (x4, y4) = pt(r_in, end), pt(r_in, start)
    return (f'<path d="M{x1:.2f},{y1:.2f}A{r_out},{r_out} 0 {large},1 {x2:.2f},{y2:.2f}'
            f'L{x3:.2f},{y3:.2f}A{r_in},{r_in} 0 {large},0 {x4:.2f},{y4:.2f}Z"/>')


AXES = '<path d="M1 1h1.5v12.5H15V15H1z"/>'

ICONS = {
    "fc-home": '<path d="M8 1.2 0.8 7.6l1 1.1L2.5 8.1V15h4.2v-4.3h2.6V15h4.2V8.1l.7.6 1-1.1z"/>',
    "fc-gauge": (arc_band(8, 9.5, 7, 5.4, 150, 30) + seg(8, 9.5, 11.6, 5.6, 1.6) + dot(8, 9.5, 2)),
    "fc-curve": AXES + polyline([(4, 12), (7, 11), (10, 7), (14, 2.5)]),
    "fc-curve-graph": AXES + polyline([(4, 12), (7, 11), (10, 7), (14, 2.5)]),
    "fc-curve-linear": AXES + polyline([(4, 12), (14, 3)]),
    "fc-curve-flat": AXES + polyline([(4, 7), (14, 7)]),
    "fc-curve-mix": (polyline([(1.5, 12), (7, 5), (14.5, 3)]) + polyline([(1.5, 4), (8, 11), (14.5, 12.5)])),
    "fc-curve-trigger": AXES + polyline([(4, 11.5), (8.5, 11.5), (8.5, 4.5), (14, 4.5)]),
    "fc-thermometer": ('<rect x="6.2" y="1" width="3.6" height="10" rx="1.8"/>' + dot(8, 12, 3.2)),
    "fc-palette": ('<path fill-rule="evenodd" d="M8 1C4.1 1 1 3.9 1 7.6 1 11.3 4 14.5 7.7 14.5c1.2 0 1.8-.8 '
                   '1.8-1.6 0-.5-.2-.8-.5-1.2-.3-.3-.4-.7-.4-1.1 0-.9.7-1.5 1.6-1.5h1.9c2.2 0 3.9-1.8 3.9-3.9'
                   'C16 3.6 12.4 1 8 1zM4.2 8.6a1.3 1.3 0 1 1 0-2.6 1.3 1.3 0 0 1 0 2.6zm1.9-3.4a1.3 1.3 0 1 1 '
                   '0-2.6 1.3 1.3 0 0 1 0 2.6zm3.8 0a1.3 1.3 0 1 1 0-2.6 1.3 1.3 0 0 1 0 2.6zm2.4 2.8a1.3 1.3 0 '
                   '1 1 0-2.6 1.3 1.3 0 0 1 0 2.6z"/>'),
    "fc-settings": (ring(8, 8, 5, 2.2) + "".join(
        f'<rect x="6.8" y="0.8" width="2.4" height="3.4" rx=".5" transform="rotate({a} 8 8)"/>'
        for a in range(0, 360, 45))),
    "fc-about": (ring(8, 8, 7, 5.5) + '<rect x="7.2" y="6.8" width="1.6" height="5" rx=".5"/>' + dot(8, 4.8, 1)),
    "fc-fan": ("".join(f'<ellipse cx="8" cy="4.2" rx="2.3" ry="3.4" transform="rotate({a} 8 8)"/>'
                       for a in (20, 110, 200, 290)) + dot(8, 8, 1.8)),
    "fc-curve-sync": (polyline([(1.5, 5), (14.5, 5)]) + polyline([(1.5, 11), (14.5, 11)])
                      + '<path d="M11 2l3.5 3L11 8zM5 8l-3.5 3L5 14z"/>'),
    "fc-curve-auto": (AXES + "".join(f'<rect x="{x}" y="5.2" width="1.6" height="1.6"/>' for x in (4, 7, 10, 13))
                      + polyline([(4, 12.5), (8, 11), (11, 7.5), (14, 6)])),
    "fc-custom-sensor": ('<rect x="2.2" y="1" width="3.6" height="10" rx="1.8"/>' + dot(4, 12, 3.2)
                         + '<rect x="8" y="3" width="7" height="1.6" rx=".5"/><rect x="8" y="7" width="5" height="1.6" rx=".5"/>'
                         + '<rect x="8" y="11" width="7" height="1.6" rx=".5"/>'),
    "fc-eye": ('<path fill-rule="evenodd" d="M8 3C4.5 3 1.8 5.4.5 8c1.3 2.6 4 5 7.5 5s6.2-2.4 7.5-5C14.2 5.4 11.5 3 8 3z'
               'm0 1.6a3.4 3.4 0 1 1 0 6.8 3.4 3.4 0 0 1 0-6.8z"/>' + dot(8, 8, 1.8)),
    "fc-keyboard": ('<path fill-rule="evenodd" d="M1 3.5h14v9H1zm1.5 1.5v6h11V5z"/>'
                    + "".join(f'<rect x="{x}" y="6" width="1.6" height="1.6"/>' for x in (3.5, 6, 8.5, 11))
                    + '<rect x="4.5" y="8.6" width="7" height="1.6"/>'),
    "fc-profile": ('<rect x="1" y="2" width="14" height="2" rx=".6"/><rect x="1" y="7" width="9" height="2" rx=".6"/>'
                   '<rect x="1" y="12" width="11" height="2" rx=".6"/><path d="M12 6.5l1.2 2 2.3.3-1.7 1.6.4 2.2-2.2-1'
                   '-2 1 .4-2.2-1.7-1.6 2.3-.3z"/>'),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, body in ICONS.items():
        with open(os.path.join(OUT, f"{name}-symbolic.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                    f'<g fill="#2e3436">{body}</g></svg>\n')
    print(f"Wrote {len(ICONS)} icons to {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
