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
        self.assertEqual(m["permissions"], {"network": True, "usb": ["1462:7d25"], "i2c": False, "start": []})
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


class ManagerBase(unittest.TestCase):
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

    def colors_now(self):
        try:
            return self.out()["colors"][0]
        except (FileNotFoundError, ValueError):
            return None


class ManagerTests(ManagerBase):

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


class FakeLiquidDevice:
    """Stands in for a liquidctl driver object (a Kraken-like device with three colour channels)."""

    def __init__(self, fail=False):
        self._color_channels = {"external": 1, "ring": 2, "logo": 4, "sync": 7}
        self.calls = []
        self.fail = fail

    def set_color(self, channel, mode, colors, **kwargs):
        if self.fail:
            raise OSError("usb write failed")
        self.calls.append((channel, mode, colors, kwargs))


class LiquidctlLightingTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.tmp = tempfile.mkdtemp()
        for name, sub in (("FANCONTROL_PLUGINS_DIR", "plugins"), ("FANCONTROL_PLUGIN_DATA_DIR", "data"),
                          ("FANCONTROL_CONFIG_DIR", "cfg")):
            os.environ[name] = os.path.join(self.tmp, sub)
        self.fake = FakeLiquidDevice()
        self.dev = SimpleNamespace(key="liquidctl:nzxt-kraken", dev=self.fake, name="NZXT Kraken X (X53)",
                                   status=[("Liquid temperature", 30, "°C"), ("Pump speed", 2000, "rpm")],
                                   lock=threading.RLock())
        from fancontrol_linux.lighting.liquidctl_lighting import LiquidctlLighting
        self.provider = LiquidctlLighting(lambda: [self.dev])
        self.m = LightingManager()
        self.m.set_builtin(self.provider, "liquidctl (built in)")
        self.m.start()

    def tearDown(self):
        self.m.shutdown()
        shutil.rmtree(self.tmp)

    def wait(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_device_and_channels(self):
        d = self.m.list_devices()[0]
        self.assertEqual((d["key"], d["type"], d["direct"], d["leds"]), ("liquidctl:nzxt-kraken", "cooler", True, 3))
        self.assertEqual([z["name"] for z in d["zones"]], ["external", "ring", "logo"])     # "sync" is not a zone
        entry = self.m.list_plugins()[0]
        self.assertTrue(entry["builtin"] and entry["status"] == "running")

    def test_static_colour_uses_fixed_mode_and_never_saves(self):
        self.m.set_effect("liquidctl:nzxt-kraken", {"type": "static", "color": [10, 20, 30]})
        self.assertTrue(self.wait(lambda: len(self.fake.calls) == 3))
        self.assertEqual([(c, m, col) for c, m, col, _kw in self.fake.calls],
                         [("external", "fixed", [[10, 20, 30]]), ("ring", "fixed", [[10, 20, 30]]),
                          ("logo", "fixed", [[10, 20, 30]])])
        self.assertTrue(all(kw == {} for *_a, kw in self.fake.calls))       # no non_volatile or other options
        time.sleep(0.8)
        self.assertEqual(len(self.fake.calls), 3)                            # unchanged colour is not resent

    def test_animated_effect_is_throttled(self):
        self.m.set_effect("liquidctl:nzxt-kraken", {"type": "rainbow", "speed": 100})
        time.sleep(1.6)
        per_channel = len([c for c in self.fake.calls if c[0] == "ring"])
        self.assertLessEqual(per_channel, 4)        # 0.5 s minimum interval, not 20 updates per second
        self.assertGreaterEqual(per_channel, 2)

    def test_driver_error_shows_on_the_device(self):
        self.fake.fail = True
        self.m.set_effect("liquidctl:nzxt-kraken", {"type": "static", "color": [1, 2, 3]})
        self.assertTrue(self.wait(lambda: "usb write failed" in self.m.list_devices()[0]["error"]))

    def test_fan_control_and_lighting_share_the_device_lock(self):
        got = []
        with self.dev.lock:                              # the fan loop is talking to the device
            self.m.set_effect("liquidctl:nzxt-kraken", {"type": "static", "color": [1, 2, 3]})
            time.sleep(0.5)
            got.append(len(self.fake.calls))
        self.assertEqual(got, [0])
        self.assertTrue(self.wait(lambda: len(self.fake.calls) == 3))

    def test_devices_without_known_channels_are_not_listed(self):
        self.fake._color_channels = {}
        self.assertEqual(self.provider.devices(), [])

    def test_hardware_effects_are_refused_and_id_is_reserved(self):
        with self.assertRaises(LightingError):
            self.m.set_effect("liquidctl:nzxt-kraken", {"type": "hardware", "mode": "rainbow"})
        with self.assertRaises(mf.ManifestError):
            mf.load_manifest('id = "liquidctl"\nname = "x"\nversion = "1.0.0"')


class LightingFeatureTests(ManagerBase):
    """Power switches, profiles, fan-driven colours, resume and 'off on exit' (reuses the plugin fixture)."""

    def test_power_off_and_on_restores_the_previous_effect(self):
        self.install_and_enable()
        key = "test-plugin:d1"
        self.m.set_effect(key, {"type": "static", "color": [10, 20, 30]})
        self.m.power(key, False)
        self.assertFalse(next(d for d in self.m.list_devices() if d["key"] == key)["on"])
        self.assertTrue(self.wait(lambda: self.colors_now() == [0, 0, 0]))
        self.m.power(key, True)
        self.assertTrue(self.wait(lambda: self.colors_now() == [10, 20, 30]))
        self.assertTrue(next(d for d in self.m.list_devices() if d["key"] == key)["on"])

    def test_power_on_without_history_uses_white_and_hardware_only_needs_a_choice(self):
        self.install_and_enable()
        self.m.power("test-plugin:d1", True)
        self.assertEqual(self.m.list_devices()[0]["effect"]["color"], [255, 255, 255])
        for d in self.m.runtimes["test-plugin"].devices:    # pretend d2 has no direct colours
            if d["id"] == "d2":
                d["direct"] = False
        with self.assertRaises(LightingError):
            self.m.power("test-plugin:d2", True)
        self.assertEqual(self.m.power_all(False), 2)          # d1 goes black, d2 (hardware effects only) is released
        self.assertIsNone(next(d for d in self.m.list_devices() if d["id"] == "d2")["effect"])

    def test_profile_data_roundtrip(self):
        self.install_and_enable()
        self.m.set_effect("test-plugin:d1", {"type": "static", "color": [1, 2, 3]})
        data = self.m.profile_data()
        self.m.set_effect("test-plugin:d1", {"type": "off"})
        self.m.load_profile_data(data)
        self.assertEqual(self.m.list_devices()[0]["effect"]["color"], [1, 2, 3])
        self.m.load_profile_data({"devices": {}})
        self.assertIsNone(self.m.list_devices()[0]["effect"])
        self.assertEqual(store.load_state()["devices"], {})

    def test_temperature_effect_can_follow_a_fan(self):
        self.install_and_enable()
        m = LightingManager(temps=lambda: {"cpu": 90.0, "fan:pwm1": 100.0})
        self.assertEqual(m._temps()[""], 90.0)                  # "hottest" ignores fan values
        e = effects.normalize({"type": "temperature", "sensor": "fan:pwm1"})
        self.assertEqual(e["stops"][0][0], 0)                    # fan stops run 0-100 %
        self.assertEqual(effects.render(e, 1, 0, m._temps())[0], (255, 30, 0))

    def test_resume_from_suspend_sends_everything_again(self):
        self.install_and_enable()
        self.m.set_effect("test-plugin:d1", {"type": "static", "color": [5, 5, 5]})
        self.assertTrue(self.wait(lambda: self.m.applied))
        self.m.boot_gap -= 10                                   # as if the machine slept for 10 s
        self.m.applied.clear()
        self.assertTrue(self.wait(lambda: self.m.applied))       # the loop re-applied without any user action
        self.assertGreater(len(self.m.applied), 0)

    def test_off_on_exit_sends_black_but_keeps_the_assignment(self):
        self.install_and_enable()
        self.m.set_effect("test-plugin:d1", {"type": "static", "color": [9, 9, 9]})
        self.assertTrue(self.wait(lambda: self.colors_now() == [9, 9, 9]))
        self.assertEqual(self.m.set_light_settings({"off_on_exit": True}), {"off_on_exit": True})
        self.m.shutdown()
        self.assertEqual(self.out()["colors"][0], [0, 0, 0])
        self.assertEqual(store.load_state()["devices"]["test-plugin:d1"]["color"], [9, 9, 9])
        self.assertTrue(store.load_state()["off_on_exit"])


class ProfileLightingTests(unittest.TestCase):
    def test_profile_config_keeps_and_validates_lighting(self):
        from fancontrol_linux import config as cfgmod
        cfg = cfgmod.normalize({"lighting": {"devices": {"a:b": {"type": "static", "color": [1, 2, 3]},
                                                         "c:d": {"type": "nonsense"}}}})
        self.assertEqual(list(cfg["lighting"]["devices"]), ["a:b"])                # invalid entries are dropped
        self.assertIsNone(cfgmod.normalize({})["lighting"])                        # old profiles say nothing

    def test_saving_and_loading_a_profile_carries_the_lights(self):
        import fake_hwmon

        from fancontrol_linux.daemon import Daemon
        from fancontrol_linux.hwmon import Hardware
        with tempfile.TemporaryDirectory() as tmp:
            for name, sub in (("FANCONTROL_PLUGINS_DIR", "plugins"), ("FANCONTROL_PLUGIN_DATA_DIR", "data"),
                              ("FANCONTROL_CONFIG_DIR", "cfg")):
                os.environ[name] = os.path.join(tmp, sub)
            fake_hwmon.create(os.path.join(tmp, "hwmon"))
            d = Daemon(hardware=Hardware(root=os.path.join(tmp, "hwmon"), nvidia=False))
            d.lighting.state["devices"]["x:y"] = {"type": "static", "color": [7, 7, 7], "brightness": 100.0}
            d.handle({"cmd": "save_profile", "name": "Gaming"})
            d.lighting.state["devices"].clear()
            d.handle({"cmd": "set_config", "config": d.handle({"cmd": "get_config"})})   # a GUI push must not wipe lights
            d.lighting.state["devices"]["x:y"] = {"type": "off"}
            d.handle({"cmd": "load_profile", "name": "Gaming"})
            self.assertEqual(d.lighting.state["devices"]["x:y"]["color"], [7, 7, 7])


class HelperTests(unittest.TestCase):
    def setUp(self):
        from fancontrol_linux.lighting import helpers
        self.h = helpers
        self.helper = helpers.OpenRgbHelper()
        self._which, self._pm = helpers.shutil.which, helpers.package_manager
        self._orig = (helpers.fetch_upstream_assets, helpers.download, helpers.OpenRgbHelper._run,
                      helpers.platform.machine)
        self._running = helpers._server_running
        helpers._server_running = lambda: False          # independent of an OpenRGB running on this computer

    def tearDown(self):
        self.h._server_running = self._running
        self.h.shutil.which = self._which
        self.h.package_manager = self._pm
        self.h.fetch_upstream_assets, self.h.download = self._orig[:2]
        self.h.OpenRgbHelper._run, self.h.platform.machine = self._orig[2:]

    def wait_done(self):
        end = time.monotonic() + 5
        while self.helper.installing and time.monotonic() < end:
            time.sleep(0.02)

    def test_manifest_lists_helpers_and_unknown_ones_are_ignored(self):
        m = mf.load_manifest(TOML + '\n[requires]\nhelpers = ["openrgb", "made-up"]\n')
        self.assertEqual(m["requires"]["helpers"], ["openrgb", "made-up"])
        mgr = LightingManager()
        self.assertRaises(LightingError, mgr.helper_install, "made-up")

    def test_status_hints(self):
        self.h.shutil.which = lambda name: None
        self.h.package_manager = lambda: ("apt-get", ["apt-get"])
        st = self.helper.status()
        self.assertFalse(st["installed"])
        self.assertTrue(st["can_install"])
        self.assertIn("not installed", st["hint"])
        self.h.package_manager = lambda: (None, None)
        self.assertTrue(self.helper.status()["can_install"])        # falls back to the project's own release
        self.assertIn("official releases", self.helper.status()["hint"])
        self.h.shutil.which = lambda name: "/usr/bin/openrgb" if name == "openrgb" else None
        self.assertIn("sudo openrgb --server", self.helper.status()["hint"])

    def test_install_runs_only_the_package_command(self):
        ran = []
        self.h.shutil.which = lambda name: None
        self.h.package_manager = lambda: ("fake", ["sh", "-c", "echo ok"])
        real = self.h.subprocess.run

        def fake_run(cmd, **kw):
            ran.append(cmd)
            return real(cmd, **kw)
        self.h.subprocess.run = fake_run
        try:
            self.helper.start_install()
            self.wait_done()
        finally:
            self.h.subprocess.run = real
        self.assertEqual(self.helper.error, "")
        self.assertEqual(len(ran), 1)                        # no service, no other commands
        self.assertNotIn("systemctl", " ".join(ran[0]))

    def test_missing_package_falls_back_to_the_official_release(self):
        self.h.shutil.which = lambda name: None
        self.h.package_manager = lambda: ("apt-get", ["sh", "-c", "echo 'E: Unable to locate package openrgb' >&2; exit 100"])
        assets = [("openrgb_1.0_amd64_bookworm_abc1234.deb", self.h.UPSTREAM_FILES + "release_1.0/a.deb", 1)]
        self.h.fetch_upstream_assets = lambda: assets
        self.h.download = lambda url, name: "/tmp/never-used.deb"
        self.h.platform.machine = lambda: "x86_64"
        ran = []
        self.h.OpenRgbHelper._run = lambda self_, cmd: (ran.append(cmd), (0, "") if len(ran) > 1 else (100, "E: Unable to locate package"))[1]
        self.helper.start_install()
        self.wait_done()
        self.assertEqual(self.helper.error, "")
        self.assertEqual(ran[1], ["apt-get", "install", "-y", "/tmp/never-used.deb"])

    def test_nothing_reachable_gives_a_clear_message(self):
        self.h.shutil.which = lambda name: None
        self.h.package_manager = lambda: ("fake", ["sh", "-c", "echo 'E: Unable to locate package openrgb' >&2; exit 100"])

        def offline():
            raise OSError("no route")
        self.h.fetch_upstream_assets = offline
        self.helper.start_install()
        self.wait_done()
        self.assertIn("openrgb.org", self.helper.error)

    def test_choice_of_official_files(self):
        base = self.h.UPSTREAM_FILES + "release_1.0/"
        names = ["openrgb_1.0_amd64_bookworm_81bbe18.deb", "openrgb_1.0_amd64_trixie_81bbe18.deb",
                 "openrgb_1.0_arm64_trixie_81bbe18.deb", "OpenRGB_1.0_x86_64_81bbe18.AppImage",
                 "OpenRGB_1.0_Windows_64_81bbe18.zip"]
        assets = [(n, base + n, 1) for n in names]
        pick = lambda mgr, info, arch: [n for _k, n, _u in self.h.pick_upstream(assets, mgr, info, arch)]  # noqa: E731
        self.assertEqual(pick("apt-get", {"ID": "debian", "VERSION_ID": "13"}, "x86_64"),
                         ["openrgb_1.0_amd64_trixie_81bbe18.deb", "openrgb_1.0_amd64_bookworm_81bbe18.deb",
                          "OpenRGB_1.0_x86_64_81bbe18.AppImage"])
        self.assertEqual(pick("apt-get", {"ID": "ubuntu", "VERSION_ID": "24.04"}, "x86_64")[0],
                         "openrgb_1.0_amd64_bookworm_81bbe18.deb")
        self.assertEqual(pick("apk", {"ID": "alpine"}, "x86_64"), ["OpenRGB_1.0_x86_64_81bbe18.AppImage"])
        self.assertEqual(pick("apt-get", {"ID": "debian", "VERSION_ID": "13"}, "riscv64"), [])
        foreign = [("openrgb_1.0_amd64_trixie_81bbe18.deb", "https://evil.example/x.deb", 1)]
        self.assertEqual(self.h.pick_upstream(foreign, "apt-get", {"ID": "debian", "VERSION_ID": "13"}, "x86_64"), [])
        with self.assertRaises(RuntimeError):
            self.h.download("https://evil.example/x.deb", "x.deb")

    def test_nothing_is_installed_when_it_is_already_there(self):
        self.h.shutil.which = lambda name: "/usr/bin/openrgb"
        self.h.package_manager = lambda: ("fake", ["false"])
        self.helper.start_install()
        self.assertFalse(self.helper.installing)
        self.assertEqual(self.helper.error, "")


class SlowStopTests(ManagerBase):
    def test_removing_a_busy_plugin_is_quick(self):
        """A plugin stuck in discover() (for example waiting for a hung server) must not make removal slow:
        the GUI's client gives up after a few seconds."""
        toml = TOML.replace("test-plugin", "busy")
        code = ("import time\nfrom lifaco_plugin import Device, Plugin, run\n"
                "class P(Plugin):\n    def discover(self):\n        time.sleep(60)\nrun(P)\n")
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": toml, "plugin.py": code})).decode())
        self.m.enable("busy", True, approve=True)
        time.sleep(1.0)                                       # the plugin is now inside discover()
        started = time.monotonic()
        self.m.remove("busy")
        self.assertLess(time.monotonic() - started, 3.0)


class RetryTests(ManagerBase):
    def test_failing_plugin_is_retried_with_growing_waits_and_quiet_logs(self):
        toml = TOML.replace("test-plugin", "flaky")
        code = ("from lifaco_plugin import Plugin, run\nclass P(Plugin):\n    def discover(self):\n"
                "        raise RuntimeError('server not reachable')\nrun(P)\n")
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": toml, "plugin.py": code})).decode())
        with self.assertLogs("fancontrol-linuxd", level="WARNING") as logs:
            self.m.enable("flaky", True, approve=True)
            waits = []
            for _ in range(3):
                self.assertTrue(self.wait(lambda: self.m.runtimes["flaky"].state == "error"))
                rt = self.m.runtimes["flaky"]
                waits.append(round(rt.next_try - time.monotonic()))
                rt.next_try = 0                       # do not really wait
                self.m._tick()
        self.assertEqual([rt.attempts], [3])          # the count survives the restarts
        self.assertGreater(waits[2], waits[0])        # 4 s, 8 s, 16 s … not 4 s forever
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)            # the same failure is reported once


