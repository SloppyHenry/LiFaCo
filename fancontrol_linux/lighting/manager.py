"""LightingManager: installed plugins, their processes, discovered devices and the effect render loop.

It has its own lock and threads, so a slow or broken plugin can never delay fan control.
"""

import base64
import binascii
import logging
import shutil
import threading
import time

from . import effects, store, udev
from . import manifest as mf
from .catalog import Catalog, CatalogError, catalog_url, search
from .process import PluginError, PluginProcess

log = logging.getLogger("fancontrol-linuxd")
FAST, SLOW = 0.05, 0.25         # render interval with and without animated effects
KEEPALIVE = 30.0                # resend unchanged colours this often (devices that reset, resume from sleep)
MAX_RESTARTS = 5


class LightingError(ValueError):
    pass


class PluginRuntime:
    """A plugin process plus the frames waiting to be sent to it."""

    def __init__(self, manager, info):
        self.manager = manager
        self.info = info
        self.pid = info["manifest"]["id"]
        self.state = "stopped"          # stopped | starting | running | error
        self.error = ""
        self.devices = []
        self.process = None
        self.stopping = False
        self.attempts = 0
        self.next_try = 0.0
        self.running_since = 0.0
        self.cond = threading.Condition()
        self.pending = {}               # device id -> ("colors", frame) | ("mode", effect)

    def start(self):
        """Blocking: start the process, ask for devices. Run in a thread."""
        self.stopping = False
        self.state, self.error = "starting", ""
        settings = self.manager.plugin_settings(self.pid)
        proc = PluginProcess(self.info, settings, self.manager.on_event)
        self.process = proc
        try:
            proc.start()
            devices = self._discover(proc)
        except PluginError as e:
            proc.stop()
            self.state, self.error = "error", str(e)
            self.attempts += 1
            self.next_try = time.monotonic() + min(60, 2 ** self.attempts * 2)
            log.warning("Plugin %s: %s", self.pid, e)
            return
        self.devices = devices
        self.running_since = time.monotonic()
        self.state = "running"
        self.manager.reset_device_cache(self.pid)
        threading.Thread(target=self._sender, args=(proc,), daemon=True).start()

    def _discover(self, proc):
        result = proc.call("discover", timeout=30) or {}
        devices = []
        for d in result.get("devices", []):
            try:
                devices.append(self._clean_device(d))
            except (KeyError, TypeError, ValueError):
                log.warning("Plugin %s: ignored an invalid device entry", self.pid)
        return devices

    def _clean_device(self, d):
        zones = [{"name": str(z["name"])[:60], "leds": max(0, min(int(z["leds"]), 4096))} for z in d.get("zones", [])]
        modes = [{"name": str(m["name"])[:80], "colors": max(0, min(int(m.get("colors", 0)), 8)),
                  "speed": bool(m.get("speed")), "brightness": bool(m.get("brightness"))}
                 for m in d.get("modes", [])][:300]
        dev = {"id": str(d["id"])[:120], "name": str(d["name"])[:120],
               "type": d.get("type") if d.get("type") in mf.DEVICE_TYPES else "other",
               "vendor": str(d.get("vendor") or "")[:60], "zones": zones, "modes": modes,
               "direct": bool(d.get("direct")) and sum(z["leds"] for z in zones) > 0}
        dev["leds"] = sum(z["leds"] for z in zones)
        if d.get("frame_timeout"):
            dev["frame_timeout"] = max(0.2, min(float(d["frame_timeout"]), 3600.0))
        return dev

    def rescan(self):
        if self.state != "running":
            return
        try:
            self.devices = self._discover(self.process)
        except PluginError as e:
            self.state, self.error = "error", str(e)

    def submit(self, device_id, kind, payload):
        with self.cond:
            self.pending[device_id] = (kind, payload)
            self.cond.notify()

    def _sender(self, proc):
        """Sends the newest frame per device; slow plugins skip frames instead of building up a queue."""
        while not self.stopping and proc is self.process:
            with self.cond:
                while not self.pending and not self.stopping and proc.alive():
                    self.cond.wait(1.0)
                if self.stopping or not proc.alive():
                    return
                work, self.pending = self.pending, {}
            for device_id, (kind, payload) in work.items():
                try:
                    if kind == "colors":
                        proc.call("set_colors", {"device": device_id, "colors": payload}, timeout=5)
                    else:
                        proc.call("set_mode", {"device": device_id, "mode": payload["mode"],
                                               "colors": payload.get("colors", []),
                                               "speed": payload.get("speed", 50) / 100.0,
                                               "brightness": payload.get("brightness", 100) / 100.0}, timeout=10)
                except PluginError as e:
                    if not self.stopping and proc is self.process and proc.alive():
                        log.warning("Plugin %s: %s", self.pid, e)
                        self.manager.note_device_error(self.pid, device_id, str(e))
                    elif not proc.alive() and not self.stopping:
                        self.state, self.error = "error", str(e)
                        return

    def stop(self):
        self.stopping = True
        proc, self.process = self.process, None
        with self.cond:
            self.cond.notify_all()
        if proc:
            proc.stop()
        self.devices = []
        if self.state != "error":
            self.state = "stopped"


