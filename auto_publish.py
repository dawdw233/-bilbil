"""Scheduled MiniMax generation and Bilibili publication."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

BASE_URLS = {"cn": "https://api.minimax.cn/v1", "global": "https://api.minimax.io/v1"}
DEFAULTS = {
    "enabled": False, "region": "cn", "time": "20:00", "limit": 1,
    "direction": "", "tid": 21, "schedule_after": "",
}
ACTIVE_PHASES = {"queued", "requesting", "generating", "ready", "uploading"}
MAX_VIDEO_BYTES = 100 * 1024 * 1024


class PublishError(Exception):
    """An error safe to display in the dashboard."""


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise PublishError("读取自主投稿数据失败，请检查服务端数据文件。") from exc


def _write(path: Path, data) -> None:
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
            temporary = Path(stream.name)
            try:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            except Exception:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise PublishError("保存自主投稿数据失败，请检查服务端存储。") from exc


def parse_plan(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PublishError("选题模型未返回有效的投稿信息。") from exc
    if not isinstance(data, dict):
        raise PublishError("选题模型未返回有效的投稿信息。")
    fields = {}
    for name, size in (("title", 80), ("prompt", 2000), ("desc", 500), ("tags", 160)):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise PublishError("选题缺少标题、镜头描述、简介或标签。")
        fields[name] = value.strip()[:size]
    return fields


class MiniMaxVideo:
    def __init__(self, region: str, key: str):
        self.base_url = BASE_URLS[region]
        self.headers = {"Authorization": f"Bearer {key}"}

    async def _request(self, method, path, **kwargs):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.request(
                method, self.base_url + path, headers=self.headers, allow_redirects=False, **kwargs,
            ) as response:
                if 400 <= response.status < 500 and response.status not in (408, 429):
                    raise PublishError(f"MiniMax 拒绝请求（HTTP {response.status}）。")
                response.raise_for_status()
                if response.status != 200:
                    raise aiohttp.ClientError("Unexpected API status")
                data = await response.json()
        status = data.get("base_resp", {}).get("status_code")
        if type(status) is not int:
            raise aiohttp.ClientError("Missing MiniMax result status")
        if status != 0:
            raise PublishError(f"MiniMax 拒绝请求（错误码 {status}）。")
        return data

    async def create(self, prompt: str) -> str:
        data = await self._request("POST", "/video_generation", json={
            "model": "MiniMax-Hailuo-2.3", "prompt": prompt, "duration": 6, "resolution": "768P",
        })
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise aiohttp.ClientError("Missing MiniMax task id")
        return task_id

    async def query(self, task_id: str) -> dict:
        return await self._request("GET", "/query/video_generation", params={"task_id": task_id})

    async def download(self, file_id: str, temporary: Path, destination: Path) -> None:
        data = await self._request("GET", "/files/retrieve", params={"file_id": file_id})
        url = data.get("file", {}).get("download_url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PublishError("MiniMax 返回的成片下载地址无效。")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                async with session.get(url, allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.status != 200:
                        raise aiohttp.ClientError("Unexpected download status")
                    total = 0
                    with temporary.open("wb") as stream:
                        async for chunk in response.content.iter_chunked(128 * 1024):
                            total += len(chunk)
                            if total > MAX_VIDEO_BYTES:
                                raise PublishError("生成视频超过 100 MB，已停止下载。")
                            stream.write(chunk)
            with temporary.open("rb") as stream:
                if total < 1024 or stream.read(12)[4:8] != b"ftyp":
                    raise PublishError("下载结果不是有效的 MP4 文件。")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


class AutoPublisher:
    def __init__(self, data_dir: Path, plan, publish, account, upload_busy):
        self.data_dir = data_dir
        self.settings_path = data_dir / "auto_publish_settings.json"
        self.jobs_path = data_dir / "auto_publish_jobs.json"
        self.plan = plan
        self.publish = publish
        self.account = account
        self.upload_busy = upload_busy

    def settings(self) -> dict:
        stored = _read(self.settings_path, {})
        if not isinstance(stored, dict):
            raise PublishError("自主投稿设置格式有误。")
        return {**DEFAULTS, "key": "", **stored}

    def jobs(self) -> list[dict]:
        rows = _read(self.jobs_path, [])
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("phase"), str)
            or not isinstance(row.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", row["id"])
            for row in rows
        ):
            raise PublishError("自主投稿任务记录格式有误。")
        return rows

    def state(self) -> dict:
        settings = self.settings()
        rows = self.jobs()
        return {
            "settings": {key: settings[key] for key in DEFAULTS if key != "schedule_after"},
            "key_configured": bool(settings["key"]),
            "logged_in": bool(self.account()),
            "jobs": [
                {key: row.get(key, "") for key in ("id", "phase", "title", "created_at", "file", "bvid", "message")}
                for row in reversed(rows[-10:])
            ],
        }

    def save_settings(self, payload: dict, now: datetime) -> None:
        current = self.settings()
        settings = {key: payload.get(key, current[key]) for key in DEFAULTS if key != "schedule_after"}
        if type(settings["enabled"]) is not bool:
            raise PublishError("自主投稿开关格式不正确。")
        if settings["region"] not in tuple(BASE_URLS):
            raise PublishError("请选择 MiniMax 平台。")
        if not isinstance(settings["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", settings["time"]):
            raise PublishError("运行时间应为 HH:MM。")
        if type(settings["limit"]) is not int or not 1 <= settings["limit"] <= 5:
            raise PublishError("每日生成上限应为 1～5 支。")
        if type(settings["tid"]) is not int or not 1 <= settings["tid"] <= 9999:
            raise PublishError("请填写有效的 B站分区 ID。")
        if not isinstance(settings["direction"], str) or len(settings["direction"]) > 500:
            raise PublishError("创作方向最多 500 字。")
        supplied = payload.get("api_key", "")
        remove = payload.get("remove_key", False)
        if type(remove) is not bool or not isinstance(supplied, str) or (supplied and not re.fullmatch(r"[!-~]{1,512}", supplied)):
            raise PublishError("API Key 格式不正确。")
        settings["key"] = "" if remove else supplied or current["key"]
        if settings["enabled"] and not settings["key"]:
            raise PublishError("先填写 MiniMax API Key，再开启自主投稿。")
        settings["schedule_after"] = current["schedule_after"]
        if settings["enabled"] and (not current["enabled"] or settings["time"] != current["time"]):
            settings["schedule_after"] = now.isoformat()
        _write(self.settings_path, settings)

    def _update(self, rows: list[dict], job: dict, phase: str, **fields) -> None:
        job.update(phase=phase, **fields)
        _write(self.jobs_path, rows[-60:])

    def enqueue(self, now: datetime, *, manual: bool = False) -> dict | None:
        settings = self.settings()
        rows = self.jobs()
        account = self.account()
        blocked = ""
        if not settings["enabled"] or not settings["key"]:
            blocked = "请先配置 MiniMax API Key 并开启自主投稿。"
        elif not account:
            blocked = "请先在「刷视频」页登录 B站。"
        elif any(job["phase"] in ACTIVE_PHASES for job in rows):
            blocked = "已有自主投稿任务，请等它处理完。"
        today = now.date().isoformat()
        today_jobs = [job for job in rows if job.get("day") == today]
        if not blocked and len(today_jobs) >= settings["limit"]:
            blocked = "已达到今天的生成上限。"
        if blocked:
            if manual:
                raise PublishError(blocked)
            return None
        if not manual:
            hour, minute = map(int, settings["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            after = settings["schedule_after"]
            if now < due or (after and due < datetime.fromisoformat(after)) or any(
                job.get("scheduled") for job in today_jobs
            ):
                return None

        job = {
            "id": uuid.uuid4().hex, "phase": "queued", "created_at": now.isoformat(timespec="seconds"),
            "day": today, "scheduled": not manual, "account": account, "tid": settings["tid"],
        }
        _write(self.jobs_path, [*rows, job][-60:])
        return job

    async def tick(self, now: datetime) -> None:
        settings = self.settings()
        if not settings["enabled"] or not settings["key"]:
            return
        rows = self.jobs()
        job = next((row for row in reversed(rows) if row["phase"] in ACTIVE_PHASES), None)
        if job is None:
            job = self.enqueue(now)
            if job is None:
                return
            rows = self.jobs()
            job = rows[-1]
        phase = job["phase"]
        if phase in ("requesting", "uploading"):
            self._update(rows, job, "review", message="上次提交未能确认，请到 MiniMax 或 B站核对。")
            return
        account = self.account()
        if not account:
            return
        if account != job["account"]:
            self._update(rows, job, "review", message="B站账号已切换，任务暂停待核对。")
            return
        if phase == "queued":
            try:
                recent = [row["title"] for row in rows[-13:-1] if row.get("title")]
                plan = parse_plan(await self.plan(settings["direction"], recent))
                if plan["title"] in recent:
                    raise PublishError("选题与近期稿件重复。")
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
                return
            except Exception:
                self._update(rows, job, "failed", message="选题失败，请检查 AstrBot 的文本模型配置。")
                return
            latest = self.settings()
            if not latest["enabled"] or not latest["key"] or self.account() != job["account"]:
                return
            self._update(
                rows, job, "requesting", title=plan["title"], desc=plan["desc"], tags=plan["tags"],
                region=latest["region"], submitted_at=now.isoformat(),
            )
            try:
                task_id = await MiniMaxVideo(latest["region"], latest["key"]).create(plan["prompt"])
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
            except Exception:
                self._update(rows, job, "review", message="MiniMax 受理状态不明，请核对生成记录。")
            else:
                self._update(rows, job, "generating", task_id=task_id)
            return

        if phase == "generating":
            client = MiniMaxVideo(job["region"], settings["key"])
            submitted = datetime.fromisoformat(job["submitted_at"])
            try:
                result = await client.query(job["task_id"])
                if result.get("status") == "Fail":
                    raise PublishError("MiniMax 生成失败。")
                if result.get("status") != "Success":
                    if now - submitted >= timedelta(minutes=30):
                        self._update(rows, job, "review", message="生成等待超时，请核对 MiniMax 任务。")
                    return
                file_id = result.get("file_id")
                if not file_id:
                    raise PublishError("MiniMax 已完成生成，但缺少成片编号。")
                name = f"ai-{job['id']}.mp4"
                destination = self.data_dir / "media" / name
                if not destination.is_file():
                    await client.download(str(file_id), self.data_dir / f".ai-{job['id']}.part", destination)
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
                return
            except Exception:
                if now - submitted >= timedelta(minutes=30):
                    self._update(rows, job, "review", message="暂未取得成片，请核对 MiniMax 任务。")
                return
            self._update(rows, job, "ready", file=name)
            return

        if self.upload_busy():
            return
        video = self.data_dir / "media" / f"ai-{job['id']}.mp4"
        if not video.is_file():
            self._update(rows, job, "failed", message="成片文件已移除。")
            return
        self._update(rows, job, "uploading")
        try:
            async with asyncio.timeout(900):
                result = await self.publish({
                    "video_path": str(video), "title": job["title"],
                    "desc": "AI 生成视频 · MiniMax 海螺\n\n" + job["desc"],
                    "tags": "AI生成," + job["tags"], "tid": job["tid"],
                })
        except Exception:
            self._update(rows, job, "review", message="投稿结果未能确认，请到 B站创作中心核对。")
            return
        bvid = result.get("bvid")
        if result.get("success") and isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            self._update(rows, job, "published", bvid=bvid)
        else:
            self._update(rows, job, "review", message="投稿结果未能确认，请到 B站创作中心核对。")
