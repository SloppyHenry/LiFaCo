"""One running plugin: the child process, its identity and the JSON-line protocol."""

import grp
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import threading
from collections import deque

from . import PLUGIN_API
from .store import data_root

log = logging.getLogger("fancontrol-linuxd")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_USER = "lifaco-plugins"
USB_GROUP = "lifaco-usb"
I2C_GROUP = "lifaco-i2c"


class PluginError(Exception):
    pass


_netns_ok = None


def _can_unshare_net():
    """True if 'unshare --net' works here (needs root and util-linux). Probed once."""
    global _netns_ok
    if _netns_ok is None:
        _netns_ok = False
        if os.geteuid() == 0 and shutil.which("unshare"):
            try:
                _netns_ok = subprocess.run(["unshare", "--net", "true"], capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        if os.geteuid() == 0 and not _netns_ok:
            log.warning("Network isolation for plugins is not available (needs 'unshare' from util-linux and "
                        "CAP_SYS_ADMIN): plugins without the network permission still run without root, but can "
                        "reach the network")
    return _netns_ok


def python_command():
    """(command, extra environment) that starts a Python interpreter which ignores the user's environment.

    Inside the AppImage Python only runs through the bundled loader and needs PYTHONHOME (see AppRun); anywhere else
    'python -I' is used, which also keeps the script folder off the import path.
    """
    base = os.environ.get("FANCONTROL_APPIMAGE_DIR")
    if base:
        lib = f"{base}/usr/lib/x86_64-linux-gnu"
        return ([f"{lib}/ld-linux-x86-64.so.2", "--library-path", f"{lib}:{base}/usr/lib", f"{base}/usr/bin/python3",
                 "-s", "-B"], {"PYTHONHOME": f"{base}/usr"})
    return [sys.executable, "-I", "-B"], {}


def resolve_identity(perms):
    """(uid, gid, groups) the plugin runs as, or None when the service itself is not root (demo, tests).

    A plugin never runs as root: without the 'lifaco-plugins' user the unprivileged 'nobody' is used, and if that
    does not exist either, the plugin is refused.
    """
    if os.geteuid() != 0:
        return None
    for name in (PLUGIN_USER, "nobody"):
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            continue
        if pw.pw_uid == 0:
            continue
        groups = [pw.pw_gid]
        for wanted, group in ((bool(perms["usb"]), USB_GROUP), (perms["i2c"], I2C_GROUP)):
            if wanted:
                try:
                    groups.append(grp.getgrnam(group).gr_gid)
                except KeyError:
                    log.warning("Group '%s' does not exist – run the installer again", group)
        return pw.pw_uid, pw.pw_gid, groups
    raise PluginError("No unprivileged user for plugins ('lifaco-plugins') exists – run the installer again")


def prepare_data_dir(pid, identity):
    path = os.path.join(data_root(), pid)
    os.makedirs(path, exist_ok=True)
    if identity:
        os.chown(path, identity[0], identity[1])
    os.chmod(path, 0o700 if identity else 0o755)
    return path


class PluginProcess:
    """Starts a plugin and exchanges requests with it. Thread-safe: one request per call, matched by id."""

    def __init__(self, info, settings, on_event=None):
        self.info = info
        self.manifest = info["manifest"]
        self.settings = settings
        self.on_event = on_event
        self.proc = None
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self.exit_reason = ""
        self._stderr_tail = deque(maxlen=6)
        self._stderr_thread = None

    @property
    def pid_name(self):
        return self.manifest["id"]

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        m = self.manifest
        identity = resolve_identity(m["permissions"])
        data_dir = prepare_data_dir(m["id"], identity)
        cfg = {"plugin_dir": self.info["path"], "entry": m["entry"], "sdk_dir": os.path.join(HERE, "sdk"),
               "uid": identity[0] if identity else None, "gid": identity[1] if identity else None,
               "groups": identity[2] if identity else []}
        python, python_env = python_command()
        cmd = python + [os.path.join(HERE, "bootstrap.py"), json.dumps(cfg)]
        if not m["permissions"]["network"] and _can_unshare_net():
            cmd = ["unshare", "--net", "--"] + cmd
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": data_dir,
               "LIFACO_PLUGIN_ID": m["id"], "LIFACO_DATA_DIR": data_dir, **python_env}
        if log.isEnabledFor(logging.DEBUG):
            env["LIFACO_PLUGIN_DEBUG"] = "1"
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=env, cwd="/", close_fds=True, start_new_session=True)
        except OSError as e:
            raise PluginError(f"Cannot start the plugin: {e}") from None
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self.call("init", {"api": PLUGIN_API, "settings": self.settings, "data_dir": data_dir}, timeout=15)

    def _read_stdout(self):
        proc = self.proc
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("plugin %s: unreadable output: %r", self.pid_name, line[:200])
                continue
            if not isinstance(msg, dict):
                continue
            if "event" in msg:
                if self.on_event:
                    self.on_event(self.pid_name, msg)
                continue
            with self._pending_lock:
                slot = self._pending.pop(msg.get("id"), None)
            if slot:
                slot["reply"] = msg
                slot["event"].set()
        code = proc.wait()
        self._close_pipes(proc)
        self._stderr_thread.join(1)
        # The last line the plugin wrote is usually the reason (a Python error message).
        last = next((line for line in reversed(self._stderr_tail) if line.strip()), "")
        self.exit_reason = f"The plugin stopped (exit code {code})" + (f": {last[:200]}" if last and code else "")
        with self._pending_lock:
            slots, self._pending = list(self._pending.values()), {}
        for slot in slots:
            slot["reply"] = {"error": self.exit_reason}
            slot["event"].set()
        if self.on_event:
            self.on_event(self.pid_name, {"event": "exited", "reason": self.exit_reason})

    def _read_stderr(self):
        stderr = self.proc.stderr
        for line in stderr:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                log.info("plugin %s: %s", self.pid_name, text[:500])
        stderr.close()

    def _close_pipes(self, proc):
        """Called once the process has ended, so restarts do not leak file descriptors."""
        proc.stdout.close()
        with self._write_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def call(self, method, params=None, timeout=10.0):
        if not self.alive():
            raise PluginError(self.exit_reason or "The plugin is not running")
        slot = {"event": threading.Event(), "reply": None}
        with self._pending_lock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = slot
        line = json.dumps({"id": rid, "method": method, "params": params or {}}, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                self.proc.stdin.write(line.encode())
                self.proc.stdin.flush()
        except (OSError, ValueError):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise PluginError(self.exit_reason or "The plugin is not running") from None
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise PluginError(f"The plugin did not answer '{method}' within {timeout:g} s")
        reply = slot["reply"]
        if reply.get("error"):
            raise PluginError(str(reply["error"]))
        return reply.get("result")

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self.call("close", timeout=1)
            except PluginError:
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.exit_reason = self.exit_reason or "Stopped"
