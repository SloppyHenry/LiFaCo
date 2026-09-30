"""Built-in helpers that install software a plugin depends on (for example OpenRGB).

A plugin names a helper in its plugin.toml (`[requires] helpers = ["openrgb"]`). What a helper does as root is fixed
here in LiFaCo: plugins can never make the service run commands of their own. A helper installs software (from the
distribution's repositories, or from the project's own releases), starts its server while an approved plugin is on
(a transient unit, never a boot service) and keeps the server's device list; it does not change boot settings.
"""

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

log = logging.getLogger("fancontrol-linuxd")
OPENRGB_PORT = 6742
UNIT = "lifaco-openrgb"      # transient systemd unit of the server LiFaCo starts
OPENRGB_DOWNLOAD = "https://openrgb.org/releases.html"
# The project's own releases (original repository). Only files below this address are ever downloaded.
UPSTREAM_API = "https://codeberg.org/api/v1/repos/OpenRGB/OpenRGB/releases/latest"
UPSTREAM_FILES = "https://codeberg.org/OpenRGB/OpenRGB/releases/download/"
MAX_DOWNLOAD = 120 * 1024 * 1024
DOWNLOAD_DIR = "/var/lib/fancontrol-linux/downloads"
# Configuration of the server LiFaCo starts (devices added by hand, zone sizes). Kept apart from a user's own OpenRGB.
CONFIG_HOME = "/var/lib/fancontrol-linux"
CONFIG_DIR = os.path.join(CONFIG_HOME, "OpenRGB")
LOCAL_NETWORKS = "localhost link-local multicast 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7"
MAX_MANUAL = 64
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_SERIAL = re.compile(r"^/dev/(tty(USB|ACM|S|AMA)[0-9]{1,3}|serial/by-id/[A-Za-z0-9._:+-]{1,200})$")


def _field(key, label, kind="text", default="", **extra):
    return dict(key=key, label=label, kind=kind, default=default, **extra)


_NAME = _field("name", "Name")
_IP = _field("ip", "IP address", "host")
_LEDS = _field("num_leds", "Number of LEDs", "int", 30, min=1, max=4096)
# Devices that OpenRGB cannot find by itself and that are added by hand (its "Manual devices" settings). Keys and
# fields are the ones OpenRGB reads from OpenRGB.json; `fixed` values are written as they are.
MANUAL_TYPES = {
    "E131Devices": {"label": "E1.31 / sACN (network LED controller)", "fields": [
        _NAME, _field("ip", "IP address (empty = multicast)", "host", optional=True), _LEDS,
        _field("start_universe", "Start universe", "int", 1, min=1, max=63999),
        _field("start_channel", "Start channel", "int", 1, min=1, max=512)],
        "fixed": {"type": "LINEAR", "rgb_order": "RGB"}},
    "DDPDevices": {"label": "DDP (WLED, ESPixelStick …; OpenRGB 1.0 or newer)", "fields": [
        _NAME, _IP, _field("port", "Port", "int", 4048, min=1, max=65535), _LEDS]},
    "LEDStripDevices": {"label": "LED strip on a serial port (Arduino: Adalight, TPM2 …)", "fields": [
        _NAME, _field("port", "Serial port", "serial"), _LEDS,
        _field("baud", "Baud rate", "int", 115200, min=9600, max=4000000),
        _field("protocol", "Protocol", "choice", "adalight",
               choices=["adalight", "tpm2", "keyboard_visualizer", "basic_i2c"])]},
    "LIFXDevices": {"label": "LIFX lamp", "fields": [_NAME, _IP]},
    "YeelightDevices": {"label": "Yeelight lamp", "fields": [
        _IP, _field("music_mode", "Music mode (faster updates)", "switch", False),
        _field("host_ip", "IP address of this computer (music mode)", "host", optional=True)]},
    "GoveeDevices": {"label": "Govee lamp or strip (LAN control on)", "fields": [_IP]},
    "ElgatoKeyLightDevices": {"label": "Elgato Key Light", "fields": [_IP]},
    "ElgatoLightStripDevices": {"label": "Elgato Light Strip", "fields": [_IP]},
    "KasaSmartDevices": {"label": "TP-Link Kasa smart bulb", "fields": [_NAME, _IP]},
    "PhilipsWizDevices": {"label": "Philips WiZ lamp", "fields": [_IP]},
}


