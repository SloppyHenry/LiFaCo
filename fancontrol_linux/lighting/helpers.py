"""Built-in helpers that install software a plugin depends on (for example OpenRGB).

A plugin names a helper in its plugin.toml (`[requires] helpers = ["openrgb"]`). What a helper does as root is fixed
here in LiFaCo: plugins can never make the service run commands of their own. A helper only installs
software (from the distribution's repositories, or from the project's own releases); it does not create services or
change boot settings.
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


class OpenRgbHelper:
    id = "openrgb"
    name = "OpenRGB"

    def __init__(self):
        self.lock = threading.Lock()
        self.installing = False
        self.error = ""

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
            hint = ("OpenRGB is installed, but its server is not running. Start it with the command "
                    "'sudo openrgb --server' (it needs administrator rights to reach the hardware).")
        return {"id": self.id, "name": self.name, "installed": installed, "server_running": running,
                "can_install": True, "installing": self.installing, "error": self.error, "hint": hint}

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
        command = ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet", "-p", "IPAddressDeny=any",
                   "-p", "IPAddressAllow=localhost", "-p", "Restart=on-failure", binary, "--server"]
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
