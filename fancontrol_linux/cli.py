"""fancontrol-linuxctl: command line access to the daemon."""

import argparse
import json
import sys
import time

from .ipc import Client, DaemonError


def _name(entry, sid):
    return entry.get("name") or entry.get("label") or sid


def print_status(st):
    print(f"Profile: {st.get('profile')}")
    if st.get("safety_active"):
        print("!! Safety temperature reached – all fans at 100 %")
    print("\nTemperatures:")
    for sid, t in st["temps"].items():
        v = "–" if t["value"] is None else f"{t['value']:.1f} °C"
        print(f"  {_name(t, sid):40} {v:>10}")
    print("\nFans:")
    for sid, f in st["fans"].items():
        v = "–" if f["value"] is None else f"{f['value']} RPM"
        print(f"  {_name(f, sid):40} {v:>10}")
    print("\nControls:")
    for sid, p in st["pwms"].items():
        pct = "–" if p["percent"] is None else f"{p['percent']:.0f} %"
        mode = "controlled" if p["controlled"] else "auto/BIOS"
        if p.get("overridden"):
            mode += " (?)"
        err = f"  ERROR: {p['error']}" if p.get("error") else ""
        print(f"  {_name(p, sid):40} {pct:>6}  {mode}  [{sid}]{err}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fancontrol-linuxctl", description="Linux FanControl – command line control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show current values")
    sub.add_parser("profiles", help="list profiles")
    for name, help_ in (("load", "load a profile"), ("save", "save the current configuration as a profile"), ("delete", "delete a profile")):
        sub.add_parser(name, help=help_).add_argument("name")
    cal = sub.add_parser("calibrate", help="calibrate a fan (measure start/stop point)")
    cal.add_argument("control", help="control ID, see 'status'")
    ident = sub.add_parser("identify", help="spin a fan at 100 %% briefly to locate it")
    ident.add_argument("control")
    ident.add_argument("--seconds", type=float, default=10)
    sub.add_parser("rescan", help="rescan hardware")
    sub.add_parser("export", help="print the active configuration as JSON")
    imp = sub.add_parser("import", help="apply a configuration from a JSON file")
    imp.add_argument("file")
    args = parser.parse_args(argv)

    client = Client()
    try:
        if args.cmd == "status":
            print_status(client.call("status"))
        elif args.cmd == "profiles":
            data = client.call("list_profiles")
            for p in data["profiles"]:
                print(("* " if p == data["active"] else "  ") + p)
        elif args.cmd == "load":
            client.call("load_profile", name=args.name)
            print(f"Profile '{args.name}' loaded")
        elif args.cmd == "save":
            client.call("save_profile", name=args.name)
            print(f"Profile '{args.name}' saved")
        elif args.cmd == "delete":
            client.call("delete_profile", name=args.name)
            print(f"Profile '{args.name}' deleted")
        elif args.cmd == "identify":
            client.call("identify", control=args.control, seconds=args.seconds)
            print(f"{args.control} runs at 100 % for {args.seconds:g} s")
        elif args.cmd == "rescan":
            print_status(client.call("rescan"))
        elif args.cmd == "export":
            print(json.dumps(client.call("get_config"), indent=2, ensure_ascii=False))
        elif args.cmd == "import":
            with open(args.file) as f:
                client.call("set_config", config=json.load(f))
            print("Configuration applied")
        elif args.cmd == "calibrate":
            client.call("calibrate", control=args.control)
            try:
                while True:
                    time.sleep(1)
                    cal = client.call("status")["calibration"]
                    print(f"\r  {cal['progress'] * 100:5.1f} %  (PWM {cal['percent'] or 0} %)   ", end="", flush=True)
                    if not cal["running"]:
                        break
            except KeyboardInterrupt:
                client.call("cancel_calibration")
                print("\nCancelled")
                return 1
            print()
            if cal["error"]:
                print(f"Error: {cal['error']}")
                return 1
            r = cal["result"]
            print(f"Fan: {r['fan']}  max. {r['max_rpm']} RPM")
            print(f"Suggested: minimum {r['suggested_min_percent']:.0f} %, start {r['suggested_start_percent']:.0f} %")
            for pct, rpm in r["rpm_curve"]:
                print(f"  {pct:3d} % -> {rpm} RPM")
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