def clean_manual_device(raw):
    """A device entry from the user, checked field by field. Raises ValueError with a readable text."""
    if not isinstance(raw, dict) or raw.get("type") not in MANUAL_TYPES:
        raise ValueError("Unknown device type")
    kind = MANUAL_TYPES[raw["type"]]
    out = {}
    for f in kind["fields"]:
        value = raw.get(f["key"], f["default"])
        label = f["label"]
        if f["kind"] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: enter a whole number") from None
            if not f["min"] <= value <= f["max"]:
                raise ValueError(f"{label}: {f['min']} to {f['max']}")
        elif f["kind"] == "switch":
            value = bool(value)
        elif f["kind"] == "choice":
            if value not in f["choices"]:
                raise ValueError(f"{label}: choose one of {', '.join(f['choices'])}")
        else:
            value = str(value or "").strip()
            if not value:
                if f.get("optional"):
                    continue
                if f["kind"] == "text":
                    value = MANUAL_TYPES[raw["type"]]["label"].split(" (")[0]
                else:
                    raise ValueError(f"{label} is missing")
            if f["kind"] == "host" and not _HOST.match(value):
                raise ValueError(f"{label}: enter an address such as 192.168.1.50")
            if f["kind"] == "serial" and not _SERIAL.match(value):
                raise ValueError(f"{label}: choose a port such as /dev/ttyUSB0")
            if f["kind"] == "text":
                value = "".join(ch for ch in value if ch.isprintable())[:60]
        out[f["key"]] = value
    out.update(kind.get("fixed", {}))
    return out


def serial_ports():
    ports = []
    for folder, prefixes in (("/dev/serial/by-id", ("",)), ("/dev", ("ttyUSB", "ttyACM"))):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        ports += [os.path.join(folder, n) for n in names if n.startswith(prefixes)]
    return [p for p in ports if _SERIAL.match(p)]


def read_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError("The OpenRGB settings of LiFaCo's server cannot be read") from None
    return data if isinstance(data, dict) else {}


def write_config(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)
# The AppImage does not bundle libusb: the system's package for it (best effort).
LIBUSB_PACKAGE = {"apt-get": "libusb-1.0-0", "dnf": "libusb1", "pacman": "libusb", "zypper": "libusb-1_0-0",
                  "xbps-install": "libusb", "apk": "libusb"}
_ARCH = {"x86_64": ("amd64", "x86_64"), "amd64": ("amd64", "x86_64"), "aarch64": ("arm64", "arm64"),
         "arm64": ("arm64", "arm64"), "i686": ("i386", "i386"), "i386": ("i386", "i386"),
         "armv7l": ("armhf", "armhf")}
_DEB = re.compile(r"^openrgb_[\d.]+_(?P<arch>\w+)_(?P<codename>[a-z]+)_[0-9a-f]+\.deb$")
_APPIMAGE = re.compile(r"^OpenRGB_[\d.]+_(?P<arch>\w+)_[0-9a-f]+\.AppImage$")

# Package manager -> command that installs OpenRGB from the distribution's repositories.
PACKAGE_MANAGERS = (
    ("apt-get", ["apt-get", "install", "-y", "--no-install-recommends", "openrgb"]),
    ("dnf", ["dnf", "install", "-y", "openrgb"]),
    ("pacman", ["pacman", "-S", "--noconfirm", "--needed", "openrgb"]),
    ("zypper", ["zypper", "--non-interactive", "install", "openrgb"]),
    ("xbps-install", ["xbps-install", "-y", "openrgb"]),
    ("apk", ["apk", "add", "openrgb"]),
)


