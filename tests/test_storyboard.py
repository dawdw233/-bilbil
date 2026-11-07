"""Storyboards use mocked cloud/Bilibili calls; composition tests use synthetic local videos."""
import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_appearance import plugin

auto = import_module(plugin.__package__ + ".auto_publish")
media = import_module(plugin.__package__ + ".video_compose")


def plan(durations):
    return json.dumps({
        "title": "大肥鱼的晚餐", "prompt": "大肥鱼准备食物、吃饭并满足地打嗝。", "desc": "一条鱼的温暖晚餐", "tags": "鱼,动画",
        "continuity": "同一条金色圆滚滚的大肥鱼，红色围巾，蓝色小屋，温柔卡通画风。",
        "shots": [{"duration": duration, "prompt": f"镜头 {index + 1}：大肥鱼继续吃饭，动作承接前镜头。"} for index, duration in enumerate(durations)],
    }, ensure_ascii=False)


class StoryboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        (self.path / "media").mkdir()
        self.now = datetime(2026, 11, 7, 19, tzinfo=plugin.BEIJING_TIME)
        self.account = "42"
        self.plan = AsyncMock(side_effect=lambda _direction, _recent, durations: plan(durations))
        self.single_plan = AsyncMock()
        self.publish = AsyncMock(return_value={"success": True, "bvid": "BV1234567890"})
        self.verify = AsyncMock(return_value=False)
        self.category = AsyncMock(return_value={"tid": 47, "category_name": "动画 / 短片", "category_reason": "原创动画故事"})
        self.compose = AsyncMock(side_effect=self.compose_result)
        self.create = AsyncMock(side_effect=lambda *_args: "task-" + str(self.create.await_count))
        self.query = AsyncMock(return_value="artifact")
        self.download = AsyncMock(side_effect=self.download_result)
        for name in ("create", "query", "download"):
            item = patch.object(auto.MiniMaxVideo, name, getattr(self, name))
            item.start()
            self.addCleanup(item.stop)
        self.worker = self.new_worker()
        self.worker.save_settings({"enabled": True, "api_key": "fake-key", "mode": "storyboard", "total_duration": 30,
                                   "duration": 10, "auto_category": True}, self.now)
        self.worker.enqueue(self.now, manual=True)

    def new_worker(self):
        return auto.AutoPublisher(self.path, self.single_plan, self.publish, lambda: self.account, lambda: False,
                                  self.category, verify_publication=self.verify, plan_storyboard=self.plan, compose=self.compose)

    def compose_result(self, data_dir, job_id, _durations, _max_bytes):
        result = data_dir / "media" / f"ai-{job_id}.mp4"
        result.write_bytes(b"mock composed video")
        return result

    def download_result(self, _artifact, _temporary, destination):
        destination.write_bytes(b"mock generated shot")

    async def drive(self, terminal=("published", "failed", "review")):
        for _ in range(50):
            await self.worker.tick(self.now)
            job = self.worker.jobs()[0]
            if job["phase"] in terminal:
                return job
        self.fail("Storyboard did not reach the requested state")

    async def test_full_pipeline_resumes_shots_and_uses_shared_character_and_whole_story_category(self):
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.worker = self.new_worker()
        result = await self.drive()
        self.assertEqual(result["phase"], "published")
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(self.download.await_count, 3)
        self.assertEqual(len(result["shots"]), 3)
        self.assertEqual(self.plan.await_args.args[2], [10, 10, 10])
        self.single_plan.assert_not_awaited()
        self.plan.assert_awaited_once()
        self.assertTrue(all("红色围巾" in call.args[0] for call in self.create.await_args_list))
        self.assertEqual(len(self.category.await_args.args[0]["shots"]), 3)
        self.assertEqual(self.publish.await_args.args[0]["tid"], 47)
        self.compose.assert_awaited_once()
        self.assertEqual(len(self.worker.jobs()), 1)
        with self.assertRaises(auto.PublishError):
            self.worker.enqueue(self.now, manual=True)

    async def test_duration_and_mode_are_pinned_at_enqueue(self):
        self.worker.save_settings({"mode": "single", "total_duration": 60, "duration": 6}, self.now)
        result = await self.drive()
        self.assertEqual((result["mode"], result["duration"], result["shot_durations"]), ("storyboard", 30, [10, 10, 10]))

    async def test_invalid_storyboard_never_starts_paid_generation(self):
        self.plan.side_effect = None
        self.plan.return_value = plan([10, 10])
        result = await self.drive()
        self.assertEqual(result["phase"], "failed")
        self.create.assert_not_awaited()

    async def test_ambiguous_create_and_interrupted_create_never_repeat(self):
        self.create.side_effect = asyncio.CancelledError()
        await self.worker.tick(self.now)
        with self.assertRaises(asyncio.CancelledError):
            await self.worker.tick(self.now)
        self.worker = self.new_worker()
        result = await self.drive()
        self.assertEqual(result["phase"], "review")
        self.create.assert_awaited_once()
        with self.assertRaises(auto.PublishError):
            self.worker.resume(result["id"])

    async def test_explicit_failure_retries_only_failed_shot_after_user_action(self):
        self.query.side_effect = ["artifact", auto.GenerationFailed("服务商明确失败"), "artifact", "artifact"]
        result = await self.drive()
        first_file = self.path / "media" / result["shots"][0]["file"]
        first_identity = auto.AutoPublisher._file_identity(first_file)
        self.assertEqual((result["phase"], result["retry_stage"]), ("failed", "shot"))
        await self.worker.tick(self.now)
        self.assertEqual(self.create.await_count, 2)
        self.worker.resume(result["id"])
        with self.assertRaises(auto.PublishError):
            self.worker.resume(result["id"])
        result = await self.drive()
        self.assertEqual(result["phase"], "published")
        self.assertEqual(self.create.await_count, 4)
        self.assertEqual(auto.AutoPublisher._file_identity(first_file), first_identity)
        self.plan.assert_awaited_once()

    async def test_query_access_error_is_not_treated_as_confirmed_generation_failure(self):
        self.query.side_effect = auto.PublishError("HTTP 403")
        result = await self.drive()
        self.assertEqual(result["phase"], "review")
        self.assertFalse(result.get("recoverable", False))
        self.create.assert_awaited_once()

    async def test_download_retry_keeps_provider_task_and_does_not_pay_again(self):
        async def download(*args):
            if self.download.await_count == 2:
                raise ConnectionError()
            return self.download_result(*args)
        self.download.side_effect = download
        result = await self.drive()
        original_id = result["shots"][1]["task_id"]
        self.assertEqual(result["retry_stage"], "download")
        self.worker.resume(result["id"])
        result = await self.drive()
        self.assertEqual(result["phase"], "published")
        self.assertEqual(result["shots"][1]["task_id"], original_id)
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(self.download.await_count, 4)

    async def test_composition_failure_reuses_all_finished_shots(self):
        async def compose(*args):
            if self.compose.await_count == 1:
                raise media.CompositionError("编码器暂时失败")
            return self.compose_result(*args)
        self.compose.side_effect = compose
        result = await self.drive()
        self.assertEqual(result["retry_stage"], "assemble")
        self.assertTrue(all(shot["phase"] == "done" for shot in result["shots"]))
        self.worker.resume(result["id"])
        result = await self.drive()
        self.assertEqual(result["phase"], "published")
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual(self.compose.await_count, 2)

    async def test_key_change_retains_completed_shots_and_stops_new_requests(self):
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.worker.save_settings({"api_key": "new-key"}, self.now)
        result = await self.drive()
        self.assertEqual(result["phase"], "review")
        self.assertEqual(result["shots"][0]["phase"], "done")
        self.create.assert_awaited_once()

    async def test_preflight_checks_again_before_each_paid_shot(self):
        await self.worker.tick(self.now)
        self.worker.preflight = AsyncMock(side_effect=auto.PublishError("登录已过期"))
        await self.worker.tick(self.now)
        self.create.assert_not_awaited()
        self.assertIn("登录已过期", self.worker.jobs()[0]["message"])
        self.assertTrue(self.worker.preflight.await_args.kwargs["storyboard"])

    async def test_disabled_during_composition_keeps_result_without_publishing(self):
        async def compose(*args):
            self.worker.save_settings({"enabled": False}, self.now)
            return self.compose_result(*args)
        self.compose.side_effect = compose
        result = await self.drive(terminal=("ready",))
        await self.worker.tick(self.now)
        self.publish.assert_not_awaited()
        self.assertTrue((self.path / "media" / result["file"]).is_file())

    async def test_verified_publication_cleans_final_and_original_shots(self):
        result = await self.drive()
        files = [self.path / "media" / result["file"], *[self.path / "media" / shot["file"] for shot in result["shots"]]]
        self.assertTrue(all(path.is_file() for path in files))
        self.verify.return_value = True
        await self.worker.tick(self.now)
        self.assertTrue(all(not path.exists() for path in files))
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "deleted")
        self.publish.assert_awaited_once()

    async def test_modified_shot_keeps_all_files_after_publication(self):
        result = await self.drive()
        first = self.path / "media" / result["shots"][0]["file"]
        first.write_bytes(b"manually modified original shot")
        self.verify.return_value = True
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "kept")
        self.assertTrue((self.path / "media" / result["file"]).is_file())


