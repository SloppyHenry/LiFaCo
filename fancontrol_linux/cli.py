"""fancontrol-linuxctl: command line access to the daemon."""

import argparse
import json
import sys
import time

from .ipc import Client, DaemonError


def _name(entry, sid):
    return entry.get("name") or entry.get("label") or sid


def print_status(st):
    print(f"Profil: {st.get('profile')}")
    if st.get("safety_active"):
        print("!! Sicherheitstemperatur erreicht – alle Lüfter auf 100 %")
    print("\nTemperaturen:")
    for sid, t in st["temps"].items():
        v = "–" if t["value"] is None else f"{t['value']:.1f} °C"
        print(f"  {_name(t, sid):40} {v:>10}")
    print("\nLüfter:")
    for sid, f in st["fans"].items():
        v = "–" if f["value"] is None else f"{f['value']} RPM"
        print(f"  {_name(f, sid):40} {v:>10}")
    print("\nSteuerungen:")
    for sid, p in st["pwms"].items():
        pct = "–" if p["percent"] is None else f"{p['percent']:.0f} %"
        mode = "gesteuert" if p["controlled"] else "Auto/BIOS"
        if p.get("overridden"):
            mode += " (?)"
        err = f"  FEHLER: {p['error']}" if p.get("error") else ""
        print(f"  {_name(p, sid):40} {pct:>6}  {mode}  [{sid}]{err}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fancontrol-linuxctl", description="FanControl for Linux – Steuerung per Kommandozeile")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="aktuelle Werte anzeigen")
    sub.add_parser("profiles", help="Profile auflisten")
    for name, help_ in (("load", "Profil laden"), ("save", "aktuelle Konfiguration als Profil speichern"), ("delete", "Profil löschen")):
        sub.add_parser(name, help=help_).add_argument("name")
    cal = sub.add_parser("calibrate", help="Lüfter kalibrieren (Start-/Stopp-Punkt messen)")
    cal.add_argument("control", help="ID der Steuerung, siehe 'status'")
    ident = sub.add_parser("identify", help="Lüfter kurz auf 100 %% drehen, um ihn zu finden")
    ident.add_argument("control")
    ident.add_argument("--seconds", type=float, default=10)
    sub.add_parser("rescan", help="Hardware neu erkennen")
    sub.add_parser("export", help="aktive Konfiguration als JSON ausgeben")
    imp = sub.add_parser("import", help="Konfiguration aus JSON-Datei übernehmen")
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
            print(f"Profil '{args.name}' geladen")
        elif args.cmd == "save":
            client.call("save_profile", name=args.name)
            print(f"Profil '{args.name}' gespeichert")
        elif args.cmd == "delete":
            client.call("delete_profile", name=args.name)
            print(f"Profil '{args.name}' gelöscht")
        elif args.cmd == "identify":
            client.call("identify", control=args.control, seconds=args.seconds)
            print(f"{args.control} läuft {args.seconds:g} s auf 100 %")
        elif args.cmd == "rescan":
            print_status(client.call("rescan"))
        elif args.cmd == "export":
            print(json.dumps(client.call("get_config"), indent=2, ensure_ascii=False))
        elif args.cmd == "import":
            with open(args.file) as f:
                client.call("set_config", config=json.load(f))
            print("Konfiguration übernommen")
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
                print("\nAbgebrochen")
                return 1
            print()
            if cal["error"]:
                print(f"Fehler: {cal['error']}")
                return 1
            r = cal["result"]
            print(f"Lüfter: {r['fan']}  max. {r['max_rpm']} RPM")
            print(f"Empfohlen: Min {r['suggested_min_percent']:.0f} %, Start {r['suggested_start_percent']:.0f} %")
            for pct, rpm in r["rpm_curve"]:
                print(f"  {pct:3d} % -> {rpm} RPM")
    except DaemonError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
