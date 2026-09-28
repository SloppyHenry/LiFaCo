"""Line-delimited JSON protocol between daemon and clients over a Unix socket."""

import json
import os
import socket

MAX_MESSAGE = 4 * 1024 * 1024


def socket_path():
    return os.environ.get("FANCONTROL_SOCKET", "/run/fancontrol-linux/daemon.sock")


class DaemonError(Exception):
    pass


class DaemonUnavailable(DaemonError):
    pass


class Client:
    def __init__(self, path=None, timeout=5.0):
        self.path = path or socket_path()
        self.timeout = timeout

    def call(self, cmd, **params):
        request = json.dumps({"cmd": cmd, **params}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall(request)
                data = read_line(s)
        except FileNotFoundError:
            raise DaemonUnavailable("Der Dienst läuft nicht (Socket nicht gefunden).") from None
        except PermissionError:
            raise DaemonUnavailable(
                "Keine Berechtigung für den Dienst. Ist dein Benutzer in der Gruppe 'fancontrol'? "
                "(Nach dem Hinzufügen neu anmelden.)") from None
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            raise DaemonUnavailable(f"Dienst nicht erreichbar: {e}") from None
        if not data:
            raise DaemonUnavailable("Leere Antwort vom Dienst")
        reply = json.loads(data)
        if not reply.get("ok"):
            raise DaemonError(reply.get("error", "Unbekannter Fehler"))
        return reply.get("data")


def read_line(sock):
    chunks, size = [], 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if chunk.endswith(b"\n"):
            break
        if size > MAX_MESSAGE:
            raise ValueError("Nachricht zu groß")
    return b"".join(chunks)
