#!/usr/bin/env python3
"""Tests of install.py's hook merge, on temporary settings files only."""
import importlib.util, json, tempfile, unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("install", Path(__file__).resolve().parent.parent / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class MergeHooks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def heartbeat_count(self, data):
        return sum("guard_ram.py heartbeat" in h["command"]
                   for entries in data["hooks"].values() for e in entries for h in e["hooks"])

    def test_keeps_existing_settings_and_hooks(self):
        other = {"type": "command", "command": "python3 other-hook.py"}
        self.settings.write_text(json.dumps({"theme": "dark", "hooks": {
            "PostToolUse": [{"matcher": "Bash", "hooks": [other]}]}}))
        install.merge_hooks(self.settings)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertIn(other, data["hooks"]["PostToolUse"][0]["hooks"])
        self.assertEqual(self.heartbeat_count(data), 3)
        self.assertTrue(list(Path(self.dir.name).glob("settings.json.bak-*")), "the previous file is kept")

    def test_running_twice_adds_nothing(self):
        install.merge_hooks(self.settings)
        install.merge_hooks(self.settings)
        self.assertEqual(self.heartbeat_count(json.loads(self.settings.read_text())), 3)

    def test_invalid_json_left_untouched(self):
        self.settings.write_text("{ not json")
        with self.assertRaises(SystemExit):
            install.merge_hooks(self.settings)
        self.assertEqual(self.settings.read_text(), "{ not json")


if __name__ == "__main__":
    unittest.main()
