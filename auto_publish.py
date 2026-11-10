"""Scheduled video generation and Bilibili publication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from PIL import Image, ImageOps
from .api_config import api_base

from .video_compose import CompositionError, compose_video
from .reference_images import ReferenceError, ReferenceLibrary
from .voiceover import VoiceoverError
from .story_director import NATIVE_SOUND_PREFIXES, character_prompt, directed_shot, frame_path, handoff_frame, native_sound, parse_dialogue, shot_prompt

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
    "openai": {"name": "Videos API 兼容（自定义服务）", "model": "", "duration": 4,
               "regions": {"custom": ("自定义服务", "")}},
}
DEFAULTS = {
    "enabled": False, "provider": "minimax", "region": "cn", "time": "20:00",
    "limit": 1, "direction": "", "tid": 21, "auto_category": False, "schedule_after": "",
    "mode": "single", "total_duration": 30, "reference_enabled": False, "shot_link": "independent",
}
ACTIVE_PHASES = {"queued", "requesting", "generating", "storyboard", "assembling", "voiceover", "ready", "uploading"}
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VIDEO_MODELS = {
    "minimax": ("MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02", "T2V-01", "T2V-01-Director"),
    "wan": ("wan2.6-t2v", "wan2.5-t2v-preview", "wan2.2-t2v-plus", "wanx2.1-t2v-turbo", "wanx2.1-t2v-plus"),
    "ark": (VIDEO_PROVIDERS["ark"]["model"],),
    "openai": (),
}
DURATION_RULES = {
    "minimax": [("MiniMax-Hailuo-", [6, 10]), ("T2V-", [6])],
    "wan": [("wan2.6-", [5, 10, 15]), ("wan2.5-", [5, 10])],
    "ark": [
        ("doubao-seedance-2-5-", list(range(4, 31))),
        ("doubao-seedance-2-0-", list(range(4, 16))),
        ("doubao-seedance-1-0-", list(range(2, 13))),
    ],
    "openai": [("", [4, 8, 10, 12])],
}


def duration_options(provider: str, model: str) -> list[int]:
    for prefix, options in DURATION_RULES[provider]:
        if model.startswith(prefix):
            return list(options)
    # Opaque Ark endpoint IDs do not disclose their underlying model.
    return [5, 10] if provider == "ark" else [VIDEO_PROVIDERS[provider]["duration"]]


def video_duration(provider: str, model: str, value) -> int:
    if type(value) is not int or value not in duration_options(provider, model):
        raise PublishError("所选视频模型不支持这个时长，请重新选择。")
    return value


def storyboard_lengths(provider: str, model: str, total: int) -> list[int]:
    if type(total) is not int or total not in (30, 60):
        raise PublishError("分镜成片时长请选择 30 秒或 60 秒。")
    options = sorted((value for value in duration_options(provider, model) if value <= 15), reverse=True)
    plans = {0: []}
    for seconds in range(1, total + 1):
        choices = [[*plans[seconds - value], value] for value in options if seconds - value in plans]
        if choices:
            plans[seconds] = min(choices, key=len)
    result = sorted(plans.get(total, []), reverse=True)
    if not 2 <= len(result) <= 12:
        raise PublishError("这个模型暂时无法组成所选成片时长。")
    return result


class PublishError(Exception):
    """An error safe to display in the dashboard."""


class GenerationFailed(PublishError):
    """The provider explicitly confirmed that an existing task failed."""


REFERENCE_MODELS = {
    "minimax": {model: model for model in ("MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02")},
    "wan": {"wan2.6-t2v": "wan2.6-i2v", "wan2.5-t2v-preview": "wan2.5-i2v-preview"},
}


def reference_video_model(provider: str, model: str) -> str:
    if provider == "openai" and supported_video_model(provider, model):
        return model
    if provider == "ark" and supported_video_model(provider, model) and "-t2v" not in model:
        return model
    models = REFERENCE_MODELS.get(provider, {})
    if model in models:
        return models[model]
    if model in models.values():
        return model
    raise PublishError("此模型尚未适配参考图，请选择海螺 2.3/02、支持图生视频的方舟模型或万相 2.5/2.6。")


def supported_video_model(provider: str, model: str) -> bool:
    if provider == "openai":
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}", model))
    if provider in ("minimax", "wan"):
        return model in VIDEO_MODELS[provider]
    return bool(re.fullmatch(r"(?:doubao-seedance-[A-Za-z0-9_.-]+|ep-[A-Za-z0-9-]+)", model)) and "-i2v" not in model


def publication_tags(value: str) -> str:
    tags = ["AI生成"]
    seen = {"ai生成"}
    for tag in re.split(r"[,，;；\r\n]+", value):
        tag = tag.strip()
        if tag and tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())
        if len(tags) == 10:
            break
    return ",".join(tags)


def key_revision(key: str) -> str:
    # Only a fingerprint is stored on the job, never a second copy of the Key.
    return hashlib.sha256(key.encode()).hexdigest()


def workspace_id(value) -> str:
    if not isinstance(value, str) or (value and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)):
        raise PublishError("百炼业务空间 ID 格式不正确，请填写 ID 而不是网址。")
    return value


def upload_error(value) -> str:
    text = str(value or "未返回明确的投稿结果")
    text = re.sub(r"https?://\S+", "[地址已隐藏]", text)
    text = re.sub(r"(?i)(SESSDATA|bili_jct|token|api[_ -]?key|authorization)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    return re.sub(r"[\r\n\x00-\x1f]+", " ", text)[:240]


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


def parse_plan(text: str, duration: int = 65) -> dict:
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
    fields["tags"] = publication_tags(fields["tags"])
    if isinstance(data.get("learning_used"), list):
        fields["learning_used"] = [
            {key: str(row.get(key, ""))[:size] for key, size in (("id", 32), ("topic", 40), ("rule", 180), ("limitation", 120), ("status", 20))}
            for row in data["learning_used"][:5] if isinstance(row, dict) and re.fullmatch(r"[0-9a-f]{32}", str(row.get("id", "")))
        ]
    if "dialogue" in data:
        try:
            fields["dialogue"] = parse_dialogue(data["dialogue"], duration)
        except CompositionError as exc:
            raise PublishError(str(exc)) from exc
    return fields


def parse_storyboard(text: str, durations: list[int]) -> dict:
    fields = parse_plan(text)
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    data = json.loads(raw)
    continuity, shots = data.get("continuity"), data.get("shots")
    if not isinstance(continuity, str) or not continuity.strip() or not isinstance(shots, list) or len(shots) != len(durations):
        raise PublishError("分镜选题缺少统一设定，或镜头数量不符合要求。")
    parsed = []
    for shot, duration in zip(shots, durations):
        if (not isinstance(shot, dict) or not isinstance(shot.get("prompt"), str) or not shot["prompt"].strip()
                or type(shot.get("duration", duration)) is not int or shot.get("duration", duration) != duration):
            raise PublishError("分镜描述或时长无效，尚未开始付费生成。")
        try:
            direction = directed_shot(shot, duration)
        except CompositionError as exc:
            raise PublishError(str(exc)) from exc
        parsed.append({"prompt": shot["prompt"].strip()[:1200], "duration": duration, "phase": "queued", **direction})
    if any("narration" in shot for shot in parsed) and not all("narration" in shot for shot in parsed):
        raise PublishError("分镜方案不完整，请为每一镜安排动作、声音和旁白。")
    return {**fields, "continuity": continuity.strip()[:400], "shots": parsed}


def parse_category(text: str, categories: list[dict]) -> dict:
    """Accept only an available submission category and a concrete explanation."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PublishError("分区判断未返回有效结果。") from exc
    if not isinstance(data, dict):
        raise PublishError("分区判断未返回有效结果。")
    tid, reason = data.get("tid"), data.get("reason")
    category = next((item for item in categories if type(tid) is int and item["tid"] == tid), None)
    if category is None or not isinstance(reason, str) or not reason.strip():
        raise PublishError("分区判断未选择可用分区或未说明理由。")
    return {"tid": tid, "category_name": category["name"], "category_reason": reason.strip()[:200]}


