"""Exercise auto-publish without calling MiniMax or Bilibili."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_appearance import plugin


auto_publish = import_module(plugin.__package__ + ".auto_publish")
PLAN = json.dumps({
    "title": "大肥鱼吃饭", "prompt": "一条圆滚滚的鱼吃鱼粮", "desc": "大肥鱼的晚餐", "tags": "鱼,动画",
})


class AutoPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        (self.path / "media").mkdir()
        self.now = datetime(2026, 11, 6, 19, tzinfo=plugin.BEIJING_TIME)
        self.account = "42"
        self.busy = False
        self.plan = AsyncMock(return_value=PLAN)
        self.publish = AsyncMock(return_value={"success": True, "bvid": "BV1234567890"})
        self.select_category = AsyncMock(return_value={
            "tid": 47, "category_name": "动画 / 短片·手书", "category_reason": "以虚构鱼为主角的剧情动画。",
        })
        self.worker = self.new_worker()
        self.create = AsyncMock(return_value="test-task")
        self.query = AsyncMock(return_value="test-file")
        self.download = AsyncMock(side_effect=lambda _id, _tmp, dest: dest.write_bytes(b"test MP4"))
        for name in ("create", "query", "download"):
            item = patch.object(auto_publish.MiniMaxVideo, name, getattr(self, name))
            item.start()
            self.addCleanup(item.stop)

    def new_worker(self):
        return auto_publish.AutoPublisher(
            self.path, self.plan, self.publish, lambda: self.account, lambda: self.busy,
            self.select_category,
        )

    def configure(self, **kwargs):
        self.worker.save_settings({
            "enabled": True, "api_key": "fake-secret", "direction": "动物短片", **kwargs,
        }, self.now)

    async def test_scheduled_pipeline_resumes_and_preserves_result(self):
        self.configure()
        due = self.now + timedelta(hours=1)
        await self.worker.tick(self.now)
        self.create.assert_not_awaited()
        await self.worker.tick(due)
        self.create.assert_awaited_once_with("一条圆滚滚的鱼吃鱼粮")
        self.assertEqual(self.worker.jobs()[0]["task_id"], "test-task")

        self.worker = self.new_worker()
        await self.worker.tick(due)
        job = self.worker.jobs()[0]
        self.assertEqual(job["phase"], "ready")
        media = self.path / "media" / job["file"]
        self.assertTrue(media.is_file())
        self.busy = True
        await self.worker.tick(due)
        self.publish.assert_not_awaited()
        self.busy = False
        await self.worker.tick(due)
        self.assertEqual(self.publish.await_args.args[0]["video_path"], str(media))
        self.assertIn("AI 生成", self.publish.await_args.args[0]["desc"])
        result = self.worker.state()["jobs"][0]
        self.assertEqual((result["phase"], result["bvid"]), ("published", "BV1234567890"))
        await self.worker.tick(due)
        self.create.assert_awaited_once()
        with self.assertRaises(auto_publish.PublishError):
            self.worker.enqueue(due, manual=True)

    async def test_disabled_and_account_change_pause_publication(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.worker.save_settings({"enabled": False}, self.now)
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "ready")
        self.worker.save_settings({"enabled": True}, self.now)
        self.account = "88"
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "review")
        self.publish.assert_not_awaited()

    async def test_ambiguous_create_is_never_retried(self):
        self.configure()
        self.create.side_effect = TimeoutError
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "review")
        await self.new_worker().tick(self.now + timedelta(hours=1))
        self.create.assert_awaited_once()
        self.query.assert_not_awaited()

    async def test_interrupted_publication_is_not_repeated(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.publish.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "uploading")
        worker = self.new_worker()
        await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["phase"], "review")
        self.publish.assert_awaited_once()

    async def test_key_is_write_only_and_disabled_by_default(self):
        await self.worker.tick(self.now + timedelta(hours=2))
        self.create.assert_not_awaited()
        with self.assertRaises(auto_publish.PublishError):
            self.worker.save_settings({"enabled": True}, self.now)
        self.configure()
        state = self.worker.state()
        self.assertTrue(state["profiles"]["minimax:cn"]["key_configured"])
        self.assertNotIn("fake-secret", json.dumps(state))
        self.worker.save_settings({"enabled": False, "remove_key": True}, self.now)
        self.assertFalse(self.worker.state()["profiles"]["minimax:cn"]["key_configured"])

    async def test_legacy_key_migrates_and_keeps_running_task_on_original_service(self):
        self.worker.settings_path.write_text(json.dumps({
            "enabled": True, "key": "old-minimax-key", "region": "global",
        }), encoding="utf-8")
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        rows = self.worker.jobs()
        rows[0].pop("provider")
        rows[0].pop("model")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        with self.assertRaises(auto_publish.PublishError):
            self.worker.save_settings({"provider": "ark", "region": "cn"}, self.now)
        self.worker.save_settings({
            "provider": "ark", "region": "cn", "api_key": "test-ark-key", "limit": 2,
        }, self.now)
        saved = self.worker.settings()
        self.assertNotIn("key", saved)
        self.assertEqual(saved["profiles"]["minimax:global"]["key"], "old-minimax-key")
        self.assertNotIn("old-minimax-key", json.dumps(self.worker.state()))
        self.assertNotIn("test-ark-key", json.dumps(self.worker.state()))

        await self.worker.tick(self.now)
        self.query.assert_awaited_once_with("test-task")
        self.assertEqual(self.worker.jobs()[0]["provider"], "minimax")
        await self.worker.tick(self.now)
        self.assertIn("MiniMax", self.publish.await_args.args[0]["desc"])

        self.plan.return_value = json.dumps({**json.loads(PLAN), "title": "猫咪喝水"})
        self.worker.enqueue(self.now, manual=True)
        with patch.object(auto_publish.ArkVideo, "create", new_callable=AsyncMock, return_value="ark-task") as create:
            await self.worker.tick(self.now)
            create.assert_awaited_once()
        job = self.worker.jobs()[-1]
        self.assertEqual((job["provider"], job["task_id"]), ("ark", "ark-task"))
        self.create.assert_awaited_once()

    async def test_region_switch_does_not_reuse_a_different_regions_key(self):
        self.configure(region="cn")
        with self.assertRaises(auto_publish.PublishError):
            self.worker.save_settings({"region": "global"}, self.now)
        self.worker.save_settings({"region": "global", "api_key": "test-global-key"}, self.now)
        self.assertEqual(self.worker.settings()["profiles"]["minimax:cn"]["key"], "fake-secret")

    async def test_model_lookup_uses_unsaved_key_without_persisting_it(self):
        self.configure()
        listing = AsyncMock(return_value=[{"id": "MiniMax-Hailuo-02", "name": "海螺 02"}])
        with patch.object(auto_publish.MiniMaxVideo, "models", listing):
            result = await self.worker.models({"provider": "minimax", "region": "cn", "api_key": "temporary-key"})
        self.assertEqual(result["models"][0]["id"], "MiniMax-Hailuo-02")
        self.assertNotIn("temporary-key", self.worker.settings_path.read_text(encoding="utf-8"))
        self.worker.save_settings({"model": "MiniMax-Hailuo-02"}, self.now)
        job = self.worker.enqueue(self.now, manual=True)
        self.assertEqual(job["model"], "MiniMax-Hailuo-02")
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["model"], "MiniMax-Hailuo-02")
        with self.assertRaises(auto_publish.PublishError):
            self.worker.save_settings({"model": "MiniMax-M3"}, self.now)

    async def test_auto_category_is_pinned_and_reaches_publication_after_restart(self):
        self.configure(auto_category=True, tid=21)
        self.worker.enqueue(self.now, manual=True)
        self.worker.save_settings({"auto_category": False, "tid": 17}, self.now)
        await self.worker.tick(self.now)
        self.select_category.assert_awaited_once_with(json.loads(PLAN))
        self.worker = self.new_worker()
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.assertEqual(self.publish.await_args.args[0]["tid"], 47)
        job = self.worker.state()["jobs"][0]
        self.assertEqual((job["category_mode"], job["category_name"]), ("auto", "动画 / 短片·手书"))
        self.assertIn("剧情动画", job["category_reason"])
        self.select_category.assert_awaited_once()

    async def test_manual_and_legacy_jobs_never_request_category_judgement(self):
        self.configure(tid=17)
        self.assertFalse(self.worker.settings()["auto_category"])
        self.worker.enqueue(self.now, manual=True)
        rows = self.worker.jobs()
        rows[0].pop("auto_category")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        self.worker.save_settings({"auto_category": True}, self.now)
        for _ in range(3):
            await self.worker.tick(self.now)
        self.select_category.assert_not_awaited()
        self.assertEqual(self.publish.await_args.args[0]["tid"], 17)
        self.assertEqual(self.worker.jobs()[0]["category_mode"], "manual")

    async def test_category_failure_uses_pinned_fallback_without_stopping_video(self):
        for failure in (TimeoutError(), auto_publish.PublishError("unavailable"), ValueError("bad response")):
            with self.subTest(failure=type(failure).__name__):
                self.worker.jobs_path.unlink(missing_ok=True)
                self.configure(auto_category=True, tid=21)
                self.worker.enqueue(self.now, manual=True)
                self.worker.save_settings({"tid": 17}, self.now)
                self.select_category.side_effect = failure
                for _ in range(3):
                    await self.worker.tick(self.now)
                self.assertEqual(self.publish.await_args.args[0]["tid"], 21)
                job = self.worker.state()["jobs"][0]
                self.assertEqual((job["phase"], job["category_mode"]), ("published", "fallback"))
                self.assertIn("备用分区", job["category_reason"])

    async def test_disabling_during_category_selection_prevents_paid_request(self):
        self.configure(auto_category=True)
        self.worker.enqueue(self.now, manual=True)
        async def disable(_plan):
            self.worker.save_settings({"enabled": False}, self.now)
            return self.select_category.return_value
        self.select_category.side_effect = disable
        await self.worker.tick(self.now)
        self.create.assert_not_awaited()
        self.assertEqual(self.worker.jobs()[0]["phase"], "queued")

    async def test_category_switch_requires_a_boolean(self):
        for value in (1, "true", None):
            with self.assertRaises(auto_publish.PublishError):
                self.configure(auto_category=value)


class CategoryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_classifier_receives_video_details_and_current_categories(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        categories = [{"tid": 47, "name": "动画 / 短片·手书"}]
        instance._provider_id = lambda: "text-model"
        instance.context = type("Context", (), {"llm_generate": AsyncMock(return_value=type(
            "Result", (), {"completion_text": '{"tid":47,"reason":"虚构角色动画"}'},
        )())})()
        with patch.object(plugin.bili_api, "bili_upload_categories", AsyncMock(return_value=categories)):
            result = await instance._select_auto_video_category(json.loads(PLAN))
        self.assertEqual(result["tid"], 47)
        payload = json.loads(instance.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual(payload["视频内容"], json.loads(PLAN))
        self.assertEqual(payload["可用投稿分区"], categories)

    async def test_category_parser_rejects_invented_ids_parent_ids_and_invalid_types(self):
        categories = [{"tid": 47, "name": "动画 / 短片·手书"}]
        for text in ('{"tid":1,"reason":"父分区"}', '{"tid":9999,"reason":"虚构"}',
                     '{"tid":"47","reason":"字符串"}', '{"tid":true,"reason":"布尔"}',
                     '{"tid":47,"reason":" "}', '[]', 'not json'):
            with self.subTest(text=text), self.assertRaises(auto_publish.PublishError):
                auto_publish.parse_category(text, categories)
        result = auto_publish.parse_category('```json\n{"tid":47,"reason":"剧情动画"}\n```', categories)
        self.assertEqual(result["category_name"], "动画 / 短片·手书")

    async def test_bilibili_list_uses_visible_child_categories_only(self):
        from bilibili_api.utils import network
        class FakeApi:
            def __init__(self, **kwargs):
                assert kwargs["method"] == "GET"
            def update_params(self, **kwargs):
                return self
            @property
            async def result(self):
                return {"typelist": [
                    {"id": 1, "name": "动画", "children": [
                        {"id": 47, "name": "短片·手书", "show": True, "description": "原创动画"},
                        {"id": 99, "name": "隐藏", "show": False},
                        {"id": True, "name": "无效"},
                    ]}, {"id": 2, "name": "隐藏父分区", "show": False, "children": [{"id": 100, "name": "隐藏"}]},
                ]}
        with patch.object(network, "Api", FakeApi), patch.object(plugin.bili_api, "get_cred", return_value=object()):
            result = await plugin.bili_api.bili_upload_categories()
        self.assertEqual(result, [{"tid": 47, "name": "动画 / 短片·手书", "description": "原创动画"}])



class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_ark_submits_configured_model_and_reads_result_url(self):
        client = auto_publish.ArkVideo("ark", "cn", "fake-key", "ep-custom")
        request = AsyncMock(side_effect=[
            {"id": "ark-task"},
            {"status": "running"},
            {"status": "succeeded", "content": {"video_url": "https://example.com/video.mp4"}},
        ])
        with patch.object(client, "_request", request):
            self.assertEqual(await client.create("鱼吃饭"), "ark-task")
            self.assertIsNone(await client.query("ark-task"))
            self.assertEqual(await client.query("ark-task"), "https://example.com/video.mp4")
        self.assertEqual(request.await_args_list[0].args, ("POST", "/contents/generations/tasks"))
        self.assertEqual(request.await_args_list[0].kwargs["json"]["model"], "ep-custom")
        self.assertEqual(request.await_args_list[2].args, ("GET", "/contents/generations/tasks/ark-task"))

    async def test_wan_uses_async_header_and_dashscope_result_shape(self):
        client = auto_publish.WanVideo("wan", "global", "fake-key", "wan2.6-t2v")
        request = AsyncMock(side_effect=[
            {"output": {"task_id": "wan-task"}},
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://example.com/wan.mp4"}},
        ])
        with patch.object(client, "_request", request):
            self.assertEqual(await client.create("鱼吃饭"), "wan-task")
            self.assertEqual(await client.query("wan-task"), "https://example.com/wan.mp4")
        self.assertEqual(client.base_url, "https://dashscope-intl.aliyuncs.com/api/v1")
        self.assertEqual(request.await_args_list[0].kwargs["headers"], {"X-DashScope-Async": "enable"})
        self.assertEqual(request.await_args_list[1].args, ("GET", "/tasks/wan-task"))

    async def test_minimax_listing_accepts_openai_shape_and_filters_non_video_models(self):
        client = auto_publish.MiniMaxVideo("minimax", "cn", "fake-key", "T2V-01")
        request = AsyncMock(return_value={"data": [
            {"id": "MiniMax-M3"}, {"id": "MiniMax-Hailuo-02"}, {"id": "T2V-01"},
            {"id": "I2V-01"}, {"id": "MiniMax-Hailuo-2.3-Fast"},
        ]})
        with patch.object(auto_publish.VideoClient, "_request", request):
            models = await client.models()
        self.assertEqual([item["id"] for item in models], ["MiniMax-Hailuo-02", "T2V-01"])
        request.assert_awaited_once_with(client, "GET", "/models", params={})
        with patch.object(client, "_request", AsyncMock(return_value={"task_id": "task"})) as submit:
            await client.create("鱼吃饭")
        self.assertEqual(submit.await_args.kwargs["json"]["model"], "T2V-01")
        self.assertEqual(submit.await_args.kwargs["json"]["resolution"], "720P")

    async def test_wan_model_listing_reads_all_pages_and_filters_unsupported_protocols(self):
        client = auto_publish.WanVideo("wan", "cn", "fake-key", "wan2.6-t2v")
        request = AsyncMock(side_effect=[
            {"output": {"total": 101, "models": [{"model": "wan2.6-t2v"}] * 100}},
            {"output": {"total": 101, "models": [{"model": "wan2.5-t2v-preview"}, {"model": "wan3.0-video"}]}},
        ])
        with patch.object(auto_publish.VideoClient, "_request", request):
            models = await client.models()
        self.assertEqual([item["id"] for item in models], ["wan2.6-t2v", "wan2.5-t2v-preview"])
        self.assertEqual(request.await_args_list[1].kwargs["params"]["page_no"], 2)
