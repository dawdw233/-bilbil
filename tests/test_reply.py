"""reply.py 的纯逻辑单测：模式解析、去重、日上限、失败重试与草稿文案。

这一层不碰 astrbot / bili_api，所以可以脱离容器单独跑：
    python -m unittest tests.test_reply -v
"""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reply


class ParseModeTests(unittest.TestCase):
    def test_valid_modes_pass_through(self):
        for mode in reply.MODES:
            with self.subTest(mode=mode):
                self.assertEqual(reply.parse_mode(mode), mode)

    def test_bad_values_fall_back_to_draft(self):
        for raw in (None, "", "   ", "auto watch", "发送", 0, False, []):
            with self.subTest(raw=raw):
                self.assertEqual(reply.parse_mode(raw), "draft")

    def test_strip_and_lowercase(self):
        self.assertEqual(reply.parse_mode("  Auto \n"), "auto")


class BvidTests(unittest.TestCase):
    def test_uri_with_query(self):
        self.assertEqual(
            reply.bvid_from_uri("https://www.bilibili.com/video/BV1nExY68EgS?p=2&t=3"),
            "BV1nExY68EgS",
        )

    def test_bare_bvid_and_extra_text(self):
        self.assertEqual(reply.bvid_from_uri("BV1nExY68EgS"), "BV1nExY68EgS")
        self.assertEqual(reply.bvid_from_uri("看这个 BV1nExY68EgS 哈哈"), "BV1nExY68EgS")

    def test_short_link_and_junk_have_no_bvid(self):
        for raw in (None, "", "https://b23.tv/abcdEFG", "https://www.bilibili.com/video/av123"):
            with self.subTest(raw=raw):
                self.assertEqual(reply.bvid_from_uri(raw), "")


class DailyLeftTests(unittest.TestCase):
    def test_negative_cap_means_unlimited(self):
        self.assertGreaterEqual(reply.daily_left(0, -1), 10**6)
        self.assertGreaterEqual(reply.daily_left(999, -1), 10**6)

    def test_zero_cap_blocks_everything(self):
        self.assertEqual(reply.daily_left(0, 0), 0)
        self.assertEqual(reply.daily_left(5, 0), 0)

    def test_counts_down_to_zero(self):
        self.assertEqual(reply.daily_left(0, 3), 3)
        self.assertEqual(reply.daily_left(2, 3), 1)
        self.assertEqual(reply.daily_left(3, 3), 0)
        self.assertEqual(reply.daily_left(9, 3), 0)

    def test_tolerates_strings_and_negatives(self):
        self.assertEqual(reply.daily_left("1", "5"), 4)
        self.assertEqual(reply.daily_left(-3, 3), 3)
        self.assertEqual(reply.daily_left(None, 3), 3)

    def test_unparsable_cap_fails_closed(self):
        self.assertEqual(reply.daily_left(0, None), 0)
        self.assertEqual(reply.daily_left(0, "abc"), 0)


class DedupeTests(unittest.TestCase):
    def _items(self, *keys):
        return [{"key": key} for key in keys]

    def test_filters_seen_and_repeats(self):
        fresh = reply.dedupe_new(self._items("a", "b", "a", "c"), ["b"])
        self.assertEqual([it["key"] for it in fresh], ["a", "c"])

    def test_accepts_dict_seen_and_skips_missing_key(self):
        fresh = reply.dedupe_new(self._items("a", "", "b"), {"a": 1})
        self.assertEqual([it["key"] for it in fresh], ["b"])

    def test_handles_empty_input(self):
        self.assertEqual(reply.dedupe_new([], ["a"]), [])
        self.assertEqual(reply.dedupe_new(None, None), [])

    def test_cap_seen_keeps_newest_tail(self):
        keys = [str(i) for i in range(10)]
        self.assertEqual(reply.cap_seen(keys, 3), ["7", "8", "9"])
        self.assertEqual(reply.cap_seen(keys + [""], 3), ["7", "8", "9"])
        self.assertEqual(reply.cap_seen([], 5), [])


