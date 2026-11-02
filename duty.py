"""刷一轮的主流程（从 dsh-bili-scroll 的 duty 章程翻译成确定性流水线）。

DSH 那边是靠一个 agent + 一长串工具调用跑完的；这里改成固定步骤，
每一步都能单独看日志、单独失败，省 token 也好排查：

    搜 → 去重 → 读字幕 → 逐条判断 → （可选）自动评论 → 写日报 → 记判重表 → 私信 / 推 QQ

判重表 seen.json 的结构：
    {"videos": {"BV号": {"date","title","up","verdict","link","reason"}},
     "runs":   [{"at","picked","skipped","note"}]}

评论流水 comments.jsonl（人工发的和自动发的都记，自动的多一个 "auto": true）：
    {"at","bvid","target_rpid","root","rpid","text","auto"}
    回复比评论多一个 "reply_to"（对方昵称）：**回复不算评论额度**，两边各有各的日上限。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

try:  # 插件包里是相对导入；单测里 duty 是顶层模块（sys.path 指到插件目录）
    from . import guard
except ImportError:  # pragma: no cover
    import guard  # type: ignore

DEFAULT_KEYWORDS = ["搞笑", "沙雕", "整活", "名场面"]
VERDICTS = ("值得看", "可以看", "不值当")

# 自动评论的兜底闸：LLM 写出来的东西先过一遍，命中就整条丢弃（宁可不发）
COMMENT_BLOCKLIST = (
    "http", "www.", "@", "微信", "加群", "二维码", "公众号", "淘宝", "加我",
    "作为ai", "我是ai", "ai助手", "机器人", "本ai",
)

# ---------- 刷视频时段 ----------
# 时段和条数分开配，按行对齐：
#     时段：09:00-15:00        条数：10
#          18:00-24:00              5
# 语义：时段内**累计**最多挑 N 条（跨多轮算总账）；不在任何时段内就整轮不跑。
# 额度怎么落到每一轮：剩余条数 ÷ 这个时段还剩几个 run_time 调度点（见 per_run_cap）。
# 所以「一个时段只给一个定时点」注定吃不满额度 —— 要让时段里有几个点分摊。
# 留空 = 不限制时段（保持老行为）。也兼容老写法（行尾直接带条数：09:00-15:00 10）。
WINDOW_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*[-~—–至到]\s*(\d{1,2}):(\d{2})\s*(?:[,，]?\s*(\d{1,3}))?$"
)


def parse_browse_windows(raw_windows, raw_counts="") -> tuple[list[dict], list[str]]:
    """解析时段 + 条数 → ([{start,end,quota,range,text}], [错误])。

    条数列表可以留空（那就得用老写法把条数写在时段行尾）。
    """
    windows: list[dict] = []
    errors: list[str] = []
    if isinstance(raw_windows, (list, tuple)):
        lines = [str(x).strip() for x in raw_windows]
    else:
        lines = [ln.strip() for ln in re.split(r"[\n;；]+", str(raw_windows or ""))]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    if isinstance(raw_counts, (list, tuple)):
        counts = [str(x).strip() for x in raw_counts]
    else:
        counts = [x for x in re.split(r"[\n,，;；\s]+", str(raw_counts or "")) if x]
    counts = [c for c in counts if c]

    if counts and len(counts) != len(lines):
        return [], [f"条数给了 {len(counts)} 个，时段有 {len(lines)} 个，两边行数要对上"]

    for idx, line in enumerate(lines):
        m = WINDOW_RE.match(line)
        if not m:
            errors.append(f"时段「{line}」格式不对，要像 09:00-15:00")
            continue
        h1, m1, h2, m2 = (int(m.group(i)) for i in range(1, 5))
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if not (0 <= start < 1440) or not (0 <= end <= 1440) or start == end:
            errors.append(f"时段「{line}」时间不对（结束可以写 24:00，起止不能相同）")
            continue
        raw_quota = m.group(5) or (counts[idx] if counts else "")
        try:
            quota = int(raw_quota)
        except (TypeError, ValueError):
            errors.append(f"第 {idx + 1} 个时段没给条数")
            continue
        if not 1 <= quota <= 50:
            errors.append(f"第 {idx + 1} 个时段的条数要在 1~50 之间")
            continue
        span = f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
        windows.append({
            "start": start,
            "end": end,
            "quota": quota,
            "range": span,
            "text": f"{span} {quota}",
        })
    return windows, errors


def active_window(windows: list[dict], now: datetime) -> dict | None:
    """当前时间落在哪个时段里（支持跨午夜，如 22:00-02:00）。"""
    cur = now.hour * 60 + now.minute
    for w in windows:
        if w["start"] < w["end"]:
            if w["start"] <= cur < w["end"]:
                return w
        elif cur >= w["start"] or cur < w["end"]:
            return w
    return None


def _window_bounds(window: dict, now: datetime) -> tuple[datetime, datetime]:
    """当前这次时段的具体起止时刻（跨午夜的算成前一天/后一天）。"""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = window["start"], window["end"]
    if start < end:
        return day + timedelta(minutes=start), day + timedelta(minutes=end)
    if now.hour * 60 + now.minute >= start:
        return day + timedelta(minutes=start), day + timedelta(days=1, minutes=end)
    return (day - timedelta(days=1) + timedelta(minutes=start),
            day + timedelta(minutes=end))


def window_usage(window: dict, now: datetime, runs: list[dict]) -> int:
    """这个时段里已经挑走了多少条（数 seen.json 的 runs）。"""
    start, end = _window_bounds(window, now)
    used = 0
    for r in runs:
        try:
            at = datetime.fromisoformat(str(r.get("at") or ""))
        except ValueError:
            continue
        if start <= at < end:
            used += int(r.get("picked") or 0)
    return used


SLOT_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_run_slots(raw) -> list[int]:
    """解析 run_time（'09:00,21:30'）→ 分钟数（去重排序）。

    定时器和额度分摊必须用同一份解析规则，所以放在这里由 main 复用。
    只要有一项写错就整份作废（返回 []）：半个配置生效比全都不生效更难查。
    """
    slots: list[int] = []
    for part in re.split(r"[,，\s]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        m = SLOT_RE.match(part)
        if not m:
            return []
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return []
        slots.append(hour * 60 + minute)
    return sorted(set(slots))


def slots_left_in_window(window: dict, now: datetime, run_slots: list[int]) -> int:
    """这个时段里还剩几个调度点（含当前这一分钟，不含已经过去的）。

    额度分摊靠它：剩余条数 ÷ 剩余调度点 = 本轮该拿多少。
    这样前面某轮少挑、或者干脆跑挂了，后面的轮次会自动多拿，
    不用手工去调 daily_count 凑数。
    """
    if not run_slots:
        return 0
    start, end = _window_bounds(window, now)
    now_min = now.replace(second=0, microsecond=0)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    while day < end:  # 时段最长 24 小时，最多转两圈（跨午夜）
        for minute in run_slots:
            at = day + timedelta(minutes=minute)
            if start <= at < end and at >= now_min:
                count += 1
        day += timedelta(days=1)
    return count


def slot_label(minutes: int) -> str:
    """分钟数 → "HH:MM"（定时状态文件里的键）。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def plan_slots(
    slots: list[int],
    now: datetime,
    state: dict | None,
    *,
    grace_seconds: int = 3600,
    min_gap_seconds: int = 300,
    max_attempts: int = 3,
) -> dict:
    """算这个 tick 该跑哪个定时点（纯函数，好测）。

    和以前「精确到分钟匹配」的区别：**过了点也认**，只要还在宽限内。
    以前 container 恰好在 09:00 重启、或上一轮跑得久把事件循环堵住，
    这个点就永久消失了 —— 早段 10 条额度直接少一截。

    返回 {date, state, miss:[{slot,why}], fire, late_minutes, note}
    """
    today = now.strftime("%Y-%m-%d")
    base = state if isinstance(state, dict) and state.get("date") == today else {}

    def _dict_of(key: str) -> dict:
        value = base.get(key)
        return dict(value) if isinstance(value, dict) else {}

    done = _dict_of("done")
    attempts = _dict_of("attempts")
    last_attempt = _dict_of("last_attempt")
    try:
        last_fire = float(base.get("last_fire") or 0)
    except (TypeError, ValueError):
        last_fire = 0.0

    cur = now.hour * 60 + now.minute
    now_ts = now.timestamp()
    miss: list[dict] = []
    fire: int | None = None
    note = ""

    due = sorted(s for s in slots if s <= cur and slot_label(s) not in done)
    if due:
        latest = due[-1]
        # 一次只补最新那个点：重启后不该把早上错过的 3 个点连着跑 3 轮
        for s in due[:-1]:
            miss.append({"slot": s, "why": "错过了（容器没在/上一轮还没跑完）"})
        waited = (cur - latest) * 60
        key = slot_label(latest)
        if waited > grace_seconds:
            why = (
                f"超过补跑宽限（{grace_seconds // 60} 分钟）"
                if grace_seconds
                else "已经过了这个点（补跑关着）"
            )
            miss.append({"slot": latest, "why": why})
        elif now_ts - last_fire < min_gap_seconds:
            note = "距上一次跑太近，等下一轮"
        elif int(attempts.get(key) or 0) >= max_attempts:
            miss.append({"slot": latest, "why": f"连续失败 {max_attempts} 次，放弃这个点"})
        else:
            fire = latest

    for item in miss:
        done[slot_label(item["slot"])] = item["why"]
    new_state = {
        "date": today, "done": done, "attempts": attempts,
        "last_attempt": last_attempt, "last_fire": last_fire,
    }
    return {
        "date": today,
        "state": new_state,
        "miss": miss,
        "fire": fire,
        "late_minutes": max(0, cur - fire) if fire is not None else 0,
        "note": note,
    }


