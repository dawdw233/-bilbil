"""Learning uses explicit disclosure, bounded visual evidence and no paid/video calls in tests."""
import asyncio
import json
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin

learning = import_module(plugin.__package__ + ".creative_learning")
media = import_module(plugin.__package__ + ".learning_media")
auto = import_module(plugin.__package__ + ".auto_publish")
INFO = {"bvid": "BV1234567890", "title": "吃饭的小女孩", "description": "本视频画面全部由AI生成。",
        "duration": 10, "pages": 1}
LESSON = {"topic": "吃饭动作", "rule": "先明确勺子与嘴的位置，再让角色完成一次送饭入口。",
          "evidence": "第1帧勺子在嘴前，第2帧勺子移近嘴部。", "limitation": "抽帧不能确认中间运动是否自然。",
          "tags": ["吃饭", "餐具接触"], "frames": [1, 2]}


class LearningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.analyze = AsyncMock(return_value=json.dumps({"lessons": [LESSON]}))
        self.frames = AsyncMock(return_value=[{"time": i, "image": "data:image/jpeg;base64,test"} for i in range(4)])
        self.worker = learning.CreativeLearning(self.path, self.analyze, self.frames)
        self.bili = SimpleNamespace(bili_learning_info=AsyncMock(return_value=dict(INFO)))

    async def call(self, fn, *args):
        return await fn(*args)

    async def observe(self, bvid="BV1234567890"):
        await self.worker.observe(bvid, "字幕素材", self.call, self.bili)

    async def learn(self):
        self.worker.save_settings({"enabled": True})
        await self.observe()
        return self.worker.state()["records"][0]

    def test_only_unambiguous_disclosure_passes(self):
        for value in ("AI视频", "AI生成视频教程", "本视频由AI生成吗？", "本视频并非AI生成。",
                      "部分画面由AI生成", "本视频只有配音由AI生成", "如何做到本视频由AI生成", "本视频由AI生成？"):
            with self.subTest(value=value):
                self.assertIsNone(learning.ai_evidence({"description": value, "title": "AI生成", "tags": ["AI生成"]}))
        for value in ("本视频画面全部由AI生成。", "该视频使用人工智能技术合成", "视频由AI生成", "本视频为AI生成"):
            self.assertIsNotNone(learning.ai_evidence({"description": value}))
        self.assertIsNotNone(learning.ai_evidence({"ai_declaration": "视频由AI生成"}))
        self.assertIsNone(learning.ai_evidence({"ai_declaration": "视频由AI生成", "description": "本视频并非AI生成"}))

    async def test_default_off_makes_no_requests_and_keeps_disk_untouched(self):
        await self.observe()
        self.bili.bili_learning_info.assert_not_awaited()
        self.frames.assert_not_awaited(); self.analyze.assert_not_awaited()
        self.assertFalse(self.worker.path.exists())
        self.assertEqual(self.worker.retrieve("吃饭"), [])

    async def test_ambiguous_ai_never_downloads_or_analyzes(self):
        self.worker.save_settings({"enabled": True})
        self.bili.bili_learning_info.return_value = {**INFO, "description": "AI 视频教程"}
        await self.observe()
        self.frames.assert_not_awaited(); self.analyze.assert_not_awaited()
        self.assertEqual(self.worker.state()["today_attempts"], 0)

    async def test_success_and_restart_deduplication(self):
        row = await self.learn()
        self.assertEqual(row["lessons"][0]["status"], "pending")
        self.assertEqual(row["sampled_at"], [0, 1, 2, 3])
        self.worker = learning.CreativeLearning(self.path, self.analyze, self.frames)
        await self.observe()
        self.analyze.assert_awaited_once()
        self.assertEqual(self.worker.state()["today_attempts"], 1)
        self.assertEqual(len(self.worker.retrieve("女孩吃饭")), 1)
        self.assertEqual(self.worker.retrieve("山水风景"), [])

    async def test_failed_analysis_reserves_budget_and_is_not_retried(self):
        self.worker.save_settings({"enabled": True, "daily_limit": 1})
        self.analyze.side_effect = RuntimeError("private details must not be returned")
        with self.assertRaises(RuntimeError):
            await self.observe()
        await self.observe()
        await self.observe("BV1234567891")
        self.analyze.assert_awaited_once()
        row = self.worker.state()["records"][0]
        self.assertEqual(row["phase"], "failed")
        self.assertNotIn("private", row["message"])

    async def test_delete_keeps_deduplication_daily_budget_and_other_records(self):
        row = await self.learn()
        self.worker.manage({"action": "delete", "id": row["id"]})
        await self.observe()
        self.analyze.assert_awaited_once()
        self.assertEqual(self.worker.state()["records"], [])
        self.assertEqual(self.worker.state()["today_attempts"], 1)
        self.assertEqual(self.worker.retrieve("吃饭"), [])

    async def test_feedback_excludes_bad_experience_and_disable_excludes_all(self):
        row = await self.learn()
        payload = {"action": "feedback", "id": row["id"], "lesson_id": row["lessons"][0]["id"]}
        self.worker.manage({**payload, "status": "ineffective"})
        self.assertEqual(self.worker.retrieve("吃饭"), [])
        self.worker.manage({**payload, "status": "useful"})
        self.assertEqual(self.worker.retrieve("吃饭")[0]["status"], "useful")
        self.worker.save_settings({"enabled": False})
        self.assertEqual(self.worker.retrieve("吃饭"), [])

    async def test_disable_before_model_call_or_during_it_stops_saved_experience(self):
        self.worker.save_settings({"enabled": True})
        original = self.frames.return_value
        async def frames(*args):
            self.worker.save_settings({"enabled": False})
            return original
        self.frames.side_effect = frames
        with self.assertRaises(learning.LearningError):
            await self.observe()
        self.analyze.assert_not_awaited()
        self.worker.save_settings({"enabled": True})
        self.frames.side_effect = None
        async def analyze(*args):
            self.worker.save_settings({"enabled": False})
            return json.dumps({"lessons": [LESSON]})
        self.analyze.side_effect = analyze
        await self.observe("BV1234567891")
        self.assertEqual(self.worker.state()["lesson_count"], 0)
        self.assertEqual(self.worker.state()["records"][0]["phase"], "skipped")

    async def test_concurrent_observers_cannot_exceed_budget(self):
        self.worker.save_settings({"enabled": True, "daily_limit": 1})
        await asyncio.gather(self.observe(), self.observe("BV1234567891"))
        self.analyze.assert_awaited_once()
        self.assertEqual(self.worker.state()["today_attempts"], 1)

    async def test_long_or_multi_part_videos_never_reach_model(self):
        self.worker.save_settings({"enabled": True})
        self.bili.bili_learning_info.return_value = {**INFO, "duration": 200}
        with self.assertRaises(learning.LearningError):
            await self.observe()
        self.bili.bili_learning_info.return_value = {**INFO, "pages": 2}
        with self.assertRaises(learning.LearningError):
            await self.observe("BV1234567891")
        self.frames.assert_not_awaited(); self.analyze.assert_not_awaited()

    async def test_cancellation_is_recorded_without_automatic_retry(self):
        self.worker.save_settings({"enabled": True})
        self.analyze.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.observe()
        self.assertFalse(self.worker.state()["busy"])
        await self.observe()
        self.analyze.assert_awaited_once()

    def test_invalid_settings_and_corrupt_storage_fail_closed(self):
        for payload in ({"enabled": 1}, {"daily_limit": 0}, {"daily_limit": True}, {"frame_count": 10}, []):
            with self.assertRaises(learning.LearningError):
                self.worker.save_settings(payload)
        self.worker.path.write_text("invalid")
        with self.assertRaises(learning.LearningError):
            self.worker.retrieve("吃饭")
        with self.assertRaises(learning.LearningError):
            self.worker.save_settings({"enabled": True})
        self.assertEqual(self.worker.path.read_text(), "invalid")

    def test_missing_or_invalid_visual_evidence_is_not_saved(self):
        for update in ({"frames": []}, {"frames": [5]}, {"frames": [True]}, {"limitation": ""}, {"tags": []}):
            with self.assertRaises(learning.LearningError):
                learning.parse_lessons(json.dumps({"lessons": [{**LESSON, **update}]}), 4)

    async def test_context_is_bounded_and_repeated_rules_are_merged(self):
        row = await self.learn()
        with self.worker._lock:
            data = self.worker._read()
            first = data["records"][0]["lessons"][0]
            data["records"][0]["lessons"] = [{**first, "id": f"{i:032x}", "rule": str(i) + "经验" * 80,
                                               "limitation": "局限" * 50} for i in range(30)] + [first, first]
            self.worker._write(data)
        context = self.worker.retrieve("吃饭")
        self.assertLessEqual(len(context), 5)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))), 1200)
        self.assertEqual(len({row["rule"] for row in context}), len(context))

    async def test_refinement_keeps_metadata_dialogue_and_records_exact_experience(self):
        await self.learn()
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._learning = self.worker
        instance.cfg = {"provider_id": "test"}
        selected = self.worker.retrieve("吃饭")
        generate = AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({
            "prompt": "勺子从嘴前送入口中，再抽出空勺。", "shots": [], "used_ids": [selected[0]["id"]],
            "title": "不允许覆盖的标题", "dialogue": [],
        })))
        instance.context = SimpleNamespace(llm_generate=generate)
        plan = {"title": "吃饭", "prompt": "女孩吃饭", "tags": "动画", "desc": "简介", "dialogue": [{"text": "开饭啦"}]}
        result = json.loads(await instance._apply_creative_learning(json.dumps(plan), "吃饭"))
        self.assertEqual(result["title"], plan["title"])
        self.assertEqual(result["dialogue"], plan["dialogue"])
        self.assertEqual(result["learning_used"][0]["id"], selected[0]["id"])
        result.pop("dialogue")
        self.assertEqual(auto.parse_plan(json.dumps(result))["learning_used"][0]["id"], selected[0]["id"])
        generate.side_effect = RuntimeError("failure")
        original = json.dumps(plan)
        self.assertEqual(await instance._apply_creative_learning(original, "吃饭"), original)
        self.worker.save_settings({"enabled": False})
        generate.reset_mock()
        self.assertEqual(await instance._apply_creative_learning(original, "吃饭"), original)
        generate.assert_not_awaited()

    async def test_browse_hook_preserves_normal_flow_and_risk_pause(self):
        bili = SimpleNamespace(load_credential=lambda: SimpleNamespace(sessdata="test", bili_jct="test", dedeuserid="42"),
                               bili_search=AsyncMock(return_value={"videos": [{"bvid": "BV1234567890", "title": "动画"}]}),
                               bili_subtitle=AsyncMock(return_value={"text": "吃饭画面"}))
        for outcome in (RuntimeError("analysis failed"), plugin.RiskPause("风控暂停", 60)):
            with tempfile.TemporaryDirectory() as folder:
                learner = AsyncMock(side_effect=outcome)
                runner = plugin.DutyRunner(bili=bili, config={"keywords": ["动画"]}, data_dir=Path(folder),
                                           judge=AsyncMock(return_value={"verdict": "值得看"}), log=lambda _: None, learner=learner)
                result = await runner.run()
                learner.assert_awaited_once()
                self.assertEqual(len(result["picked"]), 1)

    def test_streams_use_low_resolution_and_reject_external_urls(self):
        data = {"dash": {"video": [{"height": 1080, "baseUrl": "https://a.bilivideo.com/high"},
                                     {"height": 360, "base_url": "http://a.bilivideo.com/low"}]}}
        self.assertEqual(media.stream_url(data), "https://a.bilivideo.com/low")
        for url in ("http://127.0.0.1/file", "https://a.bilivideo.com.evil.test/a", "https://u:p@a.bilivideo.com/a"):
            with self.assertRaises(learning.LearningError):
                media.stream_url({"durl": [{"url": url}]})

    async def test_real_ffmpeg_keyframes_are_bounded_and_temporary_files_removed(self):
        if not media.shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        from importlib import import_module
        compose = import_module(plugin.__package__ + ".video_compose")
        clip = self.path / "source.mp4"
        await compose.run_media("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                "testsrc2=size=320x240:rate=5", "-t", "3", "-c:v", "libx264", str(clip))
        async def download(url, destination, bvid):
            destination.write_bytes(clip.read_bytes())
        self.bili.bili_learning_stream = AsyncMock(return_value={"durl": [{"url": "https://a.bilivideo.com/video"}]})
        with patch.object(media, "download_stream", download):
            samples = await media.video_frames(INFO, 4, self.call, self.bili)
        self.assertEqual(len(samples), 4)
        self.assertTrue(all(sample["image"].startswith("data:image/jpeg;base64,") for sample in samples))
        self.assertTrue(all(0 < sample["time"] < 3 for sample in samples))

    async def test_http_download_is_bounded_and_does_not_follow_redirects(self):
        from aiohttp import web
        async def handler(request):
            if request.path == "/redirect":
                raise web.HTTPFound("/video")
            if request.path == "/risk":
                return web.Response(status=412)
            return web.Response(body=b"v" * 2048)
        app = web.Application()
        app.router.add_get("/{name}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        target = self.path / "download.mp4"
        await media.download_stream(url + "/video", target, INFO["bvid"])
        self.assertEqual(target.stat().st_size, 2048)
        with patch.object(media, "MAX_BYTES", 1024), self.assertRaises(learning.LearningError):
            await media.download_stream(url + "/video", target, INFO["bvid"])
        with self.assertRaises(learning.LearningError):
            await media.download_stream(url + "/redirect", target, INFO["bvid"])
        self.assertEqual((await media.download_stream(url + "/risk", target, INFO["bvid"]))["code"], 412)

    async def test_storyboard_refinement_only_replaces_prompts_and_disable_cancels_use(self):
        await self.learn()
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._learning = self.worker
        instance.cfg = {"provider_id": "test"}
        selected = self.worker.retrieve("吃饭")
        revised = {"prompt": "女孩吃饭的故事", "shots": [{"prompt": "抽出空勺后闭嘴咀嚼。"}], "used_ids": [selected[0]["id"]]}
        generate = AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(revised)))
        instance.context = SimpleNamespace(llm_generate=generate)
        plan = {"title": "吃饭", "prompt": "女孩吃饭", "shots": [{"duration": 10, "start_state": "勺子在嘴前", "end_state": "勺子放下", "prompt": "吃一口饭", "dialogue": []}]}
        text = json.dumps(plan)
        result = json.loads(await instance._apply_creative_learning(text, "吃饭"))
        self.assertEqual({key: value for key, value in result["shots"][0].items() if key != "prompt"},
                         {key: value for key, value in plan["shots"][0].items() if key != "prompt"})
        async def disable(**kwargs):
            self.worker.save_settings({"enabled": False})
            return SimpleNamespace(completion_text=json.dumps(revised))
        generate.side_effect = disable
        self.assertEqual(await instance._apply_creative_learning(text, "吃饭"), text)
        fake = {**plan, "learning_used": [{"id": "a" * 32, "rule": "invented"}]}
        cleaned = json.loads(await instance._apply_creative_learning(json.dumps(fake), "吃饭"))
        self.assertNotIn("learning_used", cleaned)

    async def test_publisher_persists_learning_snapshot_for_both_generation_modes(self):
        snapshot = [{"id": "a" * 32, "topic": "吃饭", "rule": "明确勺子位置", "limitation": "动作仍依赖模型", "status": "pending"}]
        now = learning.datetime.now(learning.ZONE)
        for mode in ("single", "storyboard"):
            with tempfile.TemporaryDirectory() as folder:
                plan = {"title": "吃饭", "desc": "原创动画", "tags": "动画", "prompt": "完成一次吃饭动作", "learning_used": snapshot,
                        "continuity": "统一画风和角色", "shots": [{"prompt": "送饭入口", "duration": 10} for _ in range(3)]}
                planner = AsyncMock(return_value=json.dumps(plan))
                worker = auto.AutoPublisher(Path(folder), planner, AsyncMock(), lambda: "42", lambda: False, plan_storyboard=planner)
                worker.save_settings({"enabled": True, "api_key": "fake", "mode": mode, "total_duration": 30}, now)
                worker.enqueue(now, manual=True)
                with patch.object(auto.MiniMaxVideo, "create", AsyncMock(return_value="test-task")):
                    await worker.tick(now)
                self.assertIn("learning_used", worker.jobs()[0], worker.jobs()[0].get("message"))
                self.assertEqual(worker.jobs()[0]["learning_used"], snapshot)
                self.assertEqual(worker.state()["jobs"][0]["learning_used"], snapshot)
