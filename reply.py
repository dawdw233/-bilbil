"""回复楼中楼的纯逻辑 —— 不 import astrbot / bili_api，方便单测。

天上掉下来的东西（B站 消息中心返回、模型写的 JSON）一律当**不可信内容**：
这里每一步都只做「整理 / 过滤 / 隔离」，真正的发送在 main.py 里、要过闸门。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

# 回复模式：off=不检测；draft=先给使用者看、他点了才发（whale 的 B 档）；auto=自己发
MODES = ("off", "draft", "auto")
DEFAULT_MODE = "draft"

# 一条回复最多这么长；和评论的兜底闸（duty.COMMENT_BLOCKLIST / 120 字）对齐
REPLY_MAX_LEN = 120
# 一批最多处理几条新回复，防止某次开机把积压全处理了
MAX_PER_CYCLE = 5
# 状态里最多记多少条「见过」，超了就丢最老的（只是防文件无限长，不影响判重正确性）
SEEN_LIMIT = 2000
# 一条回复连续几次「没跑出结论」（LLM 报错 / 返回不可解析）就放弃。
# 有上限才不会在模型坏掉时每轮重试到天荒地老；3 次够扛住偶发抽风。
MAX_RETRY = 3
# 失败计数最多留多少条（只影响状态文件大小，不影响判重）
RETRY_LIMIT = 200

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def parse_mode(raw) -> str:
    """配置里写错了就退回 draft（宁可多问一次，也不悄悄开始公开回复）。"""
    value = str(raw or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def bvid_from_uri(uri) -> str:
    """从消息中心的 uri（https://www.bilibili.com/video/BVxxx）里抠 BV 号。"""
    text = str(uri or "")
    if "/video/" in text:
        tail = text.rsplit("/video/", 1)[-1].split("?")[0].strip("/")
        if _BVID_RE.fullmatch(tail):
            return tail
    match = _BVID_RE.search(text)
    return match.group(0) if match else ""


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def feed_items(payload) -> list[dict]:
    """把 bili_received_replies 的返回整理成统一结构。

    只保留「回复我的」里 type=reply 的条目；@我的（type=at）先不处理 ——
    那是另一条产品线（要不要回 @ 得单独想），混在一起会让去重键失去意义。

    返回每项：
        key      去重键（root:source）
        bvid / root / source / who / mid
        their_words   对方说了什么
        my_comment    被回复的那条（通常是它自己发的）
        at            本地时间字符串
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = raw.get("item") or {}
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind and kind != "reply":
            continue
        root = _as_int(item.get("root_id"))
        source = _as_int(item.get("source_id"))
        if root <= 0 or source <= 0:
            continue
        user = raw.get("user") or {}
        ts = _as_int(raw.get("reply_time"))
        out.append(
            {
                "key": f"{root}:{source}",
                "kind": "reply",
                "id": str(raw.get("id") or source),
                "bvid": bvid_from_uri(item.get("uri") or item.get("native_uri")),
                "root": root,
                "source": source,
                "who": str((user or {}).get("nickname") or "").strip()[:40],
                "mid": _as_int(user.get("mid")),
                "their_words": str(item.get("source_content") or "").strip(),
                "my_comment": str(item.get("root_reply_content") or "").strip(),
                "at": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                "ts": ts,
            }
        )
    # 老的在前：先回早的，语气才接得上
    out.sort(key=lambda x: x.get("ts") or 0)
    return out


def at_items(payload) -> list[dict]:
    """把「@ 我的」（`get_at`）整理成和 `feed_items` 一样的结构。

    和「回复我的」的区别：@ 是直接发在评论区的，**没有 root**（`root_id=0`），
    所以去重键用 `0:source`；发送时 `bili_reply` 会把 rpid 当成 parent，正好就是
    "回这条评论"。`kind="at"` 让提示词那边换个说法（不是在接某条回复）。

    这里**不按 type 过滤**：`get_at` 那一栏本身就是「@ 我」，条目 type 实测也是
    `reply`（不是 `at`），按 type 判会把真的 @ 一并丢掉。
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("items")
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        item = raw.get("item") or {}
        if not isinstance(item, dict):
            continue
        source = _as_int(item.get("source_id"))
        if source <= 0:  # 没有 source 就不知道回哪条，只能丢
            continue
        user = raw.get("user") or {}
        ts = _as_int(item.get("at_time") or raw.get("at_time") or raw.get("reply_time"))
        out.append(
            {
                "key": f"0:{source}",
                "kind": "at",
                "id": str(raw.get("id") or source),
                "bvid": bvid_from_uri(item.get("uri") or item.get("native_uri")),
                "root": 0,
                "source": source,
                "who": str((user or {}).get("nickname") or "").strip()[:40],
                "mid": _as_int(user.get("mid")),
                "their_words": str(item.get("source_content") or "").strip(),
                "my_comment": "",
                "at": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                "ts": ts,
            }
        )
    out.sort(key=lambda x: x.get("ts") or 0)
    return out


