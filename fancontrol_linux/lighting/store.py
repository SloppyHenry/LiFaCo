"""Where plugins and lighting settings live on disk, and safe installation of plugin packages."""

import io
import json
import os
import shutil
import stat
import tempfile
import zipfile

from .. import config as cfgmod
from .manifest import ManifestError, load_manifest, load_manifest_file

MAX_ZIP = 8 * 1024 * 1024
MAX_UNPACKED = 24 * 1024 * 1024
MAX_FILES = 300


def plugins_dir():
    return os.environ.get("FANCONTROL_PLUGINS_DIR", "/var/lib/fancontrol-linux/plugins")


def data_root():
    return os.environ.get("FANCONTROL_PLUGIN_DATA_DIR", "/var/lib/fancontrol-linux/plugin-data")


def state_path():
    return os.path.join(cfgmod.config_dir(), "lighting.json")


def empty_state():
    return {"version": 1, "catalog_url": "", "plugins": {}, "devices": {}}


def load_state():
    try:
        with open(state_path()) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return empty_state()
    except (ValueError, OSError):
        return empty_state()
    state = empty_state()
    if isinstance(raw, dict):
        if isinstance(raw.get("catalog_url"), str):
            state["catalog_url"] = raw["catalog_url"]
        for pid, entry in (raw.get("plugins") or {}).items():
            if isinstance(entry, dict):
                state["plugins"][str(pid)] = {"enabled": bool(entry.get("enabled")),
                                              "approved": str(entry.get("approved") or ""),
                                              "settings": entry.get("settings") if isinstance(entry.get("settings"), dict) else {},
                                              "source": "catalog" if entry.get("source") == "catalog" else "local"}
        for key, effect in (raw.get("devices") or {}).items():
            if isinstance(effect, dict):
                state["devices"][str(key)] = effect
    return state


def save_state(state):
    cfgmod._atomic_write_json(state_path(), state, mode=0o644)


def _read_plugin(path):
    manifest = load_manifest_file(os.path.join(path, "plugin.toml"))
    if not os.path.isfile(os.path.join(path, manifest["entry"])):
        raise ManifestError(f"'{manifest['entry']}' is missing")
    return manifest


def installed():
    """{id: {"manifest", "path"}} for all valid plugins; broken folders are reported in 'errors'."""
    found, errors = {}, {}
    try:
        names = sorted(os.listdir(plugins_dir()))
    except FileNotFoundError:
        return found, errors
    for name in names:
        path = os.path.join(plugins_dir(), name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        try:
            manifest = _read_plugin(path)
        except (ManifestError, OSError) as e:
            errors[name] = str(e)
            continue
        if manifest["id"] != name:
            errors[name] = f"Folder name does not match the plugin id '{manifest['id']}'"
            continue
        found[name] = {"manifest": manifest, "path": path}
    return found, errors


class InstallError(ValueError):
    pass


def _safe_members(zf):
    infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    if not infos:
        raise InstallError("The package is empty")
    if len(infos) > MAX_FILES:
        raise InstallError("The package contains too many files")
    total = 0
    for info in infos:
        name = info.filename
        parts = name.split("/")
        if name.startswith("/") or "\\" in name or ".." in parts or any(p == "" for p in parts):
            raise InstallError(f"Unsafe file name in package: {name}")
        kind = stat.S_IFMT(info.external_attr >> 16)      # 0 when the tool that made the ZIP sets no file type
        if kind and kind != stat.S_IFREG:
            raise InstallError(f"Only plain files are allowed in a plugin package: {name}")
        total += info.file_size
        if info.file_size > MAX_ZIP or total > MAX_UNPACKED:
            raise InstallError("The package is too large when unpacked")
    return infos


def peek_id(data):
    """Plugin id inside a package without installing it (None if it cannot be read)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith("plugin.toml") and name.count("/") <= 1:
                    return load_manifest(zf.read(name).decode("utf-8"))["id"]
    except (zipfile.BadZipFile, ManifestError, UnicodeDecodeError, KeyError, OSError):
        pass
    return None


def install_package(data, expect_id=None, expect_version=None):
    """Install a plugin from ZIP bytes. Returns the manifest. The folder inside the ZIP is optional."""
    if len(data) > MAX_ZIP:
        raise InstallError("The package is larger than 8 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InstallError("This is not a valid ZIP file") from None
    with zf:
        infos = _safe_members(zf)
        names = [i.filename for i in infos]
        prefix = ""
        if "plugin.toml" not in names:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) == 1 and f"{tops.copy().pop()}/plugin.toml" in names:
                prefix = tops.pop() + "/"
            else:
                raise InstallError("plugin.toml not found (it must be in the package's top folder)")
        os.makedirs(plugins_dir(), exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".install-", dir=plugins_dir())
        try:
            for info in infos:
                rel = info.filename[len(prefix):]
                if not info.filename.startswith(prefix):
                    raise InstallError("Files outside the plugin folder in the package")
                dest = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            try:
                manifest = _read_plugin(tmp)
            except (ManifestError, OSError) as e:
                raise InstallError(str(e)) from None
            if expect_id and manifest["id"] != expect_id:
                raise InstallError(f"The package contains '{manifest['id']}', not '{expect_id}'")
            if expect_version and manifest["version"] != expect_version:
                raise InstallError(f"The package has version {manifest['version']}, the catalog lists {expect_version}")
            for root, _dirs, files in os.walk(tmp):
                os.chmod(root, 0o755)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o644)
            target = os.path.join(plugins_dir(), manifest["id"])
            old = None
            if os.path.exists(target):
                old = target + ".old"
                shutil.rmtree(old, ignore_errors=True)
                os.rename(target, old)
            os.rename(tmp, target)
            if old:
                shutil.rmtree(old, ignore_errors=True)
            return manifest
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def remove_plugin(pid):
    path = os.path.join(plugins_dir(), pid)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(plugins_dir()) or not os.path.isdir(path):
        raise InstallError(f"Plugin '{pid}' is not installed")
    shutil.rmtree(path)
    shutil.rmtree(os.path.join(data_root(), pid), ignore_errors=True)


def pack_directory(path):
    """ZIP bytes of a plugin folder (for upload). Validates the manifest first."""
    path = os.path.abspath(path)
    manifest = _read_plugin(path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git") and not d.startswith("."))
            for f in sorted(files):
                if f.endswith((".pyc", ".pyo")) or f.startswith("."):
                    continue
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                zf.write(full, os.path.join(manifest["id"], os.path.relpath(full, path)))
    return manifest, buf.getvalue()


__all__ = ["InstallError", "install_package", "installed", "load_state", "pack_directory", "plugins_dir",
           "remove_plugin", "save_state", "load_manifest"]
