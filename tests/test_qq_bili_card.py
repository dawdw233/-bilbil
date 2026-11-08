"""Use actual AstrBot message components; all video reads and QQ delivery are mocked."""
import json
import unittest
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Json, Plain, Share
from astrbot.core.provider.entities import ProviderRequest
from test_appearance import plugin

card = import_module(plugin.__package__ + ".qq_bili_card")
BV = "BV1234567890"
URL = "https://www.bilibili.com/video/" + BV
SHORT = "https://b23.tv/abc123"
INFO = {"bvid": BV, "title": "真实视频标题", "description": "关于角色吃饭的创作演示", "author": "测试UP主"}


def miniapp(url=URL):
    return {"app": "com.tencent.miniapp_01", "meta": {"detail_1": {"qqdocurl": url, "desc": "卡片标题不作为事实"}}}


def event(components, private=True, at=False, platform="aiocqhttp", message_id="message-1"):
    extras = {}
    e = SimpleNamespace(message_str="", message_obj=SimpleNamespace(message_id=message_id, message=components),
                        unified_msg_origin="test:FriendMessage:test", is_at_or_wake_command=at, call_llm=False)
    e.get_messages = lambda: components
    e.get_platform_name = lambda: platform
    e.is_private_chat = lambda: private
    e.get_sender_id = lambda: "test-user"
    e.set_extra = lambda key, value: extras.update({key: value})
    e.get_extra = lambda key, default=None: extras.get(key, default)
    e.should_call_llm = lambda value: setattr(e, "call_llm", value)
    e.plain_result = lambda text: text
    return e


