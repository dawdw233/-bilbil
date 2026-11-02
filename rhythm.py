"""作息引擎 —— dsh-bili-whale `tools/lib/night-plan.mjs` 的 Python 移植（MIT）。

语义（和上游一致，别自作聪明改）：
    · 每天总醒来次数 = 各窗口 wakes 之和（最多 12）
    · 每次醒来在窗口内**随机**一个时刻；距上次醒来至少 minGapMin 分钟
    · 每次醒来有 skipChance 的概率「今晚不看」—— 跳过也**消耗一次机会**
    · 计划（atMs 绝对时间）要落盘：重启不重抽，只有改作息才清
    · 历史按「日期|窗口」记次数，跨午夜两侧都算，留 3 天

纯函数、不 import astrbot / 文件 IO：调度器负责读写，这里只算。
所有时间戳都是**毫秒整数**（和 JS 对齐，省得来回换算）。
"""

from __future__ import annotations

import random as _random
import re
import time
from datetime import datetime, timedelta

MAX_WAKES_PER_DAY = 12          # 一天最多醒 12 次，防手滑写成 99
MIN_GAP_FLOOR_MS = 60_000       # 相邻两次至少隔 1 分钟，再小没有意义
KEEP_DAYS = 3                   # 历史留几天

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WINDOW_LINE_RE = re.compile(
    r"^(\d{1,2}:\d{2})\s*[-~—–至到]\s*(\d{1,2}:\d{2})(?:\s*[×xX*]?\s*(\d{1,2})\s*次?)?$"
)


# ---------- 时间小工具 ----------

def now_ms() -> int:
    return int(time.time() * 1000)


def day_of(ms: int) -> str:
    """本地日期（YYYY-MM-DD）。"""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def stamp_ms(day: str, minutes: int) -> int:
    """某天的第 N 分钟 → 毫秒。"""
    base = datetime.strptime(day, "%Y-%m-%d")
    return int((base + timedelta(minutes=minutes)).timestamp() * 1000)


