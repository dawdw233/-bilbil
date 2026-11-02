"""Use the real plugin methods in an AstrBot environment; no account actions."""
import base64
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "bili_appearance_under_test"
spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = package
spec.loader.exec_module(package)
plugin = importlib.import_module(PACKAGE + ".main")


class AppearanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(self.directory.name)
        self.payload = {}
        test = self

        class Request:
            async def json(self, default=None):
                return test.payload

        self.patches = [patch.object(plugin, "request", Request()),
                        patch.object(plugin, "json_response", lambda value: value),
                        patch.object(plugin, "error_response", lambda message: {"error": message})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_save_restore_and_remove_illustration(self):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsample").decode()
        self.payload = {"settings": {"x": 0, "opacity": 0, "strength": 0, "zoom": 150, "mask": "#ABCDEF"}, "image": image}
        self.assertEqual(await self.instance._api_appearance_save(), {"saved": True})
        restored = await self.instance._api_appearance_state()
        self.assertEqual(restored["image"], image)
        self.assertEqual(restored["settings"]["x"], 0)
        self.assertEqual(restored["settings"]["opacity"], 0)
        self.assertEqual(restored["settings"]["mask"], "#abcdef")
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))
        self.payload = {"settings": restored["settings"], "image": None}
        await self.instance._api_appearance_save()
        self.assertIsNone(self.instance._load_appearance()["image"])
        self.assertEqual(self.instance._load_appearance()["settings"]["zoom"], 150)

    async def test_invalid_settings_are_not_written(self):
        for settings in ({"zoom": 99}, {"opacity": 101}, {"x": True}, {"y": float("nan")},
                         {"strength": float("inf")}, {"mask": "url(test)"}, {"fit": "invalid"}):
            with self.subTest(settings=settings):
                self.payload = {"settings": settings, "image": None}
                self.assertIn("error", await self.instance._api_appearance_save())
        self.assertFalse((self.instance.data_dir / "appearance.json").exists())

    async def test_invalid_images_are_rejected(self):
        for image in ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,!!!!",
                      "data:image/webp;base64," + base64.b64encode(b"not a webp").decode(),
                      "a" * (plugin.MAX_ILLUSTRATION_BYTES * 4 // 3 + 101), {}]:
            with self.subTest(kind=type(image).__name__):
                self.payload = {"settings": {}, "image": image}
                self.assertIn("error", await self.instance._api_appearance_save())
        self.assertFalse((self.instance.data_dir / "appearance.json").exists())

    async def test_storage_error_keeps_existing_appearance(self):
        self.payload = {"settings": {"zoom": 120}, "image": None}
        await self.instance._api_appearance_save()
        original = (self.instance.data_dir / "appearance.json").read_bytes()
        self.payload = {"settings": {"zoom": 200}, "image": None}
        with patch.object(Path, "write_text", side_effect=OSError("disk full")), patch.object(plugin.logger, "exception"):
            self.assertIn("error", await self.instance._api_appearance_save())
        self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), original)

    async def test_missing_or_broken_preferences_use_defaults(self):
        self.assertEqual(self.instance._load_appearance(), {"settings": plugin.APPEARANCE_DEFAULTS, "image": None})
        (self.instance.data_dir / "appearance.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.instance._load_appearance()["settings"], plugin.APPEARANCE_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
