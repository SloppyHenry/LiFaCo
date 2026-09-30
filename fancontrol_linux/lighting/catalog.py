"""The plugin catalog: an index.json in the plugin repository, searched and downloaded from the settings page."""

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from .manifest import ID_RE, VERSION_RE

DEFAULT_URL = "https://raw.githubusercontent.com/SloppyHenry/LiFaCo-plugins/main/index.json"
MAX_INDEX = 2 * 1024 * 1024
MAX_PACKAGE = 8 * 1024 * 1024
CACHE_SECONDS = 600
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CatalogError(Exception):
    pass


def catalog_url(state):
    """Order: environment (for development), setting in lighting.json, default."""
    return os.environ.get("FANCONTROL_CATALOG_URL") or state.get("catalog_url") or DEFAULT_URL


def _allowed(url, trusted):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return True
    if not trusted:
        return False
    if parts.scheme == "file":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def _fetch(url, limit, trusted, timeout=15):
    if not _allowed(url, trusted):
        raise CatalogError("Only https:// addresses are allowed for the plugin catalog")
    req = urllib.request.Request(url, headers={"User-Agent": "LiFaCo"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme checked above)
            data = resp.read(limit + 1)
    except OSError as e:
        raise CatalogError(f"Cannot reach the plugin catalog: {getattr(e, 'reason', e)}") from None
    if len(data) > limit:
        raise CatalogError("The download is larger than allowed")
    return data


def _entry(raw, base):
    if not isinstance(raw, dict):
        return None
    pid, version = raw.get("id"), raw.get("version")
    sha = str(raw.get("sha256", "")).lower()
    if not (isinstance(pid, str) and ID_RE.match(pid) and isinstance(version, str) and VERSION_RE.match(version)
            and _SHA256.match(sha) and isinstance(raw.get("download"), str)):
        return None
    tags = [t for t in raw.get("tags", []) if isinstance(t, str)][:12]
    perms = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
    return {"id": pid, "name": str(raw.get("name") or pid)[:60], "version": version,
            "description": str(raw.get("description") or "")[:600], "author": str(raw.get("author") or "")[:80],
            "tags": tags, "sha256": sha, "size": int(raw.get("size") or 0),
            "download": urllib.parse.urljoin(base, raw["download"]), "homepage": str(raw.get("homepage") or "")[:200],
            "permissions": {"network": bool(perms.get("network")), "usb": [str(u) for u in perms.get("usb", [])][:20],
                            "i2c": bool(perms.get("i2c"))}}


def search(entries, query):
    """Every word must appear in name, id, description, tags or author. Name matches come first."""
    words = [w for w in re.split(r"\s+", (query or "").lower().strip()) if w]
    if not words:
        return sorted(entries, key=lambda e: e["name"].lower())
    scored = []
    for e in entries:
        head = f"{e['name']} {e['id']}".lower()
        rest = f"{e['description']} {' '.join(e['tags'])} {e['author']}".lower()
        if all(w in head or w in rest for w in words):
            scored.append((-sum(w in head for w in words), e["name"].lower(), e))
    return [e for _s, _n, e in sorted(scored, key=lambda t: t[:2])]


class Catalog:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = None        # (url, fetched_at, entries)

    def entries(self, url, force=False):
        trusted = bool(os.environ.get("FANCONTROL_CATALOG_URL"))
        with self.lock:
            if not force and self.cache and self.cache[0] == url and time.monotonic() - self.cache[1] < CACHE_SECONDS:
                return self.cache[2]
        data = _fetch(url, MAX_INDEX, trusted)
        try:
            index = json.loads(data)
        except ValueError:
            raise CatalogError("The plugin catalog is not valid JSON") from None
        if not isinstance(index, dict) or not isinstance(index.get("plugins"), list):
            raise CatalogError("The plugin catalog has an unknown format")
        entries = [e for e in (_entry(r, url) for r in index["plugins"]) if e]
        with self.lock:
            self.cache = (url, time.monotonic(), entries)
        return entries

    def find(self, url, pid):
        for e in self.entries(url):
            if e["id"] == pid:
                return e
        raise CatalogError(f"Plugin '{pid}' is not in the catalog")

    def download(self, url, entry):
        trusted = bool(os.environ.get("FANCONTROL_CATALOG_URL"))
        data = _fetch(entry["download"], MAX_PACKAGE, trusted, timeout=60)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise CatalogError("The download does not match its checksum – it was not installed")
        return data
