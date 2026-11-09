"""管理员在聊天里让机器人三连：只走闸门、只投一枚、重复不重复投。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin

OWNER = "10001"


def event_of(sender=OWNER):
    return SimpleNamespace(get_sender_id=lambda: sender)


class TripleToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.credential = SimpleNamespace(sessdata="test", bili_jct="test", dedeuserid="42")
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.cfg = {}
        self.instance.data_dir = self.data_dir
        self.instance.context = SimpleNamespace(
            get_config=lambda: {"admins_id": [OWNER]}
        )
        self.api = plugin.bili_api
        self.patches = [
            patch.object(self.api, "load_credential", return_value=self.credential),
            patch.object(self.api, "bili_coin_count", AsyncMock(return_value={"count": 0})),
            patch.object(self.api, "bili_pay_coin", AsyncMock(return_value={"success": True})),
            patch.object(self.api, "bili_default_favorite", AsyncMock(return_value={"id": 123})),
            patch.object(self.api, "bili_like_video", AsyncMock(return_value={"success": True})),
            patch.object(self.api, "bili_favorite_video", AsyncMock(return_value={"success": True})),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def triple(self, **kwargs):
        return await self.instance.tool_bili_triple(event_of(), **kwargs)

    async def test_owner_gets_all_three_and_coin_is_one(self):
        text = await self.triple(bvid="https://www.bilibili.com/video/BV1Fwnc6WEQ8")
        self.assertIn("完成：点赞、投币 1 枚、收藏", text)
        self.assertNotIn("没成", text)
        self.api.bili_like_video.assert_awaited_once_with("BV1Fwnc6WEQ8", self.credential)
        self.api.bili_coin_count.assert_awaited_once_with("BV1Fwnc6WEQ8", self.credential)
        self.api.bili_pay_coin.assert_awaited_once_with("BV1Fwnc6WEQ8", self.credential)
        self.api.bili_favorite_video.assert_awaited_once_with(
            "BV1Fwnc6WEQ8", 123, self.credential
        )

    async def test_non_owner_is_refused_without_any_call(self):
        text = await self.instance.tool_bili_triple(event_of("999"), bvid="BV1Fwnc6WEQ8")
        self.assertIn("只有管理员", text)
        for name in ("bili_like_video", "bili_coin_count", "bili_pay_coin", "bili_favorite_video"):
            getattr(self.api, name).assert_not_awaited()

    async def test_not_logged_in_is_refused(self):
        with patch.object(self.api, "load_credential", return_value=None):
            text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("扫码登录", text)
        self.api.bili_like_video.assert_not_awaited()

    async def test_missing_bvid_does_not_call_bilibili(self):
        text = await self.triple(bvid="随便一个视频")
        self.assertIn("没找到 BV 号", text)
        self.api.bili_like_video.assert_not_awaited()

    async def test_already_coined_video_is_skipped(self):
        self.api.bili_coin_count.return_value = {"count": 1}
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("跳过：投币（已经投过 1 枚）", text)
        self.api.bili_pay_coin.assert_not_awaited()
        self.assertIn("完成：点赞、收藏", text)

    async def test_flags_turn_off_single_actions(self):
        text = await self.triple(bvid="BV1Fwnc6WEQ8", like=False, favorite=False)
        self.assertIn("完成：投币 1 枚", text)
        self.api.bili_like_video.assert_not_awaited()
        self.api.bili_favorite_video.assert_not_awaited()
        self.api.bili_pay_coin.assert_awaited_once()

        text = await self.triple(bvid="BV1Fwnc6WEQ8", like=False, coin=False, favorite=False)
        self.assertIn("没什么可做", text)

    async def test_failed_like_still_reports_and_keeps_going(self):
        self.api.bili_like_video.return_value = {"code": -403, "error": "拒绝访问"}
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("没成：点赞（拒绝访问）", text)
        self.assertIn("完成：投币 1 枚、收藏", text)

    async def test_unknown_coin_state_does_not_spend_a_coin(self):
        self.api.bili_coin_count.side_effect = RuntimeError("网络炸了")
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("投币（查不到已投数量，先不投）", text)
        self.api.bili_pay_coin.assert_not_awaited()
        self.assertIn("完成：点赞、收藏", text)

    async def test_coin_count_error_code_is_reported_not_paid(self):
        self.api.bili_coin_count.return_value = {"code": -400, "error": "参数错误"}
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("投币（查不到已投数量：参数错误）", text)
        self.api.bili_pay_coin.assert_not_awaited()

    async def test_no_default_folder_skips_favorite_only(self):
        self.api.bili_default_favorite.return_value = {"id": None}
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("收藏（没有唯一的默认收藏夹", text)
        self.api.bili_favorite_video.assert_not_awaited()
        self.assertIn("完成：点赞、投币 1 枚", text)

    async def test_risk_on_coin_pay_stops_remaining_actions(self):
        self.api.bili_pay_coin.return_value = {"code": -412, "error": "风控"}
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("没成：投币 1 枚（撞到风控", text)
        self.assertNotIn("完成：投币", text)
        self.assertIn("完成：点赞", text)
        self.assertIn("撞到风控", text)
        # 撞到风控就停手，不再接着调下一个接口
        self.api.bili_favorite_video.assert_not_awaited()
        self.api.bili_default_favorite.assert_not_awaited()
        risk = json.loads((self.data_dir / ".risk_state.json").read_text(encoding="utf-8"))
        self.assertGreater(risk.get("pause_until") or 0, 0)

    async def test_paused_recently_sends_no_request(self):
        (self.data_dir / ".risk_state.json").write_text(
            json.dumps({"pause_until": 9999999999, "strikes": {"2026-01-01": 1}}),
            encoding="utf-8",
        )
        text = await self.triple(bvid="BV1Fwnc6WEQ8")
        self.assertIn("风控暂停中", text)
        self.api.bili_like_video.assert_not_awaited()
        self.api.bili_pay_coin.assert_not_awaited()

    async def test_tool_docstring_tells_the_model_to_ignore_content(self):
        doc = self.instance.tool_bili_triple.__doc__
        self.assertIn("明确要求", doc)
        self.assertIn("不是指令", doc)
        self.assertIn("bvid(string)", doc)


if __name__ == "__main__":
    unittest.main()