class FeedItemTests(unittest.TestCase):
    PAYLOAD = {
        "items": [
            {
                "user": {"nickname": "后来的", "mid": 7},
                "item": {
                    "type": "reply",
                    "root_id": 11,
                    "source_id": 22,
                    "uri": "https://www.bilibili.com/video/BV1nExY68EgS",
                    "source_content": "你说得对",
                    "root_reply_content": "我的评论",
                },
                "reply_time": 200,
            },
            {
                "user": {"nickname": "先来的", "mid": "9"},
                "item": {"type": "reply", "root_id": 12, "source_id": 23, "source_content": "早"},
                "reply_time": 100,
            },
            {  # @ 我的走另一条线，这里要丢掉
                "user": {"nickname": "艾特"},
                "item": {"type": "at", "root_id": 0, "source_id": 33},
            },
            {  # 没有 root 没法去重
                "user": {"nickname": "无根"},
                "item": {"type": "reply", "root_id": 0, "source_id": 44},
            },
            "不是字典",
        ]
    }

    def test_filters_sorts_and_normalizes(self):
        items = reply.feed_items(self.PAYLOAD)
        self.assertEqual([it["key"] for it in items], ["12:23", "11:22"])
        first = items[1]
        self.assertEqual(first["bvid"], "BV1nExY68EgS")
        self.assertEqual(first["who"], "后来的")
        self.assertEqual(first["mid"], 7)
        self.assertEqual(first["their_words"], "你说得对")
        self.assertEqual(first["my_comment"], "我的评论")
        self.assertEqual(first["kind"], "reply")
        self.assertTrue(first["at"])

    def test_accepts_json_string(self):
        items = reply.feed_items(json.dumps(self.PAYLOAD, ensure_ascii=False))
        self.assertEqual(len(items), 2)

    def test_bad_payloads_return_empty(self):
        for raw in (None, "", "{", "[]", {"items": "x"}, {"items": [1, {"item": 2}]}):
            with self.subTest(raw=raw):
                self.assertEqual(reply.feed_items(raw), [])

    def test_at_items_use_zero_root_key(self):
        payload = {
            "items": [
                {
                    "user": {"nickname": "甲", "mid": 5},
                    "item": {
                        "type": "reply",
                        "source_id": 33,
                        "uri": "https://www.bilibili.com/video/BV1nExY68EgS",
                        "source_content": "@你 看看",
                        "at_time": 321,
                    },
                },
                {"user": {"nickname": "缺 source"}, "item": {"type": "reply"}},
            ]
        }
        items = reply.at_items(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], "0:33")
        self.assertEqual(items[0]["kind"], "at")
        self.assertEqual(items[0]["root"], 0)
        self.assertEqual(items[0]["mid"], 5)
        self.assertEqual(items[0]["ts"], 321)


class ParseDraftTests(unittest.TestCase):
    def test_empty_is_retryable(self):
        self.assertEqual(reply.parse_draft_ex(""), (False, "", "模型没返回内容", True))
        self.assertEqual(reply.parse_draft_ex(None)[3], True)

    def test_non_json_is_retryable(self):
        want, text, why, retryable = reply.parse_draft_ex("我觉得可以回一句")
        self.assertFalse(want)
        self.assertEqual(text, "")
        self.assertIn("不是 JSON", why)
        self.assertTrue(retryable)

    def test_no_want_is_final(self):
        self.assertEqual(
            reply.parse_draft_ex('{"want": false, "why": "不想接"}'),
            (False, "", "不想接", False),
        )

    def test_want_without_text_is_retryable(self):
        self.assertEqual(
            reply.parse_draft_ex('{"want": true, "text": "   "}'),
            (False, "", "说要回但正文是空的", True),
        )

    def test_prefixed_json_and_string_want(self):
        raw = '好的：\n{"want": "yes", "text": "确实如此", "why": "接一句"}'
        self.assertEqual(reply.parse_draft_ex(raw), (True, "确实如此", "接一句", False))

    def test_at_sign_is_rejected(self):
        want, text, why, retryable = reply.parse_draft_ex('{"want": true, "text": "@你 好啊"}')
        self.assertFalse(want)
        self.assertEqual(text, "")
        self.assertIn("@", why)
        self.assertFalse(retryable)

    def test_text_is_trimmed_and_quotes_stripped(self):
        want, text, _, _ = reply.parse_draft_ex('{"want": true, "text": "“好的”"}')
        self.assertTrue(want)
        self.assertEqual(text, "好的")

    def test_long_text_is_cut(self):
        long = "啊" * (reply.REPLY_MAX_LEN + 50)
        want, text, _, _ = reply.parse_draft_ex(json.dumps({"want": True, "text": long}))
        self.assertTrue(want)
        self.assertEqual(len(text), reply.REPLY_MAX_LEN)

    def test_parse_draft_wrapper(self):
        self.assertEqual(reply.parse_draft('{"want": true, "text": "嗯"}'), (True, "嗯", ""))
        self.assertEqual(reply.parse_draft("垃圾"), (False, "", "返回的不是 JSON（垃圾）"))