class ServerStartTests(ManagerBase):
    """A plugin may start a helper's server while it is on; that needs the user's approval."""

    START_TOML = TOML.replace("[permissions]", '[permissions]\nstart = ["openrgb"]')

    def setUp(self):
        super().setUp()
        from fancontrol_linux.lighting import helpers
        self.helpers = helpers
        self.calls = []
        self.real = helpers.HELPERS["openrgb"]

        class Stub:
            def ensure_server(_s):
                self.calls.append("start")
                return ""

            def stop_server(_s):
                self.calls.append("stop")

            def status(_s):
                return {"id": "openrgb", "name": "OpenRGB", "installed": True, "server_running": True,
                        "can_install": True, "installing": False, "error": "", "hint": ""}
        helpers.HELPERS["openrgb"] = Stub()
        import fancontrol_linux.lighting.manager as manager_module
        manager_module.HELPERS = helpers.HELPERS

    def tearDown(self):
        self.helpers.HELPERS["openrgb"] = self.real
        super().tearDown()

    def install(self, toml=None):
        toml = toml or self.START_TOML
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": toml, "plugin.py": PLUGIN_PY})).decode())

    def test_permission_is_shown_and_needed(self):
        self.install()
        p = self.m.list_plugins()[0]
        self.assertTrue(any("OpenRGB server" in line and "administrator" in line for line in p["permission_lines"]))
        with self.assertRaises(LightingError):
            self.m.enable("test-plugin", True)                      # not approved yet: nothing is started
        self.assertEqual(self.calls, [])

    def test_server_starts_with_the_plugin_and_stops_when_it_is_switched_off_or_removed(self):
        self.install()
        self.m.enable("test-plugin", True, approve=True)
        self.assertTrue(self.wait(lambda: "start" in self.calls))
        self.m.enable("test-plugin", False)
        self.assertEqual(self.calls[-1], "stop")
        self.m.enable("test-plugin", True, approve=True)
        self.assertTrue(self.wait(lambda: self.calls.count("start") == 2))
        self.m.remove("test-plugin")
        self.assertEqual(self.calls[-1], "stop")

    def test_restart_and_settings_changes_do_not_stop_the_server(self):
        self.install()
        self.m.enable("test-plugin", True, approve=True)
        self.assertTrue(self.wait(lambda: "start" in self.calls))
        self.m.set_settings("test-plugin", {"count": 3})
        self.m.restart("test-plugin")
        self.assertNotIn("stop", self.calls)

    def test_old_approvals_stay_valid_and_new_permission_needs_a_new_one(self):
        plain = mf.load_manifest(TOML)["permissions"]
        self.assertEqual(mf.permission_key(plain), "net=1;usb=1462:7d25;i2c=0")      # unchanged format
        with_start = mf.load_manifest(self.START_TOML)["permissions"]
        self.assertNotEqual(mf.permission_key(plain), mf.permission_key(with_start))


