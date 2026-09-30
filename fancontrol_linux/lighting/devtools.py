"""Helpers for people who write plugins: scaffold a plugin, check it, pack it, try it without the daemon."""

import os
import re
import subprocess
import sys

from . import PLUGIN_API, store
from .manifest import ID_RE, ManifestError, load_manifest_file

SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdk")

TOML_TEMPLATE = '''# plugin.toml – describes your plugin. Full guide: docs/plugin-guide.md
id = "{id}"                  # lowercase letters, digits and "-"; also the folder name
name = "{name}"
version = "0.1.0"            # change it with every release (major.minor.patch)
api = {api}                     # plugin API version
description = "One sentence: which devices does this plugin control?"
author = "Your name"
license = "MIT"
tags = ["example"]           # words people can search for in the plugin catalog

# What the plugin may do. Everything not listed is blocked. The user is shown this list before enabling.
# [permissions]
# network = true                  # talk to devices or servers on the network
# usb = ["1462:7d25"]             # USB devices it opens (vendor:product, see `lsusb`)
# i2c = false                     # mainboard SMBus (RAM and mainboard LEDs) - use with great care

# Settings the user can change. LiFaCo builds the settings form from these; you read them from self.settings.
# [[settings]]
# key = "host"
# label = "Address"
# type = "text"                   # text | password | number | switch | choice
# default = ""
# help = "IP address or host name of the device"
'''

PY_TEMPLATE = '''"""{name} – a LiFaCo lighting plugin."""

from lifaco_plugin import Device, Plugin, run


class {cls}(Plugin):
    def setup(self):
        """Called once at start. self.settings holds the values from the [[settings]] in plugin.toml."""

    def discover(self):
        """Return the devices you found. `id` must stay the same between runs."""
        return [Device("demo", "{name} demo strip", type="strip", leds=10)]

    def set_colors(self, device_id, colors):
        """`colors` has one (r, g, b) per LED. Send them to your hardware here."""
        self.log(f"{{device_id}}: first LED is {{colors[0]}}")

    def close(self):
        """LiFaCo is stopping the plugin. Close your connections here."""


run({cls})
'''


def new_plugin(directory, pid, name=None):
    if not ID_RE.match(pid):
        raise ValueError("The id must be 2-40 characters: lowercase letters, digits and '-', starting with a letter")
    path = os.path.abspath(os.path.join(directory, pid))
    if os.path.exists(path):
        raise ValueError(f"{path} already exists")
    name = name or pid.replace("-", " ").title()
    cls = "".join(p.capitalize() for p in pid.split("-")) + "Plugin"
    os.makedirs(path)
    with open(os.path.join(path, "plugin.toml"), "w") as f:
        f.write(TOML_TEMPLATE.format(id=pid, name=name, api=PLUGIN_API))
    with open(os.path.join(path, "plugin.py"), "w") as f:
        f.write(PY_TEMPLATE.format(name=name, cls=cls))
    return path


def validate(path):
    """(manifest, warnings). Raises ManifestError for problems that stop the plugin from loading."""
    path = os.path.abspath(path)
    manifest = load_manifest_file(os.path.join(path, "plugin.toml"))
    entry = os.path.join(path, manifest["entry"])
    if not os.path.isfile(entry):
        raise ManifestError(f"{manifest['entry']} is missing")
    if os.path.basename(path) != manifest["id"]:
        raise ManifestError(f"The folder is called '{os.path.basename(path)}' but the id is '{manifest['id']}'")
    warnings = []
    with open(entry, encoding="utf-8") as f:
        code = f.read()
    try:
        compile(code, entry, "exec")
    except SyntaxError as e:
        raise ManifestError(f"{manifest['entry']} line {e.lineno}: {e.msg}") from None
    if "lifaco_plugin" not in code or not re.search(r"^run\(", code, re.M):
        warnings.append("plugin.py should import from lifaco_plugin and end with run(YourPluginClass)")
    if not manifest["description"]:
        warnings.append("Add a description – it is what people see in the catalog")
    if not manifest["tags"]:
        warnings.append("Add a few tags so people can find the plugin")
    if manifest["permissions"]["i2c"]:
        warnings.append("i2c access can damage hardware when misused: make sure the plugin only touches known devices")
    if re.search(r"\bprint\(", code) and "file=" not in code:
        warnings.append("print() goes to the LiFaCo log; use self.log() instead")
    _m, data = store.pack_directory(path)
    if len(data) > store.MAX_ZIP:
        warnings.append(f"The package is {len(data) // 1024} KB; the limit is {store.MAX_ZIP // 1024 // 1024} MB")
    return manifest, warnings


def pack(path, output=None):
    manifest, data = store.pack_directory(path)
    output = output or f"{manifest['id']}-{manifest['version']}.zip"
    with open(output, "wb") as f:
        f.write(data)
    return output, manifest


def dev_run(path, extra):
    """Run plugin.py --dev as the current user, without the LiFaCo service."""
    path = os.path.abspath(path)
    manifest = load_manifest_file(os.path.join(path, "plugin.toml"))
    env = dict(os.environ, PYTHONPATH=SDK_DIR + os.pathsep + path + os.pathsep + os.path.join(path, "lib"))
    return subprocess.call([sys.executable, manifest["entry"], "--dev"] + extra, cwd=path, env=env)
