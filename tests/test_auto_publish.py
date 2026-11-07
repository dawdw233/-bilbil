"""Exercise auto-publish without calling MiniMax or Bilibili."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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

    async def test_minimax_text_only_listing_explains_video_permission_is_unverified(self):
        self.configure()
        before = self.worker.settings_path.read_bytes()
        with patch.object(auto_publish.VideoClient, "_request", AsyncMock(return_value={"data": [{"id": "MiniMax-M3"}]})) as request:
            result = await self.worker.models({"provider": "minimax", "region": "cn"})
        self.assertEqual(result["models"], [])
        self.assertIn("未列出海螺", result["message"])
        self.assertIn("权限尚未验证", result["message"])
        self.assertEqual(request.await_args.args[1:], ("GET", "/models"))
        self.assertEqual(self.worker.settings_path.read_bytes(), before)
        self.create.assert_not_awaited()

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
        self.select_category.assert_awaited_once_with(auto_publish.parse_plan(PLAN))
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

    async def test_tags_are_normalized_before_generation_and_fit_upload_limit(self):
        from bilibili_api.video_uploader import VideoMeta
        from bilibili_api.utils.picture import Picture
        self.configure()
        self.plan.return_value = json.dumps({**json.loads(PLAN), "tags": "AI生成，鱼, 鱼；" + ",".join("标签" + str(i) for i in range(14))})
        self.worker.enqueue(self.now, manual=True)
        for _ in range(3):
            await self.worker.tick(self.now)
        tags = self.publish.await_args.args[0]["tags"].split(",")
        self.assertEqual((len(tags), tags.count("AI生成"), tags.count("鱼")), (10, 1, 1))
        VideoMeta(tid=21, title="test", desc="test", cover=Picture(), tags=tags)

    async def test_key_replacement_preserves_remote_task_for_review(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        self.worker.save_settings({"api_key": "another-account-key"}, self.now)
        await self.worker.tick(self.now)
        job = self.worker.jobs()[0]
        self.assertEqual((job["phase"], job["task_id"]), ("review", "test-task"))
        self.query.assert_not_awaited()
        self.create.assert_awaited_once()
        self.assertNotIn("key_revision", json.dumps(self.worker.state()))

    async def test_workspace_and_models_are_independent_across_services(self):
        self.configure(provider="wan", workspace="workspace-123", model="wan2.6-t2v")
        self.worker.save_settings({"provider": "minimax", "api_key": "minimax-key", "workspace": "", "model": "T2V-01"}, self.now)
        profiles = self.worker.state()["profiles"]
        self.assertEqual(profiles["wan:cn"]["workspace"], "workspace-123")
        self.assertEqual(profiles["wan:cn"]["model"], "wan2.6-t2v")
        self.assertEqual(profiles["minimax:cn"]["workspace"], "")
        self.assertNotIn("minimax-key", json.dumps(profiles))

    async def test_duration_is_saved_pinned_and_used_for_planning(self):
        self.configure(duration=10)
        self.worker.enqueue(self.now, manual=True)
        self.worker.save_settings({"duration": 6}, self.now)
        await self.worker.tick(self.now)
        self.assertEqual(self.plan.await_args.args[2], 10)
        self.assertEqual(self.worker.state()["jobs"][0]["duration"], 10)
        self.assertEqual(self.new_worker().jobs()[0]["duration"], 10)

    async def test_unsupported_duration_is_rejected_before_creating_a_job(self):
        for duration in (True, "10", 0, 7, 30):
            with self.subTest(duration=duration), self.assertRaises(auto_publish.PublishError):
                self.configure(duration=duration)
        self.create.assert_not_awaited()
        self.assertEqual(self.worker.jobs(), [])

    async def test_duration_profiles_and_legacy_jobs_keep_their_own_defaults(self):
        self.configure(duration=10)
        self.worker.enqueue(self.now, manual=True)
        rows = self.worker.jobs()
        rows[0].pop("duration")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        self.worker.save_settings({"provider": "wan", "api_key": "wan-key", "duration": 15}, self.now)
        self.assertEqual(self.worker.state()["profiles"]["minimax:cn"]["duration"], 10)
        self.assertEqual(self.worker.state()["profiles"]["wan:cn"]["duration"], 15)
        await self.worker.tick(self.now)
        self.assertEqual(self.plan.await_args.args[2], 6)

    async def test_model_change_does_not_keep_an_incompatible_duration(self):
        self.configure(duration=10)
        with self.assertRaises(auto_publish.PublishError):
            self.worker.save_settings({"model": "T2V-01", "duration": 10}, self.now)
        self.worker.save_settings({"model": "T2V-01"}, self.now)
        self.assertEqual(self.worker.state()["profiles"]["minimax:cn"]["duration"], 6)

    async def test_legacy_key_binding_happens_before_key_is_overwritten(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        rows = self.worker.jobs()
        rows[0].pop("key_revision")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        self.worker.save_settings({"api_key": "replacement-key"}, self.now)
        await self.new_worker().tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "review")
        self.query.assert_not_awaited()

    async def test_key_replacement_during_planning_never_starts_paid_request(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        async def plan(*_args):
            self.worker.save_settings({"api_key": "replacement-key"}, self.now)
            return PLAN
        self.plan.side_effect = plan
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "review")
        self.create.assert_not_awaited()

    async def test_queued_job_keeps_original_direction(self):
        self.configure(direction="原来的鱼")
        self.worker.enqueue(self.now, manual=True)
        self.worker.save_settings({"direction": "后来的猫"}, self.now)
        await self.worker.tick(self.now)
        self.assertEqual(self.plan.await_args.args[0], "原来的鱼")

    async def test_failed_preflight_waits_without_planning_or_generating_then_recovers(self):
        self.configure()
        preflight = AsyncMock(side_effect=auto_publish.PublishError("登录已过期，请重新扫码"))
        self.worker.preflight = preflight
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        self.assertIn("登录已过期", self.worker.state()["jobs"][0]["message"])
        self.plan.assert_not_awaited()
        self.create.assert_not_awaited()
        await self.worker.tick(self.now + timedelta(seconds=20))
        preflight.assert_awaited_once_with("42")
        preflight.side_effect = None
        await self.worker.tick(self.now + timedelta(minutes=1))
        self.create.assert_awaited_once()
        self.assertEqual(self.worker.jobs()[0]["message"], "")

    async def test_preflight_rechecks_login_before_publication_and_keeps_finished_video(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        self.worker.preflight = AsyncMock(side_effect=TimeoutError())
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["phase"], "ready")
        self.assertTrue((self.path / "media" / self.worker.jobs()[0]["file"]).is_file())
        self.publish.assert_not_awaited()

    async def test_preflight_disable_and_upload_race_do_not_submit(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        async def disable(_account):
            self.worker.save_settings({"enabled": False}, self.now)
        self.worker.preflight = disable
        await self.worker.tick(self.now)
        self.create.assert_not_awaited()
        self.worker.save_settings({"enabled": True}, self.now)
        self.worker.preflight = None
        await self.worker.tick(self.now)
        await self.worker.tick(self.now)
        async def busy(_account):
            self.busy = True
        self.worker.preflight = busy
        await self.worker.tick(self.now)
        self.publish.assert_not_awaited()

    async def test_failed_upload_explains_reason_keeps_file_and_does_not_retry(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        self.publish.return_value = {"success": False, "error": "标签不合法 token=secret https://example.com/?secret"}
        for _ in range(4):
            await self.worker.tick(self.now)
        job = self.worker.jobs()[0]
        self.assertEqual(job["phase"], "review")
        self.assertIn("标签不合法", job["message"])
        self.assertNotIn("secret", job["message"])
        self.assertTrue((self.path / "media" / job["file"]).is_file())
        self.publish.assert_awaited_once()

    async def prepare_cleanup(self):
        self.configure()
        self.worker.enqueue(self.now, manual=True)
        for _ in range(3):
            await self.worker.tick(self.now)
        self.worker.verify_publication = AsyncMock(return_value=True)
        return self.path / "media" / self.worker.jobs()[0]["file"]

    async def test_cleanup_requires_remote_confirmation_and_keeps_receipt_and_manual_files(self):
        media = await self.prepare_cleanup()
        manual = self.path / "media" / "my-video.mp4"
        manual.write_bytes(b"manual video")
        self.assertTrue(media.exists())
        self.assertEqual(self.worker.jobs()[0]["phase"], "published")
        await self.worker.tick(self.now)
        self.assertFalse(media.exists())
        self.assertTrue(manual.exists())
        job = self.worker.state()["jobs"][0]
        self.assertEqual((job["phase"], job["cleanup_state"], job["bvid"]), ("published", "deleted", "BV1234567890"))
        self.assertIn("已清理", job["message"])
        self.publish.assert_awaited_once()

    async def test_pending_review_and_network_errors_keep_file_then_retry(self):
        media = await self.prepare_cleanup()
        verify = self.worker.verify_publication
        verify.side_effect = [False, TimeoutError(), True]
        await self.worker.tick(self.now)
        await self.worker.tick(self.now + timedelta(seconds=20))
        verify.assert_awaited_once()
        self.assertTrue(media.exists())
        await self.worker.tick(self.now + timedelta(minutes=1))
        self.assertTrue(media.exists())
        await self.worker.tick(self.now + timedelta(minutes=2))
        self.assertFalse(media.exists())
        self.publish.assert_awaited_once()

    async def test_failed_or_unconfirmed_upload_is_never_cleaned(self):
        for response in ({"success": False, "bvid": "BV1234567890"}, {"success": True, "bvid": "bad"}, {"success": "false", "bvid": "BV1234567890"}):
            with self.subTest(response=response):
                self.worker.jobs_path.unlink(missing_ok=True)
                self.configure()
                self.worker.enqueue(self.now, manual=True)
                self.publish.return_value = response
                self.worker.verify_publication = AsyncMock(return_value=True)
                for _ in range(4):
                    await self.worker.tick(self.now)
                job = self.worker.jobs()[0]
                self.assertEqual(job["phase"], "review")
                self.assertTrue((self.path / "media" / job["file"]).exists())
                self.worker.verify_publication.assert_not_awaited()

    async def test_old_published_jobs_are_not_retroactively_cleaned(self):
        media = await self.prepare_cleanup()
        rows = self.worker.jobs()
        rows[0].pop("cleanup_after_publish")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        await self.worker.tick(self.now)
        self.assertTrue(media.exists())
        self.worker.verify_publication.assert_not_awaited()

    async def test_cleanup_resumes_after_restart_and_after_file_removal_before_record_write(self):
        media = await self.prepare_cleanup()
        self.worker = self.new_worker()
        self.worker.verify_publication = AsyncMock(return_value=True)
        media.unlink()
        await self.worker.tick(self.now)
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "deleted")
        self.publish.assert_awaited_once()

    async def test_modified_file_is_kept(self):
        media = await self.prepare_cleanup()
        media.write_bytes(b"manually replaced video with different content")
        await self.worker.tick(self.now)
        self.assertTrue(media.exists())
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "kept")

    async def test_foreign_path_and_symlink_are_never_deleted(self):
        media = await self.prepare_cleanup()
        outside = self.path / "manual.mp4"
        outside.write_bytes(b"manual")
        rows = self.worker.jobs()
        rows[0]["file"] = "../manual.mp4"
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        await self.worker.tick(self.now)
        self.assertTrue(outside.exists())
        self.assertTrue(media.exists())
        rows = self.worker.jobs()
        rows[0].update(file=media.name, cleanup_state="pending")
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        media.unlink()
        media.symlink_to(outside)
        await self.worker.tick(self.now)
        self.assertTrue(outside.exists())
        self.assertTrue(media.is_symlink())
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "kept")

    async def test_permission_failure_retries_cleanup_without_uploading_again(self):
        media = await self.prepare_cleanup()
        unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path == media:
                raise PermissionError()
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", denied):
            await self.worker.tick(self.now)
        self.assertTrue(media.exists())
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "pending")
        await self.worker.tick(self.now + timedelta(minutes=1))
        self.assertFalse(media.exists())
        self.publish.assert_awaited_once()

    async def test_cleanup_does_not_overwrite_job_enqueued_during_remote_check(self):
        media = await self.prepare_cleanup()
        self.worker.save_settings({"limit": 2}, self.now)
        async def concurrent(_job):
            self.worker.enqueue(self.now, manual=True)
            self.worker.save_settings({"enabled": False}, self.now)
            return True
        self.worker.verify_publication.side_effect = concurrent
        await self.worker.tick(self.now)
        self.assertFalse(media.exists())
        rows = self.worker.jobs()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["cleanup_state"], rows[1]["phase"]), ("deleted", "queued"))

    async def test_account_switch_during_cleanup_check_keeps_the_video(self):
        media = await self.prepare_cleanup()
        async def switched(_job):
            self.account = "88"
            return True
        self.worker.verify_publication.side_effect = switched
        await self.worker.tick(self.now)
        self.assertTrue(media.exists())
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "pending")

    async def test_other_upload_starting_during_cleanup_check_defers_deletion(self):
        media = await self.prepare_cleanup()
        async def busy(_job):
            self.busy = True
            return True
        self.worker.verify_publication.side_effect = busy
        await self.worker.tick(self.now)
        self.assertTrue(media.exists())
        self.assertEqual(self.worker.jobs()[0]["cleanup_state"], "pending")

    async def test_other_accounts_pending_video_does_not_block_current_accounts_cleanup(self):
        original = await self.prepare_cleanup()
        rows = self.worker.jobs()
        other_id = "f" * 32
        current_video = self.path / "media" / f"ai-{other_id}.mp4"
        current_video.write_bytes(b"another generated video")
        rows.append({**rows[0], "id": other_id, "account": "88", "file": current_video.name,
                     "bvid": "BV0987654321", "cleanup_identity": self.worker._file_identity(current_video)})
        self.worker.jobs_path.write_text(json.dumps(rows), encoding="utf-8")
        self.account = "88"
        await self.worker.tick(self.now)
        self.assertTrue(original.exists())
        self.assertFalse(current_video.exists())
        self.assertEqual(self.worker.verify_publication.await_args.args[0]["account"], "88")


class PreflightContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(directory.name)
        (self.instance.data_dir / "media").mkdir()
        self.instance._auto_publish_account = lambda: "42"
        self.credentials = patch.object(plugin.bili_api, "load_credential", return_value=object())
        self.credentials.start()
        self.addCleanup(self.credentials.stop)

    async def test_missing_ffmpeg_stops_before_login_or_paid_work(self):
        with patch.object(plugin.shutil, "which", return_value=None), patch.object(plugin.bili_api, "_cred_alive_sync") as login:
            with self.assertRaisesRegex(auto_publish.PublishError, "ffmpeg"):
                await self.instance._auto_publish_preflight("42")
        login.assert_not_called()

    async def test_cleanup_verification_requires_correct_video_owner_normal_state_and_pages(self):
        job = {"bvid": "BV1234567890", "account": "42"}
        normal = {"bvid": job["bvid"], "state": 0, "owner": {"mid": 42}, "pages": [{"cid": 123}]}
        for info, expected in ((normal, True), ({**normal, "state": -40}, False), ({**normal, "state": False}, False),
                               ({**normal, "owner": {"mid": 88}}, False), ({**normal, "bvid": "BV0000000000"}, False),
                               ({**normal, "pages": []}, False), ({**normal, "pages": [{"cid": 0}]}, False)):
            with self.subTest(info=info), patch.object(plugin.bili_api.video, "Video", return_value=Mock(get_info=AsyncMock(return_value=info))):
                self.assertEqual(await self.instance._verify_auto_publication(job), expected)

    async def test_low_disk_space_and_unwritable_directory_stop_preflight(self):
        with patch.object(plugin.shutil, "which", return_value="ffmpeg"), patch.object(plugin.shutil, "disk_usage", return_value=Mock(free=1)):
            with self.assertRaisesRegex(auto_publish.PublishError, "空间"):
                await self.instance._auto_publish_preflight("42")
        with patch.object(plugin.shutil, "which", return_value="ffmpeg"), patch.object(plugin.tempfile, "TemporaryFile", side_effect=PermissionError()):
            with self.assertRaisesRegex(auto_publish.PublishError, "不可写"):
                await self.instance._auto_publish_preflight("42")

    async def test_login_must_be_strictly_confirmed(self):
        with patch.object(plugin.shutil, "which", return_value="ffmpeg"), patch.object(plugin.bili_api, "_cred_alive_sync", return_value=(False, "登录已过期")) as login:
            with self.assertRaisesRegex(auto_publish.PublishError, "登录已过期"):
                await self.instance._auto_publish_preflight("42")
        self.assertTrue(login.call_args.kwargs["strict"])

    async def test_strict_login_distinguishes_expiry_network_limit_and_other_account(self):
        from bilibili_api import Credential
        credential = Credential(sessdata="fake", bili_jct="fake", dedeuserid="42")
        for response in ({"code": -101}, {"code": -412}, {"code": 0, "data": {"isLogin": True, "mid": 43}}):
            with patch("curl_cffi.requests.get", return_value=Mock(json=lambda: response)):
                self.assertFalse(plugin.bili_api._cred_alive_sync(credential, strict=True)[0])
        with patch("curl_cffi.requests.get", side_effect=TimeoutError()):
            self.assertFalse(plugin.bili_api._cred_alive_sync(credential, strict=True)[0])
            self.assertTrue(plugin.bili_api._cred_alive_sync(credential)[0])
        with patch("curl_cffi.requests.get", return_value=Mock(json=lambda: {"code": 0, "data": {"isLogin": True, "mid": 42}})):
            self.assertTrue(plugin.bili_api._cred_alive_sync(credential, strict=True)[0])

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
    async def test_selected_duration_reaches_all_provider_requests(self):
        for provider, model, duration in (("minimax", "MiniMax-Hailuo-2.3", 10), ("ark", "doubao-seedance-2-5-260628", 30), ("wan", "wan2.6-t2v", 15)):
            with self.subTest(provider=provider):
                client = auto_publish.VIDEO_CLIENTS[provider](provider, "cn", "fake-key", model, duration=duration)
                request = AsyncMock(return_value={"task_id": "task", "id": "task", "output": {"task_id": "task"}})
                with patch.object(client, "_request", request):
                    await client.create("鱼吃饭")
                body = request.await_args.kwargs["json"]
                self.assertEqual((body["parameters"] if provider == "wan" else body)["duration"], duration)

    async def test_model_specific_duration_limits(self):
        for provider, model, duration in (("minimax", "T2V-01", 10), ("wan", "wan2.5-t2v-preview", 15), ("wan", "wan2.2-t2v-plus", 10), ("ark", "doubao-seedance-2-0-260128", 30)):
            with self.subTest(model=model), self.assertRaises(auto_publish.PublishError):
                auto_publish.VIDEO_CLIENTS[provider](provider, "cn", "fake-key", model, duration=duration)

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

    async def test_minimax_listing_rejects_http_200_api_errors_without_exposing_response(self):
        client = auto_publish.MiniMaxVideo("minimax", "cn", "fake-key", "T2V-01")
        for response in (
            {"base_resp": {"status_code": 1004, "status_msg": "sensitive-response"}, "data": []},
            {"error": {"message": "sensitive-response"}, "data": []},
            {"base_resp": None, "data": []},
            {"base_resp": {"status_code": False}, "data": []},
        ):
            with self.subTest(response=response), patch.object(auto_publish.VideoClient, "_request", AsyncMock(return_value=response)):
                with self.assertRaises(auto_publish.PublishError) as error:
                    await client.models()
                self.assertNotIn("sensitive-response", str(error.exception))
        with patch.object(auto_publish.VideoClient, "_request", AsyncMock(return_value={"base_resp": {"status_code": 0}, "data": []})):
            self.assertEqual(await client.models(), [])

    async def test_wan_model_listing_reads_all_pages_and_filters_unsupported_protocols(self):
        client = auto_publish.WanVideo("wan", "cn", "fake-key", "wan2.6-t2v", "workspace-123")
        self.assertEqual(client.models_url, "https://workspace-123.cn-beijing.maas.aliyuncs.com/api/v1")
        self.assertEqual(client.base_url, "https://dashscope.aliyuncs.com/api/v1")
        request = AsyncMock(side_effect=[
            {"output": {"total": 101, "models": [{"model": "wan2.6-t2v"}] * 100}},
            {"output": {"total": 101, "models": [{"model": "wan2.5-t2v-preview"}, {"model": "wan3.0-video"}]}},
        ])
        with patch.object(auto_publish.VideoClient, "_request", request):
            models = await client.models()
        self.assertEqual([item["id"] for item in models], ["wan2.6-t2v", "wan2.5-t2v-preview"])
        self.assertEqual(request.await_args_list[1].kwargs["params"]["page_no"], 2)

    async def test_wan_workspace_required_only_for_listing_and_rejects_urls(self):
        client = auto_publish.WanVideo("wan", "global", "fake-key", "wan2.6-t2v")
        with self.assertRaisesRegex(auto_publish.PublishError, "业务空间"):
            await client.models()
        for value in ("https://evil.example", "x.evil", "abc/../", "a" * 64):
            with self.assertRaises(auto_publish.PublishError):
                auto_publish.WanVideo("wan", "cn", "fake-key", "wan2.6-t2v", value)
