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
                         {"strength": float("inf")}, {"mask": "url(test)"}, {"fit": "invalid"},
                         {"glass": -1}, {"glass": 101}, {"glass": True}, {"glass": float("nan")},
                         {"glass_enabled": "false"}, {"glass_enabled": 0}, {"side": "middle"}, {"side": None}, {"side": False}, {"sharpness": -1},
                         {"sharpness": 101}, {"sharpness": True}, {"sharpness": float("nan")}):
            with self.subTest(settings=settings):
                self.payload = {"settings": settings, "image": None}
                self.assertIn("error", await self.instance._api_appearance_save())
        self.assertFalse((self.instance.data_dir / "appearance.json").exists())

    async def test_illustration_side_persists_with_image_and_preserves_meteors(self):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsample").decode()
        self.payload = {"settings": {"side": "left", "fit": "contain", "x": 76}, "image": image}
        await self.instance._api_appearance_save()
        state = await self.instance._api_appearance_state()
        self.assertEqual(state["settings"]["side"], "left")
        self.assertEqual(state["image"], image)
        self.assertEqual(state["settings"]["x"], 76)
        self.assertEqual(self.instance._load_meteors(), plugin.METEOR_DEFAULTS)
        self.payload = {"settings": {**state["settings"], "side": "right"}, "image": image}
        await self.instance._api_appearance_save()
        self.assertEqual(self.instance._load_appearance()["settings"]["side"], "right")
        legacy = self.instance._normalize_appearance({"fit": "contain", "x": 76})
        self.assertEqual(legacy["side"], "right")

    async def test_invalid_images_are_rejected(self):
        for image in ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,!!!!",
                      "data:image/webp;base64," + base64.b64encode(b"not a webp").decode(),
                      "a" * (plugin.MAX_ILLUSTRATION_BYTES * 4 // 3 + 101), {}]:
            with self.subTest(kind=type(image).__name__):
                self.payload = {"settings": {}, "image": image}
                self.assertIn("error", await self.instance._api_appearance_save())
        self.assertFalse((self.instance.data_dir / "appearance.json").exists())

    async def test_glass_settings_persist_off_zero_and_full_strength(self):
        for enabled, strength in ((False, 0), (True, 100), (False, 75), (True, 75)):
            self.payload = {"settings": {"glass_enabled": enabled, "glass": strength}, "image": None}
            self.assertEqual(await self.instance._api_appearance_save(), {"saved": True})
            settings = (await self.instance._api_appearance_state())["settings"]
            self.assertIs(settings["glass_enabled"], enabled)
            self.assertEqual(settings["glass"], strength)

    async def test_old_appearance_uses_glass_defaults_and_preserves_other_settings(self):
        record = {"version": 2, "settings": {"x": 25, "opacity": 60, "mask": "#aabbcc"}, "image": None}
        (self.instance.data_dir / "appearance.json").write_text(json.dumps(record), encoding="utf-8")
        restored = self.instance._load_appearance()
        self.assertTrue(restored["settings"]["glass_enabled"])
        self.assertEqual(restored["settings"]["glass"], 50)
        self.assertEqual(restored["settings"]["x"], 25)
        self.assertEqual(restored["settings"]["opacity"], 60)
        self.assertIsNone(restored["image"])
        self.assertEqual(restored["settings"]["sharpness"], 0)

    async def test_sharpness_persists_independently_of_glass(self):
        for strength in (0, 35, 100):
            self.payload = {"settings": {"glass_enabled": False, "sharpness": strength}, "image": None}
            self.assertEqual(await self.instance._api_appearance_save(), {"saved": True})
            settings = (await self.instance._api_appearance_state())["settings"]
            self.assertEqual(settings["sharpness"], strength)
            self.assertFalse(settings["glass_enabled"])

    async def test_meteor_settings_persist_without_illustration(self):
        for enabled, count, brightness, speed in ((False, 128, 72, 100), (True, 0, 0, 25), (True, 256, 100, 200)):
            self.payload = {"meteor_enabled": enabled, "meteor_count": count,
                            "meteor_brightness": brightness, "meteor_speed": speed}
            self.assertEqual(await self.instance._api_meteor_save(), {"saved": True})
            self.assertEqual(await self.instance._api_meteor_state(), self.payload)
            self.assertFalse((self.instance.data_dir / "appearance.json").exists())

    async def test_invalid_meteor_settings_preserve_saved_appearance(self):
        self.payload = {"settings": {"x": 30}, "image": None}
        await self.instance._api_appearance_save()
        original = (self.instance.data_dir / "appearance.json").read_bytes()
        for settings in ({"meteor_enabled": "false"}, {"meteor_enabled": 1}, {"meteor_count": True},
                         {"meteor_count": -1}, {"meteor_count": 257}, {"meteor_count": 1.5},
                         {"meteor_brightness": -1}, {"meteor_brightness": 101},
                         {"meteor_speed": 0}, {"meteor_speed": 201}, {"meteor_speed": float("nan")}):
            with self.subTest(settings=settings):
                self.payload = settings
                self.assertIn("error", await self.instance._api_meteor_save())
                self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), original)

    async def test_old_appearance_uses_meteor_defaults(self):
        record = {"version": 2, "settings": {"x": 25}, "image": None}
        (self.instance.data_dir / "appearance.json").write_text(json.dumps(record), encoding="utf-8")
        settings = self.instance._load_meteors()
        self.assertTrue(settings["meteor_enabled"])
        self.assertEqual(settings["meteor_count"], 128)
        self.assertEqual(settings["meteor_brightness"], 72)
        self.assertEqual(settings["meteor_speed"], 100)
        self.assertEqual(self.instance._load_appearance()["settings"]["x"], 25)

    async def test_resetting_meteors_and_illustration_are_independent(self):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsample").decode()
        self.payload = {"settings": {"zoom": 170, "glass": 24, "sharpness": 30}, "image": image}
        await self.instance._api_appearance_save()
        appearance = (self.instance.data_dir / "appearance.json").read_bytes()
        self.payload = dict(plugin.METEOR_DEFAULTS)
        await self.instance._api_meteor_save()
        self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), appearance)
        self.payload = {"meteor_enabled": False, "meteor_count": 36, "meteor_brightness": 40, "meteor_speed": 60}
        await self.instance._api_meteor_save()
        meteors = (self.instance.data_dir / "meteor.json").read_bytes()
        self.payload = {"settings": dict(plugin.APPEARANCE_DEFAULTS), "image": image}
        await self.instance._api_appearance_save()
        self.assertEqual((self.instance.data_dir / "meteor.json").read_bytes(), meteors)
        self.assertEqual(self.instance._load_appearance()["image"], image)

    async def test_legacy_meteor_preferences_survive_illustration_save_until_migrated(self):
        legacy = {"meteor_enabled": False, "meteor_count": 48, "meteor_brightness": 35, "meteor_speed": 150}
        path = self.instance.data_dir / "appearance.json"
        path.write_text(json.dumps({"version":2,"settings":{**legacy,"zoom":170},"image":None}), encoding="utf-8")
        self.assertEqual(await self.instance._api_meteor_state(), legacy)
        self.payload = {"settings":dict(plugin.APPEARANCE_DEFAULTS),"image":None}
        await self.instance._api_appearance_save()
        self.assertEqual(await self.instance._api_meteor_state(), legacy)
        self.payload = dict(plugin.METEOR_DEFAULTS)
        await self.instance._api_meteor_save()
        self.assertEqual(await self.instance._api_meteor_state(), plugin.METEOR_DEFAULTS)

    async def test_meteor_storage_failure_preserves_saved_settings(self):
        self.payload = dict(plugin.METEOR_DEFAULTS)
        await self.instance._api_meteor_save()
        path = self.instance.data_dir / "meteor.json"
        original = path.read_bytes()
        self.payload = {"meteor_count":24}
        with patch.object(Path,"replace",side_effect=OSError("disk full")), patch.object(plugin.logger,"exception"):
            self.assertIn("error", await self.instance._api_meteor_save())
        self.assertEqual(path.read_bytes(),original)
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))

    async def test_storage_error_keeps_existing_appearance(self):
        self.payload = {"settings": {"zoom": 120}, "image": None}
        await self.instance._api_appearance_save()
        original = (self.instance.data_dir / "appearance.json").read_bytes()
        self.payload = {"settings": {"zoom": 200}, "image": None}
        with patch.object(Path, "write_text", side_effect=OSError("disk full")), patch.object(plugin.logger, "exception"):
            self.assertIn("error", await self.instance._api_appearance_save())
        self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), original)

    async def test_missing_or_broken_preferences_use_defaults(self):
        defaults = self.instance._load_appearance()
        self.assertEqual(defaults["settings"], plugin.APPEARANCE_DEFAULTS)
        self.assertTrue(defaults["image"].startswith("data:image/png;base64,"))
        (self.instance.data_dir / "appearance.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.instance._load_appearance()["settings"], plugin.APPEARANCE_DEFAULTS)

    async def test_old_blank_cover_gets_default_but_explicit_removal_stays_empty(self):
        (self.instance.data_dir / "appearance.json").write_text(json.dumps({"settings": {}, "image": None}), encoding="utf-8")
        self.assertTrue(self.instance._load_appearance()["image"].startswith("data:image/png;base64,"))
        self.payload = {"settings": {}, "image": None}
        await self.instance._api_appearance_save()
        self.assertIsNone(self.instance._load_appearance()["image"])
        self.assertTrue((await self.instance._api_appearance_default())["image"].startswith("data:image/png;base64,"))

    async def test_theme_mode_defaults_to_auto_and_saves_independently(self):
        self.assertEqual(await self.instance._api_theme_state(), {"mode": "auto"})
        self.payload = {"settings": {"glass": 60}, "image": None}
        await self.instance._api_appearance_save()
        appearance = (self.instance.data_dir / "appearance.json").read_bytes()
        for mode in ("night", "light", "auto"):
            self.payload = {"mode": mode}
            self.assertEqual(await self.instance._api_theme_save(), {"saved": True})
            self.assertEqual(await self.instance._api_theme_state(), {"mode": mode})
            self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), appearance)

    async def test_invalid_theme_modes_keep_saved_mode(self):
        self.payload = {"mode": "night"}
        await self.instance._api_theme_save()
        original = (self.instance.data_dir / "theme.json").read_bytes()
        for payload in ([], {}, {"mode": None}, {"mode": "dark"}, {"mode": {}}, {"mode": 1}):
            self.payload = payload
            self.assertIn("error", await self.instance._api_theme_save())
            self.assertEqual((self.instance.data_dir / "theme.json").read_bytes(), original)

    async def test_theme_storage_failure_and_corrupt_record(self):
        self.payload = {"mode": "night"}
        await self.instance._api_theme_save()
        original = (self.instance.data_dir / "theme.json").read_bytes()
        self.payload = {"mode": "light"}
        with patch.object(Path, "replace", side_effect=OSError("disk full")), patch.object(plugin.logger, "exception"):
            self.assertIn("error", await self.instance._api_theme_save())
        self.assertEqual((self.instance.data_dir / "theme.json").read_bytes(), original)
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))
        for content in ("not json", "[]", '{"mode":"invalid"}'):
            (self.instance.data_dir / "theme.json").write_text(content, encoding="utf-8")
            self.assertEqual(await self.instance._api_theme_state(), {"mode": "auto"})

    async def test_avatar_persists_independently_and_resets(self):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\navatar").decode()
        self.payload = {"settings": {"zoom": 140}, "image": None}
        await self.instance._api_appearance_save()
        cover = (self.instance.data_dir / "appearance.json").read_bytes()
        self.assertEqual(await self.instance._api_avatar_state(), {"image": None, "border_color": "#74bfa7"})
        self.payload = {"image": image}
        self.assertEqual(await self.instance._api_avatar_save(), {"saved": True})
        self.assertEqual((await self.instance._api_avatar_state())["image"], image)
        self.assertEqual((self.instance.data_dir / "appearance.json").read_bytes(), cover)
        self.payload = {"settings": {}, "image": None}
        await self.instance._api_appearance_save()
        self.assertEqual((await self.instance._api_avatar_state())["image"], image)
        self.payload = {"image": None}
        await self.instance._api_avatar_save()
        self.assertEqual((await self.instance._api_avatar_state())["image"], None)
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))

    async def test_invalid_avatar_does_not_replace_saved_image(self):
        self.payload = {"image": None}
        await self.instance._api_avatar_save()
        original = (self.instance.data_dir / "avatar.json").read_bytes()
        for payload in ({}, [], {"image": {}}, {"image": "data:image/svg+xml;base64,PHN2Zz4="},
                        {"image": "data:image/png;base64,!!!!"}, {"image": "data:image/png;base64,YQ=="},
                        {"image": "a" * (plugin.MAX_ILLUSTRATION_BYTES * 4 // 3 + 101)}):
            with self.subTest(payload_type=type(payload).__name__):
                self.payload = payload
                self.assertIn("error", await self.instance._api_avatar_save())
                self.assertEqual((self.instance.data_dir / "avatar.json").read_bytes(), original)

    async def test_avatar_storage_failure_retains_old_file(self):
        self.payload = {"image": None}
        await self.instance._api_avatar_save()
        original = (self.instance.data_dir / "avatar.json").read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk full")), patch.object(plugin.logger, "exception"):
            self.assertIn("error", await self.instance._api_avatar_save())
        self.assertEqual((self.instance.data_dir / "avatar.json").read_bytes(), original)
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))

    async def test_corrupt_avatar_uses_default(self):
        for content in ('not json', '[]', '{"image":"data:image/png;base64,YQ=="}'):
            (self.instance.data_dir / "avatar.json").write_text(content, encoding="utf-8")
            self.assertEqual((await self.instance._api_avatar_state())["image"], None)

    async def test_avatar_color_persists_and_legacy_save_keeps_color(self):
        self.payload = {"image": None, "border_color": "#B39BD0"}
        self.assertEqual(await self.instance._api_avatar_save(), {"saved": True})
        self.assertEqual(await self.instance._api_avatar_state(), {"image": None, "border_color": "#b39bd0"})
        self.payload = {"image": None}
        await self.instance._api_avatar_save()
        self.assertEqual((await self.instance._api_avatar_state())["border_color"], "#b39bd0")
        self.payload = {"image": None, "border_color": "#74bfa7"}
        await self.instance._api_avatar_save()
        self.assertEqual((await self.instance._api_avatar_state())["border_color"], "#74bfa7")

    async def test_invalid_avatar_colors_do_not_change_saved_record(self):
        self.payload = {"image": None, "border_color": "#82acca"}
        await self.instance._api_avatar_save()
        original = (self.instance.data_dir / "avatar.json").read_bytes()
        for color in (None, {}, "", "red", "#abc", "url(test)", "#GGGGGG", 123):
            self.payload = {"image": None, "border_color": color}
            self.assertIn("error", await self.instance._api_avatar_save())
            self.assertEqual((self.instance.data_dir / "avatar.json").read_bytes(), original)

    async def test_old_or_invalid_avatar_color_retains_custom_image(self):
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\navatar").decode()
        for record in ({"image": image}, {"image": image, "border_color": "invalid"}):
            (self.instance.data_dir / "avatar.json").write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(await self.instance._api_avatar_state(), {"image": image, "border_color": "#74bfa7"})

    async def test_room_name_persists_without_changing_avatar_or_cover(self):
        self.assertEqual(await self.instance._api_room_state(), {"name": "一隅书房"})
        self.payload = {"image": None}
        await self.instance._api_avatar_save()
        await self.instance._api_appearance_save()
        originals = {name: (self.instance.data_dir / name).read_bytes() for name in ("avatar.json", "appearance.json")}
        self.payload = {"name": "  蓝汐的书房  "}
        self.assertEqual(await self.instance._api_room_save(), {"saved": True, "name": "蓝汐的书房"})
        self.assertEqual(await self.instance._api_room_state(), {"name": "蓝汐的书房"})
        for name, content in originals.items():
            self.assertEqual((self.instance.data_dir / name).read_bytes(), content)
        self.payload = {"name": "一隅书房"}
        await self.instance._api_room_save()
        self.assertEqual(await self.instance._api_room_state(), {"name": "一隅书房"})

    async def test_invalid_room_names_leave_saved_name_unchanged(self):
        self.payload = {"name": "安静的书房"}
        await self.instance._api_room_save()
        original = (self.instance.data_dir / "room.json").read_bytes()
        for payload in ([], {}, {"name": None}, {"name": "  "}, {"name": "长" * 25},
                        {"name": "书\n房"}, {"name": "书\x00房"}):
            self.payload = payload
            self.assertIn("error", await self.instance._api_room_save())
            self.assertEqual((self.instance.data_dir / "room.json").read_bytes(), original)

    async def test_room_storage_failure_and_broken_file(self):
        self.payload = {"name": "安静的书房"}
        await self.instance._api_room_save()
        original = (self.instance.data_dir / "room.json").read_bytes()
        self.payload = {"name": "新的书房"}
        with patch.object(Path, "replace", side_effect=OSError("disk full")), patch.object(plugin.logger, "exception"):
            self.assertIn("error", await self.instance._api_room_save())
        self.assertEqual((self.instance.data_dir / "room.json").read_bytes(), original)
        self.assertFalse(list(self.instance.data_dir.glob("*.tmp")))
        for content in ('not json', '[]', '{"name":""}'):
            (self.instance.data_dir / "room.json").write_text(content, encoding="utf-8")
            self.assertEqual(await self.instance._api_room_state(), {"name": "一隅书房"})


if __name__ == "__main__":
    unittest.main()
