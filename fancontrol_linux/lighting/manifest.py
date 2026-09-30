"""plugin.toml: parsing and validation."""

import ast
import os
import re

from . import PLUGIN_API

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
USB_ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SETTING_TYPES = ("text", "password", "number", "switch", "choice")
DEVICE_TYPES = ("mainboard", "gpu", "ram", "keyboard", "mouse", "cooler", "fan", "strip", "headset", "case", "other")
MAX_MANIFEST = 64 * 1024
RESERVED_IDS = ("liquidctl",)     # built-in lighting providers


class ManifestError(ValueError):
    pass


# --- TOML ------------------------------------------------------------------------------
# Python 3.11 has tomllib. For 3.10 a parser for the small subset plugin.toml needs is included:
# tables, arrays of tables, strings, numbers, booleans and (multi-line) arrays.

def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == "\\" and quote == '"':
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _split_items(text):
    items, depth, quote, cur = [], 0, None, []
    prev = ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote and prev != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        prev = ch
    if "".join(cur).strip():
        items.append("".join(cur))
    return items


def _value(text):
    text = text.strip()
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("[") and text.endswith("]"):
        return [_value(t) for t in _split_items(text[1:-1])]
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise ManifestError(f"Invalid string: {text}") from None
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    try:
        return int(text.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(text.replace("_", ""))
    except ValueError:
        raise ManifestError(f"Cannot read value: {text}") from None


def parse_toml(text):
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ManifestError(f"plugin.toml is not valid TOML: {e}") from None
    root, current = {}, None
    current = root
    lines = iter(text.splitlines())
    for raw in lines:
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[["):
            name = line.strip("[] ").strip()
            current = {}
            root.setdefault(name, []).append(current)
        elif line.startswith("["):
            name = line.strip("[] ").strip()
            current = root.setdefault(name, {})
        else:
            if "=" not in line:
                raise ManifestError(f"Cannot read line: {raw.strip()}")
            key, _, val = line.partition("=")
            val = val.strip()
            while val.startswith("[") and val.count("[") > val.count("]"):
                val += " " + _strip_comment(next(lines, "")).strip()
            current[key.strip()] = _value(val)
    return root


# --- validation -------------------------------------------------------------------------

def _text(data, key, default="", limit=300):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ManifestError(f"'{key}' must be text")
    return value.strip()[:limit]


def load_manifest(text):
    """Validated manifest with defaults filled in. Raises ManifestError."""
    if len(text) > MAX_MANIFEST:
        raise ManifestError("plugin.toml is too large")
    data = parse_toml(text)
    if not isinstance(data, dict):
        raise ManifestError("plugin.toml must contain key = value lines")
    pid = _text(data, "id", limit=40)
    if pid in RESERVED_IDS:
        raise ManifestError(f"The id '{pid}' is reserved for a part of LiFaCo")
    if not ID_RE.match(pid):
        raise ManifestError("'id' must be 2-40 characters: lowercase letters, digits and '-', starting with a letter")
    version = _text(data, "version", limit=20)
    if not VERSION_RE.match(version):
        raise ManifestError("'version' must look like 1.0.0")
    api = data.get("api", PLUGIN_API)
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise ManifestError("'api' must be a whole number")
    if api > PLUGIN_API:
        raise ManifestError(f"The plugin needs plugin API {api}, this LiFaCo supports up to {PLUGIN_API} – update LiFaCo")
    name = _text(data, "name", limit=60)
    if not name:
        raise ManifestError("'name' is missing")
    entry = _text(data, "entry", "plugin.py", limit=100)
    if (not entry.endswith(".py") or os.path.isabs(entry) or ".." in entry.split("/") or "\\" in entry
            or entry.startswith(".")):
        raise ManifestError("'entry' must be a relative .py file inside the plugin")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ManifestError("'tags' must be a list of words")

    perms_raw = data.get("permissions", {})
    if not isinstance(perms_raw, dict):
        raise ManifestError("[permissions] must be a table")
    unknown = set(perms_raw) - {"network", "usb", "i2c"}
    if unknown:
        raise ManifestError(f"Unknown permission: {', '.join(sorted(unknown))}")
    usb = perms_raw.get("usb", [])
    if not isinstance(usb, list) or not all(isinstance(u, str) and USB_ID_RE.match(u.lower()) for u in usb):
        raise ManifestError("permissions.usb must be a list of USB IDs like \"1462:7d25\" (see 'lsusb')")
    permissions = {"network": bool(perms_raw.get("network", False)),
                   "usb": sorted({u.lower() for u in usb}),
                   "i2c": bool(perms_raw.get("i2c", False))}

    settings, seen = [], set()
    raw_settings = data.get("settings", [])
    if not isinstance(raw_settings, list):
        raise ManifestError("settings must be written as [[settings]] blocks")
    for s in raw_settings:
        if not isinstance(s, dict):
            raise ManifestError("Invalid [[settings]] block")
        key = _text(s, "key", limit=40)
        if not SETTING_KEY_RE.match(key) or key in seen:
            raise ManifestError(f"Setting key '{key}' is invalid or used twice")
        seen.add(key)
        kind = _text(s, "type", "text", limit=20)
        if kind not in SETTING_TYPES:
            raise ManifestError(f"Setting '{key}': type must be one of {', '.join(SETTING_TYPES)}")
        item = {"key": key, "label": _text(s, "label", key, 80), "type": kind, "help": _text(s, "help", "", 300)}
        default = s.get("default", {"switch": False, "number": 0}.get(kind, ""))
        if kind == "switch":
            default = bool(default)
        elif kind == "number":
            if isinstance(default, bool) or not isinstance(default, (int, float)):
                raise ManifestError(f"Setting '{key}': default must be a number")
            item.update(min=float(s.get("min", 0)), max=float(s.get("max", 100)), step=float(s.get("step", 1)))
        elif kind == "choice":
            choices = s.get("choices")
            if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
                raise ManifestError(f"Setting '{key}': 'choices' must be a list of text")
            item["choices"] = choices
            default = default if default in choices else choices[0]
        else:
            default = str(default)
        item["default"] = default
        settings.append(item)

    requires = data.get("requires", {})
    commands = requires.get("commands", []) if isinstance(requires, dict) else []
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ManifestError("requires.commands must be a list of program names")

    return {"id": pid, "name": name, "version": version, "api": api, "entry": entry,
            "description": _text(data, "description", limit=600), "author": _text(data, "author", limit=80),
            "license": _text(data, "license", limit=40), "homepage": _text(data, "homepage", limit=200),
            "tags": [t.strip().lower()[:30] for t in tags if t.strip()][:12],
            "permissions": permissions, "settings": settings, "requires": {"commands": commands}}


def load_manifest_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return load_manifest(f.read(MAX_MANIFEST + 1))
    except FileNotFoundError:
        raise ManifestError("plugin.toml is missing") from None
    except UnicodeDecodeError:
        raise ManifestError("plugin.toml must be UTF-8 text") from None


def default_settings(manifest):
    return {s["key"]: s["default"] for s in manifest["settings"]}


def clean_settings(manifest, values):
    """User values checked against the plugin's setting schema; unknown keys are dropped."""
    values = values if isinstance(values, dict) else {}
    out = {}
    for s in manifest["settings"]:
        v = values.get(s["key"], s["default"])
        if s["type"] == "switch":
            v = bool(v)
        elif s["type"] == "number":
            try:
                v = min(s["max"], max(s["min"], float(v)))
            except (TypeError, ValueError):
                v = s["default"]
            if v == int(v) and s["step"] == int(s["step"]):
                v = int(v)
        elif s["type"] == "choice":
            v = v if v in s["choices"] else s["default"]
        else:
            v = str(v)[:500]
        out[s["key"]] = v
    return out


def version_tuple(version):
    return tuple(int(p) for p in version.split("."))


def permission_lines(perms):
    """Human readable list of what a plugin may do, for the approval dialog."""
    lines = []
    if perms["network"]:
        lines.append("Use the network (talk to devices and servers on your network)")
    for usb in perms["usb"]:
        lines.append(f"Access the USB device {usb}")
    if perms["i2c"]:
        lines.append("Access the mainboard's I²C/SMBus (RAM and mainboard LEDs – careless writes can damage hardware)")
    if not lines:
        lines.append("No special access (cannot use the network or hardware)")
    return lines


def permission_key(perms):
    """Stable text form of the permissions; the approval is valid only while it stays the same."""
    return f"net={int(perms['network'])};usb={','.join(perms['usb'])};i2c={int(perms['i2c'])}"
