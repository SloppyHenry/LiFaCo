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
from .helpers import HELPERS
from .process import PluginError, PluginProcess

log = logging.getLogger("fancontrol-linuxd")
FAST, SLOW = 0.05, 0.25         # render interval with and without animated effects
KEEPALIVE = 30.0                # resend unchanged colours this often (devices that reset, resume from sleep)
MAX_ZONE_LEDS = 4096
RETRY_MAX_WAIT = 60      # seconds between automatic restarts of a failing plugin (grows 4, 8, 16, 32, 60 …)
DEFAULT_ON = {"type": "static", "color": [255, 255, 255], "brightness": 100.0}


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
        for name in self.info["manifest"]["permissions"].get("start", []):     # approved by the user when enabling
            helper = HELPERS.get(name)
            if helper:
                problem = helper.ensure_server()
                if problem:
                    log.warning("Plugin %s: %s", self.pid, problem)
        proc = PluginProcess(self.info, settings, self.manager.on_event)
        self.process = proc
        try:
            proc.start()
            devices = self._discover(proc)
        except PluginError as e:
            proc.stop()
            self.state, self.error = "error", str(e)
            self.attempts += 1
            self.next_try = time.monotonic() + min(RETRY_MAX_WAIT, 2 ** self.attempts * 2)
            self.manager.log_failure(self.pid, str(e))
            return
        self.devices = devices
        self.running_since = time.monotonic()
        self.state = "running"
        self.manager.last_failure.pop(self.pid, None)
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
        zones = []
        for z in d.get("zones", []):
            zone = {"name": str(z["name"])[:60], "leds": max(0, min(int(z["leds"]), MAX_ZONE_LEDS))}
            if z.get("min_leds") is not None and z.get("max_leds") is not None:
                lo, hi = max(0, int(z["min_leds"])), min(int(z["max_leds"]), MAX_ZONE_LEDS)
                if hi > lo:
                    zone["min_leds"], zone["max_leds"] = lo, hi
            zones.append(zone)
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

    def call(self, method, params=None, timeout=10.0):
        """A request of the user (resize a zone, search the hardware again); errors are shown to him."""
        proc = self.process
        if self.state != "running" or not proc:
            raise LightingError("The plugin is not running")
        try:
            return proc.call(method, params, timeout=timeout)
        except PluginError as e:
            raise LightingError(str(e)) from None

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
        self.boot_gap = self._gap()
        self.last_failure = {}
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
            off = self.state["off_on_exit"]
        if off:
            self._all_black(runtimes)
        for rt in runtimes:
            rt.stop()

    def _all_black(self, runtimes):
        """Send black to every device that can show colours, and give the plugins a moment to deliver it."""
        for rt in runtimes:
            if rt.state == "running":
                for dev in rt.devices:
                    if dev["direct"]:
                        rt.submit(dev["id"], "colors", [(0, 0, 0)] * dev["leds"])
        end = time.monotonic() + 2.0
        while time.monotonic() < end and any(getattr(rt, "pending", None) for rt in runtimes):
            time.sleep(0.05)
        time.sleep(0.2)

    @staticmethod
    def _gap():
        """Time the computer has spent suspended: boot time minus monotonic time (monotonic stops during sleep)."""
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        except (AttributeError, OSError):
            return 0.0

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
        if not (perms["network"] or perms["usb"] or perms["i2c"] or perms.get("start")):
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

    def _launch(self, pid, attempts=0):
        rt = self.runtimes.get(pid)
        if rt:
            rt.stop()
        rt = PluginRuntime(self, self.infos[pid])
        rt.attempts = attempts          # automatic retries carry the count over, so the waiting time really grows
        rt.state = "starting"
        self.runtimes[pid] = rt
        threading.Thread(target=rt.start, daemon=True, name=f"plugin-{pid}").start()

    def _halt(self, pid):
        rt = self.runtimes.pop(pid, None)
        if rt:
            rt.stop()

    def log_failure(self, pid, text):
        """One warning when a plugin fails or its error changes; repeated identical failures stay quiet."""
        if self.last_failure.get(pid) != text:
            self.last_failure[pid] = text
            log.warning("Plugin %s: %s", pid, text)
        else:
            log.debug("Plugin %s still failing: %s", pid, text)

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
            if rt and not rt.stopping and rt.state == "running":       # a failing start is handled by start()
                rt.state, rt.error = "error", str(msg.get("reason") or "The plugin stopped")
                rt.next_try = time.monotonic() + min(RETRY_MAX_WAIT, 2 ** (rt.attempts + 1) * 2)
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
                            "permissions": {"network": False, "usb": [], "i2c": False, "start": []},
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
                            "device_names": [d["name"] for d in rt.devices][:50] if rt else [],
                            "missing_commands": [c for c in m["requires"]["commands"] if not _which(c)],
                            "helpers": [HELPERS[h].status() for h in m["requires"]["helpers"] if h in HELPERS]})
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
                self._stop_helpers(info)
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

    def _stop_helpers(self, info):
        """Servers LiFaCo started for a plugin stop with it (a server the user started himself is left alone)."""
        for name in info["manifest"]["permissions"].get("start", []):
            helper = HELPERS.get(name)
            if helper:
                helper.stop_server()

    def remove(self, pid):
        with self.lock:
            if pid in self.infos:
                self._stop_helpers(self.infos[pid])
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

    def helper_install(self, hid):
        helper = HELPERS.get(hid)
        if not helper:
            raise LightingError(f"Unknown helper '{hid}'")
        return helper.start_install()

    def _helper(self, hid):
        helper = HELPERS.get(hid)
        if not helper or not hasattr(helper, "manual_config"):
            raise LightingError(f"Unknown helper '{hid}'")
        return helper

    def helper_devices(self, hid):
        return self._helper(hid).manual_config()

    def helper_set_devices(self, hid, devices):
        """Devices added by hand to a helper's server (OpenRGB). The server restarts; its plugins reconnect."""
        helper = self._helper(hid)
        try:
            problem = helper.set_manual_devices(devices)
        except (ValueError, OSError) as e:
            raise LightingError(str(e)) from None
        with self.lock:
            for pid, info in self.infos.items():
                m = info["manifest"]
                uses = hid in m["permissions"].get("start", []) or hid in m["requires"]["helpers"]
                if uses and pid in self.runtimes and self._entry(pid)["enabled"] and self._approved(pid):
                    self._launch(pid)
        result = helper.manual_config()
        result["problem"] = problem
        return result

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
                    effect = self.state["devices"].get(key)
                    out.append(dict(d, key=key, plugin=pid, plugin_name=rt.info["manifest"]["name"], effect=effect,
                                    on=bool(effect) and effect["type"] != "off", error=self.errors.get(key, ""),
                                    synced=self.state["sync"]["on"] and d["direct"],
                                    shown=self.state["sync"]["effect"] if self.state["sync"]["on"] and d["direct"]
                                    else effect))
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

    def power(self, key, on):
        """Switch one device off (colours black, or released if it only has hardware effects) or back on."""
        with self.lock:
            _rt, dev = self._device(key)
            current = self.state["devices"].get(key)
            if not on:
                if current and current["type"] != "off":
                    self.state["previous"][key] = current
                self.set_effect(key, {"type": "off"} if dev["direct"] else None)
                return
            effect = self.state["previous"].get(key)
            if not effect and current and current["type"] != "off":
                effect = current
            if not effect:
                if not dev["direct"]:
                    raise LightingError("Choose an effect for this device first")
                effect = DEFAULT_ON
            self.set_effect(key, effect)

    def power_all(self, on):
        """All devices on or off. Returns how many were switched. Sync mode goes off with them and comes back."""
        with self.lock:
            sync = self.state["sync"]
            if not on and sync["on"]:
                sync["on"], sync["resume"] = False, True
            elif on and sync["resume"]:
                sync["on"], sync["resume"] = True, False
            self.applied.clear()
            self._save()
        count = 0
        for d in self.list_devices():
            try:
                self.power(d["key"], on)
                count += 1
            except LightingError:
                continue
        return count

    def get_sync(self):
        with self.lock:
            sync = self.state["sync"]
            count = sum(1 for rt in self.runtimes.values() if rt.state == "running" for d in rt.devices if d["direct"])
            return {"on": sync["on"], "effect": dict(sync["effect"]), "devices": count}

    def set_sync(self, on=None, effect=None):
        """Switch sync mode and/or set its effect (static, breathing, rainbow, temperature, off)."""
        with self.lock:
            sync = self.state["sync"]
            if effect is not None:
                effect = effects.normalize(effect)
                if effect["type"] == "hardware":
                    raise LightingError("Sync mode uses LiFaCo's effects; device effects differ from device to device")
                sync["effect"] = effect
            if on is not None:
                sync["on"], sync["resume"] = bool(on), False
            self.applied.clear()        # every device gets the new colours at once
            self._save()
        return self.get_sync()

    def get_light_settings(self):
        with self.lock:
            return {"off_on_exit": self.state["off_on_exit"]}

    def set_light_settings(self, values):
        with self.lock:
            self.state["off_on_exit"] = bool(values.get("off_on_exit"))
            self._save()
        return self.get_light_settings()

    def profile_data(self):
        """What a profile stores about lighting: the effect of every device."""
        with self.lock:
            sync = self.state["sync"]
            return {"devices": {k: dict(v) for k, v in self.state["devices"].items()},
                    "sync": {"on": sync["on"], "effect": dict(sync["effect"])}}

    def load_profile_data(self, data):
        """Apply the lighting of a profile: devices not mentioned are released, the rest get their effect."""
        with self.lock:
            self.state["devices"] = {k: dict(v) for k, v in (data or {}).get("devices", {}).items()}
            self.state["sync"] = effects.normalize_sync((data or {}).get("sync"))   # older profiles: sync off
            self.applied.clear()
            self._save()

    def rescan(self):
        with self.lock:
            runtimes = list(self.runtimes.values())
        for rt in runtimes:
            rt.rescan()

    def resize_zone(self, key, zone, leds):
        """Set how many LEDs are connected to a resizable zone (for example an addressable header)."""
        with self.lock:
            rt, dev = self._device(key)
            try:
                z = dev["zones"][int(zone)]
                leds = int(leds)
            except (IndexError, TypeError, ValueError):
                raise LightingError("Unknown zone") from None
            if "min_leds" not in z:
                raise LightingError("The LED count of this zone is fixed")
            if not z["min_leds"] <= leds <= z["max_leds"]:
                raise LightingError(f"The zone takes {z['min_leds']} to {z['max_leds']} LEDs")
        if not isinstance(rt, PluginRuntime):
            raise LightingError("This device cannot change its LED count")
        rt.call("resize_zone", {"device": dev["id"], "zone": int(zone), "leds": leds}, timeout=15)
        rt.rescan()
        with self.lock:
            self.applied.pop(key, None)
        self.errors.pop(key, None)

    def hardware_rescan(self, pid):
        """Ask a plugin to search its hardware again (plugins without such a search just list their devices)."""
        with self.lock:
            rt = self.runtimes.get(pid)
        if rt is None:
            raise LightingError("The plugin is not running")
        if isinstance(rt, PluginRuntime):
            rt.call("rescan", timeout=60)
        rt.rescan()
        return len(rt.devices)

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
        hot = [v for k, v in temps.items() if not k.startswith(effects.FAN_PREFIX)]
        if hot:
            temps[""] = max(hot)
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
        gap = self._gap()
        if gap - self.boot_gap > 2.0:       # the computer was asleep: devices may have lost their colours
            self.applied.clear()
            log.info("Resumed from suspend, lighting is applied again")
        self.boot_gap = gap
        animated = False
        temps = None
        with self.lock:
            runtimes = list(self.runtimes.values())
        for rt in runtimes:
            if rt.state == "error" and now >= rt.next_try and not rt.stopping:
                with self.lock:
                    if self.runtimes.get(rt.pid) is rt and self._entry(rt.pid)["enabled"]:
                        self._launch(rt.pid, rt.attempts)
                continue
            if rt.state == "running" and rt.attempts and now - rt.running_since > 60:
                rt.attempts = 0
            if rt.state != "running":
                continue
            sync = self.state["sync"]
            for dev in rt.devices:
                key = f"{rt.pid}:{dev['id']}"
                # Sync mode: every device that takes LiFaCo's colours shows the same effect, in step (same clock)
                effect = sync["effect"] if sync["on"] and dev["direct"] else self.state["devices"].get(key)
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
