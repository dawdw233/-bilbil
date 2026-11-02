"""Read-only daily aggregation against temporary records, with no real accounts."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_appearance import plugin


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 3, 13, 0, 0)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.data_dir = Path(self.directory.name)
        self.instance.cfg = {}
        self.instance._running = False
        self.instance._pending = lambda: {"items": [{"id": "one"}, {"id": "two"}]}
        self.schedule = {"max_wakes": 6, "wakes_used": 2, "next_at": "2026-11-03 18:35", "windows": [{"label": "09:00–15:00", "wakes": 3}]}
        self.instance._rhythm_summary = lambda: self.schedule
        for item in [patch.object(plugin, "datetime", FrozenDateTime), patch.object(plugin, "json_response", lambda value: value)]:
            item.start()
            self.addCleanup(item.stop)

    def write(self, name, content):
        (self.instance.data_dir / name).write_text(content, encoding="utf-8")

    async def test_today_counts_and_timeline_are_read_only(self):
        self.write("seen.json", json.dumps({"videos": {"a": {}, "b": {}}, "runs": [
            {"at": "2026-11-02T20:00:00", "picked": 8, "scanned": 9},
            {"at": "2026-11-03T09:00:00", "picked": 3, "scanned": 5},
            {"at": "2026-11-03T11:00:00", "picked": 2, "scanned": 4},
        ]}))
        self.write("comments.jsonl", "\n".join([json.dumps(row) for row in [
            {"at": "2026-11-03T10:00:00", "auto": True},
            {"at": "2026-11-03T10:30:00", "auto": True, "reply_to": {"id": 1}},
            {"at": "2026-11-03T10:40:00", "auto": False},
            {"at": "2026-11-02T10:00:00", "auto": True},
        ]]) + "\ninvalid-json")
        before = {p.name: p.read_bytes() for p in self.instance.data_dir.iterdir()}
        result = await self.instance._api_dashboard()
        self.assertEqual(result["stats"], {"today_runs": 2, "selected": 5, "auto_comments": 1, "pending": 2, "recorded": 2})
        self.assertEqual(result["progress"], {"completed": 2, "target": 6, "percent": 33})
        self.assertEqual([item["kind"] for item in result["timeline"]], ["done", "done", "next"])
        self.assertEqual(result["date"], "2026-11-03")
        self.assertIn("星期二", result["date_label"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.instance.data_dir.iterdir()})

    async def test_empty_day_shows_configured_windows_without_invented_runs(self):
        self.schedule.update({"wakes_used": 0, "next_at": ""})
        result = await self.instance._api_dashboard()
        self.assertEqual(result["stats"]["today_runs"], 0)
        self.assertEqual(result["stats"]["selected"], 0)
        self.assertEqual(result["timeline"][0]["kind"], "window")
        self.assertEqual(result["timeline"][0]["time"], "09:00")

    async def test_planned_rest_is_not_presented_as_a_video_run(self):
        self.schedule["action"] = "skip"
        result = await self.instance._api_dashboard()
        self.assertIn("休息", result["timeline"][-1]["title"])
        self.assertIn("不刷视频", result["timeline"][-1]["detail"])

    async def test_no_schedule_and_running_state(self):
        self.schedule = {"max_wakes": 0, "wakes_used": 0, "next_at": "", "windows": []}
        self.instance.cfg["speaking_style_bind_persona"] = False
        result = await self.instance._api_dashboard()
        self.assertEqual(result["progress"]["percent"], 0)
        self.assertEqual(result["timeline"], [])
        self.assertIs(result["style_bound"], False)
        self.instance._running = True
        self.assertEqual((await self.instance._api_dashboard())["timeline"][0]["kind"], "running")

    async def test_progress_is_clamped_and_broken_counters_do_not_fail(self):
        self.schedule.update({"wakes_used": 8})
        self.write("seen.json", json.dumps({"videos": [], "runs": [{"at": "2026-11-03T09:00", "picked": "bad", "scanned": -3}]}))
        result = await self.instance._api_dashboard()
        self.assertEqual(result["progress"]["percent"], 100)
        self.assertEqual(result["stats"]["selected"], 0)
        self.assertEqual(result["stats"]["recorded"], 0)


if __name__ == "__main__":
    unittest.main()
