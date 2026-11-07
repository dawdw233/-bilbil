"""Scheduled video generation and Bilibili publication."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

VIDEO_PROVIDERS = {
    "minimax": {
        "name": "MiniMax 海螺", "model": "MiniMax-Hailuo-2.3", "duration": 6,
        "regions": {
            "cn": ("中国站", "https://api.minimax.cn/v1"),
            "global": ("国际站", "https://api.minimax.io/v1"),
        },
    },
    "ark": {
        "name": "火山方舟 Seedance", "model": "doubao-seedance-2-5-260628", "duration": 5,
        "regions": {"cn": ("北京", "https://ark.cn-beijing.volces.com/api/v3")},
    },
    "wan": {
        "name": "阿里云百炼 通义万相", "model": "wan2.6-t2v", "duration": 5,
        "regions": {
            "cn": ("中国内地", "https://dashscope.aliyuncs.com/api/v1"),
            "global": ("新加坡", "https://dashscope-intl.aliyuncs.com/api/v1"),
        },
    },
}
DEFAULTS = {
    "enabled": False, "provider": "minimax", "region": "cn", "time": "20:00",
    "limit": 1, "direction": "", "tid": 21, "schedule_after": "",
}
ACTIVE_PHASES = {"queued", "requesting", "generating", "ready", "uploading"}
MAX_VIDEO_BYTES = 100 * 1024 * 1024


class PublishError(Exception):
    """An error safe to display in the dashboard."""


def supported_video_model(provider: str, model: str) -> bool:
    if provider == "minimax":
        return model in {"MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02", "T2V-01", "T2V-01-Director"}
    if provider == "wan":
        return model in {
            "wanx2.1-t2v-turbo", "wanx2.1-t2v-plus", "wan2.2-t2v-plus",
            "wan2.5-t2v-preview", "wan2.6-t2v",
        }
    return bool(re.fullmatch(r"(?:doubao-seedance-[A-Za-z0-9_.-]+|ep-[A-Za-z0-9-]+)", model)) and "-i2v" not in model


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


class VideoClient:
    def __init__(self, provider: str, region: str, key: str, model: str):
        spec = VIDEO_PROVIDERS[provider]
        self.name = spec["name"]
        self.provider = provider
        self.duration = spec["duration"]
        self.model = model
        self.base_url = spec["regions"][region][1]
        self.headers = {"Authorization": f"Bearer {key}"}

    async def _request(self, method, path, *, headers=None, **kwargs):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.request(
                method, self.base_url + path, headers={**self.headers, **(headers or {})},
                allow_redirects=False, **kwargs,
            ) as response:
                if 400 <= response.status < 500 and response.status not in (408, 429):
                    raise PublishError(f"{self.name} 拒绝请求（HTTP {response.status}）。")
                response.raise_for_status()
                if response.status not in (200, 201, 202):
                    raise aiohttp.ClientError("Unexpected API status")
                data = await response.json()
        if not isinstance(data, dict):
            raise aiohttp.ClientError("Invalid API response")
        return data

    def _task_id(self, value) -> str:
        if not isinstance(value, str) or not value:
            raise aiohttp.ClientError("Missing task id")
        return value

    async def models(self) -> list[dict]:
        results = {}
        for page in range(1, 11):
            params = {"capabilities": "VG", "page_no": page, "page_size": 100} if self.provider == "wan" else {}
            # MiniMax's OpenAI-compatible listing does not return base_resp.
            data = await VideoClient._request(self, "GET", "/models", params=params)
            if data.get("code") or data.get("success") is False or data.get("error"):
                raise PublishError(f"{self.name} 未能返回模型列表，请检查 Key 与地区。")
            output = data.get("output", {})
            rows = output.get("models") if self.provider == "wan" else data.get("data")
            if not isinstance(rows, list):
                raise PublishError(f"{self.name} 返回的模型列表格式无法识别。")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                model = row.get("id") or row.get("model")
                if not isinstance(model, str) or not supported_video_model(self.provider, model):
                    continue
                if row.get("status") in ("Offline", "offline", "Disabled", "disabled"):
                    continue
                label = row.get("name")
                results[model] = {"id": model, "name": label if isinstance(label, str) else model}
            if self.provider != "wan" or len(rows) < 100:
                break
            total = output.get("total")
            if type(total) is int and page * 100 >= total:
                break
        return list(results.values())

    async def download(self, url: str, temporary: Path, destination: Path) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PublishError(f"{self.name} 返回的成片下载地址无效。")
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


class MiniMaxVideo(VideoClient):
    async def _request(self, method, path, **kwargs):
        data = await super()._request(method, path, **kwargs)
        status = data.get("base_resp", {}).get("status_code")
        if type(status) is not int:
            raise aiohttp.ClientError("Missing MiniMax result status")
        if status != 0:
            raise PublishError(f"MiniMax 拒绝请求（错误码 {status}）。")
        return data

    async def create(self, prompt: str) -> str:
        data = await self._request("POST", "/video_generation", json={
            "model": self.model, "prompt": prompt, "duration": self.duration, "resolution": "720P" if self.model.startswith("T2V-") else "768P",
        })
        return self._task_id(data.get("task_id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/query/video_generation", params={"task_id": task_id})
        if data.get("status") == "Fail":
            raise PublishError("MiniMax 生成失败。")
        if data.get("status") == "Success":
            return self._task_id(data.get("file_id"))
        return None

    async def download(self, file_id: str, temporary: Path, destination: Path) -> None:
        data = await self._request("GET", "/files/retrieve", params={"file_id": file_id})
        await super().download(data.get("file", {}).get("download_url", ""), temporary, destination)


class ArkVideo(VideoClient):
    async def create(self, prompt: str) -> str:
        data = await self._request("POST", "/contents/generations/tasks", json={
            "model": self.model, "content": [{"type": "text", "text": prompt}],
            "duration": self.duration, "resolution": "720p", "ratio": "16:9",
        })
        return self._task_id(data.get("id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/contents/generations/tasks/" + quote(task_id, safe=""))
        status = data.get("status")
        if status in ("failed", "cancelled", "expired"):
            raise PublishError(f"火山方舟任务未完成（{status}）。")
        if status == "succeeded":
            return self._task_id(data.get("content", {}).get("video_url"))
        return None


class WanVideo(VideoClient):
    async def create(self, prompt: str) -> str:
        data = await self._request(
            "POST", "/services/aigc/video-generation/video-synthesis",
            headers={"X-DashScope-Async": "enable"},
            json={
                "model": self.model, "input": {"prompt": prompt},
                "parameters": {"size": "1280*720", "duration": self.duration, "watermark": True},
            },
        )
        if data.get("code"):
            raise PublishError("阿里云百炼拒绝创建视频任务，请检查 Key 与模型权限。")
        return self._task_id(data.get("output", {}).get("task_id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/tasks/" + quote(task_id, safe=""))
        output = data.get("output", {})
        status = output.get("task_status")
        if status in ("FAILED", "CANCELED"):
            raise PublishError(f"阿里云百炼任务未完成（{status}）。")
        if status == "SUCCEEDED":
            return self._task_id(output.get("video_url"))
        return None


VIDEO_CLIENTS = {"minimax": MiniMaxVideo, "ark": ArkVideo, "wan": WanVideo}


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
        settings = {**DEFAULTS, **stored}
        profiles = settings.get("profiles", {})
        if not isinstance(profiles, dict):
            raise PublishError("视频生成服务配置格式有误。")
        legacy_key = settings.pop("key", "")
        if legacy_key:
            profiles.setdefault(f"minimax:{settings['region']}", {"key": legacy_key})
        settings["profiles"] = profiles
        return settings

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
            "providers": {
                name: {
                    "name": spec["name"], "model": spec["model"], "duration": spec["duration"],
                    "regions": {region: label for region, (label, _) in spec["regions"].items()},
                }
                for name, spec in VIDEO_PROVIDERS.items()
            },
            "profiles": {
                name: {"key_configured": bool(profile.get("key")), "model": profile.get("model", "")}
                for name, profile in settings["profiles"].items()
            },
            "key_configured": bool(settings["profiles"].get(f"{settings['provider']}:{settings['region']}", {}).get("key")),
            "logged_in": bool(self.account()),
            "jobs": [
                {
                    **{key: row.get(key, "") for key in ("id", "phase", "title", "created_at", "file", "bvid", "message", "model")},
                    "provider": row.get("provider", "minimax"),
                }
                for row in reversed(rows[-10:])
            ],
        }

    async def models(self, payload: dict) -> dict:
        settings = self.settings()
        provider = payload.get("provider", settings["provider"])
        region = payload.get("region", settings["region"])
        supplied = payload.get("api_key", "")
        if not isinstance(provider, str) or provider not in VIDEO_PROVIDERS:
            raise PublishError("请选择支持的视频生成服务。")
        if not isinstance(region, str) or region not in VIDEO_PROVIDERS[provider]["regions"]:
            raise PublishError("请选择该服务支持的地区。")
        if not isinstance(supplied, str) or (supplied and not re.fullmatch(r"[!-~]{1,512}", supplied)):
            raise PublishError("API Key 格式不正确。")
        key = supplied or settings["profiles"].get(f"{provider}:{region}", {}).get("key")
        if not key:
            raise PublishError("先填写该服务的 Key，再读取模型。")
        try:
            client = VIDEO_CLIENTS[provider](provider, region, key, VIDEO_PROVIDERS[provider]["model"])
            models = await client.models()
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("读取模型列表失败，请检查服务商连接后重试。") from exc
        return {"models": models}

    def save_settings(self, payload: dict, now: datetime) -> None:
        current = self.settings()
        settings = {key: payload.get(key, current[key]) for key in DEFAULTS if key != "schedule_after"}
        if type(settings["enabled"]) is not bool:
            raise PublishError("自主投稿开关格式不正确。")
        provider, region = settings["provider"], settings["region"]
        if not isinstance(provider, str) or provider not in VIDEO_PROVIDERS:
            raise PublishError("请选择支持的视频生成服务。")
        spec = VIDEO_PROVIDERS[provider]
        if not isinstance(region, str) or region not in spec["regions"]:
            raise PublishError("请选择该服务支持的地区。")
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
        profile_name = f"{provider}:{region}"
        profiles = dict(current["profiles"])
        profile = profiles.get(profile_name, {})
        model = payload.get("model", profile.get("model", ""))
        if not isinstance(model, str) or (model and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", model)):
            raise PublishError("请选择有效的视频模型。")
        if model and not supported_video_model(provider, model):
            raise PublishError("该模型尚未适配当前文生视频接口，请从读取结果中选择。")
        key = "" if remove else supplied or profile.get("key", "")
        if settings["enabled"] and not key:
            raise PublishError("先填写所选服务的 API Key，再开启自主投稿。")
        profiles[profile_name] = {"key": key, "model": model}
        settings["profiles"] = profiles
        settings["schedule_after"] = current["schedule_after"]
        if settings["enabled"] and (
            not current["enabled"] or any(settings[key] != current[key] for key in ("time", "provider", "region"))
        ):
            settings["schedule_after"] = now.isoformat()
        # Older queued jobs did not record a region until submission.
        if (provider, region) != (current["provider"], current["region"]):
            rows = self.jobs()
            legacy = [row for row in rows if row["phase"] in ACTIVE_PHASES and "provider" not in row]
            if legacy:
                for row in legacy:
                    row["provider"] = "minimax"
                    row.setdefault("region", current["region"])
                _write(self.jobs_path, rows)
        _write(self.settings_path, settings)

    def _update(self, rows: list[dict], job: dict, phase: str, **fields) -> None:
        job.update(phase=phase, **fields)
        _write(self.jobs_path, rows[-60:])

    def enqueue(self, now: datetime, *, manual: bool = False) -> dict | None:
        settings = self.settings()
        rows = self.jobs()
        account = self.account()
        blocked = ""
        provider, region = settings["provider"], settings["region"]
        profile = settings["profiles"].get(f"{provider}:{region}", {})
        if not settings["enabled"] or not profile.get("key"):
            blocked = "请先配置所选服务的 API Key 并开启自主投稿。"
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
            "provider": provider, "region": region, "model": profile.get("model") or VIDEO_PROVIDERS[provider]["model"],
        }
        _write(self.jobs_path, [*rows, job][-60:])
        return job

    async def tick(self, now: datetime) -> None:
        settings = self.settings()
        if not settings["enabled"]:
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
        provider = job.get("provider", "minimax")
        spec = VIDEO_PROVIDERS[provider]
        region = job.get("region", settings["region"] if settings["provider"] == provider else "cn")
        profile_name = f"{provider}:{region}"
        model = job.get("model") or spec["model"]
        if phase in ("requesting", "uploading"):
            service = spec["name"] if phase == "requesting" else "B站"
            self._update(rows, job, "review", message=f"上次提交未能确认，请到{service}核对。")
            return
        account = self.account()
        if not account:
            return
        if account != job["account"]:
            self._update(rows, job, "review", message="B站账号已切换，任务暂停待核对。")
            return
        key = settings["profiles"].get(profile_name, {}).get("key")
        if phase in ("queued", "generating") and not key:
            self._update(rows, job, "review", message=f"{spec['name']} 的 Key 已清除，请到服务商核对任务。")
            return
        if phase == "queued":
            try:
                recent = [row["title"] for row in rows[-13:-1] if row.get("title")]
                plan = parse_plan(await self.plan(settings["direction"], recent, spec["duration"]))
                if plan["title"] in recent:
                    raise PublishError("选题与近期稿件重复。")
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
                return
            except Exception:
                self._update(rows, job, "failed", message="选题失败，请检查 AstrBot 的文本模型配置。")
                return
            latest = self.settings()
            key = latest["profiles"].get(profile_name, {}).get("key")
            if not latest["enabled"] or not key or self.account() != job["account"]:
                return
            self._update(
                rows, job, "requesting", title=plan["title"], desc=plan["desc"], tags=plan["tags"],
                provider=provider, region=region, model=model, submitted_at=now.isoformat(),
            )
            try:
                task_id = await VIDEO_CLIENTS[provider](provider, region, key, model).create(plan["prompt"])
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
            except Exception:
                self._update(rows, job, "review", message=f"{spec['name']} 受理状态不明，请核对生成记录。")
            else:
                self._update(rows, job, "generating", task_id=task_id)
            return

        if phase == "generating":
            client = VIDEO_CLIENTS[provider](provider, region, key, model)
            submitted = datetime.fromisoformat(job["submitted_at"])
            try:
                artifact = await client.query(job["task_id"])
                if not artifact:
                    if now - submitted >= timedelta(minutes=30):
                        self._update(rows, job, "review", message=f"生成等待超时，请核对{spec['name']}任务。")
                    return
                name = f"ai-{job['id']}.mp4"
                destination = self.data_dir / "media" / name
                if not destination.is_file():
                    await client.download(artifact, self.data_dir / f".ai-{job['id']}.part", destination)
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
                return
            except Exception:
                if now - submitted >= timedelta(minutes=30):
                    self._update(rows, job, "review", message=f"暂未取得成片，请核对{spec['name']}任务。")
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
                    "desc": f"AI 生成视频 · {spec['name']}\n\n" + job["desc"],
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
