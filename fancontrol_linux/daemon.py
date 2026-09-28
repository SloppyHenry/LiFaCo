"""fancontrol-linuxd: background service that owns the fans."""

import argparse
import grp
import json
import logging
import os
import signal
import socketserver
import threading
import time

from . import __version__, config as cfgmod
from .engine import Engine
from .hwmon import Hardware
from .ipc import read_line, socket_path

log = logging.getLogger("fancontrol-linuxd")


class Daemon:
    def __init__(self, hardware=None):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.reload_requested = False
        try:
            cfg = cfgmod.load_config()
        except (cfgmod.ConfigError, ValueError) as e:
            log.error("Invalid configuration, starting empty: %s", e)
            cfg = cfgmod.empty_config()
        self.hw = hardware or Hardware(settings=cfg["settings"])
        self.engine = Engine(self.hw, cfg)

    # --- commands -------------------------------------------------------
    def handle(self, request):
        cmd = request.get("cmd")
        handler = getattr(self, f"cmd_{cmd}", None) if isinstance(cmd, str) else None
        if handler is None:
            raise ValueError(f"Unknown command: {cmd}")
        with self.lock:
            return handler(request)

    def cmd_ping(self, _):
        return {"version": __version__}

    def cmd_status(self, _):
        return self.engine.status_cache or self.engine.tick()

    def cmd_get_config(self, _):
        return self.engine.config

    def _apply(self, cfg):
        cfg = cfgmod.normalize(cfg)
        cfgmod.save_config(cfg)
        self.engine.set_config(cfg)
        return cfg

    def cmd_set_config(self, req):
        return self._apply(req.get("config"))

    def cmd_list_profiles(self, _):
        return {"profiles": cfgmod.list_profiles(), "active": self.engine.config.get("profile")}

    def cmd_save_profile(self, req):
        cfg = cfgmod.save_profile(req.get("name", ""), self.engine.config)
        return self._apply(cfg)

    def cmd_load_profile(self, req):
        return self._apply(cfgmod.load_profile(req.get("name", "")))

    def cmd_delete_profile(self, req):
        cfgmod.delete_profile(req.get("name", ""))
        return {"profiles": cfgmod.list_profiles()}

    def cmd_calibrate(self, req):
        self.engine.start_calibration(req.get("control"), min(30.0, max(0.5, float(req.get("settle", 4.0)))))
        return self.engine.calibration.status()

    def cmd_identify(self, req):
        self.engine.identify(req.get("control"), min(60.0, max(1.0, float(req.get("seconds", 10.0)))))
        return None

    def cmd_cancel_calibration(self, _):
        self.engine.cancel_calibration()
        return None

    def cmd_rescan(self, _):
        self.engine.rescan()
        return self.engine.tick()

    # --- loop -----------------------------------------------------------
    def run(self):
        log.info("Started: %d temperature sensors, %d fan sensors, %d fan outputs",
                 len(self.hw.temps), len(self.hw.fans), len(self.hw.pwms))
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                with self.lock:
                    if self.reload_requested:
                        self.reload_requested = False
                        self._reload()
                    try:
                        self.engine.tick()
                    except Exception:
                        log.exception("Error in control cycle")
                    interval = self.engine.config["settings"]["interval"]
                self.stop_event.wait(max(0.05, interval - (time.monotonic() - started)))
        finally:
            with self.lock:
                self.engine.shutdown()
            log.info("Stopped, fan control handed back to firmware/BIOS")


    def _reload(self):
        try:
            cfg = cfgmod.load_config()
        except (cfgmod.ConfigError, ValueError) as e:
            log.error("Reload failed: %s", e)
            return
        self.engine.set_config(cfg)
        self.engine.rescan()
        log.info("Configuration reloaded")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = read_line(self.connection)
            request = json.loads(line)
            data = self.server.daemon_ref.handle(request)
            reply = {"ok": True, "data": data}
        except (cfgmod.ConfigError, ValueError, KeyError, TypeError) as e:
            reply = {"ok": False, "error": str(e)}
        except OSError as e:
            reply = {"ok": False, "error": f"System error: {e}"}
        except Exception as e:
            log.exception("Error handling request")
            reply = {"ok": False, "error": f"Internal error: {e}"}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _prepare_socket(path, group):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o117)
    try:
        server = _Server(path, _Handler)
    finally:
        os.umask(old_umask)
    if group:
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except (KeyError, PermissionError) as e:
            log.warning("Cannot set group '%s' on socket: %s", group, e)
    return server


def _prepare_sensor_dir(socket_file, group):
    """Directory where users of the group can drop *.sensor files for file sensors."""
    path = os.path.join(os.path.dirname(socket_file), "sensors")
    try:
        os.makedirs(path, exist_ok=True)
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(path, -1, gid)
        os.chmod(path, 0o2775 if group else 0o755)
    except (OSError, KeyError) as e:
        log.warning("File sensor directory not prepared: %s", e)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux FanControl – background service")
    parser.add_argument("--socket", default=socket_path())
    parser.add_argument("--group", default="fancontrol", help="group allowed to access the socket ('' = none)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if not args.verbose:
        # liquidctl logs every single write at INFO level, which would flood the journal.
        logging.getLogger("liquidctl").setLevel(logging.WARNING)

    daemon = Daemon()
    server = _prepare_socket(args.socket, args.group)
    server.daemon_ref = daemon
    _prepare_sensor_dir(args.socket, args.group)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(*_):
        daemon.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, lambda *_: setattr(daemon, "reload_requested", True))
    try:
        daemon.run()
    finally:
        server.shutdown()
        server.server_close()
        try:
            os.unlink(args.socket)
        except OSError:
            pass


if __name__ == "__main__":
    main()
