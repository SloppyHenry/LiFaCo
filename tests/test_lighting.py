import base64
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from fancontrol_linux.lighting import catalog, devtools, effects, store, udev  # noqa: E402
from fancontrol_linux.lighting import manifest as mf
from fancontrol_linux.lighting.manager import LightingError, LightingManager  # noqa: E402

TOML = '''
id = "test-plugin"
name = "Test plugin"
version = "1.2.3"
description = "A plugin"
tags = ["Alpha", "beta"]

[permissions]
network = true
usb = ["1462:7D25"]

[[settings]]
key = "host"
label = "Host"
type = "text"
default = "127.0.0.1"

[[settings]]
key = "count"
type = "number"
default = 2
min = 1
max = 5

[[settings]]
key = "mode"
type = "choice"
choices = ["a", "b"]   # a comment
'''

PLUGIN_PY = '''
from lifaco_plugin import Device, Mode, Plugin, Zone, run
import json, os

class P(Plugin):
    def discover(self):
        n = int(self.settings.get("count", 2))
        return [Device("d1", "Device one", type="strip", leds=n),
                Device("d2", "Device two", type="keyboard", zones=[Zone("Keys", 4)], modes=[Mode("Wave", speed=True)])]

    def set_colors(self, device_id, colors):
        with open(os.path.join(self.data_dir, "out.json"), "w") as f:
            json.dump({"device": device_id, "colors": colors}, f)

    def set_mode(self, device_id, mode, colors, speed, brightness):
        with open(os.path.join(self.data_dir, "mode.json"), "w") as f:
            json.dump({"device": device_id, "mode": mode, "speed": speed}, f)

run(P)
'''


