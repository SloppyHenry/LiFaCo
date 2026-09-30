"""Built-in helpers that install software a plugin depends on (for example OpenRGB).

A plugin names a helper in its plugin.toml (`[requires] helpers = ["openrgb"]`). What a helper does as root is fixed
here in LiFaCo: plugins can never make the service run commands of their own. A helper only installs the package of
the user's distribution; it does not create services or change boot settings.
"""

import logging
import os
import shutil
import socket
import subprocess
import threading

log = logging.getLogger("fancontrol-linuxd")
OPENRGB_PORT = 6742
OPENRGB_DOWNLOAD = "https://openrgb.org/releases.html"

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
            hint = ("OpenRGB is not installed. LiFaCo can install your distribution's package."
                    if manager else f"OpenRGB is not installed. Download it from {OPENRGB_DOWNLOAD}")
        else:
            hint = ("OpenRGB is installed, but its server is not running. Start it with the command "
                    "'sudo openrgb --server' (it needs administrator rights to reach the hardware).")
        return {"id": self.id, "name": self.name, "installed": installed, "server_running": running,
                "can_install": manager is not None, "installing": self.installing, "error": self.error, "hint": hint}

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

    def _install(self):
        try:
            manager, command = package_manager()
            if not manager:
                raise RuntimeError(f"No supported package manager found. Download OpenRGB from {OPENRGB_DOWNLOAD}")
            # The service runs in a strict sandbox (read-only system). A transient unit started by systemd itself
            # is outside of it and may install packages.
            if _systemd():
                command = ["systemd-run", "--wait", "--collect", "--pipe", "--quiet", "--service-type=oneshot"] + command
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            done = subprocess.run(command, capture_output=True, text=True, timeout=900, env=env, check=False)
            if done.returncode != 0:
                text = " ".join(((done.stderr or done.stdout or "").strip().splitlines())[-3:])
                if not_in_repositories(text):
                    text = f"OpenRGB is not in your distribution's repositories. Download it from {OPENRGB_DOWNLOAD}"
                raise RuntimeError(text or f"The installation failed (exit code {done.returncode})")
            log.info("OpenRGB installed with %s", manager)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            self.error = str(e)[:400]
            log.warning("OpenRGB installation failed: %s", e)
        finally:
            self.installing = False


HELPERS = {"openrgb": OpenRgbHelper()}
