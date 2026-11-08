"""Bounded, opt-in observations from explicitly disclosed AI videos, not model training."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ZONE = timezone(timedelta(hours=8))
DEFAULTS = {"enabled": False, "daily_limit": 2, "frame_count": 4}
MAX_RECORDS, MAX_SEEN, CONTEXT_LIMIT = 100, 5000, 1200
LIMITATIONS = [
    "AI 身份不能保证百分之百准确：仅接受平台明确的 AI 生成声明，或作者对整段视频画面的明确声明；无声明、含糊和冲突信息均跳过。",
    "少量关键帧只能分析部分构图和动作变化，不能完整判断快速动作、节奏或口型同步；当前不分析音频。",
    "无法从成片还原作者的真实提示词：保存的是待验证的创作推断，不训练视频模型，也不保证生成质量。",
]


class LearningError(ValueError):
    pass


def object_json(text):
    if not isinstance(text, str) or len(text) > 40000:
        raise LearningError("学习模型返回内容过长或无效。")
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I))
    except ValueError as exc:
        raise LearningError("学习模型未返回有效 JSON。") from exc
    if not isinstance(data, dict):
        raise LearningError("学习模型未返回有效对象。")
    return data


def ai_evidence(info: dict) -> dict | None:
    """No model guesses, tags, comments, or generic mentions of AI can open this gate."""
    # A restrictive declaration grammar deliberately prefers false negatives.
    fields = [("平台声明", str(info.get("ai_declaration") or "")),
              ("作者简介声明", str(info.get("description") or ""))]
    accepted = []
    for source, text in fields:
        if re.search(r"(?:AI|人工智能).{0,12}(?:生成|合成).{0,4}[？?]", text, re.I):
            return None
        clauses = re.split(r"[。！!？?\n；;]", text[:4000])
        declarations = []
        conflict = False
        for clause in clauses:
            clause = clause.strip()
            if re.search(r"并非|不是|非AI|非 AI|非人工智能|并未|未使用|不是由|部分|仅.{0,6}(配音|字幕|封面|文案)", clause, re.I):
                conflict = True
            pattern = (r"^(?:本|该)?(?:视频|作品)(?:内容|画面)?(?:全部|完全|均|全程)?"
                       r"(?:由|使用|采用|为)?\s*(?:AI|人工智能)(?:技术)?\s*(?:生成|合成)(?:制作)?"
                       r"(?:[，,：:\s].*)?$")
            if re.fullmatch(pattern, clause, re.I):
                declarations.append(clause)
        if conflict:
            return None
        if declarations:
            accepted.append({"source": source, "text": declarations[0][:160]})
    return accepted[0] if accepted else None


def parse_lessons(text: str, frames: int) -> list[dict]:
    raw = object_json(text).get("lessons")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise LearningError("学习结果须包含 1～3 条有画面依据的经验。")
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise LearningError("学习经验格式无效。")
        row = {}
        for field, size in (("topic", 40), ("rule", 180), ("evidence", 180), ("limitation", 120)):
            value = item.get(field)
            if not isinstance(value, str) or not 2 <= len(value.strip()) <= size:
                raise LearningError("学习经验缺少适用主题、规则、画面依据或局限。")
            row[field] = value.strip()
        tags, supports = item.get("tags"), item.get("frames")
        if (not isinstance(tags, list) or not 1 <= len(tags) <= 6
                or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 20 for tag in tags)
                or not isinstance(supports, list) or not supports
                or any(type(i) is not int or not 1 <= i <= frames for i in supports)):
            raise LearningError("学习经验缺少有效的适用场景或关键帧编号。")
        fingerprint = hashlib.sha256(row["rule"].encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append({**row, "tags": list(dict.fromkeys(tag.strip() for tag in tags)),
                       "frames": sorted(set(supports)), "id": uuid.uuid4().hex, "status": "pending"})
    return result


def terms(text: str) -> set[str]:
    text = text.lower()
    return set(re.findall(r"[a-z0-9]{2,}", text)) | {
        part[i:i+2] for part in re.findall(r"[\u4e00-\u9fff]+", text) for i in range(len(part)-1)
    }


class CreativeLearning:
    def __init__(self, data_dir: Path, analyze, frames):
        self.path = Path(data_dir) / "creative_learning.json"
        self.analyze, self.frames = analyze, frames
        self._lock = threading.RLock()
        self._busy = False
        self._active_id = None

    def _read(self):
        if not self.path.exists():
            return {"settings": dict(DEFAULTS), "records": [], "seen": {}, "daily": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or not isinstance(data.get("records"), list)
                    or not isinstance(data.get("seen"), dict) or not isinstance(data.get("daily"), dict)):
                raise ValueError()
            data["settings"] = self.validate(data.get("settings", {}))
            return data
        except (ValueError, OSError, TypeError) as exc:
            raise LearningError("学习记录无法读取，已停止学习和经验引用；请检查 creative_learning.json，不会覆盖原文件。") from exc

    @staticmethod
    def validate(payload):
        if not isinstance(payload, dict):
            raise LearningError("学习设置必须为对象。")
        settings = {key: payload.get(key, value) for key, value in DEFAULTS.items()}
        if type(settings["enabled"]) is not bool:
            raise LearningError("学习开关必须为布尔值。")
        if type(settings["daily_limit"]) is not int or not 1 <= settings["daily_limit"] <= 3:
            raise LearningError("每日学习上限为 1～3 条视频。")
        if type(settings["frame_count"]) is not int or settings["frame_count"] not in (4, 6):
            raise LearningError("关键帧数量请选择 4 或 6。")
        return settings

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError as exc:
            raise LearningError("学习记录保存失败，请检查服务器存储。") from exc

    def state(self):
        with self._lock:
            data = self._read()
            today = datetime.now(ZONE).date().isoformat()
            return {"settings": data["settings"], "records": list(reversed(data["records"])),
                    "today_attempts": data["daily"].get(today, 0), "busy": self._busy,
                    "active_id": self._active_id,
                    "lesson_count": sum(len(row.get("lessons", [])) for row in data["records"]),
                    "limitations": LIMITATIONS, "context_limit": CONTEXT_LIMIT,
                    "capacity_reached": len(data["seen"]) >= MAX_SEEN}

    def save_settings(self, payload):
        with self._lock:
            data = self._read()
            data["settings"] = self.validate({**data["settings"], **payload}) if isinstance(payload, dict) else self.validate(payload)
            self._write(data)
        return self.state()

    def manage(self, payload):
        if not isinstance(payload, dict) or payload.get("action") not in ("delete", "feedback"):
            raise LearningError("学习记录操作无效。")
        with self._lock:
            data = self._read()
            record = next((row for row in data["records"] if row["id"] == payload.get("id")), None)
            if record is None:
                raise LearningError("学习记录不存在。")
            if record["id"] == self._active_id:
                raise LearningError("这条记录正在学习，请稍后再操作。")
            if payload["action"] == "delete":
                data["records"].remove(record)  # Keep the independent deduplication and daily budget.
            else:
                if payload.get("status") not in ("pending", "useful", "ineffective"):
                    raise LearningError("经验反馈无效。")
                lesson = next((row for row in record.get("lessons", []) if row["id"] == payload.get("lesson_id")), None)
                if lesson is None:
                    raise LearningError("经验不存在。")
                lesson["status"] = payload["status"]
            self._write(data)
        return self.state()

    async def observe(self, bvid, text, call, bili):
        with self._lock:
            data = self._read()
            today = datetime.now(ZONE).date().isoformat()
            settings = data["settings"]
            if (not settings["enabled"] or self._busy or bvid in data["seen"] or len(data["seen"]) >= MAX_SEEN
                    or data["daily"].get(today, 0) >= settings["daily_limit"]):
                return
            self._busy = True
        record_id = None
        try:
            info = await asyncio.wait_for(call(bili.bili_learning_info, bvid), timeout=20)
            evidence = ai_evidence(info)
            if not evidence:
                return
            with self._lock:
                data = self._read()
                # A user may have disabled learning while the metadata request was in flight.
                if not data["settings"]["enabled"] or data["daily"].get(today, 0) >= data["settings"]["daily_limit"]:
                    return
                record_id = uuid.uuid4().hex
                record = {"id": record_id, "bvid": bvid, "title": str(info.get("title") or "")[:160],
                          "created_at": datetime.now(ZONE).isoformat(timespec="seconds"), "declaration": evidence,
                          "phase": "analyzing", "message": "正在分析；中断后不自动重试。", "lessons": []}
                data["seen"][bvid] = today
                data["daily"] = {key: value for key, value in data["daily"].items() if key >= today}
                data["daily"][today] = data["daily"].get(today, 0) + 1
                data["records"] = (data["records"] + [record])[-MAX_RECORDS:]
                self._write(data)  # Reserve budget and deduplicate before downloads and model calls.
                self._active_id = record_id
            if type(info.get("duration")) not in (int, float) or not 2 <= info["duration"] <= 120 or info.get("pages") != 1:
                raise LearningError("第一版仅学习单 P、2～120 秒的视频，已跳过。")
            async with asyncio.timeout(120):
                samples = await self.frames(info, settings["frame_count"], call, bili)
                with self._lock:
                    if not self._read()["settings"]["enabled"]:
                        raise LearningError("学习已关闭，未继续调用分析模型。")
                result = await self.analyze(info, text[:2000], samples)
                lessons = parse_lessons(result, len(samples))
            with self._lock:
                data = self._read()
                row = next((row for row in data["records"] if row["id"] == record_id), None)
                if row and data["settings"]["enabled"]:
                    row.update(phase="done", message="已提取待验证经验；不代表视频模型已学会。", lessons=lessons,
                               sampled_at=[sample["time"] for sample in samples])
                    self._write(data)
                elif row:
                    row.update(phase="skipped", message="学习已关闭，已丢弃本次分析结果。")
                    self._write(data)
        except BaseException as exc:
            if record_id:
                with self._lock:
                    data = self._read()
                    row = next((row for row in data["records"] if row["id"] == record_id), None)
                    if row:
                        row.update(phase="failed", message=str(exc) if isinstance(exc, LearningError) else "学习中断或分析失败，未保存经验；不会自动重复分析。")
                        self._write(data)
            # Preserve DutyRunner's risk pause and cancellation semantics.
            raise
        finally:
            with self._lock:
                self._busy = False
                self._active_id = None

    def retrieve(self, subject: str) -> list[dict]:
        with self._lock:
            data = self._read()
            if not data["settings"]["enabled"]:
                return []
            query, candidates = terms(subject[:3000]), []
            for record in data["records"]:
                for lesson in record.get("lessons", []):
                    if record["phase"] != "done" or lesson["status"] == "ineffective":
                        continue
                    overlap = len(query & terms(lesson["topic"] + " " + " ".join(lesson["tags"])))
                    if overlap or "通用" in lesson["tags"]:
                        candidates.append((overlap, lesson["status"] == "useful", record["created_at"], lesson))
            candidates.sort(key=lambda item: item[:3], reverse=True)
            selected, length, rules = [], 2, set()
            for _, _, _, row in candidates:
                if row["rule"] in rules:
                    continue
                item = {key: row[key] for key in ("id", "topic", "rule", "limitation", "status")}
                size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
                if length + size > CONTEXT_LIMIT:
                    continue
                selected.append(item); length += size; rules.add(row["rule"])
                if len(selected) == 5:
                    break
            return selected
