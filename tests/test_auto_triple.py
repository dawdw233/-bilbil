"""Exercise the real browse flow with a fake model and Bilibili client."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin


class AutoTripleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.credential = SimpleNamespace(sessdata="test", bili_jct="test", dedeuserid="42")
        self.bili = SimpleNamespace(
            load_credential=lambda: self.credential,
            bili_search=AsyncMock(),
            bili_subtitle=AsyncMock(side_effect=lambda bvid: {"text": "" if bvid == "fallback" else bvid}),
            bili_comments=AsyncMock(return_value={"comments": []}),
            bili_coin_count=AsyncMock(return_value={"count": 0}),
            bili_pay_coin=AsyncMock(return_value={"success": True}),
            bili_default_favorite=AsyncMock(return_value={"id": 123}),
            bili_like_video=AsyncMock(return_value={"success": True}),
            bili_favorite_video=AsyncMock(return_value={"success": True}),
        )
        self.verdicts = {}

    async def run_browse(self, videos, config=None):
        self.bili.bili_search.return_value = {
            "videos": [{"bvid": name, "title": name, "description": "只有简介"} for name in videos]
        }
        judge = AsyncMock(side_effect=lambda text, title: self.verdicts.get(
            title, {"verdict": "值得看", "want_triple": True, "reason": "喜欢剪辑的反差"}
        ))
        runner = plugin.DutyRunner(
            bili=self.bili, config={"keywords": ["整活"], **(config or {})},
            data_dir=self.data_dir, judge=judge, log=lambda _: None,
        )
        return await runner.run()

    async def test_default_off_even_when_model_likes_a_video(self):
        result = await self.run_browse(["favorite"], {"auto_coin": True})
        self.assertEqual(len(result["picked"]), 1)
        self.assertNotIn("三连", result["report_md"])
        self.bili.bili_coin_count.assert_not_awaited()
        self.bili.bili_pay_coin.assert_not_awaited()
        self.assertFalse((self.data_dir / "coin_attempts.json").exists())

    async def test_only_liked_videos_with_subtitles_can_receive_a_triple(self):
        self.verdicts = {
            "ordinary": {"verdict": "可以看", "want_triple": True, "reason": "有一点意思"},
            "ambiguous": {"verdict": "值得看", "want_triple": "true", "reason": "不明确"},
            "declined": {"verdict": "值得看", "want_triple": False, "reason": "有质量但不合兴趣"},
            "no_reason": {"verdict": "值得看", "want_triple": True, "reason": ""},
        }
        result = await self.run_browse(
            ["ordinary", "ambiguous", "declined", "no_reason", "fallback", "favorite"],
            {"auto_triple": True, "daily_count": 6},
        )
        self.bili.bili_coin_count.assert_awaited_once_with("favorite", self.credential)
        self.bili.bili_pay_coin.assert_awaited_once_with("favorite", self.credential)
        self.bili.bili_like_video.assert_awaited_once_with("favorite", self.credential)
        self.bili.bili_favorite_video.assert_awaited_once_with("favorite", 123, self.credential)
        self.assertEqual(result["report_md"].count("已三连：点赞、投币 1 枚、收藏"), 1)

    async def test_existing_coin_is_skipped_and_daily_limit_survives_another_run(self):
        self.bili.bili_coin_count.side_effect = [{"count": 1}, {"count": 0}]
        await self.run_browse(["already_coined", "first"], {"auto_triple": True})
        result = await self.run_browse(["second"], {"auto_triple": True})
        self.bili.bili_pay_coin.assert_awaited_once_with("first", self.credential)
        self.assertEqual(self.bili.bili_coin_count.await_count, 2)
        self.assertIn("上限", result["report_md"])

    async def test_uncertain_send_keeps_reservation_if_browse_history_is_lost(self):
        self.bili.bili_pay_coin.side_effect = TimeoutError
        config = {"auto_triple": True, "auto_coin_per_day": 2}
        first = await self.run_browse(["uncertain"], config)
        self.assertIn("投币结果未确认", first["report_md"])
        (self.data_dir / "seen.json").unlink()
        self.bili.bili_pay_coin.side_effect = None
        second = await self.run_browse(["uncertain", "new"], config)
        self.assertEqual([call.args[0] for call in self.bili.bili_pay_coin.await_args_list], ["uncertain", "new"])
        self.assertEqual(second["report_md"].count("已三连：点赞、投币 1 枚、收藏"), 1)

    async def test_risk_response_prevents_spending(self):
        self.bili.bili_coin_count.return_value = {"code": -412, "error": "风控"}
        result = await self.run_browse(["first", "second"], {"auto_triple": True})
        self.bili.bili_coin_count.assert_awaited_once()
        self.bili.bili_pay_coin.assert_not_awaited()
        self.assertIn("风控", result["report_md"])
        self.assertFalse((self.data_dir / "coin_attempts.json").exists())

    async def test_missing_default_folder_prevents_spending(self):
        self.bili.bili_default_favorite.return_value = {"id": None}
        result = await self.run_browse(["favorite"], {"auto_triple": True})
        self.bili.bili_pay_coin.assert_not_awaited()
        self.assertIn("未能确认默认收藏夹", result["report_md"])
        self.assertFalse((self.data_dir / "coin_attempts.json").exists())

    async def test_partial_success_is_reported_without_spending_twice(self):
        self.bili.bili_favorite_video.return_value = {"code": -403}
        config = {"auto_triple": True, "auto_coin_per_day": 2}
        result = await self.run_browse(["favorite"], config)
        self.assertIn("三连未完成，已确认：投币 1 枚、点赞", result["report_md"])
        self.assertNotIn("已三连", result["report_md"])
        (self.data_dir / "seen.json").unlink()
        await self.run_browse(["favorite"], config)
        self.bili.bili_pay_coin.assert_awaited_once()

    async def test_sdk_selects_the_default_folder_and_preserves_existing_actions(self):
        folders = [{"id": 12, "attr": 22}, {"id": 34, "attr": 1}]
        list_folders = AsyncMock(return_value={"list": folders})
        api = plugin.bili_api
        with patch.object(api.favorite_list, "get_video_favorite_list", list_folders):
            self.assertEqual(await api.bili_default_favorite(self.credential), {"id": 34})
        list_folders.assert_awaited_once_with(42, credential=self.credential)

        video = SimpleNamespace(
            has_liked=AsyncMock(side_effect=[0, 1]), like=AsyncMock(),
            has_favoured=AsyncMock(side_effect=[False, True]), set_favorite=AsyncMock(),
        )
        with patch.object(api.video, "Video", return_value=video):
            for _ in range(2):
                self.assertEqual(await api.bili_like_video("favorite", self.credential), {"success": True})
                self.assertEqual(await api.bili_favorite_video("favorite", 34, self.credential), {"success": True})
        video.like.assert_awaited_once_with(True)
        video.set_favorite.assert_awaited_once_with(add_media_ids=[34])

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
        self.assertFalse(instance._config_values()["auto_triple"])
        await instance._judge("有字幕内容", "整活视频")
        self.assertNotIn("want_triple", generate.call_args.kwargs["system_prompt"])
        patch_values, errors = instance._coerce_config({"auto_triple": True, "auto_coin_per_day": 2})
        self.assertFalse(errors)
        instance.cfg.update(patch_values)
        await instance._judge("有字幕内容", "整活视频")
        self.assertIn("want_triple", generate.call_args.kwargs["system_prompt"])
        self.assertIn("整活", generate.call_args.kwargs["system_prompt"])
        self.assertEqual(instance._config_values()["auto_coin_per_day"], 2)
        for payload in ({"auto_triple": "false"}, {"auto_coin_per_day": 1.5}, {"auto_coin_per_day": 6}):
            self.assertTrue(instance._coerce_config(payload)[1])