class VideoClient:
    def __init__(self, provider: str, region: str, key: str, model: str, workspace: str = "", duration: int | None = None, *, base_url: str = "", transport: str = "multipart"):
        spec = VIDEO_PROVIDERS[provider]
        self.name = spec["name"]
        self.provider = provider
        self.duration = video_duration(provider, model, spec["duration"] if duration is None else duration)
        self.model = model
        try:
            self.base_url = api_base(base_url) or spec["regions"][region][1]
        except ValueError as exc:
            raise PublishError(str(exc)) from exc
        self.transport = transport
        self.custom_endpoint = bool(base_url)
        if provider == "openai" and self.base_url.endswith("/videos"):
            self.base_url = self.base_url[:-7]
        if provider == "openai" and not self.base_url:
            raise PublishError("请填写兼容视频服务的 API 地址，不能使用已下线的官方视频接口。")
        self.headers = {"Authorization": f"Bearer {key}"}
        self.models_url = self.base_url
        if provider == "wan" and not base_url:
            workspace = workspace_id(workspace)
            location = "cn-beijing" if region == "cn" else "ap-southeast-1"
            self.models_url = f"https://{workspace}.{location}.maas.aliyuncs.com/api/v1" if workspace else ""

    async def _request(self, method, path, *, headers=None, **kwargs):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.request(
                method, (self.models_url if path == "/models" else self.base_url) + path,
                headers={**self.headers, **(headers or {})},
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
        if not self.models_url:
            raise PublishError("读取百炼模型需填写业务空间 ID；也可以直接选择已适配模型。")
        results = {}
        for page in range(1, 11):
            params = {"capabilities": "VG", "page_no": page, "page_size": 100} if self.provider == "wan" else {}
            # OpenAI-compatible responses may omit base_resp, but explicit
            # MiniMax errors (including HTTP 200 errors) must still be rejected.
            data = await VideoClient._request(self, "GET", "/models", params=params)
            if self.provider == "minimax" and "base_resp" in data:
                result = data["base_resp"]
                status = result.get("status_code") if isinstance(result, dict) else None
                if type(status) is not int or status != 0:
                    raise PublishError("MiniMax 未能返回模型列表，请检查 Key 与地区。")
            if data.get("code") or data.get("success") is False or data.get("error"):
                raise PublishError(f"{self.name} 未能返回模型列表，请检查 Key 与地区。")
            output = data.get("output", {})
            rows = output.get("models") if self.provider == "wan" else data.get("data")
            if self.custom_endpoint and not isinstance(rows, list):
                rows = data.get("data")
            if not isinstance(rows, list):
                raise PublishError(f"{self.name} 返回的模型列表格式无法识别。")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                model = row.get("id") or row.get("model")
                if not isinstance(model, str) or not (supported_video_model(self.provider, model) or self.custom_endpoint and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}", model)):
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

    async def download(self, url: str, temporary: Path, destination: Path, *, headers=None) -> None:
        parsed = urlsplit(url)
        local_api = self.base_url.startswith("http://") and url.startswith(self.base_url + "/")
        if (parsed.scheme != "https" and not local_api) or not parsed.hostname or parsed.username or parsed.password:
            raise PublishError(f"{self.name} 返回的成片下载地址无效。")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                async with session.get(url, **({"headers": headers} if headers else {}), allow_redirects=False) as response:
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

    async def create(self, prompt: str, *, reference_image: str | None = None) -> str:
        body = {
            "model": self.model, "prompt": prompt, "duration": self.duration, "resolution": "720P" if self.model.startswith("T2V-") else "768P",
        }
        if reference_image:
            body.update(model=reference_video_model(self.provider, self.model), first_frame_image=reference_image)
        data = await self._request("POST", "/video_generation", json=body)
        return self._task_id(data.get("task_id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/query/video_generation", params={"task_id": task_id})
        if data.get("status") == "Fail":
            raise GenerationFailed("MiniMax 生成失败。")
        if data.get("status") == "Success":
            return self._task_id(data.get("file_id"))
        return None

    async def download(self, file_id: str, temporary: Path, destination: Path) -> None:
        data = await self._request("GET", "/files/retrieve", params={"file_id": file_id})
        await super().download(data.get("file", {}).get("download_url", ""), temporary, destination)


class ArkVideo(VideoClient):
    async def create(self, prompt: str, *, reference_image: str | None = None) -> str:
        content = [{"type": "text", "text": prompt}]
        if reference_image:
            reference_video_model(self.provider, self.model)
            content.append({"type": "image_url", "image_url": {"url": reference_image}, "role": "first_frame"})
        body = {
            "model": self.model, "content": content,
            "duration": self.duration, "resolution": "720p", "ratio": "16:9",
        }
        if native_sound(self.provider, self.model):
            body["generate_audio"] = True
        data = await self._request("POST", "/contents/generations/tasks", json=body)
        return self._task_id(data.get("id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/contents/generations/tasks/" + quote(task_id, safe=""))
        status = data.get("status")
        if status in ("failed", "cancelled", "expired"):
            raise GenerationFailed(f"火山方舟任务未完成（{status}）。")
        if status == "succeeded":
            return self._task_id(data.get("content", {}).get("video_url"))
        return None


class WanVideo(VideoClient):
    async def create(self, prompt: str, *, reference_image: str | None = None) -> str:
        model = reference_video_model(self.provider, self.model) if reference_image else self.model
        inputs = {"prompt": prompt}
        parameters = {"size": "1280*720", "duration": self.duration, "watermark": True}
        if reference_image:
            inputs["img_url"] = reference_image
            parameters.pop("size")
            parameters["resolution"] = "720P"
        data = await self._request(
            "POST", "/services/aigc/video-generation/video-synthesis",
            headers={"X-DashScope-Async": "enable"},
            json={
                "model": model, "input": inputs, "parameters": parameters,
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
            raise GenerationFailed(f"阿里云百炼任务未完成（{status}）。")
        if status == "SUCCEEDED":
            return self._task_id(output.get("video_url"))
        return None


class CompatibleVideo(VideoClient):
    async def create(self, prompt: str, *, reference_image: str | None = None) -> str:
        reference = None
        if reference_image:
            try:
                encoded = reference_image.split(",", 1)[1]
                with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as source:
                    image = ImageOps.pad(source.convert("RGB"), (1280,720), color="#e8edf0")
                    stream = io.BytesIO(); image.save(stream, format="PNG"); reference = stream.getvalue()
            except Exception as exc:
                raise PublishError("兼容视频接口的参考图无法读取，尚未提交生成。") from exc
        values = {"model": self.model, "prompt": prompt, "seconds": str(self.duration), "size": "1280x720"}
        if self.transport == "json":
            if reference:
                values["input_reference"] = {"image_url": "data:image/png;base64," + base64.b64encode(reference).decode()}
            data = await self._request("POST", "/videos", json=values)
        else:
            form = aiohttp.FormData()
            for name, value in values.items():
                form.add_field(name, value, content_type="text/plain")
            if reference:
                form.add_field("input_reference", reference, filename="reference.png", content_type="image/png")
            data = await self._request("POST", "/videos", data=form)
        return self._task_id(data.get("id"))

    async def query(self, task_id: str) -> str | None:
        data = await self._request("GET", "/videos/" + quote(task_id, safe=""))
        if data.get("status") in ("failed", "cancelled", "expired"):
            raise GenerationFailed("兼容视频服务确认生成失败。")
        return task_id if data.get("status") == "completed" else None

    async def download(self, task_id: str, temporary: Path, destination: Path) -> None:
        await super().download(self.base_url + "/videos/" + quote(task_id, safe="") + "/content", temporary, destination, headers=self.headers)


VIDEO_CLIENTS = {"minimax": MiniMaxVideo, "ark": ArkVideo, "wan": WanVideo, "openai": CompatibleVideo}


class AutoPublisher:
    def __init__(self, data_dir: Path, plan, publish, account, upload_busy, select_category=None, preflight=None, verify_publication=None, plan_storyboard=None, compose=None, voiceover=None):
        self.data_dir = data_dir
        self.settings_path = data_dir / "auto_publish_settings.json"
        self.jobs_path = data_dir / "auto_publish_jobs.json"
        self.plan = plan
        self.publish = publish
        self.account = account
        self.upload_busy = upload_busy
        self.select_category = select_category
        self.preflight = preflight
        self.verify_publication = verify_publication
        self.plan_storyboard = plan_storyboard
        self.compose = compose or compose_video
        self.voiceover = voiceover
        self.references = ReferenceLibrary(data_dir)
        try:
            self.references.ensure()
        except ReferenceError:
            pass  # Report directory errors in the panel without breaking other features.

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
        references = self.references.scan()
        return {
            "voiceover": self.voiceover.state() if self.voiceover else None,
            "references": {**references, "files": [
                {key: item[key] for key in ("name", "size", "width", "height")}
                for item in references["files"][:12]], "invalid": references["invalid"][:6]},
            "settings": {key: settings[key] for key in DEFAULTS if key != "schedule_after"},
            "providers": {
                name: {
                    "name": spec["name"], "model": spec["model"], "duration": spec["duration"],
                    "models": list(VIDEO_MODELS[name]),
                    "duration_rules": [{"prefix": prefix, "options": options} for prefix, options in DURATION_RULES[name]],
                    "duration_fallback": duration_options(name, ""),
                    "reference_models": REFERENCE_MODELS.get(name, {}),
                    "native_audio_prefixes": list(NATIVE_SOUND_PREFIXES.get(name, ())),
                    "base_urls": {region: address for region, (_, address) in spec["regions"].items()},
                    "regions": {region: label for region, (label, _) in spec["regions"].items()},
                }
                for name, spec in VIDEO_PROVIDERS.items()
            },
            "profiles": {
                name: {
                    "key_configured": bool(profile.get("key")), "model": profile.get("model", ""),
                    "workspace": profile.get("workspace", ""),
                    "base_url": profile.get("base_url", ""), "transport": profile.get("transport", "multipart"),
                    "native_audio": profile.get("native_audio", False),
                    "duration": profile.get("duration", VIDEO_PROVIDERS[name.split(":")[0]]["duration"]),
                }
                for name, profile in settings["profiles"].items()
            },
            "key_configured": bool(settings["profiles"].get(f"{settings['provider']}:{settings['region']}", {}).get("key")),
            "logged_in": bool(self.account()),
            "jobs": [
                {
                    **{key: row.get(key, "") for key in ("id", "phase", "title", "created_at", "file", "bvid", "message", "model", "tid", "category_mode", "category_name", "category_reason", "task_id", "cleanup_state")},
                    "duration": row.get("duration", VIDEO_PROVIDERS[row.get("provider", "minimax")]["duration"]),
                    "provider": row.get("provider", "minimax"),
                    "mode": row.get("mode", "single"), "recoverable": row.get("recoverable", False),
                    "retry_stage": row.get("retry_stage", ""),
                    "shot_count": len(row.get("shot_durations", [])),
                    "shot_link": row.get("shot_link", "independent"),
                    "shots": [{"number": index + 1, "duration": shot["duration"], "phase": shot["phase"],
                               "task_id": shot.get("task_id", ""), "linked": bool(shot.get("handoff")),
                               "dialogue": shot.get("dialogue", []),
                               **{key: shot.get(key, "") for key in ("beat", "start_state", "prompt", "end_state", "camera", "sound", "narration", "voice_start")}}
                              for index, shot in enumerate(row.get("shots", []))],
                    "reference_image": row.get("reference_image", {}).get("name", ""),
                    "learning_used": row.get("learning_used", []),
                    "voiceover_enabled": bool(row.get("voiceover")),
                    "voiceover_done": row.get("voiceover", {}).get("complete", False),
                    "voiceover_skipped": row.get("voiceover", {}).get("skipped", ""),
                    "voiceover_mode": row.get("voiceover", {}).get("mode", "narration"),
                    "asr_phase": row.get("voiceover", {}).get("asr", {}).get("phase", ""),
                    "subtitle_count": len(row.get("voiceover", {}).get("asr", {}).get("segments", [])),
                    "voiceover_progress": sum(clip["phase"] == "done" for clip in row.get("voiceover", {}).get("clips", [])),
                    "voiceover_count": len(row.get("voiceover", {}).get("clips", [])),
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
        workspace = workspace_id(payload.get("workspace", settings["profiles"].get(f"{provider}:{region}", {}).get("workspace", ""))) if provider == "wan" else ""
        try:
            base_url = payload.get("base_url", settings["profiles"].get(f"{provider}:{region}", {}).get("base_url", ""))
            client = VIDEO_CLIENTS[provider](provider, region, key, VIDEO_PROVIDERS[provider]["model"], workspace, base_url=base_url)
            models = await client.models()
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("读取模型列表失败，请检查服务商连接后重试。") from exc
        result = {"models": models}
        if provider == "minimax" and not models:
            result["message"] = "MiniMax 列表接口已响应，但未列出海螺视频模型。可直接选择已适配模型并保存；视频生成权限尚未验证。"
        return result

    def save_settings(self, payload: dict, now: datetime) -> None:
        current = self.settings()
        settings = {key: payload.get(key, current[key]) for key in DEFAULTS if key != "schedule_after"}
        if type(settings["enabled"]) is not bool:
            raise PublishError("自主投稿开关格式不正确。")
        if type(settings["auto_category"]) is not bool:
            raise PublishError("自动选择投稿分区开关格式不正确。")
        if type(settings["reference_enabled"]) is not bool:
            raise PublishError("参考图开关格式不正确。")
        if settings["mode"] not in ("single", "storyboard"):
            raise PublishError("请选择单段生成或分镜合成。")
        if settings["shot_link"] not in ("independent", "continue"):
            raise PublishError("请选择分别生成或连续动作。")
        if type(settings["total_duration"]) is not int or settings["total_duration"] not in (30, 60):
            raise PublishError("分镜成片时长请选择 30 秒或 60 秒。")
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
        try:
            base_url = api_base(payload.get("base_url", profile.get("base_url", "")))
        except ValueError as exc:
            raise PublishError(str(exc)) from exc
        if provider == "openai" and base_url.endswith("/videos"):
            base_url = base_url[:-7]
        if provider == "openai" and settings["enabled"] and not base_url:
            raise PublishError("请填写兼容视频服务的 API 地址。")
        transport = payload.get("transport", profile.get("transport", "multipart"))
        sound = payload.get("native_audio", profile.get("native_audio", False))
        if transport not in ("multipart", "json") or type(sound) is not bool:
            raise PublishError("兼容视频协议或原生声音开关无效。")
        model = payload.get("model", profile.get("model", ""))
        if not isinstance(model, str) or (model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}", model)):
            raise PublishError("请选择有效的视频模型。")
        if model and not supported_video_model(provider, model) and not base_url:
            raise PublishError("该模型尚未适配当前文生视频接口，请从读取结果中选择。")
        effective_model = model or spec["model"]
        if provider == "openai" and settings["enabled"] and not effective_model:
            raise PublishError("请填写兼容视频服务的模型 ID。")
        if settings["reference_enabled"] or (settings["mode"] == "storyboard" and settings["shot_link"] == "continue"):
            reference_video_model(provider, effective_model)
        duration = payload.get("duration", profile.get("duration", spec["duration"]))
        if "duration" not in payload and duration not in duration_options(provider, effective_model):
            duration = spec["duration"]
        duration = video_duration(provider, effective_model, duration)
        key = "" if remove else supplied or profile.get("key", "")
        if settings["enabled"] and not key:
            raise PublishError("先填写所选服务的 API Key，再开启自主投稿。")
        workspace = workspace_id(payload.get("workspace", profile.get("workspace", ""))) if provider == "wan" else ""
        profiles[profile_name] = {"key": key, "model": model, "workspace": workspace, "duration": duration,
                                  "base_url": base_url, "transport": transport, "native_audio": sound}
        settings["profiles"] = profiles
        settings["schedule_after"] = current["schedule_after"]
        if settings["enabled"] and (
            not current["enabled"] or any(settings[key] != current[key] for key in ("time", "provider", "region"))
        ):
            settings["schedule_after"] = now.isoformat()
        # Older queued jobs did not record a region until submission.
        rows = self.jobs()
        migrated = False
        for row in rows:
            if row["phase"] not in ACTIVE_PHASES:
                continue
            if "provider" not in row:
                row["provider"] = "minimax"
                row.setdefault("region", current["region"])
                migrated = True
            old_profile = current["profiles"].get(f"{row['provider']}:{row.get('region', current['region'])}", {})
            if "key_revision" not in row and old_profile.get("key"):
                row["key_revision"] = key_revision(old_profile["key"])
                migrated = True
        if migrated:
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

        model = profile.get("model") or VIDEO_PROVIDERS[provider]["model"]
        reference = None
        if settings["reference_enabled"]:
            try:
                model = reference_video_model(provider, model)
                reference = self.references.choose(rows)
                self.references.load(reference, provider=provider)
            except (ReferenceError, PublishError) as exc:
                if manual:
                    raise PublishError(str(exc)) from exc
                return None
        duration = video_duration(provider, model, profile.get("duration", VIDEO_PROVIDERS[provider]["duration"]))
        lengths = storyboard_lengths(provider, model, settings["total_duration"]) if settings["mode"] == "storyboard" else []
        if lengths and settings["shot_link"] == "continue":
            reference_video_model(provider, model)
        job = {
            "id": uuid.uuid4().hex, "phase": "queued", "created_at": now.isoformat(timespec="seconds"),
            "day": today, "scheduled": not manual, "account": account, "tid": settings["tid"],
            "auto_category": settings["auto_category"],
            "direction": settings["direction"], "key_revision": key_revision(profile["key"]),
            "provider": provider, "region": region, "model": model, "duration": duration,
            "base_url": profile.get("base_url", ""), "transport": profile.get("transport", "multipart"),
            "cleanup_after_publish": True,
            "mode": settings["mode"], "shot_durations": lengths,
            "shot_link": settings["shot_link"],
        }
        if reference:
            job["reference_image"] = reference
        if self.voiceover:
            try:
                pinned = self.voiceover.pin()
            except VoiceoverError as exc:
                if manual:
                    raise PublishError(str(exc)) from exc
                return None
            if pinned:
                sound = native_sound(provider, model) or provider == "openai" and profile.get("native_audio", False)
                if (pinned.get("mode") != "narration" and pinned.get("subtitles") and sound
                        and not pinned.get("asr_revision")):
                    if manual:
                        raise PublishError("请先保存语音识别服务的 Key，或关闭字幕后再生成有声视频。")
                    return None
                job["voiceover"] = pinned
                pinned["expects_audio"] = sound
                if pinned.get("mode") == "character" and sound:
                    job["character_audio"] = {"style": pinned["voice_style"]}
        if lengths:
            job["duration"] = sum(lengths)
        _write(self.jobs_path, [*rows, job][-60:])
        return job

    def resume(self, job_id: str) -> None:
        settings, rows = self.settings(), self.jobs()
        job = next((row for row in rows if row["id"] == job_id), None)
        if job and job["phase"] == "failed" and job.get("recoverable") is True and job.get("retry_stage") == "voice":
            if not self.voiceover or not settings["enabled"] or self.account() != job["account"] or any(row["phase"] in ACTIVE_PHASES for row in rows):
                raise PublishError("请开启自主投稿、登录任务原账号，并等待其他任务完成。")
            try:
                self.voiceover.resume(job)
            except VoiceoverError as exc:
                raise PublishError(str(exc)) from exc
            self._update(rows, job, "voiceover", recoverable=False, retry_stage="", message="已继续后期处理，将复用原视频和已完成配音。")
            return
        if job is None or job.get("mode") != "storyboard" or job["phase"] != "failed" or job.get("recoverable") is not True:
            raise PublishError("此任务不能直接重试，请先核对服务商或 B站记录。")
        if not settings["enabled"] or self.account() != job["account"]:
            raise PublishError("请开启自主投稿并登录任务原来的 B站账号。")
        if any(row["phase"] in ACTIVE_PHASES for row in rows):
            raise PublishError("已有自主投稿任务，请等它处理完。")
        if job.get("retry_stage") in ("shot", "download"):
            profile = settings["profiles"].get(f"{job['provider']}:{job['region']}", {})
            if not profile.get("key") or key_revision(profile["key"]) != job.get("key_revision"):
                raise PublishError("请恢复任务原来的服务 Key，再重试失败片段。")
            for shot in job["shots"]:
                if shot["phase"] == "failed":
                    shot.update(phase="queued")
                elif shot["phase"] == "download_failed":
                    shot.update(phase="generating")
        phase = "assembling" if job.get("retry_stage") == "assemble" else "storyboard"
        self._update(rows, job, phase, recoverable=False, retry_after="", message="已加入继续处理队列，已完成的片段将保留。")

    @staticmethod
    def _file_identity(path: Path) -> list[int]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PublishError("本地成片不是普通文件，已保留供核对。")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def _cleanup_path(self, job: dict, shot_index: int | None = None) -> Path:
        folder = self.data_dir / "media"
        name = f"ai-{job['id']}.mp4" if shot_index is None else f"ai-{job['id']}-shot-{shot_index + 1:02d}.mp4"
        recorded = job.get("file") if shot_index is None else job["shots"][shot_index].get("file")
        target = folder / name
        if (recorded != name or folder.is_symlink()
                or folder.resolve() != self.data_dir.resolve() / "media"
                or target.is_symlink() or target.resolve() != folder.resolve() / name):
            raise PublishError("本地成片路径已变化，已保留供核对。")
        return target

    async def _cleanup_published(self, now: datetime) -> None:
        account = self.account()
        if not account:
            return
        rows = self.jobs()
        job = next((row for row in rows if row["phase"] == "published"
                    and row.get("account") == account
                    and row.get("cleanup_after_publish") is True and row.get("cleanup_state") == "pending"
                    and (not row.get("cleanup_retry_after") or now >= datetime.fromisoformat(row["cleanup_retry_after"]))), None)
        if job is None or self.verify_publication is None or self.upload_busy():
            return
        if not isinstance(job.get("bvid"), str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", job["bvid"]):
            return
        if self.account() != job.get("account"):
            return
        try:
            verified = await asyncio.wait_for(self.verify_publication(dict(job)), timeout=25)
        except Exception:
            verified = False
        # A manual start may have appended a new job while the read-only check awaited.
        rows = self.jobs()
        current = next((row for row in rows if row["id"] == job["id"]), None)
        if (current is None or current["phase"] != "published" or current.get("cleanup_state") != "pending"
                or current.get("cleanup_after_publish") is not True
                or current.get("bvid") != job["bvid"] or self.account() != current.get("account") or self.upload_busy()):
            return
        job = current
        if verified is not True:
            self._update(rows, job, "published", cleanup_retry_after=(now + timedelta(minutes=1)).isoformat(),
                         message="投稿已提交，等待 B站确认视频正常可访问后清理本地成片。")
            return
        try:
            targets = [(self._cleanup_path(job), job.get("cleanup_identity"))]
            if job.get("mode") == "storyboard":
                for index, shot in enumerate(job["shots"]):
                    if shot["phase"] != "done":
                        raise PublishError("分镜记录不完整，已保留本地文件供核对。")
                    targets.append((self._cleanup_path(job, index), shot.get("identity")))
                    if shot.get("handoff"):
                        targets.append((frame_path(self.data_dir, job["id"], index), shot["handoff"]["identity"]))
            if job.get("voiceover"):
                if not self.voiceover:
                    raise PublishError("后期处理模块不可用，已保留所有素材。")
                targets.extend(self.voiceover.artifacts(job))
            for target, identity in targets:
                if target.exists() and self._file_identity(target) != identity:
                    raise PublishError("本地成片已被修改，已保留供核对。")
            for target, _identity in targets:
                target.unlink(missing_ok=True)
        except (PublishError, VoiceoverError, CompositionError) as exc:
            self._update(rows, job, "published", cleanup_state="kept", message=str(exc))
            return
        except OSError:
            self._update(rows, job, "published", cleanup_retry_after=(now + timedelta(minutes=1)).isoformat(),
                         message="B站视频已确认正常，本地清理暂时失败，稍后自动重试。")
            return
        self._update(rows, job, "published", cleanup_state="deleted", cleanup_retry_after="",
                     cleaned_at=now.isoformat(), message="B站视频已确认正常，本地成片及生成片段已清理。" if job.get("mode") == "storyboard" else "B站视频已确认正常，本地成片已清理。")

    async def _preflight_job(self, rows: list[dict], job: dict, now: datetime) -> bool:
        if job.get("retry_after") and now < datetime.fromisoformat(job["retry_after"]):
            return False
        try:
            if job.get("voiceover") and not (job["phase"] == "ready" and job["voiceover"].get("mode", "narration") != "narration"):
                if self.voiceover is None:
                    raise PublishError("配音模块暂不可用，任务将等待。")
                try:
                    await self.voiceover.preflight(job)
                except VoiceoverError as exc:
                    raise PublishError(str(exc)) from exc
            if self.preflight is not None:
                kwargs = {"required_bytes": 3 * MAX_VIDEO_BYTES} if job.get("voiceover") else {}
                if job.get("mode") == "storyboard":
                    remaining = sum(shot["phase"] != "done" for shot in job.get("shots", [])) if job.get("shots") else len(job["shot_durations"])
                    kwargs = {"storyboard": True, "required_bytes": (remaining + 2 + bool(job.get("voiceover"))) * MAX_VIDEO_BYTES}
                await asyncio.wait_for(self.preflight(job["account"], **kwargs), timeout=25)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PublishError) else "暂时无法完成运行前检查，请稍后重试。"
            self._update(rows, job, job["phase"], message=reason, retry_after=(now + timedelta(minutes=1)).isoformat())
            return False
        return self.settings()["enabled"] and self.account() == job["account"]

    async def _job_reference(self, job: dict) -> dict | None:
        if not job.get("reference_image"):
            return None
        try:
            return await asyncio.to_thread(self.references.load, job["reference_image"], provider=job["provider"])
        except ReferenceError as exc:
            raise PublishError(str(exc)) from exc

    async def _tick_storyboard(self, rows: list[dict], job: dict, now: datetime, key: str | None) -> None:
        shots = job["shots"]
        if job["phase"] == "assembling":
            try:
                for index, shot in enumerate(shots):
                    if self._file_identity(self._cleanup_path(job, index)) != shot.get("identity"):
                        raise CompositionError("原始分镜已被修改，请核对或恢复后重新合成。")
                destination = await self.compose(self.data_dir, job["id"], job["shot_durations"], MAX_VIDEO_BYTES)
                expected = self.data_dir / "media" / f"ai-{job['id']}.mp4"
                if destination.is_symlink() or destination.resolve() != expected.resolve():
                    raise CompositionError("合成结果路径无效。")
                identity = self._file_identity(destination)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (CompositionError, PublishError)) else "分镜合成失败，请检查 FFmpeg 和原始片段后重试。"
                self._update(rows, job, "failed", recoverable=True, retry_stage="assemble", message=message)
                return
            self._update(rows, job, "ready", file=destination.name, cleanup_identity=identity,
                         recoverable=False, retry_after="", message="分镜合成完成，等待投稿。")
            return
        index = next((index for index, shot in enumerate(shots) if shot["phase"] != "done"), None)
        if index is None:
            self._update(rows, job, "assembling", message="所有分镜已生成，正在准备合成。", retry_after="")
            return
        shot = shots[index]
        label = f"第 {index + 1}/{len(shots)} 段"
        if shot["phase"] == "requesting":
            self._update(rows, job, "review", recoverable=False, message=f"{label}受理状态不明，请到服务商核对，不会重复创建。")
            return
        provider, region, model = job["provider"], job["region"], job["model"]
        if shot["phase"] == "queued":
            if not await self._preflight_job(rows, job, now):
                return
            try:
                reference = await self._job_reference(job)
                if job.get("shot_link") == "continue" and index:
                    frame = await handoff_frame(self.data_dir, job, index)
                    shot["handoff"] = frame["record"]
                    reference = {"data_url": frame["data_url"]}
                    # Persist the exact frame before any paid video request.
                    self._update(rows, job, "storyboard", message=f"已准备{label}的承接帧。")
                prompt = shot_prompt(job, index)
            except (PublishError, CompositionError, OSError) as exc:
                shot["phase"] = "failed"
                message = str(exc) if isinstance(exc, (PublishError, CompositionError)) else "承接帧文件无法读取，请检查原始分镜。"
                self._update(rows, job, "failed", recoverable=True, retry_stage="shot", message=f"{label}尚未提交：{message}")
                return
            latest = self.settings()
            if not latest["enabled"] or self.account() != job["account"]:
                return
            profile = latest["profiles"].get(f"{provider}:{region}", {})
            key = profile.get("key")
            if not key or key_revision(key) != job["key_revision"]:
                self._update(rows, job, "review", recoverable=False, message="视频服务 Key 已更换，分镜任务保留待核对。")
                return
            shot.update(phase="requesting", submitted_at=now.isoformat())
            self._update(rows, job, "storyboard", message=f"正在提交{label}。", retry_after="")
            try:
                task_id = await VIDEO_CLIENTS[provider](provider, region, key, model, duration=shot["duration"],
                                                       base_url=job.get("base_url", ""), transport=job.get("transport", "multipart")).create(
                    prompt, **({"reference_image": reference["data_url"]} if reference else {}))
            except PublishError as exc:
                shot["phase"] = "failed"
                self._update(rows, job, "failed", recoverable=True, retry_stage="shot", message=f"{label}提交被拒绝：{exc} 已完成片段保留。")
            except Exception:
                shot["phase"] = "review"
                self._update(rows, job, "review", recoverable=False, message=f"{label}受理结果不明，请到服务商核对。")
            else:
                shot.update(phase="generating", task_id=task_id)
                self._update(rows, job, "storyboard", message=f"正在生成{label}。")
            return
        client = VIDEO_CLIENTS[provider](provider, region, key, model, duration=shot["duration"],
                                       base_url=job.get("base_url", ""), transport=job.get("transport", "multipart"))
        try:
            artifact = await client.query(shot["task_id"])
        except GenerationFailed as exc:
            shot["phase"] = "failed"
            self._update(rows, job, "failed", recoverable=True, retry_stage="shot", message=f"{label}生成失败：{exc} 可单独重试，已完成片段保留。")
            return
        except PublishError as exc:
            self._update(rows, job, "review", recoverable=False, message=f"{label}查询受阻：{exc} 请先到服务商核对。")
            return
        except Exception:
            artifact = None
        if not artifact:
            if now - datetime.fromisoformat(shot["submitted_at"]) >= timedelta(minutes=30):
                self._update(rows, job, "review", recoverable=False, message=f"{label}等待超时，请到服务商核对，已完成片段保留。")
            return
        name = f"ai-{job['id']}-shot-{index + 1:02d}.mp4"
        destination = self.data_dir / "media" / name
        try:
            if not destination.is_file():
                await client.download(artifact, self.data_dir / f".ai-{job['id']}-shot-{index + 1:02d}.part", destination)
            identity = self._file_identity(destination)
        except Exception:
            shot["phase"] = "download_failed"
            self._update(rows, job, "failed", recoverable=True, retry_stage="download", message=f"{label}下载失败，可重新下载原任务的成片，不会重新生成。")
            return
        shot.update(phase="done", file=name, identity=identity)
        self._update(rows, job, "storyboard", message=f"已完成 {index + 1}/{len(shots)} 段。")

    async def tick(self, now: datetime) -> None:
        await self._cleanup_published(now)
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
        duration = job.get("duration", spec["duration"])
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
        needs_key = phase in ("queued", "generating") or (phase == "storyboard" and any(shot["phase"] != "done" for shot in job["shots"]))
        if needs_key and not key:
            self._update(rows, job, "review", message=f"{spec['name']} 的 Key 已清除，请到服务商核对任务。")
            return
        if needs_key:
            if job.get("key_revision") and job["key_revision"] != key_revision(key):
                self._update(rows, job, "review", message="视频服务的 Key 已更换，旧任务保留待核对；不会用新 Key 查询或重新生成。")
                return
            if "key_revision" not in job:
                self._update(rows, job, phase, key_revision=key_revision(key))
        if phase in ("queued", "assembling", "ready"):
            if not await self._preflight_job(rows, job, now):
                return
            if phase == "ready" and self.upload_busy():
                return
        if phase == "voiceover" or (phase == "ready" and job.get("voiceover") and not job["voiceover"].get("complete")):
            if self.voiceover is None:
                self._update(rows, job, "failed", recoverable=True, retry_stage="voice", message="配音模块暂不可用，原视频已保留。")
                return
            self._update(rows, job, "voiceover")
            await self.voiceover.step(job, lambda: _write(self.jobs_path, rows[-60:]), MAX_VIDEO_BYTES)
            return
        if phase == "queued":
            try:
                reference = await self._job_reference(job)
                plan_options = {"reference": reference} if reference else {}
                if job.get("character_audio"):
                    plan_options["character_audio"] = job["character_audio"]
                recent = [row["title"] for row in rows[-13:-1] if row.get("title")]
                if job.get("mode") == "storyboard":
                    plan = parse_storyboard(await self.plan_storyboard(job["direction"], recent, job["shot_durations"], **plan_options), job["shot_durations"])
                    if job.get("character_audio") and any("dialogue" not in shot for shot in plan["shots"]):
                        raise PublishError("分镜方案缺少逐镜角色台词安排，尚未开始生成。")
                    for index in range(len(plan["shots"])):
                        shot_prompt({**job, **plan}, index)
                else:
                    duration = video_duration(provider, model, duration)
                    plan = parse_plan(await self.plan(job.get("direction", settings["direction"]), recent, duration, **plan_options), duration)
                    if job.get("character_audio"):
                        if "dialogue" not in plan:
                            raise PublishError("视频方案缺少角色台词安排，尚未开始生成。")
                        speech = character_prompt(job, plan)
                        plan["prompt"] = speech + "\n画面动作：" + plan["prompt"][:max(1,1500-len(speech))]
                if plan["title"] in recent:
                    raise PublishError("选题与近期稿件重复。")
            except (PublishError, CompositionError) as exc:
                self._update(rows, job, "failed", message=str(exc))
                return
            except Exception:
                message = "参考图选题失败，请检查 AstrBot 模型是否支持图片输入，以及模型连接是否正常。" if job.get("reference_image") else "选题失败，请检查 AstrBot 的文本模型配置。"
                self._update(rows, job, "failed", message=message)
                return
            category = {"category_mode": "manual", "category_reason": "使用手动设置的投稿分区。"}
            if job.get("auto_category", False):
                try:
                    chosen = await asyncio.wait_for(self.select_category(plan), timeout=90)
                    if (not isinstance(chosen, dict) or type(chosen.get("tid")) is not int
                            or not 1 <= chosen["tid"] <= 9999 or not isinstance(chosen.get("category_name"), str)
                            or not chosen["category_name"].strip() or not isinstance(chosen.get("category_reason"), str)
                            or not chosen["category_reason"].strip()):
                        raise PublishError("分区判断未返回有效结果。")
                    category = {**chosen, "category_mode": "auto"}
                except Exception:
                    category = {
                        "category_mode": "fallback",
                        "category_reason": "未能读取可用分区或完成有效判断，已使用备用分区。",
                    }
            if job.get("mode") != "storyboard":
                try:
                    reference = await self._job_reference(job)
                except PublishError as exc:
                    self._update(rows, job, "failed", message=str(exc))
                    return
            latest = self.settings()
            key = latest["profiles"].get(profile_name, {}).get("key")
            if not latest["enabled"] or not key or self.account() != job["account"]:
                return
            if key_revision(key) != job["key_revision"]:
                self._update(rows, job, "review", message="选题期间视频服务的 Key 已更换，任务已停止，未提交视频生成。")
                return
            if job.get("mode") == "storyboard":
                self._update(rows, job, "storyboard", title=plan["title"], desc=plan["desc"], tags=plan["tags"],
                             prompt=plan["prompt"], continuity=plan["continuity"], shots=plan["shots"], recoverable=False,
                             learning_used=plan.get("learning_used", []),
                             retry_after="", message=f"已规划 {len(plan['shots'])} 个分镜，等待逐段生成。", **category)
                return
            self._update(
                rows, job, "requesting", title=plan["title"], desc=plan["desc"], tags=plan["tags"],
                provider=provider, region=region, model=model, submitted_at=now.isoformat(),
                prompt=plan["prompt"], message="", retry_after="",
                learning_used=plan.get("learning_used", []),
                **category,
            )
            try:
                task_id = await VIDEO_CLIENTS[provider](provider, region, key, model, duration=duration,
                                                       base_url=job.get("base_url", ""), transport=job.get("transport", "multipart")).create(
                    plan["prompt"], **({"reference_image": reference["data_url"]} if reference else {}))
            except PublishError as exc:
                self._update(rows, job, "failed", message=str(exc))
            except Exception:
                self._update(rows, job, "review", message=f"{spec['name']} 受理状态不明，请核对生成记录。")
            else:
                self._update(rows, job, "generating", task_id=task_id)
            return

        if phase in ("storyboard", "assembling"):
            await self._tick_storyboard(rows, job, now, key)
            return

        if phase == "generating":
            client = VIDEO_CLIENTS[provider](provider, region, key, model, base_url=job.get("base_url", ""), transport=job.get("transport", "multipart"))
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
            self._update(rows, job, "ready", file=name, message="", retry_after="",
                         cleanup_identity=self._file_identity(destination))
            return

        if self.upload_busy():
            return
        video = self.data_dir / "media" / f"ai-{job['id']}.mp4"
        if not video.is_file():
            self._update(rows, job, "failed", message="成片文件已移除。")
            return
        self._update(rows, job, "uploading", message="", retry_after="")
        try:
            async with asyncio.timeout(900):
                result = await self.publish({
                    "video_path": str(video), "title": job["title"],
                    "desc": f"AI 生成视频 · {spec['name']}\n\n" + job["desc"],
                    "tags": publication_tags(job["tags"]), "tid": job["tid"],
                })
        except Exception:
            self._update(rows, job, "review", message="投稿结果未能确认，请到 B站创作中心核对。")
            return
        if not isinstance(result, dict):
            result = {}
        bvid = result.get("bvid")
        if result.get("success") is True and isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            cleanup = job.get("cleanup_after_publish") is True
            self._update(rows, job, "published", bvid=bvid, cleanup_state="pending" if cleanup else "",
                         message="投稿成功，确认 B站视频正常可访问后自动清理本地成片。" if cleanup else "")
        else:
            self._update(rows, job, "review", message="投稿未能确认：" + upload_error(result.get("error")) + "。成片已保留，请先到 B站创作中心核对。")