RESIZE_PY = '''
from lifaco_plugin import Device, Plugin, Zone, run
import json, os

class P(Plugin):
    def setup(self):
        self.size = 4
        self.searches = 0

    def discover(self):
        return [Device("hub", "Hub", zones=[Zone("Fixed", 2), Zone("Header", self.size, 0, 10)])]

    def set_colors(self, device_id, colors):
        pass

    def resize_zone(self, device_id, zone, leds):
        self.size = leds
        self.devices_changed()

    def rescan(self):
        self.searches += 1
        with open(os.path.join(self.data_dir, "searches.json"), "w") as f:
            json.dump(self.searches, f)

run(P)
'''


class ZoneSizeTests(ManagerBase):
    def setUp(self):
        super().setUp()
        toml = TOML.replace('id = "test-plugin"', 'id = "hub-plugin"')
        self.m.install_file(base64.b64encode(make_zip({"plugin.toml": toml, "plugin.py": RESIZE_PY},
                                                      "hub-plugin/")).decode())
        self.m.enable("hub-plugin", True, approve=True)
        self.assertTrue(self.wait(lambda: self.m.list_devices()))

    def test_resizable_zone_is_listed_and_resized(self):
        dev = self.m.list_devices()[0]
        self.assertEqual(dev["zones"], [{"name": "Fixed", "leds": 2},
                                        {"name": "Header", "leds": 4, "min_leds": 0, "max_leds": 10}])
        self.m.resize_zone("hub-plugin:hub", 1, 7)
        self.assertTrue(self.wait(lambda: self.m.list_devices()[0]["leds"] == 9))

    def test_resize_is_checked_before_the_plugin_is_asked(self):
        for zone, leds in ((0, 3), (1, 11), (1, -1), (5, 2), ("x", 2)):
            with self.assertRaises(LightingError):
                self.m.resize_zone("hub-plugin:hub", zone, leds)
        self.assertEqual(self.m.list_devices()[0]["leds"], 6)

    def test_hardware_rescan_asks_the_plugin(self):
        self.assertEqual(self.m.hardware_rescan("hub-plugin"), 1)
        with open(os.path.join(self.tmp, "data", "hub-plugin", "searches.json")) as f:
            self.assertEqual(json.load(f), 1)
        with self.assertRaises(LightingError):
            self.m.hardware_rescan("missing")


