"""Comment participation modes with a fake model and a fake Bilibili client."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin


class CommentTasteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(self.directory.name)
        self.instance.cfg = {
            "provider_id": "test-model", "speaking_style_bind_persona": False,
            "speaking_style": "简洁直接", "comment_taste": "只评小猫，不评纯音乐",
            "comment_eagerness": "保守", "auto_comment": True,
        }
        self.generate = AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({
            "want": True, "text": "这段钢琴从轻到重的变化，确实把情绪接住了", "why": "回应具体细节",
        }, ensure_ascii=False)))
        self.instance.context = SimpleNamespace(llm_generate=self.generate)

    def test_legacy_defaults_to_filter_and_rejects_non_boolean_switch(self):
        self.assertTrue(self.instance._config_values()["comment_taste_enabled"])
        for value in ("false", "true", 0, 1, None):
            with self.subTest(value=value):
                patch_value, errors = self.instance._coerce_config({"comment_taste_enabled": value})
                self.assertTrue(errors)
                self.assertNotIn("comment_taste_enabled", patch_value)
        self.assertEqual(self.instance._coerce_config({"comment_taste_enabled": False}),
                         ({"comment_taste_enabled": False}, []))

    async def test_save_and_reload_keep_text_and_other_sections(self):
        class Config(dict):
            async def save_config_async(config, values):
                config.update(values)

        self.instance.cfg = Config(self.instance.cfg)
        original = dict(self.instance.cfg)
        request = SimpleNamespace(json=AsyncMock(return_value={"comment_taste_enabled": False}))
        with patch.object(plugin, "request", request), patch.object(plugin, "json_response", lambda x: x):
            saved = await self.instance._api_config_save()
            self.assertTrue(saved["saved"])
            self.assertFalse(saved["values"]["comment_taste_enabled"])
            state = await self.instance._api_comments_state()
            self.assertFalse(state["taste_enabled"])
            self.assertEqual(state["taste"], original["comment_taste"])
            self.assertEqual(self.instance.cfg["comment_taste"], original["comment_taste"])
            self.assertEqual({k: self.instance.cfg[k] for k in original}, original)
            reloaded = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
            reloaded.cfg = dict(self.instance.cfg)
            self.assertFalse(reloaded._config_values()["comment_taste_enabled"])

    async def test_enabled_uses_saved_taste_and_eagerness(self):
        await self.instance._write_comment("小猫把玩具推到了桌边", "小猫")
        prompt = self.generate.call_args.kwargs["system_prompt"]
        self.assertIn(self.instance.cfg["comment_taste"], prompt)
        self.assertIn(plugin.EAGERNESS_RULES["保守"], prompt)
        self.assertIn("简洁直接", prompt)
        self.instance.cfg["comment_taste"] = ""
        await self.instance._write_comment("小猫在玩玩具", "小猫")
        self.assertIn(plugin.DEFAULT_COMMENT_TASTE, self.generate.call_args.kwargs["system_prompt"])

    async def test_disabled_does_not_read_taste_or_eagerness_in_comments_or_replies(self):
        class NoTasteRead(dict):
            def get(config, key, default=None):
                if key in ("comment_taste", "comment_eagerness"):
                    raise AssertionError("Disabled mode read a preference: " + key)
                return super().get(key, default)

        self.instance.cfg = NoTasteRead(self.instance.cfg, comment_taste_enabled=False)
        await self.instance._write_comment("钢琴从轻奏转成重奏", "钢琴演奏")
        prompt = self.generate.call_args.kwargs["system_prompt"]
        self.assertIn("参与方式：逐条评论", prompt)
        self.assertNotIn(plugin.DEFAULT_COMMENT_TASTE, prompt)
        self.assertNotIn("{{", prompt)
        await self.instance._write_reply({"their_words": "后半段是不是变调了？"})
        self.assertNotIn("只评小猫", self.generate.call_args.kwargs["system_prompt"])
        self.assertIn("不使用个人内容偏好筛选", self.generate.call_args.kwargs["system_prompt"])

    async def test_unfiltered_pipeline_comments_each_video_and_still_respects_cap(self):
        duty = __import__(plugin.__package__ + ".duty", fromlist=["DutyRunner"])
        self.instance.cfg["comment_taste_enabled"] = False
        sent = AsyncMock(return_value=json.dumps({"success": True, "rpid": 123}))
        runner = duty.DutyRunner(bili=SimpleNamespace(bili_reply=sent), config=self.instance.cfg,
                                 data_dir=self.instance.data_dir, judge=AsyncMock(), log=lambda _: None,
                                 commenter=self.instance._write_comment)
        videos = [{"bvid": "BV1aaaaaaaaa", "title": "钢琴演奏"},
                  {"bvid": "BV1bbbbbbbbb", "title": "游戏实况"}]
        sources = {p["bvid"]: "字幕包含具体内容" for p in videos}
        with patch.object(duty.asyncio, "sleep", new=AsyncMock()):
            result = await runner._auto_comment(videos, sources)
            self.assertEqual(len(result["posted"]), 2)
            self.assertEqual(sent.await_count, 2)
            repeated = await runner._auto_comment(videos, sources)
            self.assertEqual(repeated["posted"], [])
            runner.cfg["auto_comment_per_day"] = 2
            capped = await runner._auto_comment([{"bvid": "BV1ccccccccc"}], {"BV1ccccccccc": "内容"})
            self.assertIn("上限", capped["skipped"])

    async def test_unfiltered_mode_does_not_force_invalid_model_output_to_send(self):
        duty = __import__(plugin.__package__ + ".duty", fromlist=["DutyRunner"])
        self.instance.cfg["comment_taste_enabled"] = False
        sent = AsyncMock()
        runner = duty.DutyRunner(bili=SimpleNamespace(bili_reply=sent), config=self.instance.cfg,
                                 data_dir=self.instance.data_dir, judge=AsyncMock(), log=lambda _: None,
                                 commenter=self.instance._write_comment)
        for output in ("bad json", '{"want":false,"why":"素材不足","text":""}',
                       '{"want":true,"text":"加我微信拿链接，点击这里看看新的活动"}'):
            self.generate.return_value = SimpleNamespace(completion_text=output)
            result = await runner._auto_comment([{"bvid": "BV1aaaaaaaaa"}], {"BV1aaaaaaaaa": "字幕"})
            self.assertEqual(result["posted"], [])
        sent.assert_not_awaited()
