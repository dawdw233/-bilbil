"""Exercise the real browse flow with a fake model and Bilibili client."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin


class AutoCoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.credential = SimpleNamespace(sessdata="test", bili_jct="test")
        self.bili = SimpleNamespace(
            load_credential=lambda: self.credential,
            bili_search=AsyncMock(),
            bili_subtitle=AsyncMock(side_effect=lambda bvid: {"text": "" if bvid == "fallback" else bvid}),
            bili_comments=AsyncMock(return_value={"comments": []}),
            bili_coin_count=AsyncMock(return_value={"count": 0}),
            bili_pay_coin=AsyncMock(return_value={"success": True}),
        )
        self.verdicts = {}

    async def run_browse(self, videos, config=None):
        self.bili.bili_search.return_value = {
            "videos": [{"bvid": name, "title": name, "description": "只有简介"} for name in videos]
        }
        judge = AsyncMock(side_effect=lambda text, title: self.verdicts.get(
            title, {"verdict": "值得看", "want_coin": True, "reason": "喜欢剪辑的反差"}
        ))
        runner = plugin.DutyRunner(
            bili=self.bili, config={"keywords": ["整活"], **(config or {})},
            data_dir=self.data_dir, judge=judge, log=lambda _: None,
        )
        return await runner.run()

    async def test_default_off_even_when_model_likes_a_video(self):
        result = await self.run_browse(["favorite"])
        self.assertEqual(len(result["picked"]), 1)
        self.assertNotIn("投币", result["report_md"])
        self.bili.bili_coin_count.assert_not_awaited()
        self.bili.bili_pay_coin.assert_not_awaited()
        self.assertFalse((self.data_dir / "coin_attempts.json").exists())

    async def test_only_liked_videos_with_subtitles_can_receive_a_coin(self):
        self.verdicts = {
            "ordinary": {"verdict": "可以看", "want_coin": True, "reason": "有一点意思"},
            "ambiguous": {"verdict": "值得看", "want_coin": "true", "reason": "不明确"},
            "declined": {"verdict": "值得看", "want_coin": False, "reason": "有质量但不合兴趣"},
            "no_reason": {"verdict": "值得看", "want_coin": True, "reason": ""},
        }
        result = await self.run_browse(
            ["ordinary", "ambiguous", "declined", "no_reason", "fallback", "favorite"],
            {"auto_coin": True, "daily_count": 6},
        )
        self.bili.bili_coin_count.assert_awaited_once_with("favorite", self.credential)
        self.bili.bili_pay_coin.assert_awaited_once_with("favorite", self.credential)
        self.assertEqual(result["report_md"].count("已投币：1 枚"), 1)

    async def test_existing_coin_is_skipped_and_daily_limit_survives_another_run(self):
        self.bili.bili_coin_count.side_effect = [{"count": 1}, {"count": 0}]
        await self.run_browse(["already_coined", "first"], {"auto_coin": True})
        result = await self.run_browse(["second"], {"auto_coin": True})
        self.bili.bili_pay_coin.assert_awaited_once_with("first", self.credential)
        self.assertEqual(self.bili.bili_coin_count.await_count, 2)
        self.assertIn("上限", result["report_md"])

    async def test_uncertain_send_keeps_reservation_if_browse_history_is_lost(self):
        self.bili.bili_pay_coin.side_effect = TimeoutError
        config = {"auto_coin": True, "auto_coin_per_day": 2}
        first = await self.run_browse(["uncertain"], config)
        self.assertIn("投币结果未确认", first["report_md"])
        (self.data_dir / "seen.json").unlink()
        self.bili.bili_pay_coin.side_effect = None
        second = await self.run_browse(["uncertain", "new"], config)
        self.assertEqual([call.args[0] for call in self.bili.bili_pay_coin.await_args_list], ["uncertain", "new"])
        self.assertEqual(second["report_md"].count("已投币：1 枚"), 1)

    async def test_risk_response_prevents_spending(self):
        self.bili.bili_coin_count.return_value = {"code": -412, "error": "风控"}
        result = await self.run_browse(["first", "second"], {"auto_coin": True})
        self.bili.bili_coin_count.assert_awaited_once()
        self.bili.bili_pay_coin.assert_not_awaited()
        self.assertIn("风控", result["report_md"])
        self.assertFalse((self.data_dir / "coin_attempts.json").exists())

    async def test_sdk_submits_only_one_coin_without_liking(self):
        video = SimpleNamespace(pay_coin=AsyncMock())
        with patch.object(plugin.bili_api.video, "Video", return_value=video) as create:
            result = await plugin.bili_api.bili_pay_coin("favorite", self.credential)
        create.assert_called_once_with(bvid="favorite", credential=self.credential)
        video.pay_coin.assert_awaited_once_with(num=1, like=False)
        self.assertEqual(result, {"success": True})

    async def test_settings_and_prompt_require_explicit_opt_in(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance.cfg = {"keywords": ["整活"], "provider_id": "test-model"}
        generate = AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        instance.context = SimpleNamespace(llm_generate=generate)
        self.assertFalse(instance._config_values()["auto_coin"])
        await instance._judge("有字幕内容", "整活视频")
        self.assertNotIn("want_coin", generate.call_args.kwargs["system_prompt"])
        patch_values, errors = instance._coerce_config({"auto_coin": True, "auto_coin_per_day": 2})
        self.assertFalse(errors)
        instance.cfg.update(patch_values)
        await instance._judge("有字幕内容", "整活视频")
        self.assertIn("want_coin", generate.call_args.kwargs["system_prompt"])
        self.assertIn("整活", generate.call_args.kwargs["system_prompt"])
        self.assertEqual(instance._config_values()["auto_coin_per_day"], 2)
        for payload in ({"auto_coin": "false"}, {"auto_coin_per_day": 1.5}, {"auto_coin_per_day": 6}):
            self.assertTrue(instance._coerce_config(payload)[1])