def make_zip(files, prefix=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return buf.getvalue()


def plugin_zip(toml=TOML, prefix="test-plugin/"):
    return make_zip({"plugin.toml": toml, "plugin.py": PLUGIN_PY}, prefix)


class ManifestTests(unittest.TestCase):
    def test_parse_and_defaults(self):
        m = mf.load_manifest(TOML)
        self.assertEqual((m["id"], m["version"], m["api"], m["entry"]), ("test-plugin", "1.2.3", 1, "plugin.py"))
        self.assertEqual(m["tags"], ["alpha", "beta"])
        self.assertEqual(m["permissions"], {"network": True, "usb": ["1462:7d25"], "i2c": False})
        self.assertEqual([s["key"] for s in m["settings"]], ["host", "count", "mode"])
        self.assertEqual(m["settings"][2]["default"], "a")

    EXPECTED = {
        "id": "test-plugin", "name": "Test plugin", "version": "1.2.3", "description": "A plugin",
        "tags": ["Alpha", "beta"], "permissions": {"network": True, "usb": ["1462:7D25"]},
        "settings": [{"key": "host", "label": "Host", "type": "text", "default": "127.0.0.1"},
                     {"key": "count", "type": "number", "default": 2, "min": 1, "max": 5},
                     {"key": "mode", "type": "choice", "choices": ["a", "b"]}]}

    def test_parser_result(self):
        self.assertEqual(mf.parse_toml(TOML), self.EXPECTED)

    def test_fallback_parser_gives_the_same_result(self):
        # Python 3.10 has no tomllib and always uses the built-in parser; on newer versions force it.
        saved = sys.modules.get("tomllib", False)
        sys.modules["tomllib"] = None      # makes "import tomllib" raise ImportError
        try:
            fallback = mf.parse_toml(TOML)
        finally:
            if saved is False:
                del sys.modules["tomllib"]
            else:
                sys.modules["tomllib"] = saved
        self.assertEqual(fallback, self.EXPECTED)

    def test_rejections(self):
        bad = {
            'id = "Bad Id"\nname = "x"\nversion = "1.0.0"': "id",
            'id = "ok-id"\nname = "x"\nversion = "1.0"': "version",
            'id = "ok-id"\nname = "x"\nversion = "1.0.0"\napi = 99': "API",
            'id = "ok-id"\nname = "x"\nversion = "1.0.0"\nentry = "../evil.py"': "entry",
            'id = "ok-id"\nname = "x"\nversion = "1.0.0"\n[permissions]\nroot = true': "permission",
            'id = "ok-id"\nname = "x"\nversion = "1.0.0"\n[permissions]\nusb = ["nope"]': "USB",
            'id = "ok-id"\nname = "x"\nversion = "1.0.0"\n[[settings]]\nkey = "Bad Key"': "Setting",
        }
        for text, word in bad.items():
            with self.assertRaises(mf.ManifestError, msg=text) as ctx:
                mf.load_manifest(text)
            self.assertIn(word, str(ctx.exception))

    def test_settings_are_cleaned(self):
        m = mf.load_manifest(TOML)
        out = mf.clean_settings(m, {"host": "h", "count": 99, "mode": "zzz", "unknown": 1})
        self.assertEqual(out, {"host": "h", "count": 5, "mode": "a"})

    def test_permission_key_changes_with_permissions(self):
        a = mf.load_manifest(TOML)["permissions"]
        b = dict(a, i2c=True)
        self.assertNotEqual(mf.permission_key(a), mf.permission_key(b))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["FANCONTROL_PLUGINS_DIR"] = os.path.join(self.tmp, "plugins")
        os.environ["FANCONTROL_PLUGIN_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["FANCONTROL_CONFIG_DIR"] = os.path.join(self.tmp, "cfg")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_install_with_and_without_folder(self):
        for prefix in ("test-plugin/", ""):
            store.install_package(plugin_zip(prefix=prefix))
            found, errors = store.installed()
            self.assertEqual(list(found), ["test-plugin"])
            self.assertEqual(errors, {})
            store.remove_plugin("test-plugin")
        self.assertEqual(store.installed()[0], {})

    def test_reinstall_replaces(self):
        store.install_package(plugin_zip())
        store.install_package(plugin_zip(TOML.replace("1.2.3", "1.3.0")))
        self.assertEqual(store.installed()[0]["test-plugin"]["manifest"]["version"], "1.3.0")
        self.assertEqual(os.listdir(store.plugins_dir()), ["test-plugin"])

    def test_unsafe_packages_are_refused(self):
        cases = {
            "path traversal": make_zip({"../evil.py": "x", "plugin.toml": TOML, "plugin.py": ""}),
            "absolute": make_zip({"/etc/evil": "x"}),
            "no manifest": make_zip({"plugin.py": "x"}),
            "two folders": make_zip({"a/plugin.toml": TOML, "b/plugin.py": ""}),
            "not a zip": b"hello",
        }
        for name, data in cases.items():
            with self.assertRaises(store.InstallError, msg=name):
                store.install_package(data)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.py")))
        self.assertEqual(store.installed()[0], {})

    def test_symlinks_are_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("plugin.toml", TOML)
            zf.writestr("plugin.py", "")
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(store.InstallError):
            store.install_package(buf.getvalue())

    def test_expected_id_and_version_are_enforced(self):
        with self.assertRaises(store.InstallError):
            store.install_package(plugin_zip(), expect_id="other")
        with self.assertRaises(store.InstallError):
            store.install_package(plugin_zip(), expect_version="9.9.9")

    def test_size_bomb_is_refused(self):
        with self.assertRaises(store.InstallError):
            store.install_package(make_zip({"plugin.toml": TOML, "plugin.py": "#" * (store.MAX_ZIP + 1)}))

    def test_pack_and_state_roundtrip(self):
        folder = os.path.join(self.tmp, "src", "test-plugin")
        os.makedirs(folder)
        open(os.path.join(folder, "plugin.toml"), "w").write(TOML)
        open(os.path.join(folder, "plugin.py"), "w").write(PLUGIN_PY)
        os.makedirs(os.path.join(folder, "__pycache__"))
        open(os.path.join(folder, "__pycache__", "x.pyc"), "w").write("x")
        manifest, data = store.pack_directory(folder)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertEqual(sorted(names), ["test-plugin/plugin.py", "test-plugin/plugin.toml"])
        state = store.empty_state()
        state["plugins"]["x"] = {"enabled": True, "approved": "k", "settings": {"a": 1}, "source": "catalog"}
        store.save_state(state)
        self.assertEqual(store.load_state()["plugins"]["x"]["settings"], {"a": 1})


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.zip = plugin_zip()
        with open(os.path.join(cls.tmp, "test-plugin-1.2.3.zip"), "wb") as f:
            f.write(cls.zip)
        cls.sha = hashlib.sha256(cls.zip).hexdigest()
        index = {"format": 1, "plugins": [
            {"id": "test-plugin", "name": "Test plugin", "version": "1.2.3", "description": "Lights up WLED strips",
             "tags": ["wled"], "sha256": cls.sha, "download": "test-plugin-1.2.3.zip", "size": len(cls.zip)},
            {"id": "other", "name": "Keyboard helper", "version": "1.0.0", "description": "RGB keyboards",
             "tags": ["razer"], "sha256": "0" * 64, "download": "other.zip"},
            {"id": "bad entry", "version": "x"},
        ]}
        with open(os.path.join(cls.tmp, "index.json"), "w") as f:
            json.dump(index, f)
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=cls.tmp, **k)  # noqa: E731
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/index.json"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp)

    def setUp(self):
        os.environ["FANCONTROL_CATALOG_URL"] = self.url

    def tearDown(self):
        os.environ.pop("FANCONTROL_CATALOG_URL", None)

    def test_entries_skip_invalid_ones_and_resolve_relative_urls(self):
        entries = catalog.Catalog().entries(self.url)
        self.assertEqual([e["id"] for e in entries], ["test-plugin", "other"])
        self.assertEqual(entries[0]["download"], self.url.replace("index.json", "test-plugin-1.2.3.zip"))

    def test_search(self):
        entries = catalog.Catalog().entries(self.url)
        ids = lambda q: [e["id"] for e in catalog.search(entries, q)]  # noqa: E731
        self.assertEqual(ids("wled"), ["test-plugin"])
        self.assertEqual(ids("RGB keyboard"), ["other"])
        self.assertEqual(ids("keyboard wled"), [])
        self.assertEqual(ids(""), ["other", "test-plugin"])

    def test_download_checks_checksum(self):
        c = catalog.Catalog()
        entries = {e["id"]: e for e in c.entries(self.url)}
        self.assertEqual(c.download(self.url, entries["test-plugin"]), self.zip)
        with self.assertRaises(catalog.CatalogError):
            c.download(self.url, dict(entries["test-plugin"], sha256="1" * 64))

    def test_plain_http_is_refused_unless_trusted(self):
        os.environ.pop("FANCONTROL_CATALOG_URL")
        with self.assertRaises(catalog.CatalogError):
            catalog.Catalog().entries(self.url)


class EffectTests(unittest.TestCase):
    def test_normalize_rejects_bad_input(self):
        for bad in ({"type": "nope"}, {"type": "static", "color": [300, 0, 0]}, {"type": "static", "brightness": 101},
                    {"type": "hardware"}, {"type": "temperature", "stops": [[30, [0, 0, 0]]]}, "x"):
            with self.assertRaises(effects.EffectError, msg=str(bad)):
                effects.normalize(bad)

    def test_static_and_brightness(self):
        e = effects.normalize({"type": "static", "color": [200, 100, 0], "brightness": 50})
        self.assertEqual(effects.render(e, 3, 0), [(100, 50, 0)] * 3)
        self.assertEqual(effects.render(effects.normalize({"type": "off"}), 2, 0), [(0, 0, 0)] * 2)

    def test_temperature_gradient(self):
        e = effects.normalize({"type": "temperature", "sensor": "cpu", "stops": [[40, [0, 0, 255]], [80, [255, 0, 0]]]})
        self.assertEqual(effects.render(e, 1, 0, {"cpu": 20})[0], (0, 0, 255))
        self.assertEqual(effects.render(e, 1, 0, {"cpu": 60})[0], (128, 0, 128))
        self.assertEqual(effects.render(e, 1, 0, {"cpu": 99})[0], (255, 0, 0))
        self.assertEqual(effects.render(e, 1, 0, {})[0], (0, 0, 255))      # unknown sensor -> cold end

    def test_animations_change_over_time(self):
        rainbow = effects.normalize({"type": "rainbow"})
        self.assertNotEqual(effects.render(rainbow, 4, 0), effects.render(rainbow, 4, 3))
        self.assertEqual(len(set(effects.render(rainbow, 4, 0))), 4)
        breathing = effects.normalize({"type": "breathing", "color": [255, 255, 255], "speed": 100})
        self.assertNotEqual(effects.render(breathing, 1, 0), effects.render(breathing, 1, 0.5))
        self.assertTrue(effects.is_animated(rainbow) and not effects.is_animated(effects.normalize({"type": "off"})))


class UdevTests(unittest.TestCase):
    def test_rules_only_for_declared_devices(self):
        m = mf.load_manifest(TOML)["permissions"]
        text = udev.render_rules([("a", m), ("b", {"network": True, "usb": [], "i2c": False})])
        self.assertIn('ATTRS{idVendor}=="1462", ATTRS{idProduct}=="7d25"', text)
        self.assertNotIn("i2c-dev", text)
        self.assertEqual(text.count("hidraw"), 1)

    def test_i2c_rule_only_when_approved(self):
        perms = {"network": False, "usb": [], "i2c": True}
        self.assertIn('SUBSYSTEM=="i2c-dev", GROUP="lifaco-i2c"', udev.render_rules([("ram", perms)]))

    def test_no_rules_without_root_and_ids_cannot_inject(self):
        # Manifest validation is what keeps quotes and newlines out of the rules file.
        with self.assertRaises(mf.ManifestError):
            mf.load_manifest('id = "ok-id"\nname = "x"\nversion = "1.0.0"\n[permissions]\nusb = ["1234:5678\\", GROUP=\\"root"]')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["FANCONTROL_PLUGINS_DIR"] = os.path.join(self.tmp, "plugins")
        os.environ["FANCONTROL_PLUGIN_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["FANCONTROL_CONFIG_DIR"] = os.path.join(self.tmp, "cfg")
        self.m = LightingManager(temps=lambda: {"cpu": 55.0})
        self.m.start()

    def tearDown(self):
        self.m.shutdown()
        shutil.rmtree(self.tmp)

    def wait(self, cond, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def out(self, name="out.json"):
        with open(os.path.join(self.tmp, "data", "test-plugin", name)) as f:
            return json.load(f)

    def install_and_enable(self):
        self.m.install_file(base64.b64encode(plugin_zip()).decode())
        self.m.enable("test-plugin", True, approve=True)
        self.assertTrue(self.wait(lambda: self.m.list_devices()))

    def test_enable_needs_approval_for_permissions(self):
        self.m.install_file(base64.b64encode(plugin_zip()).decode())
        with self.assertRaises(LightingError):
            self.m.enable("test-plugin", True)
        p = self.m.list_plugins()[0]
        self.assertTrue(p["needs_approval"])
        self.assertIn("Access the USB device 1462:7d25", p["permission_lines"])
        self.assertFalse(self.m.list_devices())

    def test_devices_effects_and_frames(self):
        self.install_and_enable()
        keys = [d["key"] for d in self.m.list_devices()]
        self.assertEqual(keys, ["test-plugin:d1", "test-plugin:d2"])
        self.m.set_effect("test-plugin:d1", {"type": "static", "color": [10, 20, 30]})
        self.assertTrue(self.wait(lambda: os.path.exists(os.path.join(self.tmp, "data", "test-plugin", "out.json"))))
        self.assertEqual(self.out(), {"device": "d1", "colors": [[10, 20, 30]] * 2})
        self.m.set_effect("test-plugin:d2", {"type": "hardware", "mode": "Wave", "speed": 100})
        self.assertTrue(self.wait(lambda: os.path.exists(os.path.join(self.tmp, "data", "test-plugin", "mode.json"))))
        self.assertEqual(self.out("mode.json"), {"device": "d2", "mode": "Wave", "speed": 1.0})
        with self.assertRaises(LightingError):
            self.m.set_effect("test-plugin:d2", {"type": "hardware", "mode": "Missing"})
        with self.assertRaises(LightingError):
            self.m.set_effect("nope:x", {"type": "off"})

    def test_settings_restart_plugin_and_persist(self):
        self.install_and_enable()
        self.m.set_settings("test-plugin", {"count": 4})
        self.assertTrue(self.wait(lambda: any(d["leds"] == 8 - 4 for d in self.m.list_devices()
                                              if d["id"] == "d1") if self.m.list_devices() else False))
        self.assertEqual(store.load_state()["plugins"]["test-plugin"]["settings"]["count"], 4)

    def test_permission_change_invalidates_approval(self):
        self.install_and_enable()
        self.m.install_file(base64.b64encode(plugin_zip(TOML.replace("network = true", "network = true\ni2c = true"))).decode())
        p = self.m.list_plugins()[0]
        self.assertTrue(p["needs_approval"])
        self.assertEqual(p["status"], "needs_approval")
        self.assertFalse(self.m.list_devices())

    def test_crashing_plugin_reports_error_and_others_survive(self):
        crash = TOML.replace("test-plugin", "crasher")
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": crash, "plugin.py": "raise SystemExit(3)"})).decode())
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": crash.replace("crasher", "hang"), "plugin.py":
                                                       "import time\ntime.sleep(60)"})).decode())
        self.m.enable("crasher", True, approve=True)
        self.assertTrue(self.wait(lambda: next(p for p in self.m.list_plugins() if p["id"] == "crasher")["status"] == "error"))
        p = next(p for p in self.m.list_plugins() if p["id"] == "crasher")
        self.assertIn("exit code 3", p["error"])

    def test_error_message_contains_the_reason(self):
        toml = TOML.replace("test-plugin", "explodes")
        code = "from lifaco_plugin import Plugin, run\nraise RuntimeError('boom reason')\n"
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": toml, "plugin.py": code})).decode())
        self.m.enable("explodes", True, approve=True)
        self.assertTrue(self.wait(lambda: next(p for p in self.m.list_plugins() if p["id"] == "explodes")["status"] == "error"))
        error = next(p for p in self.m.list_plugins() if p["id"] == "explodes")["error"]
        self.assertIn("RuntimeError: boom reason", error)

    def test_remove_deletes_plugin_and_assignments(self):
        self.install_and_enable()
        self.m.set_effect("test-plugin:d1", {"type": "off"})
        self.m.remove("test-plugin")
        self.assertEqual(self.m.list_plugins(), [])
        self.assertEqual(store.load_state()["devices"], {})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plugins", "test-plugin")))