class StoryboardPlanningTests(unittest.TestCase):
    def test_supported_totals_use_minimal_valid_clip_counts(self):
        for provider, model, total, expected in (
            ("minimax", "MiniMax-Hailuo-2.3", 30, [10] * 3), ("minimax", "T2V-01", 60, [6] * 10),
            ("wan", "wan2.6-t2v", 60, [15] * 4), ("wan", "wan2.2-t2v-plus", 60, [5] * 12),
            ("ark", "doubao-seedance-2-5-260628", 30, [15] * 2),
        ):
            with self.subTest(model=model):
                self.assertEqual(auto.storyboard_lengths(provider, model, total), expected)
        for total in (True, 0, 31, 600):
            with self.assertRaises(auto.PublishError):
                auto.storyboard_lengths("minimax", "MiniMax-Hailuo-2.3", total)


class StoryboardMainTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_receives_whole_film_duration_and_exact_shot_lengths(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._provider_id = lambda: "text-model"
        generate = AsyncMock(return_value=type("Result", (), {"completion_text": plan([10, 10, 10])})())
        instance.context = type("Context", (), {"llm_generate": generate})()
        result = await instance._plan_auto_storyboard("大肥鱼吃饭", [], [10, 10, 10])
        self.assertEqual(len(auto.parse_storyboard(result, [10, 10, 10])["shots"]), 3)
        payload = json.loads(generate.await_args.kwargs["prompt"])
        self.assertEqual((payload["成片总时长"], payload["各分镜时长"]), (30, [10, 10, 10]))

    async def test_resume_route_validates_id_and_wakes_existing_worker(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._auto_publish = type("Worker", (), {"resume": lambda _self, _id: None})()
        instance._auto_publish_wake = asyncio.Event()
        for payload, valid in (({"id": "bad"}, False), ({"id": "a" * 32}, True)):
            request = type("Request", (), {"json": AsyncMock(return_value=payload)})()
            with patch.object(plugin, "request", request), patch.object(plugin, "json_response", lambda data: data), patch.object(plugin, "error_response", lambda message: {"error": message}):
                result = await instance._api_auto_publish_resume()
            self.assertEqual(result.get("resumed", False), valid)
        self.assertTrue(instance._auto_publish_wake.is_set())

    async def test_missing_ffprobe_is_detected_before_any_video_generation(self):
        instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._auto_publish_account = lambda: "42"
        with patch.object(plugin.bili_api, "load_credential", return_value=object()), patch.object(plugin.shutil, "which", side_effect=lambda name: "ffmpeg" if name == "ffmpeg" else None):
            with self.assertRaisesRegex(auto.PublishError, "ffprobe"):
                await instance._auto_publish_preflight("42", storyboard=True)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires local FFmpeg tools")
class RealCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ffmpeg_normalizes_resolution_frame_rate_audio_and_concatenates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media").mkdir()
            job_id = "a" * 32
            first, second = [root / "media" / f"ai-{job_id}-shot-{index:02d}.mp4" for index in (1, 2)]
            await media.run_media("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(first))
            await media.run_media("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=640x360:r=15:d=1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(second))
            result = await media.compose_video(root, job_id, [1, 1], 100 * 1024 * 1024)
            info = await media.probe_video(result)
            self.assertAlmostEqual(info["duration"], 2, delta=.2)
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1280, 720))
            self.assertTrue(info["audio"])
            self.assertTrue(first.is_file() and second.is_file())
            self.assertFalse((root / (".compose-" + job_id)).exists())

    async def test_unsafe_task_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(media.CompositionError):
                await media.compose_video(Path(directory), "../outside", [1, 1], 100000)