class BuiltinRuntime:
    """Same interface as PluginRuntime for a provider that lives inside the service (see liquidctl_lighting)."""

    def __init__(self, manager, provider, name):
        self.manager, self.provider = manager, provider
        self.pid = "liquidctl"
        self.info = {"manifest": {"name": name}}
        self.state, self.error = "running", ""
        self.devices = []
        self.stopping = False
        self.attempts, self.next_try, self.running_since = 0, 0.0, 0.0
        self.cond = threading.Condition()
        self.pending = {}
        self.rescan()
        threading.Thread(target=self._sender, daemon=True, name="lighting-" + self.pid).start()

    def rescan(self):
        try:
            self.devices = self.provider.devices()
        except Exception:  # noqa: BLE001 – a broken driver must not break lighting for other devices
            log.exception("Listing %s lighting devices failed", self.pid)
            self.devices = []

    def submit(self, device_id, kind, payload):
        if kind != "colors":
            return
        with self.cond:
            self.pending[device_id] = payload
            self.cond.notify()

    def _sender(self):
        while not self.stopping:
            with self.cond:
                while not self.pending and not self.stopping:
                    self.cond.wait(1.0)
                work, self.pending = self.pending, {}
            for device_id, colors in work.items():
                try:
                    self.provider.set_colors(device_id, colors)
                except Exception as e:  # noqa: BLE001 – shown on the device card
                    self.manager.note_device_error(self.pid, device_id, str(e))

    def stop(self):
        self.stopping = True
        with self.cond:
            self.cond.notify_all()