def dedupe_new(items: list[dict], seen) -> list[dict]:
    """挑出没见过的（seen 可以是 list / set / dict 的键集）。"""
    known = set(seen or [])
    out = []
    for item in items or []:
        key = str(item.get("key") or "")
        if not key or key in known:
            continue
        known.add(key)
        out.append(item)
    return out


def cap_seen(keys, limit: int = SEEN_LIMIT) -> list[str]:
    """只留最近 limit 条（新的一定在列表尾部 —— 调用方负责按时间追加）。"""
    seq = [str(k) for k in (keys or []) if k]
    return seq[-limit:]


def parse_draft_ex(raw) -> tuple[bool, str, str, bool]:
    """解析模型返回的 {"want","why","text"} → (想不想回, 正文, 理由, 要不要重试)。

    前三位和 parse_draft 一样；第 4 位是给调用方的信号：

        True  = 这次**没拿到结论**（模型没回内容 / 不是 JSON / 说要回但正文是空的），
                调用方别把这条记成「已处理」，下一轮还该再试。
        False = 有结论（回 / 不回 / 内容不合规），记成已见，不再重试。

    分这两种是为了不再「一次 LLM 抽风 = 永久丢掉一条回复」：以前把 seen 写在
    调模型之前，模型一报错那条就再也不会被看一眼。
    """
    text = str(raw or "").strip()
    if not text:
        return False, "", "模型没返回内容", True
    data = None
    if "{" in text and "}" in text:
        body = text[text.find("{") : text.rfind("}") + 1]
        try:
            data = json.loads(body)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return False, "", f"返回的不是 JSON（{text[:30]}）", True
    want = data.get("want")
    if isinstance(want, str):
        want = want.strip().lower() in ("true", "yes", "1", "想", "想回", "回")
    why = str(data.get("why") or "").strip()[:80]
    body = str(data.get("text") or "").strip().strip('"“”').strip()
    if not want:
        return False, "", why or "不想回", False
    if not body:
        return False, "", "说要回但正文是空的", True
    # 回复里一律不许带 @：不 @ 使用者，也不 @ 对方（2026-10-26 使用者明确要求）。
    # 楼中楼的层级本身就说明了"回的是谁"，加 @ 只会像在引流。
    if "@" in body:
        return False, "", "回复里不能带 @", False
    return True, body[:REPLY_MAX_LEN], why, False


def parse_draft(raw) -> tuple[bool, str, str]:
    """只要 (想不想回, 回复正文, 理由)；需要「要不要重试」用 parse_draft_ex。

    解析不出来一律当「不回」（fail closed）：宁可漏一条，
    也不把模型的自言自语当成公开回复发出去。
    """
    want, text, why, _retryable = parse_draft_ex(raw)
    return want, text, why


def note_failure(state: dict, key: str, *, limit: int = MAX_RETRY) -> int:
    """记一次「这条没跑出结论」，返回已经连续失败几次。

    调用方按 `>= limit` 决定放弃 —— 放弃时才把它记成已见。
    失败计数只留最近 RETRY_LIMIT 条：消息中心会把老回复挤掉，那种条目的计数
    再也不会被 clear，不封顶就会一直长。
    """
    if not key:
        return 0
    bucket = state.get("retry")
    if not isinstance(bucket, dict):
        bucket = {}
        state["retry"] = bucket
    count = int(bucket.get(str(key)) or 0) + 1
    bucket[str(key)] = count
    if len(bucket) > RETRY_LIMIT:
        for stale in list(bucket)[: len(bucket) - RETRY_LIMIT]:
            bucket.pop(stale, None)
    return count


def clear_retry(state: dict, keys) -> dict:
    """这些 key 已经有结论了，把失败计数清掉（别让状态文件一直长）。"""
    bucket = state.get("retry")
    if not isinstance(bucket, dict):
        return state
    for key in keys or ():
        bucket.pop(str(key), None)
    if not bucket:
        state.pop("retry", None)
    return state


