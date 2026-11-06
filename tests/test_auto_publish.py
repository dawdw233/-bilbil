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
        self.worker = self.new_worker()
        self.create = AsyncMock(return_value="test-task")
        self.query = AsyncMock(return_value={"status": "Success", "file_id": "test-file"})
        self.download = AsyncMock(side_effect=lambda _id, _tmp, dest: dest.write_bytes(b"test MP4"))
        for name in ("create", "query", "download"):
            item = patch.object(auto_publish.MiniMaxVideo, name, getattr(self, name))
            item.start()
            self.addCleanup(item.stop)

    def new_worker(self):
        return auto_publish.AutoPublisher(
            self.path, self.plan, self.publish, lambda: self.account, lambda: self.busy,
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
        self.assertTrue(state["key_configured"])
        self.assertNotIn("fake-secret", json.dumps(state))
        self.worker.save_settings({"enabled": False, "remove_key": True}, self.now)
        self.assertFalse(self.worker.state()["key_configured"])