def parse_time(value) -> int:
    """'09:30' → 570；支持 24:00。写错就抛（宁可不跑，也别猜）。"""
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        raise ValueError(f"无效作息时间：{value}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError(f"无效作息时间：{value}")
    return hour * 60 + minute


def _iso_ms(value) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError:
        return 0


# ---------- 配置 ----------

def parse_windows_text(raw) -> tuple[list[dict], list[str]]:
    """页面上的文本 → 窗口列表。

    `09:00-15:00 3`  = 这段里醒 3 次
    `18:00-24:00`    = 醒 1 次（次可省略）
    也接受逗号/分号分隔、`×3` / `3次`。
    """
    windows: list[dict] = []
    errors: list[str] = []
    for line in re.split(r"[\n;；]+", str(raw or "")):
        for part in line.split(","):
            part = part.strip()
            if not part:
                continue
            match = _WINDOW_LINE_RE.match(part)
            if not match:
                errors.append(f"看不懂这段作息：{part}（要写成 09:00-15:00 3）")
                continue
            start, end = match.group(1), match.group(2)
            try:
                parse_time(start)
                parse_time(end)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            wakes = int(match.group(3) or 1)
            windows.append(
                {
                    "start": start,
                    "end": end,
                    "wakes": max(0, min(MAX_WAKES_PER_DAY, wakes)),
                }
            )
    if len(windows) > 6:
        errors.append("窗口最多 6 段")
        windows = windows[:6]
    return windows, errors


def build_cfg(windows: list[dict], *, skip_chance: float = 0.3, min_gap_min: int = 10, max_videos: int = 5) -> dict:
    """调度器用的配置（形状和 whale 的 night 一致）。"""
    return {
        "windows": windows or [],
        "skipChance": max(0.0, min(1.0, float(skip_chance or 0))),
        "minGapMin": max(0, int(min_gap_min or 0)),
        "maxVideos": max(1, int(max_videos or 1)),
    }


# ---------- 窗口 ----------

def windows_of(cfg: dict) -> list[dict]:
    """窗口列表。

    ⚠️ `windows is None`（配置里根本没有这个键）才回落到默认的 20:00–24:00；
    `windows == []`（页面上清空 / 全删掉）**就是没有窗口 = 不自动刷** ——
    以前这里把空列表也当"没配置"，结果用户清空作息后它照样在 20:00 自己醒。
    """
    raw = (cfg or {}).get("windows")
    if raw is None:
        raw = [
            {
                "start": (cfg or {}).get("windowStart", "20:00"),
                "end": (cfg or {}).get("windowEnd", "24:00"),
                "wakes": (cfg or {}).get("maxWakesPerDay", 1),
            }
        ]
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = parse_time(item.get("start"))
            raw_end = parse_time(item.get("end"))
        except ValueError:
            continue
        end = raw_end + 1440 if raw_end <= start else raw_end
        try:
            wakes = int(float(item.get("wakes") if item.get("wakes") is not None else 1))
        except (TypeError, ValueError):
            wakes = 1
        out.append(
            {
                "start": start,
                "end": end,
                "wrap": end > 1440,
                "label": f"{item.get('start')}–{item.get('end')}",
                "wakes": max(0, min(MAX_WAKES_PER_DAY, wakes)),
            }
        )
    return out


def max_wakes(cfg: dict) -> int:
    return min(MAX_WAKES_PER_DAY, sum(w["wakes"] for w in windows_of(cfg)))


def occurrences(cfg: dict, now: int | None = None) -> list[dict]:
    """今天（跨午夜时连昨天）还没结束的窗口实例。"""
    now = now_ms() if now is None else int(now)
    today = day_of(now)
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    out: list[dict] = []
    for win in windows_of(cfg):
        days = [yesterday, today] if win["wrap"] else [today]
        for day in days:
            out.append(
                {
                    **win,
                    "key": f"{day}|{win['label']}",
                    "startsAt": stamp_ms(day, win["start"]),
                    "endsAt": stamp_ms(day, win["end"]),
                }
            )
    return [w for w in out if w["endsAt"] > now]


def in_window(cfg: dict, now: int | None = None) -> bool:
    now = now_ms() if now is None else int(now)
    return any(w["startsAt"] <= now for w in occurrences(cfg, now))


# ---------- 历史 / 计划 ----------

def history_of(state: dict | None = None, now: int | None = None) -> dict:
    """把状态整理成「今天醒了几次、各窗口用掉几次」。改配置不会清历史。"""
    now = now_ms() if now is None else int(now)
    state = state or {}
    used = {str(k): int(v or 0) for k, v in (state.get("usedCount") or {}).items()}
    skipped = {str(x) for x in (state.get("skippedWindows") or [])}

    if state.get("schema") != 2 and state.get("date"):
        # 老格式迁移：只有标签没有日期，把它补成 日期|标签
        for label, count in list(used.items()):
            if "|" not in label:
                reserved = 0
                if (
                    state.get("action") == "wake"
                    and not state.get("fired")
                    and not state.get("woke")
                    and label == state.get("window")
                ):
                    reserved = 1
                used[f"{state['date']}|{label}"] = max(0, count - reserved)
                del used[label]
        if state.get("action") == "skip" and state.get("fired") and state.get("window"):
            skipped.add(f"{state['date']}|{state['window']}")

    cutoff_day = (datetime.fromtimestamp(now / 1000) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for key in list(used):
        if key[:10] < cutoff_day:
            del used[key]

    today = day_of(now)
    return {
        "schema": 2,
        "date": today,
        "wakes": int(state.get("wakes") or 0) if state.get("date") == today else 0,
        "wokeAt": state.get("wokeAt"),
        "usedCount": used,
        "skippedWindows": [k for k in skipped if k[:10] >= cutoff_day],
    }


def is_pending(state: dict | None, now: int | None = None) -> bool:
    """有没有一个「已经抽好、还没到点/还没兑现」的计划（重启后靠它不重抽）。"""
    now = now_ms() if now is None else int(now)
    state = state or {}
    return (
        state.get("schema") == 2
        and state.get("action") in ("wake", "skip")
        and not state.get("fired")
        and not state.get("woke")
        and isinstance(state.get("atMs"), (int, float))
        and isinstance(state.get("endsAt"), (int, float))
        and state["endsAt"] > now
    )


def cfg_signature(cfg: dict) -> str:
    """作息配置指纹：窗口/次数/不看概率/间隔。

    计划里存一份；改了作息（指纹变了）就把已抽好的计划作废重排 ——
    页面上那句「改这里才会重新排计划」说的就是这件事。
    """
    wins = windows_of(cfg)
    base = "|".join(f"{w['start']}-{w['end']}x{w['wakes']}" for w in wins)
    skip = float(cfg.get("skipChance") or 0)
    gap = int(cfg.get("minGapMin") or 0)
    return f"{base};skip={skip:.3f};gap={gap}"


def make_plan(cfg: dict, state: dict | None = None, now: int | None = None, rand=_random.random) -> dict:
    """抽下一次醒来（纯函数；调用方负责把结果落盘）。

    返回的 dict 一定带 action：
        done  = 今天没得跑了（次数用完 / 窗口都结束了 / 间隔放不下）
        wake  = 到 atMs 去刷
        skip  = 到 atMs 决定「今晚不看」（仍记一次）
    """
    now = now_ms() if now is None else int(now)
    state = state or {}
    sig = cfg_signature(cfg)
    # 已经抽好、还没兑现，而且作息没改过 → 原样返回（重启不重抽）
    if is_pending(state, now) and str(state.get("cfgSig") or "") == sig:
        return state

    history = history_of(state, now)

    def done(reason: str) -> dict:
        return {**history, "action": "done", "done": True, "reason": reason}

    if history["wakes"] >= max_wakes(cfg):
        return done("今天醒来次数已用完（或所有窗口为零次）")

    pool = [
        w for w in occurrences(cfg, now) if w["wakes"] > (history["usedCount"].get(w["key"]) or 0)
    ]
    pool.sort(key=lambda w: (w["endsAt"], w["startsAt"]))
    if not pool:
        return done("今天可用窗口已结束或已经处理")

    last_woke = _iso_ms(state.get("wokeAt"))
    try:
        min_gap = int(float(cfg.get("minGapMin") or 0) * 60_000)
    except (TypeError, ValueError):
        min_gap = 600_000
    min_gap = max(MIN_GAP_FLOOR_MS, min_gap)

    picked = None
    for cand in pool:
        earliest = max(now, cand["startsAt"], last_woke + min_gap)
        if earliest >= cand["endsAt"] - 30_000:      # 这个窗口放不下了
            continue
        picked = (cand, earliest)
        break
    if picked is None:
        return done("今天可用窗口里已经排不下下一次（间隔不够）")

    cand, earliest = picked
    at_ms = earliest + int(rand() * max(1, cand["endsAt"] - earliest))
    at_dt = datetime.fromtimestamp(at_ms / 1000)
    skip_chance = min(1.0, max(0.0, float(cfg.get("skipChance") or 0)))
    skip = rand() < skip_chance
    plan = {
        **history,
        "action": "skip" if skip else "wake",
        "fired": False,
        "atMs": at_ms,
        "at": at_dt.hour * 60 + at_dt.minute,
        "atText": f"{day_of(at_ms)} {at_dt.hour:02d}:{at_dt.minute:02d}",
        "window": cand["label"],
        "windowKey": cand["key"],
        "startsAt": cand["startsAt"],
        "endsAt": cand["endsAt"],
        "withinWindow": (
            f"{cand['label']} 第 {(history['usedCount'].get(cand['key']) or 0) + 1}/{cand['wakes']} 次"
        ),
        "maxVideos": cfg.get("maxVideos", 5),
        "cfgSig": sig,
        "createdAt": datetime.fromtimestamp(now / 1000).isoformat(timespec="seconds"),
    }
    if skip:
        plan["reason"] = "本次计划跳过看视频，仍写日记"
    else:
        plan["roll"] = "watch"
    return plan


def complete_plan(state: dict, now: int | None = None) -> dict:
    """兑现一次（wake 或 skip 都算），推进历史。"""
    now = now_ms() if now is None else int(now)
    if state.get("fired"):
        return state
    history = history_of(state, now)
    key = state.get("windowKey")
    if not key:
        raise ValueError("计划缺少窗口标识，请重新 plan")
    if state.get("action") in ("skip", "wake"):
        history["usedCount"][key] = (history["usedCount"].get(key) or 0) + 1
        history["wakes"] += 1
    return {
        **state,
        **history,
        "fired": True,
        "woke": state.get("action") == "wake",
        "wokeAt": datetime.fromtimestamp(now / 1000).isoformat(timespec="seconds"),
    }


def describe(cfg: dict) -> str:
    """一行日志：`作息 09:00–15:00×3 18:00–24:00×3 · 每天≤6 次 · 不看 30%`"""
    wins = windows_of(cfg)
    if not wins:
        return "作息：没有窗口（不会自动刷，只能手动点「立刻刷一轮」）"
    parts = [f"{w['label']}×{w['wakes']}" for w in wins]
    return (
        "作息 " + " ".join(parts) +
        f" · 每天≤{max_wakes(cfg)} 次 · 不看 {int(round(float(cfg.get('skipChance') or 0) * 100))}%"
        + f" · 间隔≥{int(float(cfg.get('minGapMin') or 0))} 分"
    )