def daily_left(sent_today: int, cap: int) -> int:
    """今天还能自动发几条（cap<0 当不限，返回一个大数）。"""
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 0
    if cap < 0:
        return 10**6
    return max(0, cap - max(0, int(sent_today or 0)))


def new_item(item: dict, text: str, ident: str, *, mid=None) -> dict:
    """待审队列里的一条。"""
    return {
        "id": ident,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bvid": item.get("bvid") or "",
        "root": item.get("root"),
        "source": item.get("source"),
        "mid": str(mid if mid is not None else (item.get("mid") or "")),
        "who": item.get("who") or "",
        "their_words": (item.get("their_words") or "")[:200],
        "my_comment": (item.get("my_comment") or "")[:200],
        "text": text,
    }


# 「同一个人 30 天内只回一条」（whale 的 --once-per-person）：
#   防拉锯、防被当成客服/聊天机器人。对使用者本人（admins_id）不生效 —— 回自己人不用防。
ONCE_DAYS = 30


def replied_recently(state: dict, mid, *, days: int = ONCE_DAYS, now: datetime | None = None) -> str:
    """这个人最近被回过吗？返回最近一次的时间串（空串 = 没回过 / 已经过期）。"""
    key = str(mid or "")
    if not key:
        return ""
    rows = (state.get("replied") or {}).get(key) or []
    now = now or datetime.now()
    for raw in reversed(rows):
        try:
            at = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if (now - at).total_seconds() < days * 86400:
            return str(raw)
    return ""


def mark_replied(state: dict, mid, *, at: datetime | None = None, keep: int = 20) -> dict:
    """记一笔「回过这个人」。只留最近 keep 条，别让状态文件无限长。"""
    key = str(mid or "")
    if not key:
        return state
    when = (at or datetime.now()).isoformat(timespec="seconds")
    bucket = state.setdefault("replied", {})
    rows = [str(x) for x in (bucket.get(key) or [])]
    rows.append(when)
    bucket[key] = rows[-keep:]
    return state


def once_block(state: dict, mid, *, owner_mid: str = "", days: int = ONCE_DAYS) -> str:
    """要不要因为「同一人 N 天内回过 TA」拦下来。返回拦下的原因（空串 = 不拦）。

    days <= 0 = 这个规则关掉了（配置里的 `reply_once_per_person` 勾掉就是走这条路）。
    """
    if days <= 0:
        return ""
    key = str(mid or "")
    if not key or (owner_mid and key == str(owner_mid)):
        return ""
    last = replied_recently(state, key, days=days)
    if not last:
        return ""
    return f"{days} 天内已经回过 TA（{last}）"


def pending_brief(items: list[dict]) -> str:
    """推送/列表用的一小段人话。"""
    if not items:
        return "（没有待审的回复）"
    lines = [f"待审 {len(items)} 条："]
    for it in items:
        lines.append(f"  [{it.get('id')}] {it.get('who') or '?'} 回：{it.get('their_words') or ''}"[:120])
        lines.append(f"       → {it.get('text') or ''}"[:120])
    return "\n".join(lines)


def pending_push_text(items: list[dict]) -> str:
    """新草稿进队列时推给使用者的话。"""
    lines = [f"B站 有人回你了，草稿 {len(items)} 条等你审：", ""]
    for it in items:
        lines.append(f"[{it['id']}] {it['who']} 说：{it['their_words']}")
        if it.get("my_comment"):
            lines.append(f"    （回的是你的：{it['my_comment']}）")
        lines.append(f"    我打算回：{it['text']}")
        lines.append(f"    链接：https://www.bilibili.com/video/{it['bvid']}")
        lines.append("")
    lines.append("要发哪条，就说「发 <方括号里的 id>」；不想发就说「弃 <id>」。")
    return "\n".join(lines)


def manual_push_text(item: dict) -> str:
    """使用者主动让回一条时，推给他的确认（whale 的「等你审核」）。"""
    return "\n".join(
        [
            f"写好一条回复，等你点头（[{item['id']}]）：",
            "",
            f"回谁：{item.get('who') or '对方'}" + (f"（mid {item['mid']}）" if item.get("mid") else ""),
            f"TA 说：{item.get('their_words') or '（顶层评论）'}",
            f"我回：{item.get('text')}",
            f"视频：https://www.bilibili.com/video/{item.get('bvid')}",
            "",
            f"回「发 {item['id']}」就发出去；回「弃 {item['id']}」就不发。",
        ]
    )