def _systemd():
    return os.path.isdir("/run/systemd/system") and shutil.which("systemd-run") is not None


def _server_running():
    try:
        with socket.create_connection(("127.0.0.1", OPENRGB_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def package_manager():
    for binary, command in PACKAGE_MANAGERS:
        if shutil.which(binary):
            return binary, command
    return None, None


def not_in_repositories(text):
    return any(s in text for s in ("Unable to locate package", "No match for argument", "target not found",
                                   "No provider of", "not found in repositor"))


def os_release():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                info[key] = value.strip('"')
    except OSError:
        pass
    return info


def deb_codenames(info):
    """Which of the project's .deb builds fits this system best: newest base first if it is recent enough."""
    ident = info.get("ID", "")
    like = info.get("ID_LIKE", "").split()
    try:
        version = float(info.get("VERSION_ID", "0"))
    except ValueError:
        version = 0.0
    trixie_first = (ident == "debian" and version >= 13) or (ident == "ubuntu" and version >= 25.04) or (
        ident not in ("debian", "ubuntu") and "ubuntu" not in like and info.get("VERSION_CODENAME") == "trixie")
    return ["trixie", "bookworm"] if trixie_first else ["bookworm", "trixie"]


def pick_upstream(assets, manager, info, machine):
    """Ordered list of (kind, name, url) to try. assets: [(name, url, size)] of the project's latest release."""
    deb_arch, app_arch = _ARCH.get(machine, (None, None))
    if not deb_arch:
        return []
    picks = []
    if manager == "apt-get":
        for codename in deb_codenames(info):
            for name, url, _size in assets:
                m = _DEB.match(name)
                if m and m["arch"] == deb_arch and m["codename"] == codename:
                    picks.append(("deb", name, url))
    for name, url, _size in assets:
        m = _APPIMAGE.match(name)
        if m and m["arch"] == app_arch:
            picks.append(("appimage", name, url))
    return [p for p in picks if p[2].startswith(UPSTREAM_FILES)]


def fetch_upstream_assets():
    req = urllib.request.Request(UPSTREAM_API, headers={"User-Agent": "LiFaCo"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (fixed https address)
        release = json.loads(resp.read(4 * 1024 * 1024))
    return [(a["name"], a["browser_download_url"], int(a.get("size", 0))) for a in release.get("assets", [])]


def download(url, name):
    if not url.startswith(UPSTREAM_FILES):
        raise RuntimeError("Refusing to download from an unexpected address")
    os.makedirs(DOWNLOAD_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(DOWNLOAD_DIR, os.path.basename(name))
    req = urllib.request.Request(url, headers={"User-Agent": "LiFaCo"})
    size = 0
    with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as out:  # noqa: S310
        while chunk := resp.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise RuntimeError("The download is larger than expected")
            out.write(chunk)
    return path


def _libusb_command(manager):
    command = dict(PACKAGE_MANAGERS)[manager][:-1] + [LIBUSB_PACKAGE[manager]]
    return command


def upstream_command(kind, path):
    if kind == "deb":
        return ["apt-get", "install", "-y", path]
    wrapper = ("#!/bin/sh\nexec /opt/openrgb/OpenRGB.AppImage --appimage-extract-and-run \"$@\"\n")
    script = ("install -Dm755 \"$1\" /opt/openrgb/OpenRGB.AppImage && printf '%s' \"$2\" > /usr/local/bin/openrgb "
              "&& chmod 755 /usr/local/bin/openrgb")
    return ["sh", "-c", script, "sh", path, wrapper]


def _config_file():
    return os.path.join(CONFIG_DIR, "OpenRGB.json")


def _unit_active():
    if not shutil.which("systemctl"):
        return False
    done = subprocess.run(["systemctl", "is-active", "--quiet", f"{UNIT}.service"], capture_output=True, timeout=10,
                          check=False)
    return done.returncode == 0


class OpenRgbHelper:
    id = "openrgb"
    name = "OpenRGB"

    def __init__(self):
        self.lock = threading.Lock()
        self.installing = False
        self.error = ""
        self._options = {}            # binary -> options its --help lists

    def _supports(self, binary, option):
        if binary not in self._options:
            try:
                done = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30, check=False,
                                      stdin=subprocess.DEVNULL)
                self._options[binary] = set(re.findall(r"--[a-z][a-z-]+", done.stdout + done.stderr))
            except (OSError, subprocess.SubprocessError):
                return False
        return option in self._options[binary]

    def server_command(self, binary):
        """The transient unit: root (the hardware needs it), LiFaCo's own settings folder, reachable only from this
        computer. With --server-host the server listens on 127.0.0.1 only, so it may reach devices on the local
        network (lamps, E1.31 controllers); without it all network traffic except localhost is blocked."""
        local_only = self._supports(binary, "--server-host")
        command = ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet", "-p", "IPAddressDeny=any",
                   "-p", f"IPAddressAllow={LOCAL_NETWORKS if local_only else 'localhost'}",
                   "-p", "Restart=on-failure", "-E", f"XDG_CONFIG_HOME={CONFIG_HOME}", binary, "--server"]
        if local_only:
            command += ["--server-host", "127.0.0.1"]
        if self._supports(binary, "--config"):
            command += ["--config", CONFIG_DIR]
        return command

    def status(self):
        installed = shutil.which("openrgb") is not None
        running = _server_running()
        manager, _command = package_manager()
        if running:
            hint = ""
        elif not installed:
            hint = ("OpenRGB is not installed. LiFaCo can install it from your distribution or, if it is not "
                    "packaged there, from the OpenRGB project's official releases.")
        else:
            hint = ("OpenRGB is installed, but its server is not running. LiFaCo starts it when the plugin is "
                    "switched on" + ("." if _systemd() else "; on this system start it yourself: sudo openrgb --server"))
        return {"id": self.id, "name": self.name, "installed": installed, "server_running": running,
                "can_install": True, "installing": self.installing, "error": self.error, "hint": hint,
                "manual_devices": True}

    def ensure_server(self):
        """Start the OpenRGB server if it is not running (called when a plugin that may start it is switched on).

        Runs as a transient systemd unit: root (the hardware needs it), only reachable from this computer, gone when
        stopped or after a reboot (LiFaCo starts it again when the plugin is on). Returns an error text or ''.
        """
        if _server_running():
            return ""
        binary = shutil.which("openrgb")
        if not binary:
            return "OpenRGB is not installed"
        if not _systemd():
            return "Starting the server automatically needs systemd. Start it yourself: sudo openrgb --server"
        try:
            os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        except OSError as e:
            return f"Cannot create the OpenRGB settings folder: {e}"
        command = self.server_command(binary)
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return f"Cannot start the OpenRGB server: {e}"
        if done.returncode != 0 and "already" not in (done.stderr or "").lower():
            return "Cannot start the OpenRGB server: " + " ".join((done.stderr or "").strip().splitlines()[-2:])
        for _ in range(60):                                   # the port opens after the hardware scan
            if _server_running():
                return ""
            time.sleep(0.5)
        return "The OpenRGB server was started but is not answering yet"

    def stop_server(self):
        """Stop the server LiFaCo started (does nothing if the user runs his own)."""
        if _systemd() and shutil.which("systemctl"):
            subprocess.run(["systemctl", "stop", f"{UNIT}.service"], capture_output=True, timeout=30, check=False)

    # --- devices added by hand ------------------------------------------------------
    def manual_config(self):
        """What the settings page needs: the device types, the devices and where they apply."""
        try:
            data = read_config(_config_file())
            error = ""
        except ValueError as e:
            data, error = {}, str(e)
        devices = []
        for key in MANUAL_TYPES:
            section = data.get(key) if isinstance(data.get(key), dict) else {}
            for entry in section.get("devices", []) if isinstance(section.get("devices"), list) else []:
                if isinstance(entry, dict):
                    devices.append(dict(entry, type=key))
        own = _unit_active()
        foreign = _server_running() and not own
        return {"types": [dict(t, id=k) for k, t in MANUAL_TYPES.items()], "devices": devices,
                "serial_ports": serial_ports(), "error": error, "server_managed": own,
                "note": ("An OpenRGB server that LiFaCo did not start is running. Devices added here are used by the "
                         "server LiFaCo starts; stop the other one to use them.") if foreign else ""}

    def set_manual_devices(self, entries):
        """Replace the devices added by hand and restart LiFaCo's server so that it finds them."""
        if not isinstance(entries, list) or len(entries) > MAX_MANUAL:
            raise ValueError(f"At most {MAX_MANUAL} devices")
        cleaned = [(e["type"], clean_manual_device(e)) for e in entries if isinstance(e, dict)]
        with self.lock:
            own = _unit_active()
            if own:
                self.stop_server()           # first: a running server could write its old settings back
            data = read_config(_config_file())
            for key in MANUAL_TYPES:
                items = [entry for kind, entry in cleaned if kind == key]
                if items:
                    data[key] = {"devices": items}
                else:
                    data.pop(key, None)
            write_config(_config_file(), data)
            problem = self.ensure_server() if own else ""
        log.info("OpenRGB: %d devices added by hand", len(cleaned))
        return problem

    def start_install(self):
        """Install the OpenRGB package in the background. Returns the current status."""
        with self.lock:
            if self.installing:
                return self.status()
            if shutil.which("openrgb"):
                return self.status()
            self.installing, self.error = True, ""
        threading.Thread(target=self._install, daemon=True, name="helper-openrgb").start()
        return self.status()

    def _run(self, command):
        # The service runs in a strict sandbox (read-only system). A transient unit started by systemd itself is
        # outside of it and may install software.
        if _systemd():
            command = ["systemd-run", "--wait", "--collect", "--pipe", "--quiet", "--service-type=oneshot"] + command
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        done = subprocess.run(command, capture_output=True, text=True, timeout=900, env=env, check=False)
        text = " ".join(((done.stderr or done.stdout or "").strip().splitlines())[-3:])
        return done.returncode, text

    def _install(self):
        try:
            manager, command = package_manager()
            problem = ""
            if manager:                                    # 1. the distribution's own package
                code, text = self._run(command)
                if code == 0:
                    log.info("OpenRGB installed with %s", manager)
                    return
                if not not_in_repositories(text):
                    raise RuntimeError(text or f"The installation failed (exit code {code})")
                log.info("OpenRGB is not in the repositories of this system, trying the project's own releases")
            problem = self._install_upstream(manager)     # 2. the project's own release
            if problem:
                raise RuntimeError(problem)
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
            self.error = str(e)[:400]
            log.warning("OpenRGB installation failed: %s", e)
        finally:
            self.installing = False

    def _install_upstream(self, manager):
        """Download and install the official release. Returns an error text, or '' on success."""
        try:
            assets = fetch_upstream_assets()
        except (OSError, ValueError) as e:
            return f"Cannot reach the OpenRGB releases ({e}). Download it from {OPENRGB_DOWNLOAD}"
        picks = pick_upstream(assets, manager, os_release(), platform.machine())
        if not picks:
            return f"No official OpenRGB build for this system. Download it from {OPENRGB_DOWNLOAD}"
        last = ""
        for kind, name, url in picks:
            path = None
            try:
                path = download(url, name)
                if kind == "appimage" and manager:
                    _code, _text = self._run(_libusb_command(manager))      # best effort
                code, text = self._run(upstream_command(kind, path))
                if code == 0:
                    log.info("OpenRGB installed from the official release file %s", name)
                    return ""
                last = text or f"installing {name} failed"
            except (OSError, RuntimeError) as e:
                last = str(e)
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        return f"The official OpenRGB release could not be installed: {last}"


HELPERS = {"openrgb": OpenRgbHelper()}
