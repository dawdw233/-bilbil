"""Timestamped ASR over the official DashScope streaming protocol."""
from __future__ import annotations

import asyncio
import json
import math
import re

import aiohttp


class RecognitionError(Exception):
    """Safe diagnostic; never display a provider response or credentials."""


def subtitle_text(value: str) -> str:
    return re.sub(r"[\x00-\x1f]+", " ", value.strip()).translate(
        str.maketrans({"{": "（", "}": "）", "<": "＜", ">": "＞", "\\": "／"}))


def final_sentence(sentence: dict, duration: float) -> dict | None:
    if sentence.get("heartbeat") or sentence.get("sentence_end") is not True:
        return None
    text = sentence.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    begin, end = sentence.get("begin_time"), sentence.get("end_time")
    if end is None:
        words = sentence.get("words", [])
        ends = [word.get("end_time") for word in words if isinstance(word, dict) and type(word.get("end_time")) is int]
        end = max(ends) if ends else None
    if (type(begin) is not int or type(end) is not int or begin < 0 or not begin < end
            or end > round((duration + .5)*1000) or len(text) > 1000):
        raise RecognitionError("语音识别返回的字幕时间戳或文字无效，已保留原视频。")
    return {"start": begin / 1000, "end": min(duration, end / 1000), "text": subtitle_text(text)}


def validate_segments(rows: list, duration: float) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 200:
        raise RecognitionError("语音识别返回的字幕数量无效。")
    result, previous = [], 0.0
    for row in rows:
        if not isinstance(row, dict):
            raise RecognitionError("字幕记录格式无效。")
        start, end, text = row.get("start"), row.get("end"), row.get("text")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration
                or start < previous or not isinstance(text, str) or not text.strip() or len(text) > 1000):
            raise RecognitionError("字幕时间轴或文字无效，已停止烧录。")
        result.append({"start": start, "end": end, "text": subtitle_text(text)})
        previous = start
    return result


class ParaformerClient:
    def __init__(self, key: str, workspace: str = ""):
        if not isinstance(workspace, str) or (workspace and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", workspace)):
            raise RecognitionError("语音识别业务空间 ID 无效。")
        host = f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"
        self.url = "wss://" + host + "/api-ws/v1/inference"
        self.key = key

    async def recognize(self, pcm: bytes, duration: float, task_id: str) -> list[dict]:
        if (not re.fullmatch(r"[0-9a-f]{32}", task_id) or not 0 < duration <= 65
                or not pcm or len(pcm) % 2 or len(pcm) > 65 * 32000 + 64000):
            raise RecognitionError("识别音频或任务参数无效。")
        sender, finish_sent, started = None, False, False
        results = {}
        try:
            async with asyncio.timeout(duration + 45):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=duration+45)) as session:
                    async with session.ws_connect(self.url, headers={"Authorization": "Bearer " + self.key},
                                                  max_msg_size=256*1024, heartbeat=15) as ws:
                        await ws.send_json({"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                                            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                                                        "model": "paraformer-realtime-v2", "parameters": {
                                                            "format": "pcm", "sample_rate": 16000, "heartbeat": True,
                                                            "disfluency_removal_enabled": False,
                                                            "punctuation_prediction_enabled": True}, "input": {}}})
                        async def send_audio():
                            nonlocal finish_sent
                            # Stream in real time; the reader consumes partial
                            # and final events concurrently, avoiding deadlock.
                            for offset in range(0, len(pcm), 3200):
                                await ws.send_bytes(pcm[offset:offset+3200])
                                await asyncio.sleep(.1)
                            await ws.send_json({"header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"}, "payload": {"input": {}}})
                            finish_sent = True
                        for _ in range(2500):
                            message = await ws.receive(timeout=25)
                            if message.type != aiohttp.WSMsgType.TEXT:
                                raise RecognitionError("语音识别连接提前结束，原视频已保留。")
                            data = json.loads(message.data)
                            header = data.get("header", {})
                            if header.get("task_id") != task_id:
                                raise RecognitionError("语音识别任务编号不匹配。")
                            event = header.get("event")
                            if event == "task-failed":
                                raise RecognitionError("百炼语音识别失败，请检查北京地区 Key、模型权限和余额。")
                            if event == "task-started":
                                if started:
                                    raise RecognitionError("语音识别返回了重复开始事件。")
                                started = True
                                sender = asyncio.create_task(send_audio())
                            elif event == "result-generated":
                                if not started:
                                    raise RecognitionError("语音识别事件顺序无效。")
                                row = final_sentence(data.get("payload", {}).get("output", {}).get("sentence", {}), duration)
                                if row:
                                    results[row["start"]] = row
                            elif event == "task-finished":
                                if not finish_sent:
                                    raise RecognitionError("识别在音频发送完成前结束，未生成字幕。")
                                await sender
                                return validate_segments(sorted(results.values(), key=lambda row: row["start"]), duration)
                            if sender and sender.done():
                                sender.result()
                        raise RecognitionError("语音识别返回事件过多，已停止处理。")
        except RecognitionError:
            raise
        except Exception as exc:
            raise RecognitionError("语音识别连接或响应异常，已保留视频；不会自动重复计费识别。") from exc
        finally:
            if sender and not sender.done():
                sender.cancel()
            if sender:
                await asyncio.gather(sender, return_exceptions=True)