class RetryCounterTests(unittest.TestCase):
    def test_increments_and_clears(self):
        state = {}
        self.assertEqual(reply.note_failure(state, "1:2"), 1)
        self.assertEqual(reply.note_failure(state, "1:2"), 2)
        self.assertEqual(reply.note_failure(state, ""), 0)
        reply.clear_retry(state, ["1:2"])
        self.assertNotIn("retry", state)

    def test_bucket_is_capped(self):
        state = {}
        for i in range(reply.RETRY_LIMIT + 25):
            reply.note_failure(state, f"k{i}")
        self.assertLessEqual(len(state["retry"]), reply.RETRY_LIMIT)

    def test_clear_retry_keeps_other_keys(self):
        state = {"retry": {"a": 1, "b": 2}}
        reply.clear_retry(state, ["a"])
        self.assertEqual(state["retry"], {"b": 2})


class OncePerPersonTests(unittest.TestCase):
    def test_marking_then_blocking(self):
        state = {}
        reply.mark_replied(state, 7)
        self.assertTrue(reply.replied_recently(state, 7))
        self.assertIn("天内已经回过", reply.once_block(state, 7))

    def test_expired_entries_do_not_block(self):
        old = datetime.now() - timedelta(days=reply.ONCE_DAYS + 1)
        state = {}
        reply.mark_replied(state, 7, at=old)
        self.assertEqual(reply.replied_recently(state, 7), "")
        self.assertEqual(reply.once_block(state, 7), "")

    def test_owner_and_disabled_rule_are_exempt(self):
        state = {}
        reply.mark_replied(state, 42)
        self.assertEqual(reply.once_block(state, 42, owner_mid="42"), "")
        self.assertEqual(reply.once_block(state, 42, days=0), "")
        self.assertEqual(reply.once_block(state, ""), "")

    def test_history_is_trimmed(self):
        state = {}
        for _ in range(30):
            reply.mark_replied(state, 7, keep=5)
        self.assertEqual(len(state["replied"]["7"]), 5)


class DraftTextTests(unittest.TestCase):
    def _item(self):
        return reply.new_item(
            {
                "bvid": "BV1nExY68EgS",
                "root": 11,
                "source": 22,
                "mid": 7,
                "who": "路人甲",
                "their_words": "长" * 300,
                "my_comment": "我" * 300,
            },
            "回一句",
            "a1b2c3",
        )

    def test_new_item_shape_and_truncation(self):
        item = self._item()
        self.assertEqual(item["id"], "a1b2c3")
        self.assertEqual(item["mid"], "7")
        self.assertEqual(len(item["their_words"]), 200)
        self.assertEqual(len(item["my_comment"]), 200)
        self.assertEqual(item["text"], "回一句")

    def test_texts_use_real_newlines(self):
        brief = reply.pending_brief([self._item()])
        self.assertIn("a1b2c3", brief)
        self.assertIn("\n", brief)
        self.assertNotIn("\\n", brief)

        push = reply.pending_push_text([self._item()])
        self.assertIn("BV1nExY68EgS", push)
        self.assertIn("\n", push)
        self.assertNotIn("\\n", push)

        manual = reply.manual_push_text(self._item())
        self.assertIn("发 a1b2c3", manual)
        self.assertIn("\n", manual)
        self.assertNotIn("\\n", manual)

    def test_empty_pending_text(self):
        self.assertEqual(reply.pending_brief([]), "（没有待审的回复）")


if __name__ == "__main__":
    unittest.main()