class DevtoolsTests(unittest.TestCase):
    def test_scaffold_validates_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = devtools.new_plugin(tmp, "my-strip")
            manifest, warnings = devtools.validate(path)
            self.assertEqual(manifest["id"], "my-strip")
            self.assertEqual(warnings, [])
            self.assertEqual(devtools.dev_run(path, []), 0)
            with self.assertRaises(ValueError):
                devtools.new_plugin(tmp, "my-strip")
            with self.assertRaises(ValueError):
                devtools.new_plugin(tmp, "Not Valid")

    def test_validate_catches_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = devtools.new_plugin(tmp, "broken")
            with open(os.path.join(path, "plugin.py"), "w") as f:
                f.write("def (:\n")
            with self.assertRaises(mf.ManifestError):
                devtools.validate(path)


if __name__ == "__main__":
    unittest.main()


class ProcessTests(unittest.TestCase):
    def test_python_command_plain_and_appimage(self):
        from fancontrol_linux.lighting import process
        os.environ.pop("FANCONTROL_APPIMAGE_DIR", None)
        cmd, env = process.python_command()
        self.assertEqual((cmd, env), ([sys.executable, "-I", "-B"], {}))
        os.environ["FANCONTROL_APPIMAGE_DIR"] = "/opt/fancontrol-linux"
        try:
            cmd, env = process.python_command()
        finally:
            del os.environ["FANCONTROL_APPIMAGE_DIR"]
        self.assertEqual(cmd[0], "/opt/fancontrol-linux/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2")
        self.assertIn("/opt/fancontrol-linux/usr/bin/python3", cmd)
        self.assertEqual(env, {"PYTHONHOME": "/opt/fancontrol-linux/usr"})
        self.assertNotIn("-I", cmd)          # -I would ignore PYTHONHOME

    def test_plugin_cannot_import_lifaco_internals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = devtools.new_plugin(tmp, "probe")
            with open(os.path.join(path, "plugin.py"), "w") as f:
                f.write("import sys\nfor name in ('effects', 'udev'):\n"
                        "    try:\n        __import__(name)\n        print('LEAK', name)\n"
                        "    except ImportError:\n        pass\n")
            # Run through the real bootstrap. Without -I (as in the AppImage) the script folder would be importable
            # if the bootstrap did not remove it, so this is the case that matters.
            import subprocess
            cfg = {"plugin_dir": path, "entry": "plugin.py", "sdk_dir": devtools.SDK_DIR, "uid": None, "gid": None,
                   "groups": []}
            out = subprocess.run([sys.executable, "-s", "-B", os.path.join(os.path.dirname(devtools.__file__), "bootstrap.py"),
                                  json.dumps(cfg)], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
            self.assertNotIn("LEAK", out.stdout + out.stderr)
            self.assertEqual(out.returncode, 0, out.stderr)


class DaemonTests(unittest.TestCase):
    """The lighting commands must never wait for the fan-control lock."""

    def setUp(self):
        import fake_hwmon
        from fancontrol_linux.daemon import Daemon
        from fancontrol_linux.hwmon import Hardware
        self.tmp = tempfile.mkdtemp()
        for name, sub in (("FANCONTROL_PLUGINS_DIR", "plugins"), ("FANCONTROL_PLUGIN_DATA_DIR", "data"),
                          ("FANCONTROL_CONFIG_DIR", "cfg")):
            os.environ[name] = os.path.join(self.tmp, sub)
        os.environ["FANCONTROL_CATALOG_URL"] = "file://" + os.path.join(self.tmp, "no-such-index.json")   # no network
        fake_hwmon.create(os.path.join(self.tmp, "hwmon"))
        self.daemon = Daemon(hardware=Hardware(root=os.path.join(self.tmp, "hwmon"), nvidia=False))

    def tearDown(self):
        os.environ.pop("FANCONTROL_CATALOG_URL", None)
        shutil.rmtree(self.tmp)

    def test_lighting_commands_do_not_take_the_fan_lock(self):
        result = {}

        def ask():
            result["plugins"] = self.daemon.handle({"cmd": "plugin_list"})
            result["devices"] = self.daemon.handle({"cmd": "light_devices"})

        with self.daemon.lock:                       # pretend the control loop is busy
            t = threading.Thread(target=ask)
            t.start()
            t.join(3)
            self.assertFalse(t.is_alive(), "lighting waited for the fan-control lock")
        self.assertEqual(result, {"plugins": [], "devices": []})

    def test_fan_commands_still_use_the_lock(self):
        done = threading.Event()
        with self.daemon.lock:
            threading.Thread(target=lambda: (self.daemon.handle({"cmd": "ping"}), done.set()), daemon=True).start()
            self.assertFalse(done.wait(0.3))
        self.assertTrue(done.wait(3))

    def test_unknown_and_bad_plugin_commands_are_errors(self):
        with self.assertRaises(ValueError):
            self.daemon.handle({"cmd": "plugin_nope"})
        with self.assertRaises(ValueError):
            self.daemon.handle({"cmd": "plugin_install", "id": "not-in-any-catalog"})
        with self.assertRaises(ValueError):
            self.daemon.handle({"cmd": "plugin_install_file", "data": "!!!not base64!!!"})
        with self.assertRaises(ValueError):
            self.daemon.handle({"cmd": "light_set", "device": "x:y", "effect": {"type": "off"}})