class QQCardTests(unittest.IsolatedAsyncioTestCase):
    def test_json_miniapp_structured_share_cq_and_plain_links(self):
        examples = [Json(data=miniapp()), Json(data=json.dumps(miniapp(SHORT))),
                    Json(data={"app": "com.tencent.structmsg", "meta": {"news": {"jumpUrl": URL}}}),
                    Share(url=URL, title="分享"), Plain("看看 " + URL + "?share_source=QQ"),
                    {"type": "json", "data": {"data": json.dumps(miniapp())}},
                    {"type": "xml", "data": {"data": '<msg url="' + URL + '?a=1&amp;b=2"/>'}}]
        for component in examples:
            with self.subTest(component=type(component).__name__):
                self.assertIn(card.share_url(event([component])), (URL, SHORT))
        raw = json.dumps(miniapp()).replace("&", "&amp;").replace(",", "&#44;").replace("[", "&#91;").replace("]", "&#93;")
        e = event([]); e.message_str = "[CQ:json,data=" + raw + "]"
        self.assertEqual(card.share_url(e), URL)

    def test_non_bili_cards_quotes_forwarded_payloads_and_unsafe_urls_are_ignored(self):
        for value in ("https://www.bilibili.com.evil.test/video/" + BV,
                      "https://u:p@www.bilibili.com/video/" + BV,
                      "http://127.0.0.1/video/" + BV,
                      "https://www.bilibili.com/space/123", "https://b23.tv:8080/abc123"):
            self.assertEqual(card.video_url(value), "")
            self.assertEqual(card.share_url(event([Json(data=miniapp(value))])), "")
        self.assertEqual(card.share_url(event([{"type": "reply", "data": {"message": [miniapp()]}}])), "")
        self.assertEqual(card.share_url(event([{"type": "forward", "data": {"content": [miniapp()]}}])), "")
        self.assertEqual(card.raw_url('<!DOCTYPE a [<!ENTITY x "evil">]><msg url="'+URL+'"/>'), "")
        self.assertEqual(card.raw_url("x" * 20000 + URL), "")

    def test_filter_does_not_wake_unrelated_messages_or_unmentioned_groups(self):
        handler = card.QQBiliCardFilter()
        for e in (event([Plain("你好")]), event([Json(data=miniapp())], private=False),
                  event([Json(data=miniapp())], platform="telegram")):
            self.assertFalse(handler.filter(e, {}))
        for e in (event([Json(data=miniapp())]), event([Json(data=miniapp())], private=False, at=True)):
            self.assertTrue(handler.filter(e, {}))
            self.assertEqual(e.get_extra("bili_share_url"), URL)

    def test_duplicate_delivery_and_per_session_rate_limit_are_bounded(self):
        reader = card.CardReader(); clock = [0]
        with patch.object(card, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            e = event([Json(data=miniapp())])
            self.assertTrue(reader.claim(e))
            self.assertFalse(reader.claim(e))
            clock[0] = 11
            self.assertFalse(reader.claim(e))
            self.assertTrue(reader.claim(event([], message_id="message-2")))
            clock[0] = 132
            self.assertTrue(reader.claim(e))

    async def test_bare_full_link_never_makes_a_redirect_request(self):
        with patch.object(card.aiohttp.ClientSession, "get", side_effect=AssertionError("unexpected network")):
            self.assertEqual(await card.resolve_share(URL), BV)

    async def test_short_link_stops_before_external_or_private_redirect(self):
        class Response:
            status = 302
            headers = {"Location": "http://127.0.0.1/admin"}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        seen = []
        def request(session, url, **kwargs):
            seen.append(url)
            self.assertFalse(kwargs["allow_redirects"])
            return Response()
        with patch.object(card.aiohttp.ClientSession, "get", request):
            with self.assertRaises(card.CardReadError):
                await card.resolve_share(SHORT)
        self.assertEqual(seen, [SHORT])

    async def test_short_link_resolves_to_canonical_video_without_fetching_destination(self):
        class Response:
            status = 302
            headers = {"Location": URL + "?tracking=yes"}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        seen = []
        def request(session, url, **kwargs):
            seen.append(url); return Response()
        with patch.object(card.aiohttp.ClientSession, "get", request):
            self.assertEqual(await card.resolve_share(SHORT), BV)
        self.assertEqual(seen, [SHORT])

    async def test_reader_uses_server_metadata_bounds_transcript_and_caches(self):
        reader = card.CardReader()
        info = AsyncMock(return_value=json.dumps(INFO))
        subtitle = AsyncMock(return_value={"text": "吃饭的字幕" * 1500})
        first = await reader.read(URL, info, subtitle)
        self.assertEqual(first["title"], INFO["title"])
        self.assertEqual(len(first["subtitle_excerpt"]), 5000)
        self.assertTrue(first["subtitle_truncated"])
        self.assertFalse(first["visual_analysis"])
        self.assertEqual(await reader.read(URL, info, subtitle), first)
        info.assert_awaited_once(); subtitle.assert_awaited_once()

    async def test_unavailable_subtitles_are_not_mistaken_for_video_content(self):
        for result in ({"message": "没有字幕"}, {"text": "字幕由某某提供"}, {"error": "private-server-error"}):
            material = await card.CardReader().read(URL, AsyncMock(return_value=INFO), AsyncMock(return_value=result))
            self.assertEqual(material["subtitle_excerpt"], "")
            self.assertIn("仅依据简介", material["subtitle_status"])
            self.assertNotIn("private-server-error", json.dumps(material))
        material = await card.CardReader().read(URL, AsyncMock(return_value=INFO), AsyncMock(side_effect=RuntimeError("secret")))
        self.assertIn("读取失败", material["subtitle_status"])

    async def test_missing_video_stops_before_subtitle_read(self):
        subtitle = AsyncMock()
        with self.assertRaises(card.CardReadError):
            await card.CardReader().read(URL, AsyncMock(return_value={"error": "private"}), subtitle)
        subtitle.assert_not_awaited()

    async def test_handler_prepares_normal_chat_and_injects_material_once(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._qq_cards = card.CardReader()
        async def risk_call(fn, *args): return await fn(*args)
        instance._risk_call = risk_call
        e = event([Json(data=miniapp())])
        self.assertTrue(card.QQBiliCardFilter().filter(e, {}))
        with patch.object(plugin.bili_api, "bili_video_info", AsyncMock(return_value=json.dumps(INFO))), \
             patch.object(plugin.bili_api, "bili_subtitle", AsyncMock(return_value=json.dumps({"text": "角色舀起白米饭"}))):
            self.assertEqual([out async for out in instance.read_qq_bili_card(e)], [])
        self.assertIn("请简要", e.message_str)
        self.assertIn(URL, e.message_str)
        self.assertFalse(e.call_llm)  # AstrBot's normal conversation/persona response is still allowed.
        req = ProviderRequest(prompt=e.message_str, system_prompt="原有人设")
        req.func_tool = SimpleNamespace(names=lambda: ["bili_comment"])
        await instance.add_qq_bili_material(e, req)
        self.assertIsNone(req.func_tool)
        self.assertIn(INFO["title"], req.prompt)
        self.assertIn("角色舀起白米饭", req.prompt)
        self.assertTrue(req.system_prompt.startswith("原有人设"))
        self.assertIn("不可信外部内容", req.system_prompt)
        self.assertIn("没有读取视频画面", req.system_prompt)
        previous = req.prompt
        await instance.add_qq_bili_material(e, req)
        self.assertEqual(req.prompt, previous)

    async def test_failed_read_replies_once_and_suppresses_uninformed_default_llm(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._qq_cards = card.CardReader()
        instance._risk_call = AsyncMock(side_effect=RuntimeError("secret-token"))
        e = event([Json(data=miniapp())]); card.QQBiliCardFilter().filter(e, {})
        replies = [out async for out in instance.read_qq_bili_card(e)]
        self.assertEqual(len(replies), 1)
        self.assertNotIn("secret-token", replies[0])
        self.assertTrue(e.call_llm)
        self.assertEqual([out async for out in instance.read_qq_bili_card(e)], [])

    async def test_unrelated_requests_and_explicit_user_question_are_preserved(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        req = ProviderRequest(prompt="你好", system_prompt="人设")
        await instance.add_qq_bili_material(event([Plain("你好")]), req)
        self.assertEqual(req.prompt, "你好"); self.assertEqual(req.system_prompt, "人设")
        instance._qq_cards = card.CardReader()
        async def call(fn): return await fn()
        instance._risk_call = call
        e = event([Plain("这条视频主要讲什么？"), Json(data=miniapp())]); card.QQBiliCardFilter().filter(e, {})
        with patch.object(plugin.bili_api, "bili_video_info", AsyncMock(return_value=INFO)), \
             patch.object(plugin.bili_api, "bili_subtitle", AsyncMock(return_value={"message": "没有字幕"})):
            self.assertEqual([out async for out in instance.read_qq_bili_card(e)], [])
        self.assertIn("这条视频主要讲什么", e.message_str)