class OpenRgbServerCommandTests(unittest.TestCase):
    def setUp(self):
        from fancontrol_linux.lighting import helpers
        self.h = helpers
        self.saved = (helpers.shutil.which, helpers._systemd, helpers._server_running, helpers.subprocess.run,
                      helpers.time.sleep, helpers.CONFIG_HOME, helpers.CONFIG_DIR, helpers._unit_active)
        self.tmp = tempfile.mkdtemp()
        helpers.CONFIG_HOME = self.tmp
        helpers.CONFIG_DIR = os.path.join(self.tmp, "OpenRGB")
        self.helper = helpers.OpenRgbHelper()

    def tearDown(self):
        (self.h.shutil.which, self.h._systemd, self.h._server_running, self.h.subprocess.run,
         self.h.time.sleep, self.h.CONFIG_HOME, self.h.CONFIG_DIR, self.h._unit_active) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_system(self, help_text="--server-host  --config path"):
        """systemd present, OpenRGB installed; records commands; the server is up once systemd-run ran."""
        from types import SimpleNamespace
        ran, state = [], {"up": False}
        self.h.shutil.which = lambda n: "/usr/bin/" + n
        self.h._systemd = lambda: True
        self.h._server_running = lambda: state["up"]
        self.h.time.sleep = lambda s: None

        def fake_run(cmd, **kw):
            ran.append(cmd)
            if cmd[0] == "systemd-run":
                state["up"] = True
            if cmd[0] == "systemctl" and cmd[1] == "stop":
                state["up"] = False
            return SimpleNamespace(returncode=0, stderr="", stdout=help_text if cmd[-1] == "--help" else "")
        self.h.subprocess.run = fake_run
        return ran, state

    def unit(self, ran):
        return next(c for c in ran if c[0] == "systemd-run")

    def test_own_settings_folder_and_local_networks_with_server_host(self):
        ran, _state = self.fake_system()
        self.assertEqual(self.helper.ensure_server(), "")
        cmd = self.unit(ran)
        self.assertIn("IPAddressAllow=" + self.h.LOCAL_NETWORKS, cmd)
        self.assertEqual(cmd[cmd.index("--server-host") + 1], "127.0.0.1")       # the SDK stays local
        self.assertEqual(cmd[cmd.index("--config") + 1], self.h.CONFIG_DIR)
        self.assertIn(f"XDG_CONFIG_HOME={self.tmp}", cmd)
        self.assertTrue(os.path.isdir(self.h.CONFIG_DIR))

    def test_old_openrgb_without_server_host_gets_localhost_only(self):
        ran, _state = self.fake_system(help_text="--server  --config path")
        self.helper.ensure_server()
        cmd = self.unit(ran)
        self.assertIn("IPAddressAllow=localhost", cmd)
        self.assertNotIn("--server-host", cmd)
        self.assertIn("--config", cmd)

    def test_manual_devices_are_validated_and_written(self):
        ran, state = self.fake_system()
        self.h._unit_active = lambda: state["up"]
        self.helper.ensure_server()
        ran.clear()
        problem = self.helper.set_manual_devices([
            {"type": "E131Devices", "name": "Shelf", "ip": "192.168.1.60", "num_leds": 120},
            {"type": "LIFXDevices", "name": "Lamp", "ip": "lamp.local"},
            {"type": "E131Devices", "name": "Desk", "ip": "", "num_leds": 30, "start_universe": 2}])
        self.assertEqual(problem, "")
        with open(os.path.join(self.h.CONFIG_DIR, "OpenRGB.json")) as f:
            data = json.load(f)
        shelf, desk = data["E131Devices"]["devices"]
        self.assertEqual((shelf["type"], shelf["rgb_order"], shelf["num_leds"]), ("LINEAR", "RGB", 120))
        self.assertNotIn("ip", desk)                                    # empty optional address = multicast
        self.assertEqual(data["LIFXDevices"]["devices"], [{"name": "Lamp", "ip": "lamp.local"}])
        names = [c[:2] for c in ran]
        self.assertLess(names.index(["systemctl", "stop"]), names.index(["systemd-run", "--unit=lifaco-openrgb"]))
        listed = self.helper.manual_config()
        self.assertEqual([d["type"] for d in listed["devices"]], ["E131Devices", "E131Devices", "LIFXDevices"])
        self.assertTrue(listed["server_managed"])

    def test_other_settings_survive_and_removing_all_clears_the_keys(self):
        self.fake_system()
        self.h._unit_active = lambda: False
        os.makedirs(self.h.CONFIG_DIR)
        with open(os.path.join(self.h.CONFIG_DIR, "OpenRGB.json"), "w") as f:
            json.dump({"Detectors": {"x": True}, "GoveeDevices": {"devices": [{"ip": "10.0.0.3"}]}}, f)
        self.helper.set_manual_devices([])
        with open(os.path.join(self.h.CONFIG_DIR, "OpenRGB.json")) as f:
            self.assertEqual(json.load(f), {"Detectors": {"x": True}})

    def test_bad_entries_are_refused(self):
        clean = self.h.clean_manual_device
        for bad in ({"type": "Nope"}, {"type": "LIFXDevices", "ip": "a b"}, {"type": "LIFXDevices", "ip": ""},
                    {"type": "E131Devices", "num_leds": 0}, {"type": "E131Devices", "num_leds": "many"},
                    {"type": "LEDStripDevices", "port": "/etc/shadow"},
                    {"type": "LEDStripDevices", "port": "/dev/ttyUSB0", "protocol": "evil"}):
            with self.assertRaises(ValueError):
                clean(bad)
        strip = clean({"type": "LEDStripDevices", "port": "/dev/ttyACM0", "num_leds": 60})
        self.assertEqual((strip["baud"], strip["protocol"], strip["name"]), (115200, "adalight", "LED strip on a serial port"))
        with self.assertRaises(ValueError):
            self.helper.set_manual_devices([{"type": "LIFXDevices", "ip": "10.0.0.2"}] * 65)

    def test_starts_a_transient_local_only_root_unit(self):
        ran = []
        state = {"up": False}
        self.h.shutil.which = lambda n: "/usr/bin/" + n
        self.h._systemd = lambda: True
        self.h._server_running = lambda: state["up"]
        self.h.time.sleep = lambda s: None

        def fake_run(cmd, **kw):
            ran.append(cmd)
            state["up"] = True
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        self.h.subprocess.run = fake_run
        self.assertEqual(self.helper.ensure_server(), "")
        cmd = self.unit(ran)
        self.assertIn("--unit=lifaco-openrgb", cmd)
        self.assertIn("IPAddressAllow=localhost", cmd)                # --help lists no --server-host here
        self.assertIn("IPAddressDeny=any", cmd)
        self.assertEqual(cmd[-2:], ["/usr/bin/openrgb", "--server"])
        self.assertFalse(any("enable" in c for c in cmd))                         # nothing persistent

    def test_nothing_is_started_when_a_server_already_runs_or_openrgb_is_missing(self):
        ran = []
        self.h.subprocess.run = lambda cmd, **kw: ran.append(cmd)
        self.h._server_running = lambda: True
        self.assertEqual(self.helper.ensure_server(), "")
        self.h._server_running = lambda: False
        self.h.shutil.which = lambda n: None
        self.assertIn("not installed", self.helper.ensure_server())
        self.h.shutil.which = lambda n: "/usr/bin/openrgb"
        self.h._systemd = lambda: False
        self.assertIn("systemd", self.helper.ensure_server())
        self.assertEqual(ran, [])
