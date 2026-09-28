import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from fancontrol_linux.gui import theme
    from fancontrol_linux.gui.util import GuiPrefs
except (ImportError, ValueError):  # no GTK 4 / libadwaita available
    GuiPrefs = None


@unittest.skipIf(GuiPrefs is None, "GTK 4 / libadwaita not available")
class PrefsTests(unittest.TestCase):
    def _prefs(self, stored):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "fancontrol-linux"))
        if stored is not None:
            with open(os.path.join(tmp.name, "fancontrol-linux", "gui.json"), "w") as f:
                json.dump(stored, f)
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        try:
            return GuiPrefs()
        finally:
            if old is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    def test_new_installation_uses_midnight(self):
        self.assertEqual(theme.palette(self._prefs(None))["name"], theme.PALETTES["midnight_aurora"]["name"])

    def test_old_saved_default_is_migrated(self):
        prefs = self._prefs({"palette": "classic", "setup_done": True})
        self.assertIsNone(prefs.get("palette"))
        self.assertTrue(prefs.get("setup_done"))

    def test_explicit_choice_is_kept(self):
        self.assertEqual(self._prefs({"palette": "classic", "prefs_version": 2}).get("palette"), "classic")
        self.assertEqual(self._prefs({"palette": "ocean"}).get("palette"), "ocean")

    def test_custom_gradient(self):
        prefs = self._prefs({"palette": "custom", "prefs_version": 2,
                             "custom_colors": {"accent": "#ff0000", "accent2": "#0000ff"}})
        self.assertEqual(theme.palette(prefs)["accent"], ["#ff0000", "#0000ff"])


if __name__ == "__main__":
    unittest.main()