def per_run_cap(daily_count: int, left: int, slots_left: int) -> int:
    """这一轮最多挑几条。

    daily_count 是单轮安全阀（也顺手防一次评太多），left 是时段剩余额度，
    slots_left 是这个时段还剩几个调度点（0/1 表示没得摊，按老规矩 min）。
    额度按调度点均分，是为了让 09:00 那一轮别把整个时段的额度一次吃光。
    """
    if left < 0:
        return daily_count
    share = -(-left // slots_left) if slots_left > 1 else left
    return min(daily_count, share)


def window_quota_left(
    windows: list[dict], now: datetime, runs: list[dict]
) -> tuple[dict | None, int, str]:
    """→ (当前时段, 剩余条数, 不能刷的原因)。

    没配时段 → (None, -1, "")，-1 表示不限。
    """
    if not windows:
        return None, -1, ""
    win = active_window(windows, now)
    if win is None:
        return None, 0, "现在不在刷视频时段（" + "、".join(w["text"] for w in windows) + "）"
    left = win["quota"] - window_usage(win, now, runs)
    if left <= 0:
        return win, 0, f"这个时段（{win['text']}）已经刷满 {win['quota']} 条"
    return win, left, ""


# ---------- 风控闸 ----------
# 撞到 B站 的风控（412 / -412 / 429 / 请求过于频繁）之后，整段时间内不再发起请求。
# 以前只在评论被拒时 break 一轮，下一轮照样撞 —— 越撞越狠，是小号被封的典型路径。
RISK_PAUSE_STEPS = (5 * 60, 10 * 60, 20 * 60, 30 * 60)  # 当天第 1/2/3/4+ 次撞到
RISK_MARKERS = (
    "412", "429", "风控", "请求过于频繁", "请求太快", "操作频繁",
    "too many requests", "risk control",
)


class RiskPause(Exception):
    """撞到风控 / 正在暂停期：调用方应当立刻停手，别接着撞。"""

    def __init__(self, message: str, seconds: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.seconds = int(seconds or 0)


def risk_marker(raw) -> str:
    """从接口返回里认出风控信号（命中返回标记，没命中返回空串）。

    只认 error/message/msg/code 这几个字段 —— 直接 grep 整段 JSON 会把 BV 号里的
    "412"、评论正文里的"412"当成风控，误判比不判更糟。
    """
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
    bits: list[str] = []
    if isinstance(data, dict):
        for key in ("error", "message", "msg", "code", "err"):
            value = data.get(key)
            if value not in (None, "", 0):
                bits.append(str(value))
    if not bits:
        return ""
    text = " ".join(bits).lower()
    for mark in RISK_MARKERS:
        if mark in text:
            return mark
    return ""


class DutyRunner:
    def __init__(
        self,
        *,
        bili,
        config: dict,
        data_dir: Path,
        judge: Callable[[str, str], Awaitable[str]],
        log: Callable[[str], None],
        commenter: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        self.bili = bili
        self.cfg = config or {}
        self.data_dir = Path(data_dir)
        self.judge = judge
        self.log = log
        # 自动评论用的"写一句话"回调；不给就等于关掉这个功能
        self.commenter = commenter
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "out").mkdir(exist_ok=True)
        self.seen_path = self.data_dir / "seen.json"
        self.comment_log = self.data_dir / "comments.jsonl"

    # ---------- 判重表 ----------

    def load_seen(self) -> dict:
        if not self.seen_path.exists():
            return {"videos": {}, "runs": []}
        try:
            data = json.loads(self.seen_path.read_text(encoding="utf-8"))
            data.setdefault("videos", {})
            data.setdefault("runs", [])
            return data
        except Exception as exc:  # 表坏了不能当"没看过"
            # 先把坏文件留一份再重建：直接覆盖等于把「可能还能手工抢救」的判重史删了
            backup = ""
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                bad = self.seen_path.parent / f"{self.seen_path.name}.broken-{stamp}"
                bad.write_bytes(self.seen_path.read_bytes())
                backup = f"，原文件另存为 {bad.name}"
            except OSError:
                backup = "，原文件备份失败"
            self.log(f"[warn] 判重表读不出来（{exc}）{backup}；本轮不发私信")
            return {"videos": {}, "runs": [], "_broken": True}

    def save_seen(self, seen: dict) -> None:
        seen.pop("_broken", None)
        tmp = self.seen_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.seen_path)

    # ---------- 风控闸 ----------

    @property
    def _risk_path(self) -> Path:
        return self.data_dir / ".risk_state.json"

    def load_risk(self) -> dict:
        try:
            data = json.loads(self._risk_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_risk(self, state: dict) -> None:
        try:
            self._risk_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            self.log("[warn] 风控状态写不进去（重启后会忘记还在暂停）")

    def risk_pause_left(self) -> int:
        """还要暂停多少秒（0 = 放行）。"""
        try:
            until = float(self.load_risk().get("pause_until") or 0)
        except (TypeError, ValueError):
            until = 0
        return max(0, int(until - time.time()))

    def note_risk(self, mark: str) -> int:
        """记一次风控，返回本次暂停秒数（当天第 N 次撞 → 5/10/20/30 分钟）。"""
        state = self.load_risk()
        today = datetime.now().strftime("%Y-%m-%d")
        strikes = state.get("strikes")
        if not isinstance(strikes, dict):
            strikes = {}
        # 只留今天的计数：昨天的账不该影响今天的退避
        strikes = {today: int(strikes.get(today) or 0) + 1}
        step = RISK_PAUSE_STEPS[min(strikes[today] - 1, len(RISK_PAUSE_STEPS) - 1)]
        state.update({
            "strikes": strikes,
            "pause_until": time.time() + step,
            "last_mark": mark,
            "last_hit_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.save_risk(state)
        self.log(
            f"[warn] 撞到风控「{mark}」（今天第 {strikes[today]} 次）"
            f"→ 暂停 {step // 60} 分钟，期间不发起任何请求"
        )
        return step

    async def _call(self, fn, *args, **kwargs):
        """所有 B站 调用都走这里：先查暂停，回来后认风控信号。

        暂停期直接抛 RiskPause（连请求都不发），撞到就记一次并抛出，
        让调用方当轮停手 —— 而不是像以前那样一轮一轮地接着撞。
        """
        left = self.risk_pause_left()
        if left > 0:
            raise RiskPause(f"风控暂停中，还剩 {left // 60 + 1} 分钟", left)
        raw = await fn(*args, **kwargs)
        mark = risk_marker(raw)
        if mark:
            secs = self.note_risk(mark)
            raise RiskPause(f"撞到风控「{mark}」，暂停 {secs // 60} 分钟", secs)
        return raw

    async def _fallback_material(self, bvid: str, v: dict) -> tuple[str, str, bool, str]:
        """没字幕时的兜底素材：简介 + 热评。

        返回 (素材, 不能用原因, 是否"确定没素材", 素材标签)。
        只有**确定**没素材（评论接口正常返回但确实没有评论、简介也空）才该写判重表；
        取评论失败/风控这类临时故障不能写 —— 写了这条视频就永远不会再被看一眼。
        标签按实际拿到的部分给（简介+热评 / 简介 / 热评），日报里才不会写错。
        """
        if not callable(getattr(self.bili, "bili_comments", None)):
            # 桩件/老宿主没有这个能力：当"没有兜底素材"处理，按老规矩记进判重表
            return "", "", True, ""

        parts: list[str] = []
        desc = str(v.get("description") or "").strip()
        if desc:
            parts.append("【简介】" + desc[:400])
        try:
            raw = await self._call(self.bili.bili_comments, bvid, 20)
        except RiskPause:
            raise
        except Exception as exc:
            return "", f"无字幕，取热评异常：{exc}", False, ""

        data = self._loads(raw)
        if data.get("error"):
            return "", f"无字幕，取热评失败：{str(data['error'])[:60]}", False, ""
        lines = []
        for c in (data.get("comments") or [])[:20]:
            text = str(c.get("content") or "").strip().replace("\n", " ")
            if not text:
                continue
            like = c.get("like") or 0
            lines.append(text[:120] + (f"（{like} 赞）" if like else ""))
        if lines:
            parts.append("【热评】\n" + "\n".join(f"- {ln}" for ln in lines))
        if not parts:
            return "", "无字幕，也没有简介和热评", True, ""
        label = "简介+热评" if (desc and lines) else ("简介" if desc else "热评")
        return "\n\n".join(parts), "", False, label

    # ---------- 工具小函数 ----------

    @staticmethod
    def _fmt_play(n) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return str(n)
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return str(n)

    @staticmethod
    def _loads(raw, default=None):
        if isinstance(raw, (dict, list)):
            return raw
        if not raw:
            return default if default is not None else {}
        try:
            return json.loads(raw)
        except Exception:
            return default if default is not None else {}

    def _subtitle_text(self, raw) -> tuple[str, str]:
        """返回 (正文, 跳过原因)。原因非空表示这条没法判断。"""
        data = self._loads(raw)
        if isinstance(data, str):
            text = data
        else:
            if data.get("error"):
                return "", f"取字幕失败：{data['error']}"
            text = data.get("text") or data.get("subtitle") or data.get("content") or ""
            if not text and data.get("message"):
                return "", f"无字幕（{data['message']}）"
        text = (text or "").strip()
        if not text:
            return "", "无字幕"
        # 只有署名行 ⇒ 当没语音
        stripped = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(stripped) <= 1 and ("字幕" in text and len(text) < 40):
            return "", "无语音（只有字幕署名）"
        return text, ""

    # ---------- 主流程 ----------

    def _skip_result(self, today: str, msg: str, started: float, *, err: str = "") -> dict:
        """这轮不跑（没登录 / 不在时段 / 额度用完）：不写日报，只回一条说明。"""
        self.log(msg)
        return {
            "date": today, "error": err or msg, "candidates": 0, "fresh": 0,
            "picked": [], "skipped": [], "report_path": "",
            "report_md": msg, "dm": "", "elapsed": round(time.time() - started, 1),
            "auto_comment": {"posted": [], "declined": [], "skipped": "", "error": ""},
        }

    async def run(self, *, force: bool = False) -> dict:
        started = time.time()
        today = datetime.now().strftime("%Y-%m-%d")

        # 0) 没登录就别跑：否则每条都因"取字幕失败"被记成跳过，
        #    等真登录了这些视频就再也不看了 —— 宁可这轮直接失败
        try:
            cred = self.bili.load_credential()
        except Exception:
            cred = None
        # 光有文件不算登录：凭证可能是空的（{"sessdata": ""}），
        # 那种情况下每条都会报 "Credential 类未提供 sessdata"
        if not cred or not getattr(cred, "sessdata", ""):
            # 不写日报：没登录是"这轮没跑"，不是"今天没内容"，别污染日报文件
            return self._skip_result(
                today,
                "本轮失败：还没登录 B站，先在 WebUI 插件页里扫码登录。",
                started,
                err="未登录",
            )

        seen = self.load_seen()
        broken = bool(seen.get("_broken"))

        # 0.4) 凭证过期闸：文件还在、sessdata 也不空，但 cookie 可能已经被踢了。
        #      不过这道闸就白跑一轮（字幕取不到 → 每条都记"跳过"），所以开工前真问一次。
        checker = getattr(self.bili, "bili_cred_alive", None)
        if callable(checker):
            try:
                alive, why = await checker()
            except Exception as exc:  # 验证本身出错就别拦，按原来的路走
                alive, why = True, f"验证异常：{exc}"
            if not alive:
                return self._skip_result(
                    today, f"本轮失败：{why}。", started, err="凭证失效"
                )

        # 0.5) 风控闸：还在暂停期就整轮不跑，一个请求都不发
        paused = self.risk_pause_left()
        if paused > 0:
            mark = str(self.load_risk().get("last_mark") or "风控")
            return self._skip_result(
                today,
                f"被 B站 风控暂停中（{mark}），还有 {paused // 60 + 1} 分钟。期间不发起任何请求。",
                started,
                err="风控暂停",
            )
        risk_abort = ""

        # 1) 时段闸：不在时段内 / 这个时段的额度用完了 → 整轮不跑
        windows, win_errors = parse_browse_windows(
            self.cfg.get("browse_windows"), self.cfg.get("browse_counts")
        )
        if win_errors:
            self.log("[warn] 时段配置有错：" + "；".join(win_errors))
        now = datetime.now()
        win, left, why = window_quota_left(windows, now, seen["runs"])
        if why:
            return self._skip_result(today, why, started)

        keywords = [k for k in (self.cfg.get("keywords") or DEFAULT_KEYWORDS) if str(k).strip()]
        per_kw = int(self.cfg.get("candidates_per_keyword") or 10)
        daily = int(self.cfg.get("daily_count") or 3)
        # 单轮上限之外，还要受时段剩余额度约束（没配时段时 left = -1）。
        # 剩余额度按「这个时段还剩几个调度点」分摊：早段 10 条 / 4 个点 → 3,3,2,2，
        # 而不是让 09:00 那一轮按 daily_count 把整个时段的额度一次吃光。
        if left >= 0:
            slots_left = slots_left_in_window(win, now, parse_run_slots(self.cfg.get("run_time")))
            daily = per_run_cap(daily, left, slots_left)
            if win:
                self.log(
                    f"时段 {win['text']}，还剩 {left} 条额度"
                    + (f"，还有 {slots_left} 个调度点 → 本轮最多挑 {daily} 条"
                       if slots_left > 1 else "")
                )
        order = str(self.cfg.get("search_order") or "totalrank")
        # 最多扫多少条（读字幕+判断都要花时间，得有个上限兜住整轮时长）
        max_scan = int(self.cfg.get("max_scan") or 15)

        # 1) 搜
        candidates: dict[str, dict] = {}
        for kw in keywords:
            try:
                raw = await self._call(self.bili.bili_search, kw, per_kw, order)
            except RiskPause as exc:
                risk_abort = str(exc)
                self.log(f"[warn] {exc}，本轮中止")
                break
            except Exception as exc:
                self.log(f"[warn] 搜索「{kw}」失败：{exc}")
                continue
            for v in self._loads(raw).get("videos", []):
                bvid = v.get("bvid")
                if bvid and bvid not in candidates:
                    candidates[bvid] = v
        self.log(f"搜到 {len(candidates)} 条候选（关键词 {len(keywords)} 组）")

        # 2) 去重
        fresh = {b: v for b, v in candidates.items() if b not in seen["videos"]}
        self.log(f"去掉看过的，剩 {len(fresh)} 条新候选")

        # 3) 逐条读字幕 + 判断（没字幕时退到「简介 + 热评」再判一次）
        picked: list[dict] = []
        skipped: list[dict] = []
        subtitles: dict[str, str] = {}  # 挑中的留着，自动评论要用
        scanned = 0
        for bvid, v in fresh.items():
            if len(picked) >= daily or scanned >= max_scan or risk_abort:
                break
            scanned += 1
            try:
                raw_sub = await self._call(self.bili.bili_subtitle, bvid)
            except RiskPause as exc:
                risk_abort = str(exc)
                self.log(f"[warn] {exc}，本轮中止")
                break
            except Exception as exc:
                skipped.append({"bvid": bvid, "why": f"取字幕异常：{exc}", "title": v.get("title", ""),
                                "keep": False})
                continue
            text, why = self._subtitle_text(raw_sub)
            material = "字幕"
            if why:
                # 没字幕不等于看不懂：简介 + 热评也够判断（whale 的「多路互补」思路）
                try:
                    text, why2, definitive, label = await self._fallback_material(bvid, v)
                except RiskPause as exc:
                    risk_abort = str(exc)
                    self.log(f"[warn] {exc}，本轮中止")
                    break
                if not text:
                    # 兜底也失败时，别把原因一律写成「无字幕」：
                    # 「只有署名行」那种视频**是有字幕的**，只是没语音，措辞得对得上事实
                    reason = why2 or why
                    if why.startswith("无语音") and reason.startswith("无字幕"):
                        reason = "无语音" + reason[len("无字幕"):]
                    skipped.append({
                        "bvid": bvid,
                        "why": reason,
                        "title": v.get("title", ""),
                        "keep": bool(definitive),
                    })
                    continue
                material = label or "简介+热评"
                self.log(f"无字幕，改用{material}判断 {bvid}")

            try:
                verdict_raw = await self.judge(text[:12000], v.get("title", ""))
                verdict = self._loads(verdict_raw)
            except Exception as exc:
                skipped.append({"bvid": bvid, "why": f"判断失败：{exc}", "title": v.get("title", ""),
                                "keep": False})
                continue

            level = str(verdict.get("verdict") or "").strip()
            if level not in VERDICTS:
                level = "可以看"
            if level == "不值当":
                skipped.append({"bvid": bvid, "why": "判断为不值当", "title": v.get("title", ""),
                                "keep": True})
                seen["videos"][bvid] = {
                    "date": today, "title": v.get("title", ""), "up": v.get("author", ""),
                    "verdict": level, "link": f"https://www.bilibili.com/video/{bvid}",
                    "reason": str(verdict.get("reason") or "")[:200],
                }
                continue

            picked.append({
                "bvid": bvid,
                "title": v.get("title", ""),
                "up": v.get("author", ""),
                "play": self._fmt_play(v.get("play")),
                "link": f"https://www.bilibili.com/video/{bvid}",
                "verdict": level,
                "reason": str(verdict.get("reason") or "").strip(),
                "one_liner": str(verdict.get("one_liner") or "").strip(),
                "material": material,
            })
            subtitles[bvid] = text

        # 3.5) 自动评论（可关；有每轮/每天上限，被风控就停手）
        if risk_abort:
            auto_comment = {
                "posted": [], "declined": [],
                "skipped": f"风控暂停，本轮不评论（{risk_abort}）", "error": "",
            }
            self.log(f"[warn] {risk_abort}，跳过自动评论")
        else:
            auto_comment = await self._auto_comment(picked, subtitles)

        # 4) 写日报：当天一个文件，每轮追加一节（同一天跑多次不覆盖、不重复刷同一段）
        report_path = self.data_dir / "out" / f"{today}.md"
        run_at = datetime.now().strftime("%H:%M")
        prev = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        run_no = prev.count("\n## 第 ") + 1
        report_md = self._render_report(
            picked,
            skipped,
            run_no=run_no,
            run_at=run_at,
            scanned=scanned,
            fresh=len(fresh),
            candidates=len(candidates),
            window=win["text"] if win else "",
            risk=risk_abort,
        )
        with open(report_path, "a", encoding="utf-8") as f:
            if not prev:
                f.write(f"# {today} 刷B站日报\n")
            f.write("\n" + report_md)

        # 5) 记判重表：**跑过的每一条都要记**（挑中的、跳过的、不值当的）
        #    只记挑中的话，下一轮会把同一批"无字幕/接口失败"的又跑一遍，白烧请求
        for item in picked:
            seen["videos"][item["bvid"]] = {
                "date": today, "title": item["title"], "up": item["up"],
                "verdict": item["verdict"], "link": item["link"],
                "reason": item["reason"][:200],
            }
        for s in skipped:
            if s["bvid"] in seen["videos"]:
                continue
            # 只有**内容层面**的结论才写判重表（真没素材、判过不值当）。
            # 接口报错 / 取评论失败这类临时故障不能写：写了这条就永远不会再被看一次。
            if not s.get("keep"):
                continue
            seen["videos"][s["bvid"]] = {
                "date": today, "title": s.get("title", ""), "up": "",
                "verdict": "跳过", "link": f"https://www.bilibili.com/video/{s['bvid']}",
                "reason": s["why"][:200],
            }
        seen["runs"].append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "picked": len(picked), "skipped": len(skipped),
            "candidates": len(candidates), "fresh": len(fresh), "scanned": scanned,
        })
        seen["runs"] = seen["runs"][-60:]
        self.save_seen(seen)

        # 6) 投递
        dm_result = ""
        dm_mode = str(self.cfg.get("dm_mode") or "off").lower()
        dm_uid = str(self.cfg.get("dm_uid") or "").strip()
        if dm_mode in ("only", "both") and dm_uid and picked and not broken:
            dm_result = await self._send_dm(picked, dm_uid)
        elif broken and dm_mode in ("only", "both"):
            dm_result = "判重表损坏，本轮跳过私信"

        return {
            "date": today,
            "candidates": len(candidates),
            "fresh": len(fresh),
            "scanned": scanned,
            "picked": picked,
            "skipped": skipped,
            "report_path": str(report_path),
            "report_md": report_md,
            "dm": dm_result,
            "auto_comment": auto_comment,
            "window": win["text"] if win else "",
            "risk": risk_abort,
            "elapsed": round(time.time() - started, 1),
        }

    # ---------- 自动评论 ----------

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        """读整数配置。

        别写 `int(cfg.get(key) or default)` —— 0 是 falsy，会被当成"没配"而变回默认值。
        自动评论的配额 0 是有意义的（=关掉），踩过这个坑。
        """
        raw = self.cfg.get(key)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(lo, min(value, hi))

    def _comment_history(self) -> list[dict]:
        if not self.comment_log.exists():
            return []
        out: list[dict] = []
        try:
            for line in self.comment_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            return []
        return out

    def _append_comment_log(self, entry: dict) -> None:
        try:
            with open(self.comment_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            self.log("[warn] 评论流水写不进去")

    @staticmethod
    def _comment_reject(text: str) -> str:
        """过一遍兜底闸，返回拒绝原因（空串 = 可以用）。"""
        if not text:
            return "空的"
        if len(text) < 4:
            return "太短"
        if len(text) > 120:
            return "太长"
        low = text.lower()
        for bad in COMMENT_BLOCKLIST:
            if bad in low:
                return f"含禁用词「{bad}」"
        return ""

    def _at_suffix(self) -> str:
        """评论末尾要 @ 的人（B站昵称）。留空 = 不 @。

        注意：B站 评论的 @ 是纯文本「@昵称」，能不能点开取决于昵称写没写对；
        所以这里只负责拼，拼错了也不会报错，只是不会被识别成提及。
        """
        raw = str(self.cfg.get("comment_at") or "").strip()
        if not raw:
            return ""
        if not raw.startswith("@"):
            raw = "@" + raw
        return raw[:31]

    @staticmethod
    def _parse_comment_reply(raw) -> tuple[bool, str, str]:
        """解析模型返回的 {"want","why","text"} → (想不想评, 评论正文, 理由)。

        解析不出来一律当「不想评」（fail closed）：宁可少发一条，
        也不把模型的自言自语当成评论发到公开评论区。
        """
        text = str(raw or "").strip()
        if not text:
            return False, "", "模型没返回内容"
        data = None
        if "{" in text and "}" in text:
            # 模型爱加 ```json 围栏或前后缀，抠出第一个 { 到最后一个 }
            body = text[text.find("{") : text.rfind("}") + 1]
            try:
                data = json.loads(body)
            except ValueError:
                data = None
        if not isinstance(data, dict):
            return False, "", f"返回的不是 JSON（{text[:30]}）"
        want = data.get("want")
        if isinstance(want, str):
            want = want.strip().lower() in ("true", "yes", "1", "想", "想评", "想评论")
        why = str(data.get("why") or "").strip()[:80]
        comment = str(data.get("text") or "").strip().strip('"“”')
        if not want:
            return False, "", why or "不想评"
        return True, comment, why

    async def _auto_comment(self, picked: list[dict], subtitles: dict[str, str]) -> dict:
        """问模型「这条你想不想评」，想评才发。

        以前是每轮硬发一条（per_run 当配额用）；现在决定权在模型手里：
        逐条问，模型说不想就不发，一条都不发也是正常结果。

        闸门（按顺序）：
          1. 开关 auto_comment
          2. 可选硬上限 auto_comment_per_run / auto_comment_per_day
             （默认 -1=不限；这两个是兜底用的，正常不参与判断）
          3. 这条视频之前评过就跳过（comments.jsonl 里查 bvid）
          4. 模型自己判断想不想评 → 不想评就记一句理由、不打扰它
          5. LLM 写出来的话过禁用词 / 长度检查
          6. B站拒绝（风控 / 频率）→ 本轮立刻停手，不硬撞
        """
        result = {"posted": [], "declined": [], "skipped": "", "error": ""}

        def _skip(reason: str) -> dict:
            result["skipped"] = reason
            self.log(f"自动评论跳过：{reason}")
            return result

        if not self.cfg.get("auto_comment"):
            return _skip("没开自动评论")
        if not self.commenter:
            return _skip("没有可用的评论生成器")
        if not picked:
            return _skip("这轮没挑中视频")

        # 评论多少不再按「每轮/每天」卡，交给模型按喜好判断（见 main 的 comment_eagerness）。
        # 这两个数字退成可选硬上限：-1=不限（默认），0=不评，正数=上限。
        per_run = self._cfg_int("auto_comment_per_run", -1, -1, 5)
        per_day = self._cfg_int("auto_comment_per_day", -1, -1, 20)
        if per_run == 0 or per_day == 0:
            return _skip("配额是 0")

        history = self._comment_history()
        today = datetime.now().strftime("%Y-%m-%d")
        # 只数「评论」，不数「回复」：回复归 reply_max_per_day 管，
        # 混进来会让「今天已评 N 条」虚高，也会让自动评论提前停手
        done_today = sum(
            1 for h in history
            if h.get("auto")
            and not h.get("reply_to")
            and str(h.get("at", "")).startswith(today)
        )
        if per_day > 0:
            left = per_day - done_today
            if left <= 0:
                return _skip(f"今天已经自动评论 {done_today} 条，到上限了")
        else:
            # 日上限设为不限：再多的也就这轮挑中的这些
            left = len(picked)

        commented = {h.get("bvid") for h in history if h.get("bvid")}
        quota = left if per_run < 0 else min(per_run, left)
        for p in picked:
            if quota <= 0:
                break
            bvid = p["bvid"]
            if bvid in commented:
                continue
            source = subtitles.get(bvid, "")
            if not source:
                continue
            try:
                raw = await self.commenter(source[:6000], p.get("title", ""))
            except Exception as exc:
                result["error"] = f"生成评论失败：{exc}"
                self.log(f"[warn] 生成评论失败 {bvid}：{exc}")
                break
            want, text, why = self._parse_comment_reply(raw)
            if not want:
                # 模型的判断也是结果，记下来给日报看（不然"这轮没评论"像坏了）
                p["comment_skip"] = why
                result["declined"].append({"bvid": bvid, "why": why})
                self.log(f"这条不想评 {bvid}：{why}")
                continue
            bad = self._comment_reject(text)
            if not bad:
                # 敏感信息硬闸：QQ / IP / 服务器这类，命中一律不发
                bad = guard.leak_reason(
                    text, [(self.cfg.get("owner_qq") or "", "管理员 QQ")]
                )
            if bad:
                p["comment_skip"] = f"写的不合规（{bad}）"
                self.log(f"[warn] 评论不合规跳过 {bvid}（{bad}）：{text[:40]}")
                continue
            # 正文过了闸再拼 @：模型自己不许写 @，要 @ 谁由配置决定
            at = self._at_suffix()
            full = f"{text} {at}" if at else text
            try:
                data = self._loads(await self._call(self.bili.bili_reply, bvid, full))
            except RiskPause as exc:
                result["error"] = f"{exc}，本轮停止评论"
                self.log(f"[warn] {exc}，本轮停止评论")
                break
            except Exception as exc:
                result["error"] = f"发评论异常：{exc}"
                self.log(f"[warn] 发评论异常 {bvid}：{exc}")
                break
            if not data.get("success"):
                result["error"] = str(data.get("error") or "B站没接受")
                # 也写进这一条本身，日报里才能看出「这条评论没发出去、为什么」
                p["comment_skip"] = f"没发出去（{result['error']}）"
                self.log(f"[warn] 发评论被拒 {bvid}：{result['error']}")
                break  # 被风控就停手，别连着撞
            p["comment"] = full
            p["comment_rpid"] = data.get("rpid")
            result["posted"].append(
                {"bvid": bvid, "text": full, "rpid": data.get("rpid")}
            )
            self._append_comment_log(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "bvid": bvid,
                    "target_rpid": 0,
                    "root": 0,
                    "rpid": data.get("rpid"),
                    "text": full,
                    "auto": True,
                }
            )
            self.log(f"[bili-scroll] 已自动评论 {bvid}：{full[:30]}")
            quota -= 1
            # B站 对连续评论敏感，慢一点；whale 的默认值是 120 秒，这里做成可配
            await asyncio.sleep(self._cfg_int("comment_interval_seconds", 8, 5, 300))
        return result

    # ---------- 输出 ----------

    def _render_report(
        self,
        picked,
        skipped,
        *,
        run_no: int,
        run_at: str,
        scanned: int,
        fresh: int,
        candidates: int,
        window: str = "",
        risk: str = "",
    ) -> str:
        lines = [f"## 第 {run_no} 轮 · {run_at}"]
        if picked:
            for p in picked:
                meta = f"- UP：{p['up']}"
                # 刚发的视频播放量是 0/1，写出来像坏了，干脆不写
                if p.get("play") not in ("0", "1", ""):
                    meta += f"｜播放：{p['play']}"
                lines += [
                    "",
                    f"### {p['title']}",
                    f"- 链接：{p['link']}",
                    meta,
                    f"- **档位**：{p['verdict']}",
                    f"- 为什么值得：{p['reason']}",
                ]
                if p.get("one_liner"):
                    lines.append(f"- 一句话：{p['one_liner']}")
                # 没字幕、靠简介+热评判出来的，标一下：别让人以为判据和别的条目一样
                if p.get("material") and p["material"] != "字幕":
                    lines.append(f"- 素材：{p['material']}（这条没有字幕）")
                if p.get("comment"):
                    lines.append(f"- 已评论：{p['comment']}")
                elif p.get("comment_skip"):
                    # 模型自己判断「不想评」也写出来：不然日报上看不出它开过口
                    lines.append(f"- 没评论：{p['comment_skip']}")
        else:
            # 被风控打断的一轮不能写成「没内容」—— 那是 B站 不让我们看，不是今天没东西
            lines += ["", "这轮没跑完（B站 风控中止）。" if risk else "这轮没挑出值得看的。"]
        # 只把"确实没素材"算进这一栏；取字幕/取评论报错不算（那是工具问题，不是视频没内容）。
        # 所以必须看 keep 标记：临时故障的 why 也以「无字幕」开头，光看文字会把它们算进来。
        no_sub = sum(
            1 for s in skipped
            if s.get("keep")
            and (s["why"].startswith("无字幕") or s["why"].startswith("无语音"))
        )
        if risk:
            lines += ["", f"> ⚠️ 这轮被风控提前中止（{risk}），上面的结果不完整。"]
        lines += [
            "",
            f"> 搜到 {candidates} 条 · 新候选 {fresh} 条 · 扫了 {scanned} 条 · "
            f"挑中 {len(picked)} 条｜无素材跳过 {no_sub}，其它跳过 {len(skipped) - no_sub}"
            + (f"｜时段 {window}" if window else ""),
        ]
        return "\n".join(lines) + "\n"

    async def _send_dm(self, picked: list[dict], dm_uid: str) -> str:
        max_items = int(self.cfg.get("max_dm_items") or 4)
        parts = []
        for p in picked[:max_items]:
            head = p.get("one_liner") or p.get("reason", "")[:40]
            parts.append(f"{p['title']}\n{p['link']}\n{head}")
        text = "\n\n".join(parts)
        if len(text) > 490:  # B站私信实测 500 字符上限
            text = text[:487] + "…"
        try:
            uid = int(dm_uid)
        except ValueError:
            return f"dm_uid 不是数字：{dm_uid}"
        try:
            raw = await self.bili.bili_send_message(uid, text)
            return f"已私信 → {raw[:120]}"
        except Exception as exc:
            return f"私信失败：{exc}"