class LightingManager:
    def __init__(self, temps=None):
        self.lock = threading.RLock()
        self.temps = temps or (lambda: {})
        self.state = store.load_state()
        self.catalog = Catalog()
        self.runtimes = {}
        self.infos, self.broken = {}, {}
        self.applied = {}       # device key -> what was last sent to it: (kind, frame/effect json, time)
        self.errors = {}        # device key -> last error text
        self.builtin = None      # (provider, display name) for lighting that lives inside the service
        self.stop_event = threading.Event()
        self.t0 = time.monotonic()
        self.thread = None

    # --- lifecycle -------------------------------------------------------------------
    def set_builtin(self, provider, name):
        self.builtin = (provider, name)

    def start(self):
        self.refresh()
        if self.builtin:
            rt = BuiltinRuntime(self, *self.builtin)
            self.runtimes[rt.pid] = rt
        with self.lock:
            for pid, entry in self.state["plugins"].items():
                if entry["enabled"] and pid in self.infos and self._approved(pid):
                    self._launch(pid)
        self._sync_udev()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="lighting")
        self.thread.start()

    def shutdown(self):
        self.stop_event.set()
        with self.lock:
            runtimes = list(self.runtimes.values())
        for rt in runtimes:
            rt.stop()

    def refresh(self):
        infos, broken = store.installed()
        with self.lock:
            self.infos, self.broken = infos, broken

    # --- helpers ---------------------------------------------------------------------
    def _entry(self, pid):
        return self.state["plugins"].setdefault(pid, {"enabled": False, "approved": "", "settings": {}, "source": "local"})

    def _info(self, pid):
        info = self.infos.get(pid)
        if not info:
            raise LightingError(f"Plugin '{pid}' is not installed")
        return info

    def _approved(self, pid):
        perms = self.infos[pid]["manifest"]["permissions"]
        if not (perms["network"] or perms["usb"] or perms["i2c"]):
            return True            # a plugin without any special access needs no approval
        return self._entry(pid)["approved"] == mf.permission_key(perms)

    def plugin_settings(self, pid):
        return mf.clean_settings(self.infos[pid]["manifest"], self._entry(pid)["settings"])

    def _save(self):
        try:
            store.save_state(self.state)
        except OSError as e:
            raise LightingError(f"Cannot save the lighting settings: {e}") from None

    def _sync_udev(self):
        with self.lock:
            approved = [(pid, self.infos[pid]["manifest"]["permissions"]) for pid, e in self.state["plugins"].items()
                        if e["enabled"] and pid in self.infos and self._approved(pid)]
        udev.sync(approved)

    def _launch(self, pid):
        rt = self.runtimes.get(pid)
        if rt:
            rt.stop()
        rt = PluginRuntime(self, self.infos[pid])
        rt.state = "starting"
        self.runtimes[pid] = rt
        threading.Thread(target=rt.start, daemon=True, name=f"plugin-{pid}").start()

    def _halt(self, pid):
        rt = self.runtimes.pop(pid, None)
        if rt:
            rt.stop()

    def reset_device_cache(self, pid):
        with self.lock:
            for key in [k for k in self.applied if k.startswith(pid + ":")]:
                del self.applied[key]

    def note_device_error(self, pid, device_id, text):
        self.errors[f"{pid}:{device_id}"] = text

    def on_event(self, pid, msg):
        if msg.get("event") == "devices_changed":
            rt = self.runtimes.get(pid)
            if rt:
                threading.Thread(target=rt.rescan, daemon=True).start()
        elif msg.get("event") == "exited":
            rt = self.runtimes.get(pid)
            if rt and not rt.stopping and rt.state in ("running", "starting"):
                rt.state, rt.error = "error", str(msg.get("reason") or "The plugin stopped")
                rt.next_try = time.monotonic() + min(60, 2 ** (rt.attempts + 1) * 2)
                rt.attempts += 1

    # --- plugins ---------------------------------------------------------------------
    def list_plugins(self):
        with self.lock:
            out = []
            rt = self.runtimes.get("liquidctl")
            if isinstance(rt, BuiltinRuntime) and rt.devices:
                out.append({"id": rt.pid, "name": rt.info["manifest"]["name"], "builtin": True, "version": "",
                            "description": "AIO coolers, fan hubs and LED controllers found through liquidctl. "
                                           "Built into LiFaCo; follows the liquidctl switch in Settings → Hardware.",
                            "author": "LiFaCo", "license": "MIT", "homepage": "", "tags": [],
                            "permissions": {"network": False, "usb": [], "i2c": False},
                            "permission_lines": [], "needs_approval": False, "settings_schema": [], "settings": {},
                            "enabled": True, "status": "running", "error": "", "source": "builtin",
                            "devices": len(rt.devices), "missing_commands": []})
            for pid, info in sorted(self.infos.items()):
                m = info["manifest"]
                entry = self._entry(pid)
                rt = self.runtimes.get(pid)
                approved = self._approved(pid)
                if entry["enabled"] and not approved:
                    status = "needs_approval"
                elif rt:
                    status = rt.state
                else:
                    status = "stopped"
                out.append({"id": pid, "name": m["name"], "version": m["version"], "description": m["description"],
                            "author": m["author"], "license": m["license"], "homepage": m["homepage"],
                            "tags": m["tags"], "permissions": m["permissions"],
                            "permission_lines": mf.permission_lines(m["permissions"]),
                            "needs_approval": not approved, "settings_schema": m["settings"],
                            "settings": self.plugin_settings(pid), "enabled": entry["enabled"],
                            "status": status, "error": rt.error if rt and rt.state == "error" else "",
                            "source": entry["source"], "devices": len(rt.devices) if rt else 0,
                            "missing_commands": [c for c in m["requires"]["commands"] if not _which(c)]})
            for name, err in sorted(self.broken.items()):
                out.append({"id": name, "name": name, "broken": True, "error": err, "status": "error"})
            return out

    def enable(self, pid, enabled, approve=False):
        with self.lock:
            info = self._info(pid)
            entry = self._entry(pid)
            if enabled:
                perms = info["manifest"]["permissions"]
                if approve:
                    entry["approved"] = mf.permission_key(perms)
                if not self._approved(pid):
                    raise LightingError("The plugin asks for permissions that you have not approved yet")
                entry["enabled"] = True
                self._save()
                self._launch(pid)
            else:
                entry["enabled"] = False
                self._save()
                self._halt(pid)
        self._sync_udev()

    def set_settings(self, pid, values):
        with self.lock:
            info = self._info(pid)
            entry = self._entry(pid)
            entry["settings"] = mf.clean_settings(info["manifest"], values)
            self._save()
            if entry["enabled"] and self._approved(pid):
                self._launch(pid)

    def restart(self, pid):
        with self.lock:
            self._info(pid)
            if not self._entry(pid)["enabled"] or not self._approved(pid):
                raise LightingError("The plugin is not enabled")
            self._launch(pid)

    def _after_install(self, manifest, source):
        pid = manifest["id"]
        self.refresh()
        with self.lock:
            entry = self._entry(pid)
            entry["source"] = source
            self._save()
            if entry["enabled"] and self._approved(pid):
                self._launch(pid)
        self._sync_udev()

    def install_catalog(self, pid):
        url = catalog_url(self.state)
        try:
            entry = self.catalog.find(url, pid)
            data = self.catalog.download(url, entry)
        except CatalogError as e:
            raise LightingError(str(e)) from None
        self._install(data, "catalog", pid, entry["version"])

    def install_file(self, b64):
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise LightingError("The uploaded file is damaged") from None
        self._install(data, "local", None, None)

    def _install(self, data, source, expect_id, expect_version):
        try:
            with self.lock:
                # Stop the running copy first: its folder is replaced.
                pid = expect_id or store.peek_id(data)
                if pid:
                    self._halt(pid)
                manifest = store.install_package(data, expect_id, expect_version)
        except (store.InstallError, mf.ManifestError) as e:
            raise LightingError(str(e)) from None
        self._after_install(manifest, source)
        log.info("Plugin %s %s installed", manifest["id"], manifest["version"])

    def remove(self, pid):
        with self.lock:
            self._halt(pid)
            try:
                store.remove_plugin(pid)
            except store.InstallError as e:
                raise LightingError(str(e)) from None
            self.state["plugins"].pop(pid, None)
            for key in [k for k in self.state["devices"] if k.startswith(pid + ":")]:
                del self.state["devices"][key]
            self._save()
        self.refresh()
        self._sync_udev()

    def catalog_search(self, query="", refresh=False):
        url = catalog_url(self.state)
        try:
            entries = self.catalog.entries(url, force=refresh)
        except CatalogError as e:
            raise LightingError(str(e)) from None
        with self.lock:
            versions = {pid: i["manifest"]["version"] for pid, i in self.infos.items()}
        out = []
        for e in search(entries, query):
            item = dict(e)
            item["installed"] = versions.get(e["id"])
            item["update"] = bool(item["installed"]) and mf.version_tuple(e["version"]) > mf.version_tuple(item["installed"])
            item["permission_lines"] = mf.permission_lines(e["permissions"])
            out.append(item)
        return {"url": url, "plugins": out}

    # --- devices ---------------------------------------------------------------------
    def list_devices(self):
        with self.lock:
            out = []
            for pid, rt in sorted(self.runtimes.items()):
                if rt.state != "running":
                    continue
                for d in rt.devices:
                    key = f"{pid}:{d['id']}"
                    out.append(dict(d, key=key, plugin=pid, plugin_name=rt.info["manifest"]["name"],
                                    effect=self.state["devices"].get(key), error=self.errors.get(key, "")))
            return out

    def _device(self, key):
        pid, _, did = key.partition(":")
        rt = self.runtimes.get(pid)
        if rt and rt.state == "running":
            for d in rt.devices:
                if d["id"] == did:
                    return rt, d
        raise LightingError("This device is not available (is its plugin running?)")

    def set_effect(self, key, effect):
        with self.lock:
            rt, dev = self._device(key)
            if effect is None:
                self.state["devices"].pop(key, None)
                self.applied.pop(key, None)
                self._save()
                return
            effect = effects.normalize(effect)
            if effect["type"] == "hardware":
                if effect["mode"] not in [m["name"] for m in dev["modes"]]:
                    raise LightingError(f"The device has no effect called '{effect['mode']}'")
            elif not dev["direct"]:
                raise LightingError("This device can only run its own hardware effects")
            self.state["devices"][key] = effect
            self.applied.pop(key, None)
            self._save()
        self.errors.pop(key, None)

    def rescan(self):
        with self.lock:
            runtimes = list(self.runtimes.values())
        for rt in runtimes:
            rt.rescan()

    def identify(self, key):
        with self.lock:
            rt, dev = self._device(key)
        if not dev["direct"]:
            raise LightingError("This device cannot blink (it has no direct colour control)")
        count = dev["leds"]

        def blink():
            for i in range(6):
                colour = (255, 255, 255) if i % 2 == 0 else (0, 0, 0)
                rt.submit(dev["id"], "colors", [colour] * count)
                time.sleep(0.4)
            self.applied.pop(key, None)   # the render loop restores the assigned effect
        threading.Thread(target=blink, daemon=True).start()

    # --- render loop -----------------------------------------------------------------
    def _temps(self):
        try:
            temps = {k: v for k, v in self.temps().items() if v is not None}
        except Exception:  # noqa: BLE001 – sensor source trouble must not stop lighting
            return {}
        if temps:
            temps[""] = max(temps.values())
        return temps

    def _loop(self):
        interval = SLOW
        while not self.stop_event.wait(interval):
            try:
                interval = self._tick()
            except Exception:
                log.exception("Lighting cycle failed")
                interval = 1.0

    def _tick(self):
        now = time.monotonic()
        animated = False
        temps = None
        with self.lock:
            runtimes = list(self.runtimes.values())
        for rt in runtimes:
            if rt.state == "error" and rt.attempts <= MAX_RESTARTS and now >= rt.next_try and not rt.stopping:
                with self.lock:
                    if self.runtimes.get(rt.pid) is rt and self._entry(rt.pid)["enabled"]:
                        self._launch(rt.pid)
                continue
            if rt.state == "running" and rt.attempts and now - rt.running_since > 60:
                rt.attempts = 0
            if rt.state != "running":
                continue
            for dev in rt.devices:
                key = f"{rt.pid}:{dev['id']}"
                effect = self.state["devices"].get(key)
                if not effect:
                    continue
                last = self.applied.get(key)
                if effect["type"] == "hardware":
                    if last is None:
                        rt.submit(dev["id"], "mode", effect)
                        self.applied[key] = ("mode", None, now)
                    continue
                if not dev["direct"]:
                    continue
                if temps is None:
                    temps = self._temps()
                frame = effects.render(effect, dev["leds"], now - self.t0, temps)
                animated = animated or effects.is_animated(effect)
                resend = min(KEEPALIVE, dev.get("frame_timeout", KEEPALIVE) / 2)
                if last and now - last[2] < dev.get("min_interval", 0):
                    continue           # slow devices (USB round trips) get colours at most this often
                if last is None or last[1] != frame or now - last[2] >= resend:
                    rt.submit(dev["id"], "colors", frame)
                    self.applied[key] = ("colors", frame, now)
        return FAST if animated else SLOW


def _which(cmd):
    return shutil.which(cmd) is not None
