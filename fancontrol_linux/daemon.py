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

from . import __version__
from . import config as cfgmod
from .engine import Engine
from .hwmon import Hardware
from .ipc import read_line, socket_path
from .lighting.liquidctl_lighting import LiquidctlLighting
from .lighting.manager import LightingManager

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
        self.lighting = LightingManager(temps=self._temp_values)
        self.lighting.set_builtin(LiquidctlLighting(lambda: self.hw.liquidctl.devices), "liquidctl (built in)")
        self.lighting.refresh()

    def _temp_values(self):
        temps = (self.engine.status_cache or {}).get("temps") or {}
        values = {sid: t.get("value") for sid, t in temps.items()}
        for pid, p in ((self.engine.status_cache or {}).get("pwms") or {}).items():   # fan outputs, for effects
            values[f"fan:{pid}"] = p.get("percent")
        return values

    # --- commands -------------------------------------------------------
    def handle(self, request):
        cmd = request.get("cmd")
        handler = getattr(self, f"cmd_{cmd}", None) if isinstance(cmd, str) else None
        if handler is None:
            raise ValueError(f"Unknown command: {cmd}")
        if cmd.startswith(("plugin_", "light_")):
            return handler(request)     # lighting has its own lock: a slow plugin must not delay fan control
        with self.lock:
            return handler(request)

    def cmd_ping(self, _):
        return {"version": __version__}

    def cmd_status(self, _):
        idle = self.engine.clock() >= self.engine.viewer_until
        self.engine.watched()
        # After a quiet phase the cached status lacks fresh display values – measure once right away.
        return self.engine.tick() if idle or not self.engine.status_cache else self.engine.status_cache

    def cmd_get_config(self, _):
        return self.engine.config

    def _apply(self, cfg):
        cfg = cfgmod.normalize(cfg)
        cfgmod.save_config(cfg)
        self.engine.set_config(cfg)
        return cfg

    def cmd_set_config(self, req):
        cfg = req.get("config")
        if isinstance(cfg, dict):     # lighting is owned by the lighting manager; clients must not overwrite it
            cfg = dict(cfg, lighting=self.lighting.profile_data())
        return self._apply(cfg)

    def cmd_list_profiles(self, _):
        return {"profiles": cfgmod.list_profiles(), "active": self.engine.config.get("profile")}

    def cmd_save_profile(self, req):
        cfg = cfgmod.save_profile(req.get("name", ""), dict(self.engine.config, lighting=self.lighting.profile_data()))
        return self._apply(cfg)

    def cmd_load_profile(self, req):
        cfg = cfgmod.load_profile(req.get("name", ""))
        if cfg.get("lighting") is not None:       # profiles saved before lighting existed leave the lights alone
            self.lighting.load_profile_data(cfg["lighting"])
        return self._apply(cfg)

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
        self.lighting.rescan()
        return self.engine.tick()

    # --- lighting ---------------------------------------------------------
    def cmd_plugin_list(self, _):
        return self.lighting.list_plugins()

    def cmd_plugin_catalog(self, req):
        return self.lighting.catalog_search(str(req.get("query", "")), bool(req.get("refresh")))

    def cmd_plugin_install(self, req):
        self.lighting.install_catalog(str(req.get("id", "")))
        return self.lighting.list_plugins()

    def cmd_plugin_install_file(self, req):
        self.lighting.install_file(req.get("data", ""))
        return self.lighting.list_plugins()

    def cmd_plugin_remove(self, req):
        self.lighting.remove(str(req.get("id", "")))
        return self.lighting.list_plugins()

    def cmd_plugin_enable(self, req):
        self.lighting.enable(str(req.get("id", "")), bool(req.get("enabled")), bool(req.get("approve")))
        return self.lighting.list_plugins()

    def cmd_plugin_settings(self, req):
        self.lighting.set_settings(str(req.get("id", "")), req.get("settings"))
        return self.lighting.list_plugins()

    def cmd_plugin_restart(self, req):
        self.lighting.restart(str(req.get("id", "")))
        return self.lighting.list_plugins()

    def cmd_light_devices(self, _):
        return self.lighting.list_devices()

    def cmd_light_set(self, req):
        self.lighting.set_effect(str(req.get("device", "")), req.get("effect"))
        return self.lighting.list_devices()

    def cmd_light_power(self, req):
        self.lighting.power(str(req.get("device", "")), bool(req.get("on")))
        return self.lighting.list_devices()

    def cmd_light_power_all(self, req):
        return {"count": self.lighting.power_all(bool(req.get("on")))}

    def cmd_light_settings(self, req):
        if "off_on_exit" in req:
            return self.lighting.set_light_settings(req)
        return self.lighting.get_light_settings()

    def cmd_light_rescan(self, _):
        self.lighting.rescan()
        return self.lighting.list_devices()

    def cmd_light_identify(self, req):
        self.lighting.identify(str(req.get("device", "")))
        return None

    # --- loop -----------------------------------------------------------
    def run(self):
        log.info("Started: %d temperature sensors, %d fan sensors, %d fan outputs",
                 len(self.hw.temps), len(self.hw.fans), len(self.hw.pwms))
        self.lighting.start()
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
            self.lighting.shutdown()
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
    parser = argparse.ArgumentParser(description="LiFaCo – background service")
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
