"""投稿链路：分区预测 / 标签预检 / 预上传 / 话题 / AI 声明 / 4:3 封面 / 定时。

全部走 mock：不访问 B站，也不跑 ffmpeg 真截帧（封面函数单独替换）。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin

bili_api = plugin.bili_api
BEIJING = timezone(timedelta(hours=8))


class _Response:
    def __init__(self, body, status=200):
        self._text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.status = status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _respond(self, method, url, kwargs):
        result = self.handler(method, url, kwargs)
        return _Response(*result) if isinstance(result, tuple) else _Response(result)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._respond("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._respond("POST", url, kwargs)


def patch_member(test, handler):
    client = _Client(handler)

    class _CM:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *exc):
            return False

    patcher = patch.object(bili_api, "_member_client", lambda *a, **k: _CM())
    patcher.start()
    test.addCleanup(patcher.stop)
    return client


class ScheduleTests(unittest.TestCase):
    def test_accepts_beijing_time(self):
        target = (datetime.now(BEIJING) + timedelta(days=1)).replace(second=0, microsecond=0)
        stamp = bili_api.parse_schedule_time(target.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(stamp, int(target.timestamp()))

    def test_accepts_t_separator_and_seconds(self):
        target = (datetime.now(BEIJING) + timedelta(hours=5)).replace(microsecond=0)
        stamp = bili_api.parse_schedule_time(target.strftime("%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(stamp, int(target.timestamp()))

    def test_empty_means_publish_now(self):
        self.assertEqual(bili_api.parse_schedule_time(""), 0)
        self.assertEqual(bili_api.parse_schedule_time("   "), 0)

    def test_rejects_too_soon(self):
        target = datetime.now(BEIJING) + timedelta(minutes=30)
        with self.assertRaises(ValueError):
            bili_api.parse_schedule_time(target.strftime("%Y-%m-%d %H:%M"))

    def test_rejects_too_far(self):
        target = datetime.now(BEIJING) + timedelta(days=20)
        with self.assertRaises(ValueError):
            bili_api.parse_schedule_time(target.strftime("%Y-%m-%d %H:%M"))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            bili_api.parse_schedule_time("明天晚上")


class MemberApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_predict_returns_legacy_and_new_zone(self):
        patch_member(self, lambda method, url, kwargs: {
            "code": 0,
            "data": [{
                "id": 21, "name": "日常", "parent_name": "生活",
                "human_type": {"id": 10017, "name": "日常"},
            }],
        })
        zone = await bili_api.predict_upload_zone("我家猫的一天", SimpleNamespace(bili_jct="j"))
        self.assertEqual(zone["tid"], 21)
        self.assertEqual(zone["zone"], 10017)
        self.assertEqual(zone["zone_name"], "日常")

    async def test_predict_none_when_bilibili_says_no(self):
        patch_member(self, lambda method, url, kwargs: {"code": -404, "data": []})
        self.assertIsNone(await bili_api.predict_upload_zone("x", SimpleNamespace(bili_jct="j")))

    async def test_predict_none_when_request_explodes(self):
        def handler(method, url, kwargs):
            raise RuntimeError("boom")
        patch_member(self, handler)
        self.assertIsNone(await bili_api.predict_upload_zone("x", SimpleNamespace(bili_jct="j")))

    async def test_tag_check_lists_only_rejected(self):
        def handler(method, url, kwargs):
            return {"code": 0} if kwargs["params"]["tag"] == "好标签" else {"code": 21070}
        patch_member(self, handler)
        bad = await bili_api.check_upload_tags(["好标签", "坏标签"], SimpleNamespace())
        self.assertEqual(bad, ["坏标签"])

    async def test_tag_check_failure_does_not_block_upload(self):
        def handler(method, url, kwargs):
            raise RuntimeError("boom")
        patch_member(self, handler)
        self.assertEqual(await bili_api.check_upload_tags(["任意"], SimpleNamespace()), [])

    async def test_preflight_ok_returns_quietly(self):
        patch_member(self, lambda method, url, kwargs: {"OK": 1})
        await bili_api.preflight_upload(__file__, SimpleNamespace())

    async def test_preflight_captcha_is_explained(self):
        patch_member(self, lambda method, url, kwargs: (
            {"code": 601, "message": "需要验证", "detail": {"v_voucher": "v"}}, 406,
        ))
        with self.assertRaises(bili_api.PublishRefused) as raised:
            await bili_api.preflight_upload(__file__, SimpleNamespace())
        self.assertIn("验证", raised.exception.hint)

    async def test_preflight_unparsable_body_does_not_block(self):
        patch_member(self, lambda method, url, kwargs: ("<html>502</html>", 502))
        await bili_api.preflight_upload(__file__, SimpleNamespace())

    async def test_topic_by_name_keeps_mission_id(self):
        patch_member(self, lambda method, url, kwargs: {
            "code": 0,
            "data": {"result": {"topics": [
                {"id": 11, "name": "日常 记录"},
                {"id": 22, "name": "AI 创作", "mission_id": 777},
            ]}},
        })
        topic = await bili_api.find_upload_topic("AI 创作", SimpleNamespace())
        self.assertEqual(topic["id"], 22)
        self.assertEqual(topic["mission_id"], 777)

    async def test_topic_not_found_returns_none(self):
        patch_member(self, lambda method, url, kwargs: {"code": 0, "data": {"result": {"topics": []}}})
        self.assertIsNone(await bili_api.find_upload_topic("不存在的活动", SimpleNamespace()))

    async def test_archive_state_maps_review_fields(self):
        patch_member(self, lambda method, url, kwargs: {
            "code": 0,
            "data": {
                "archive": {
                    "bvid": "BV1xx411c7mD", "aid": 9, "title": "标题", "state": 2,
                    "state_desc": "未通过", "reject_reason": "封面违规", "is_only_self": 0,
                    "tag": "AI,动画", "tid": 21, "creation_statement": {"id": 1},
                    "human_type2": {"id": 10017, "name": "日常"},
                    "ptime": 0, "dtime": 0,
                },
                "videos": [{"cid": 5, "title": "P1", "status_desc": "通过", "fail_desc": ""}],
            },
        })
        state = await bili_api.bili_archive_state("BV1xx411c7mD", SimpleNamespace())
        self.assertEqual(state["state"], 2)
        self.assertFalse(state["published"])
        self.assertEqual(state["reject_reason"], "封面违规")
        self.assertTrue(state["ai_declare"])
        self.assertEqual(state["zone_name"], "日常")
        self.assertEqual(state["tags"], ["AI", "动画"])
        self.assertEqual(state["pages"][0]["cid"], 5)


class _FakePage:
    def __init__(self, path, title, description=""):
        self.path, self.title, self.description = path, title, description


class _FakeUploader:
    last = None

    def __init__(self, pages, meta, credential, cover=None):
        _FakeUploader.last = self
        self.pages = pages
        self.meta = meta
        self.credential = credential
        self.cover = cover

    def on(self, event):
        def decorator(fn):
            return fn
        return decorator

    async def start(self):
        return {"bvid": "BV1xx411c7mD", "aid": 123456}


class UploadMetaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        _FakeUploader.last = None
        self.folder = Path(folder.name)
        self.video = self.folder / "clip.mp4"
        self.video.write_bytes(b"\x00" * 2048)
        self.cred = SimpleNamespace(sessdata="s", bili_jct="j", dedeuserid="42")
        self.predict = AsyncMock(return_value={"tid": 21, "tid_name": "生活 / 日常",
                                               "zone": 10017, "zone_name": "日常"})
        self.tags = AsyncMock(return_value=[])
        self.topic = AsyncMock(return_value=None)
        self.preflight = AsyncMock()
        self.cover43 = patch.object(bili_api, "Picture")
        self.addCleanup(self.cover43.stop)
        self.cover43.start().from_file = lambda path: object()
        for name, value in (
            ("get_cred", lambda: self.cred),
            ("VideoUploaderPage", _FakePage),
            ("VideoUploader", _FakeUploader),
            ("_extract_cover_from_video", lambda path: object()),
            ("_extract_cover_43", lambda path: ""),
            ("upload_cover", AsyncMock(return_value="https://i0.hdslb.com/cover43.jpg")),
        ):
            patcher = patch.object(bili_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.predict_patcher = patch.object(bili_api, "predict_upload_zone", self.predict)
        self.predict_patcher.start()
        self.addCleanup(self.predict_patcher.stop)
        for name, value in (("check_upload_tags", self.tags),
                            ("preflight_upload", self.preflight),
                            ("find_upload_topic", self.topic)):
            patcher = patch.object(bili_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def upload(self, **overrides):
        payload = {"video_path": str(self.video), "title": "我家猫的一天", "tags": "AI,动画"}
        payload.update(overrides)
        raw = await bili_api.bili_upload_video(**payload)
        return json.loads(raw)

    async def test_ai_declaration_and_new_zone_are_sent(self):
        result = await self.upload(tid=0)
        self.assertTrue(result["success"])
        meta = _FakeUploader.last.meta
        self.assertEqual(meta["creation_statement"], {"id": 1})
        self.assertEqual(meta["human_type2"], 10017)
        self.assertEqual(meta["tid"], 21)
        self.assertEqual(meta["is_only_self"], 0)
        self.assertEqual(meta["tag"], "AI,动画")
        self.assertEqual(meta["dtime"] if "dtime" in meta else 0, 0)
        self.assertEqual(result["data"]["zone"]["zone_name"], "日常")
        self.assertIn("分区按标题预测", "；".join(result["data"]["notes"]))

    async def test_ai_declaration_can_be_turned_off(self):
        await self.upload(ai_declare=False)
        self.assertEqual(_FakeUploader.last.meta["creation_statement"], {"id": -1})

    async def test_private_schedule_topic_and_cover43(self):
        target = (datetime.now(BEIJING) + timedelta(days=1)).replace(second=0, microsecond=0)
        cover43 = self.folder / "cover43.png"
        cover43.write_bytes(b"png")
        with patch.object(bili_api, "_extract_cover_43", lambda path: str(cover43)):
            self.topic.return_value = {"id": 22, "name": "AI 创作", "mission_id": 777}
            result = await self.upload(
                tid=21, private=True, schedule_time=target.strftime("%Y-%m-%d %H:%M"),
                topic="AI 创作", cover_43=True,
            )
        meta = _FakeUploader.last.meta
        self.assertEqual(meta["is_only_self"], 1)
        self.assertEqual(meta["dtime"], int(target.timestamp()))
        self.assertEqual(meta["topic_id"], 22)
        self.assertEqual(meta["mission_id"], 777)
        self.assertEqual(meta["cover43"], "https://i0.hdslb.com/cover43.jpg")
        self.assertEqual(result["data"]["schedule"], target.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(result["data"]["topic"]["name"], "AI 创作")
        self.assertFalse(cover43.exists(), "临时 4:3 封面应该被清掉")

    async def test_prediction_failure_falls_back_to_daily_zone(self):
        self.predict.return_value = None
        result = await self.upload(tid=0)
        self.assertEqual(_FakeUploader.last.meta["tid"], 21)
        self.assertNotIn("human_type2", _FakeUploader.last.meta)
        self.assertIn("分区预测失败", "；".join(result["data"]["notes"]))

    async def test_rejected_tags_are_dropped_not_fatal(self):
        self.tags.return_value = ["动画"]
        result = await self.upload(tid=21)
        self.assertTrue(result["success"])
        self.assertEqual(_FakeUploader.last.meta["tag"], "AI")
        self.assertIn("动画", "；".join(result["data"]["notes"]))

    async def test_all_tags_rejected_stops_before_upload(self):
        self.tags.return_value = ["AI", "动画"]
        result = await self.upload(tid=21)
        self.assertFalse(result["success"])
        self.assertIn("标签", result["error"])
        self.assertIsNone(_FakeUploader.last)

    async def test_preflight_refusal_carries_hint(self):
        self.preflight.side_effect = bili_api.PublishRefused("B站拒绝了上传：需要验证", "去网页端手动传一次")
        result = await self.upload(tid=21)
        self.assertFalse(result["success"])
        self.assertIn("需要验证", result["error"])
        self.assertIn("网页端", result["hint"])
        self.assertIsNone(_FakeUploader.last)

    async def test_bad_schedule_is_rejected_before_upload(self):
        result = await self.upload(tid=21, schedule_time="明天晚上")
        self.assertFalse(result["success"])
        self.assertIn("定时发布", result["error"])
        self.assertIsNone(_FakeUploader.last)

    async def test_missing_topic_is_skipped_with_note(self):
        result = await self.upload(tid=21, topic="不存在的活动")
        self.assertTrue(result["success"])
        self.assertIsNone(result["data"]["topic"])
        self.assertIn("没找到话题", "；".join(result["data"]["notes"]))

    async def test_repost_sets_copyright_and_source(self):
        result = await self.upload(tid=21, source="https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertTrue(result["success"])
        meta = _FakeUploader.last.meta
        self.assertEqual(meta["copyright"], 2)
        self.assertEqual(meta["source"], "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(meta["no_reprint"], 0)

    async def test_missing_video_is_reported_without_network(self):
        result = await bili_api.bili_upload_video(video_path=str(self.folder / "nope.mp4"), title="x")
        self.assertFalse(json.loads(result)["success"])
        self.predict.assert_not_awaited()


class UploadApiValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(folder.name)
        (self.instance.data_dir / "media").mkdir()
        (self.instance.data_dir / "media" / "clip.mp4").write_bytes(b"\x00" * 2048)
        self.instance._upload = {"running": False, "phase": "空闲", "title": "",
                                 "started_at": "", "result": None}
        self.payload = {"file": "clip.mp4", "title": "标题", "tid": 0, "tags": "AI"}
        holder = self

        class _Request:
            async def json(self, default=None):
                return holder.payload

        for target, name, value in (
            (plugin, "request", _Request()),
            (plugin, "json_response", lambda data: data),
            (plugin, "error_response", lambda message: {"error": message}),
            (plugin.BiliScrollPlugin, "_cred_ok", staticmethod(lambda: True)),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_schedule_typo_is_rejected_without_starting(self):
        self.payload["schedule"] = "明天晚上"
        self.instance._do_upload = AsyncMock()
        result = await self.instance._api_upload_start()
        self.assertIn("定时发布", result["error"])
        self.instance._do_upload.assert_not_awaited()

    async def test_bad_tid_is_rejected(self):
        self.payload["tid"] = -3
        self.instance._do_upload = AsyncMock()
        result = await self.instance._api_upload_start()
        self.assertIn("分区", result["error"])

    async def test_auto_tid_zero_passes_through(self):
        self.payload.update({"schedule": "", "topic": "", "ai_declare": True,
                             "cover_43": True, "private": False})
        self.instance._do_upload = AsyncMock()
        result = await self.instance._api_upload_start()
        self.assertTrue(result.get("started"))
        self.assertEqual(self.instance._upload["title"], "标题")

    async def test_review_rejects_lookalike_bvid(self):
        self.payload = {"bvid": "BVnot-a-real-id"}
        result = await self.instance._api_upload_review()
        self.assertIn("BV", result["error"])

    async def test_review_returns_state(self):
        self.payload = {"bvid": "BV1xx411c7mD"}
        state = {"bvid": "BV1xx411c7mD", "state": 0, "state_desc": "已通过"}
        with patch.object(bili_api, "bili_archive_state", AsyncMock(return_value=state)):
            result = await self.instance._api_upload_review()
        self.assertEqual(result["state_desc"], "已通过")


class ChatUploadToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(folder.name)
        (self.instance.data_dir / "media").mkdir()
        (self.instance.data_dir / "media" / "clip.mp4").write_bytes(b"\x00" * 2048)
        self.instance._upload = {"running": False}
        self.instance.context = SimpleNamespace(get_config=lambda: {"admins_id": ["42"]})
        self.instance._cred_ok = lambda: True
        self.instance._do_upload = AsyncMock(return_value={
            "success": True, "bvid": "BV1xx411c7mD", "zone": {"zone_name": "日常"},
            "notes": ["B站不收这些标签，已去掉：动画"], "ai_declare": True,
            "schedule": "", "topic": None,
        })
        self.event = SimpleNamespace(get_sender_id=lambda: "42")

    async def test_non_owner_cannot_publish(self):
        result = await self.instance.tool_bili_upload_video(
            SimpleNamespace(get_sender_id=lambda: "999"), "clip.mp4", "标题")
        self.assertIn("只有管理员", result)
        self.instance._do_upload.assert_not_awaited()

    async def test_foreign_path_is_refused(self):
        result = await self.instance.tool_bili_upload_video(
            self.event, "/etc/passwd", "标题")
        self.assertIn("素材目录", result)
        self.instance._do_upload.assert_not_awaited()

    async def test_missing_file_is_reported(self):
        result = await self.instance.tool_bili_upload_video(self.event, "nope.mp4", "标题")
        self.assertIn("没有这个文件", result)

    async def test_defaults_are_forwarded(self):
        await self.instance.tool_bili_upload_video(self.event, "clip.mp4", "标题")
        payload = self.instance._do_upload.await_args.args[0]
        self.assertEqual(payload["tid"], 0)
        self.assertIs(payload["ai_declare"], True)
        self.assertIs(payload["cover_43"], True)
        self.assertIs(payload["private"], False)
        self.assertEqual(payload["schedule"], "")

    async def test_success_message_has_link_and_notes(self):
        result = await self.instance.tool_bili_upload_video(
            self.event, "clip.mp4", "标题", topic="AI 创作",
            schedule="2026-11-15 20:00", private=True, ai_declare=False)
        self.assertIn("BV1xx411c7mD", result)
        self.assertIn("日常", result)
        self.assertIn("标签", result)
        payload = self.instance._do_upload.await_args.args[0]
        self.assertEqual(payload["topic"], "AI 创作")
        self.assertEqual(payload["schedule"], "2026-11-15 20:00")

    async def test_failure_carries_hint(self):
        self.instance._do_upload = AsyncMock(return_value={
            "success": False, "error": "B站拒绝了上传：需要验证",
            "hint": "去网页端手动传一次",
        })
        result = await self.instance.tool_bili_upload_video(self.event, "clip.mp4", "标题")
        self.assertIn("没成功", result)
        self.assertIn("需要验证", result)
        self.assertIn("网页端", result)

    async def test_busy_upload_is_refused(self):
        self.instance._upload["running"] = True
        result = await self.instance.tool_bili_upload_video(self.event, "clip.mp4", "标题")
        self.assertIn("还在上传", result)


if __name__ == "__main__":
    unittest.main()
