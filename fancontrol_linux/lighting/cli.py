"""Command line for plugins and lighting (used by fancontrol-linuxctl / lifacoctl)."""

import argparse
import base64
import os
import sys

from ..ipc import Client, DaemonError
from . import devtools, effects, store
from .manifest import ManifestError


def add_parsers(sub):
    plugin = sub.add_parser("plugin", help="manage lighting plugins").add_subparsers(dest="action", required=True)
    plugin.add_parser("list", help="installed plugins")
    s = plugin.add_parser("search", help="search the plugin catalog")
    s.add_argument("query", nargs="*")
    i = plugin.add_parser("install", help="install from the catalog (id), a .zip file or a plugin folder")
    i.add_argument("source")
    plugin.add_parser("remove", help="remove a plugin").add_argument("id")
    e = plugin.add_parser("enable", help="enable a plugin (shows what it may do and asks first)")
    e.add_argument("id")
    e.add_argument("--yes", action="store_true", help="approve the permissions without asking")
    plugin.add_parser("disable", help="disable a plugin").add_argument("id")
    st = plugin.add_parser("set", help="change plugin settings: key=value ...")
    st.add_argument("id")
    st.add_argument("values", nargs="+")
    n = plugin.add_parser("new", help="create a plugin skeleton to build on")
    n.add_argument("id")
    n.add_argument("--dir", default=".")
    plugin.add_parser("validate", help="check a plugin folder").add_argument("folder")
    p = plugin.add_parser("pack", help="make a .zip of a plugin folder")
    p.add_argument("folder")
    p.add_argument("-o", "--output")
    d = plugin.add_parser("dev", help="run a plugin folder on its own (no service needed) to see what it finds")
    d.add_argument("folder")
    d.add_argument("rest", nargs=argparse.REMAINDER, help="key=value settings and --color ff8800")

    light = sub.add_parser("light", help="control lighting").add_subparsers(dest="action", required=True)
    light.add_parser("list", help="found LED devices")
    ls = light.add_parser("set", help="set an effect: off | static COLOR | breathing COLOR | rainbow | "
                                      "temperature [SENSOR] | mode NAME [COLOR ...] | release")
    ls.add_argument("device")
    ls.add_argument("effect")
    ls.add_argument("args", nargs="*")
    ls.add_argument("--brightness", type=float, default=100)
    ls.add_argument("--speed", type=float, default=50)
    light.add_parser("identify", help="blink a device").add_argument("device")
    light.add_parser("rescan", help="look for devices again")


def _rgb(text):
    text = text.lstrip("#")
    try:
        return [int(text[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        raise ValueError(f"Not a colour: {text} (use six hex digits like ff8800)") from None


def _effect(args):
    kind = args.effect
    if kind == "release":
        return None
    e = {"type": kind, "brightness": args.brightness, "speed": args.speed}
    if kind in ("static", "breathing"):
        e["color"] = _rgb(args.args[0]) if args.args else [255, 255, 255]
    elif kind == "temperature":
        e["sensor"] = args.args[0] if args.args else ""
    elif kind == "mode":
        if not args.args:
            raise ValueError("Give the effect name: light set DEVICE mode NAME")
        e = {"type": "hardware", "mode": args.args[0], "colors": [_rgb(c) for c in args.args[1:]],
             "brightness": args.brightness, "speed": args.speed}
    elif kind not in effects.EFFECT_TYPES:
        raise ValueError(f"Unknown effect '{kind}'")
    return e


def _print_plugins(plugins):
    for p in plugins:
        if p.get("broken"):
            print(f"  ✗ {p['id']:20} broken: {p['error']}")
            continue
        mark = "●" if p["status"] == "running" else "○"
        extra = {"needs_approval": " (needs approval)", "error": f" – {p['error']}"}.get(p["status"], "")
        print(f"  {mark} {p['id']:20} {p['name']:28} {p['version']:8} {p['status']}{extra}")


def run(args):
    """Returns the exit code."""
    if args.cmd == "plugin" and args.action in ("new", "validate", "pack", "dev"):
        return _dev_command(args)
    client = Client(timeout=120)
    try:
        return _daemon_command(args, client)
    except (DaemonError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _dev_command(args):
    try:
        if args.action == "new":
            path = devtools.new_plugin(args.dir, args.id)
            print(f"Created {path}\nNext: cd {path} && lifacoctl plugin dev .   (edit plugin.py, then repeat)")
        elif args.action == "validate":
            manifest, warnings = devtools.validate(args.folder)
            print(f"✓ {manifest['name']} {manifest['version']} ({manifest['id']}) is valid")
            for w in warnings:
                print(f"  ! {w}")
        elif args.action == "pack":
            manifest, warnings = devtools.validate(args.folder)
            output, _m = devtools.pack(args.folder, args.output)
            print(f"Wrote {output}  – install it with: lifacoctl plugin install {output}")
        else:
            return devtools.dev_run(args.folder, args.rest)
    except (ManifestError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _daemon_command(args, client):
    if args.cmd == "plugin":
        a = args.action
        if a == "list":
            _print_plugins(client.call("plugin_list"))
        elif a == "search":
            data = client.call("plugin_catalog", query=" ".join(args.query))
            for p in data["plugins"]:
                mark = f"installed {p['installed']}" + (" – update available" if p["update"] else "") if p["installed"] else ""
                print(f"  {p['id']:20} {p['name']:28} {p['version']:8} {mark}\n      {p['description']}")
            if not data["plugins"]:
                print("Nothing found.")
        elif a == "install":
            src = args.source
            if os.path.isdir(src):
                _m, data = store.pack_directory(src)
                client.call("plugin_install_file", data=base64.b64encode(data).decode())
            elif os.path.isfile(src):
                with open(src, "rb") as f:
                    client.call("plugin_install_file", data=base64.b64encode(f.read()).decode())
            else:
                client.call("plugin_install", id=src)
            print("Installed. Enable it with: lifacoctl plugin enable <id>")
        elif a == "remove":
            client.call("plugin_remove", id=args.id)
            print(f"Removed {args.id}")
        elif a == "enable":
            plugins = {p["id"]: p for p in client.call("plugin_list")}
            p = plugins.get(args.id)
            if not p:
                raise ValueError(f"Plugin '{args.id}' is not installed")
            approve = args.yes
            if p["needs_approval"] and not args.yes:
                print(f"{p['name']} asks for permission to:")
                for line in p["permission_lines"]:
                    print(f"  - {line}")
                approve = input("Allow this? [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
                if not approve:
                    return 1
            client.call("plugin_enable", id=args.id, enabled=True, approve=approve)
            print(f"{args.id} enabled")
        elif a == "disable":
            client.call("plugin_enable", id=args.id, enabled=False)
            print(f"{args.id} disabled")
        elif a == "set":
            values = dict(v.split("=", 1) for v in args.values if "=" in v)
            _print_plugins([p for p in client.call("plugin_settings", id=args.id, settings=values) if p["id"] == args.id])
    else:
        a = args.action
        if a == "list":
            for d in client.call("light_devices"):
                effect = (d["effect"] or {}).get("type") or "not controlled"
                kind = "direct" if d["direct"] else "hardware effects only"
                print(f"  {d['key']:34} {d['name']:30} {d['leds']:4} LEDs  {kind:22} {effect}")
                if d["error"]:
                    print(f"      ! {d['error']}")
        elif a == "set":
            client.call("light_set", device=args.device, effect=_effect(args))
            print("OK")
        elif a == "identify":
            client.call("light_identify", device=args.device)
        elif a == "rescan":
            print(f"{len(client.call('light_rescan'))} devices")
    return 0
