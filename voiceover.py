"""Provider-independent narration, timed captions and local video post-processing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .video_compose import CompositionError, probe_video, run_media

PROVIDERS = {
    "minimax": {
        "name": "MiniMax 语音", "models": ["speech-2.6-turbo", "speech-2.6-hd"],
        "voices": {"Chinese (Mandarin)_Warm_Bestie": "温暖闺蜜", "Chinese (Mandarin)_Gentle_Youth": "温柔青年", "Chinese (Mandarin)_Warm_Girl": "温暖少女"},
        "regions": {"cn": "中国站", "global": "国际站"},
        "bases": {"cn": "https://api.minimax.cn/v1", "global": "https://api.minimax.io/v1"},
    },
    "qwen": {
        "name": "通义千问语音", "models": ["qwen3-tts-flash"],
        "voices": {"Cherry": "芊悦 · 活泼女声", "Serena": "苏瑶 · 温柔女声", "Ethan": "晨煦 · 阳光男声"},
        "regions": {"cn": "中国内地", "global": "新加坡"},
        "bases": {"cn": "https://dashscope.aliyuncs.com/api/v1", "global": "https://dashscope-intl.aliyuncs.com/api/v1"},
    },
}
DEFAULTS = {"enabled": False, "provider": "minimax", "region": "cn", "mix": False,
            "original_volume": 15, "voice_volume": 100, "subtitles": True, "font_size": 28}
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class VoiceoverError(Exception):
    """Safe to display without leaking credentials or signed URLs."""


class SpeechRejected(VoiceoverError):
    """The service explicitly refused a synthesis request."""


def revision(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def identity(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise VoiceoverError("后期素材不是普通文件，已停止处理。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def write_private(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".voice-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def windows(duration: float) -> list[float]:
    if not math.isfinite(duration) or not 1 <= duration <= 65:
        raise VoiceoverError("配音视频时长应在 1～65 秒之间。")
    count = max(1, math.ceil(duration / 12))
    return [duration / count] * count


def parse_narration(text: str, slots: list[float]) -> list[dict]:
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        data = json.loads(cleaned)
        rows = data["segments"]
        if not isinstance(rows, list) or len(rows) != len(slots):
            raise ValueError()
        clips = []
        for row, duration in zip(rows, slots):
            value = row.get("text") if isinstance(row, dict) else None
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > max(4, min(36, int(duration * 2.8))):
                raise ValueError()
            value = re.sub(r"[\r\n\t]+", " ", value.strip()).translate(str.maketrans({"{": "（", "}": "）", "<": "＜", ">": "＞", "\\": "／"}))
            clips.append({"text": value, "window": duration, "phase": "queued"})
        return clips
    except (ValueError, KeyError, TypeError) as exc:
        raise VoiceoverError("旁白稿格式或长度不合适，已保留视频，未开始收费配音。") from exc


async def audio_duration(path: Path) -> float:
    raw = await run_media("ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path), timeout=20)
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or not .05 <= duration <= 60 or not any(s["codec_type"] == "audio" for s in data["streams"]):
            raise ValueError()
        return duration
    except (ValueError, KeyError, TypeError) as exc:
        raise VoiceoverError("配音文件无法解码为有效音频，已保留原视频。") from exc


class SpeechClient:
    def __init__(self, config: dict, key: str):
        self.config, self.key = config, key

    async def synthesize(self, text: str) -> dict:
        config = self.config
        provider = config["provider"]
        base = PROVIDERS[provider]["bases"][config["region"]]
        if provider == "minimax":
            path = "/t2a_v2"
            body = {"model": config["model"], "text": text, "stream": False, "output_format": "hex",
                    "voice_setting": {"voice_id": config["voice"], "speed": config["speed"], "vol": 1, "pitch": 0},
                    "audio_setting": {"sample_rate": 24000, "format": "wav", "channel": 1}}
        else:
            path = "/services/aigc/multimodal-generation/generation"
            body = {"model": config["model"], "input": {"text": text, "voice": config["voice"], "language_type": "Chinese"}}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(base + path, json=body, headers={"Authorization": "Bearer " + self.key}, allow_redirects=False) as response:
                if 400 <= response.status < 500 and response.status not in (408, 429):
                    raise SpeechRejected(f"配音服务拒绝请求（HTTP {response.status}），请检查 Key、地区和音色权限。")
                response.raise_for_status()
                if response.status != 200:
                    raise VoiceoverError("配音受理结果无法确认，请先到服务商核对。")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2 * MAX_AUDIO_BYTES + 65536:
                        raise VoiceoverError("配音返回数据超过限制，请先到服务商核对。")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                data = json.loads(raw)
        if provider == "minimax":
            status = data.get("base_resp", {}).get("status_code")
            if type(status) is int and status != 0:
                raise SpeechRejected(f"MiniMax 配音请求被拒绝（错误码 {status}），请检查语音权限和余额。")
            if type(status) is not int:
                raise VoiceoverError("配音受理状态无法确认。")
            encoded = data.get("data", {}).get("audio")
            if not isinstance(encoded, str) or len(encoded) > 2 * MAX_AUDIO_BYTES:
                raise VoiceoverError("配音服务未返回有效音频。")
            content = bytes.fromhex(encoded)
            if not 128 <= len(content) <= MAX_AUDIO_BYTES:
                raise VoiceoverError("配音返回的音频大小无效。")
            return {"content": content}
        if data.get("code") or data.get("status_code", 200) != 200:
            raise SpeechRejected("千问配音请求被拒绝，请检查 Key、地区和模型权限。")
        return {"url": self.download_url(data.get("output", {}).get("audio", {}).get("url"))}

    @staticmethod
    def download_url(url: str) -> str:
        if not isinstance(url, str):
            raise VoiceoverError("服务商未返回配音下载地址。")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme not in ("https", "http") or not host.endswith(".aliyuncs.com")
                or parts.username or parts.password or parts.port not in (None, 80, 443)):
            raise VoiceoverError("配音下载地址不属于支持的服务商。")
        return urlunsplit(("https", host, parts.path, parts.query, ""))

    async def download(self, url: str) -> bytes:
        url = self.download_url(url)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (403, 404, 410):
                    raise VoiceoverError("配音下载链接不可用或已过期，请到语音服务商核对；不会自动重新计费合成。")
                response.raise_for_status()
                if response.status != 200:
                    raise VoiceoverError("配音下载失败，请重试下载。")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise VoiceoverError("配音文件超过 8 MB 限制。")
                    chunks.append(chunk)
        return b"".join(chunks)


class VoiceoverService:
    def __init__(self, data_dir: Path, plan):
        self.data_dir, self.plan = data_dir, plan
        self.settings_path = data_dir / "voiceover_settings.json"
        self._tools_checked = False

    def settings(self) -> dict:
        try:
            stored = json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
            return {**DEFAULTS, "profiles": {}, **stored}
        except (OSError, ValueError) as exc:
            raise VoiceoverError("配音设置无法读取，请检查服务器配置文件。") from exc

    def state(self) -> dict:
        settings = self.settings()
        return {"settings": {key: settings[key] for key in DEFAULTS},
                "providers": {name: {key: value for key, value in spec.items() if key != "bases"} for name, spec in PROVIDERS.items()},
                "profiles": {name: {"key_configured": bool(p.get("key")), "model": p["model"], "voice": p["voice"], "speed": p["speed"]} for name, p in settings["profiles"].items()}}

    def save(self, payload: dict) -> None:
        current = self.settings()
        settings = {key: payload.get(key, current[key]) for key in DEFAULTS}
        provider, region = settings["provider"], settings["region"]
        if not isinstance(provider, str) or provider not in PROVIDERS or not isinstance(region, str) or region not in PROVIDERS[provider]["regions"]:
            raise VoiceoverError("请选择支持的配音服务和地区。")
        for name in ("enabled", "mix", "subtitles"):
            if type(settings[name]) is not bool:
                raise VoiceoverError("配音开关格式不正确。")
        for name, low, high in (("original_volume", 0, 100), ("voice_volume", 10, 150), ("font_size", 20, 60)):
            if type(settings[name]) is not int or not low <= settings[name] <= high:
                raise VoiceoverError("音量或字幕字号超出支持范围。")
        profiles = dict(current["profiles"])
        old = profiles.get(provider + ":" + region, {})
        spec = PROVIDERS[provider]
        model, voice = payload.get("model", old.get("model", spec["models"][0])), payload.get("voice", old.get("voice", next(iter(spec["voices"]))))
        speed = payload.get("speed", old.get("speed", 1.0))
        if not isinstance(model, str) or model not in spec["models"] or not isinstance(voice, str) or voice not in spec["voices"] or type(speed) not in (int, float) or not .8 <= speed <= 1.25:
            raise VoiceoverError("请选择支持的语音模型、预设音色和语速。")
        supplied, remove = payload.get("api_key", ""), payload.get("remove_key", False)
        if type(remove) is not bool or not isinstance(supplied, str) or (supplied and not re.fullmatch(r"[!-~]{1,512}", supplied)):
            raise VoiceoverError("配音 Key 格式不正确。")
        key = "" if remove else supplied or old.get("key", "")
        if settings["enabled"] and not key:
            raise VoiceoverError("请先填写当前配音服务的 Key。")
        profiles[provider + ":" + region] = {"key": key, "model": model, "voice": voice, "speed": speed if provider == "minimax" else 1.0}
        settings["profiles"] = profiles
        write_private(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2).encode())

    def pin(self) -> dict | None:
        settings = self.settings()
        if not settings["enabled"]:
            return None
        profile = settings["profiles"].get(settings["provider"] + ":" + settings["region"], {})
        if not profile.get("key"):
            raise VoiceoverError("已开启配音，请先保存所选配音服务的 Key。")
        return {**{key: settings[key] for key in DEFAULTS},
                **{key: profile[key] for key in ("model", "voice", "speed")}, "key_revision": revision(profile["key"])}

    def key(self, config: dict) -> str:
        settings = self.settings()
        if not settings["enabled"]:
            raise VoiceoverError("配音已关闭，任务保留；重新开启配音后可继续处理。")
        key = settings["profiles"].get(config["provider"] + ":" + config["region"], {}).get("key", "")
        if not key or revision(key) != config["key_revision"]:
            raise VoiceoverError("配音 Key 已清除或更换，请恢复任务原来的 Key 后重试。")
        return key

    async def preflight(self, job: dict) -> None:
        config = job.get("voiceover")
        if not config or config.get("complete"):
            return
        self.key(config)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise VoiceoverError("配音后期需要 ffmpeg 和 ffprobe。")
        if config["subtitles"] and not self._tools_checked:
            filters = await run_media("ffmpeg", "-hide_banner", "-filters", timeout=15)
            if b" subtitles " not in filters:
                raise VoiceoverError("服务器 FFmpeg 缺少 subtitles/libass，无法烧录字幕。")
            font = self.data_dir / "fonts" / "wqy-microhei.ttc"
            fonts = await run_media("fc-list", ":lang=zh", "family", timeout=10) if shutil.which("fc-list") else b""
            if not font.is_file() and not fonts.strip():
                raise VoiceoverError("服务器缺少中文字幕字体，请安装文泉驿或 Noto CJK 字体。")
            self._tools_checked = True

    def path(self, job: dict, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
            raise VoiceoverError("后期任务编号无效。")
        media = self.data_dir / "media"
        path = media / ("ai-" + job["id"] + suffix)
        if media.is_symlink() or media.resolve().parent != self.data_dir.resolve() or path.is_symlink():
            raise VoiceoverError("后期素材目录或文件存在目录跳转，请核对。")
        return path

    def artifacts(self, job: dict) -> list[tuple[Path, list[int]]]:
        config = job.get("voiceover", {})
        result = []
        for item in config.get("artifacts", []):
            suffix = item["suffix"]
            if not re.fullmatch(r"-(?:source\.mp4|voice-0[1-6]\.wav|subtitles\.(?:srt|ass))", suffix):
                raise VoiceoverError("后期素材记录异常，已保留文件。")
            result.append((self.path(job, suffix), item["identity"]))
        return result

    def store(self, job: dict, suffix: str, content: bytes) -> dict:
        path = self.path(job, suffix)
        if path.exists():
            if path.read_bytes() != content:
                raise VoiceoverError("已有后期素材内容变化，已保留原文件供核对。")
        else:
            write_private(path, content)
        return {"suffix": suffix, "identity": identity(path)}

    async def render(self, job: dict, max_bytes: int, save=lambda: None) -> Path:
        config = job["voiceover"]
        source, final, temporary = self.path(job, "-source.mp4"), self.path(job, ".mp4"), self.path(job, "-voiced.tmp.mp4")
        if config.get("rendered_identity") and final.exists() and identity(final) == config["rendered_identity"]:
            return final
        if identity(source) != config["source_identity"]:
            raise VoiceoverError("原始视频已被修改，无法继续后期处理。")
        if final.exists() and identity(final) != job["cleanup_identity"]:
            raise VoiceoverError("成片文件已被修改，已停止覆盖并保留供核对。")
        info = await probe_video(source)
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "1", "-filter_complex_threads", "1", "-i", str(source)]
        graph, labels, cues, start = [], [], [], 0.0
        if config["mix"] and info["audio"]:
            graph.append(f"[0:a:0]volume={config['original_volume']/100}[original]")
            labels.append("[original]")
        for index, clip in enumerate(config["clips"]):
            path = self.path(job, f"-voice-{index+1:02d}.wav")
            if identity(path) != clip["identity"]:
                raise VoiceoverError("配音素材已被修改，已保留原视频。")
            rate = max(1.0, clip["duration"] / clip["window"])
            if rate > 2:
                raise VoiceoverError("配音明显长于镜头时长，请核对旁白稿；原视频已保留。")
            args += ["-i", str(path)]
            delay = round(start * 1000)
            graph.append(f"[{index+1}:a:0]aresample=48000,atempo={rate:.8f},volume={config['voice_volume']/100},adelay={delay}:all=1[v{index}]")
            labels.append(f"[v{index}]")
            cues.append((start, min(info["duration"], start+clip["duration"]/rate), clip["text"]))
            start += clip["window"]
        graph.append("".join(labels)+f"amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.95:level=0,apad,atrim=duration={info['duration']}[audio]")
        args += ["-filter_complex", ";".join(graph), "-map", "0:v:0", "-map", "[audio]"]
        if config["subtitles"]:
            def stamp(value):
                ms = round(value*1000)
                return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
            srt = "\n\n".join(f"{i+1}\n{stamp(begin)} --> {stamp(end)}\n{text}" for i,(begin,end,text) in enumerate(cues)) + "\n"
            artifact = self.store(job, "-subtitles.srt", srt.encode("utf-8"))
            if artifact not in config["artifacts"]:
                config["artifacts"].append(artifact)
            def ass_stamp(value):
                cs = round(value*100)
                return f"{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}"
            header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\nWrapStyle: 0\n"
                      "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
                      f"Style: Default,WenQuanYi Micro Hei,{config['font_size']},&H00FFFFFF,&H00FFFFFF,&H00201812,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,35,35,40,1\n"
                      "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
            ass = header + "\n".join(f"Dialogue: 0,{ass_stamp(begin)},{ass_stamp(end)},Default,,0,0,0,,{text}" for begin,end,text in cues)
            artifact = self.store(job, "-subtitles.ass", ass.encode("utf-8"))
            if artifact not in config["artifacts"]:
                config["artifacts"].append(artifact)
            save()
            args += ["-vf", f"ass=ai-{job['id']}-subtitles.ass:fontsdir=../fonts", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "4M", "-bufsize", "8M", "-pix_fmt", "yuv420p", "-threads", "2"]
        else:
            args += ["-c:v", "copy"]
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-t", str(info["duration"]), "-movflags", "+faststart", str(temporary)]
        try:
            await run_media(*args, timeout=300, cwd=str(source.parent))
            check = await probe_video(temporary)
            if not check["audio"] or abs(check["duration"]-info["duration"])>.2 or not 1024 <= temporary.stat().st_size <= max_bytes:
                raise VoiceoverError("配音成片的音轨、时长或大小校验未通过，已保留原视频。")
            config["rendered_identity"] = identity(temporary)
            save()
            temporary.replace(final)
            return final
        finally:
            temporary.unlink(missing_ok=True)

    async def step(self, job: dict, save, max_bytes: int) -> None:
        """One bounded stage; save() persists the parent ledger before paid calls."""
        config = job["voiceover"]
        try:
            await self.preflight(job)
        except Exception as exc:
            message = str(exc) if isinstance(exc, VoiceoverError) else "配音环境检查失败，请检查 FFmpeg、字体和服务器存储。"
            job.update(phase="failed", recoverable=True, retry_stage="voice", message=message); save(); return
        if not config.get("clips"):
            try:
                source, original = self.path(job, ".mp4"), self.path(job, "-source.mp4")
                expected = job["cleanup_identity"]
                if not original.exists():
                    if identity(source) != expected:
                        raise VoiceoverError("原始视频已变化，不能开始配音。")
                    os.link(source, original)
                if identity(original) != expected:
                    raise VoiceoverError("原始视频备份已变化，已停止处理。")
                config["source_identity"] = expected
                config["artifacts"] = [{"suffix": "-source.mp4", "identity": expected}]
                info = await probe_video(original)
                slots = windows(info["duration"])
                result = await self.plan(job, slots)
                config["clips"] = parse_narration(result, slots)
            except Exception as exc:
                job.update(phase="failed", recoverable=True, retry_stage="voice", message=str(exc) if isinstance(exc, VoiceoverError) else "旁白策划失败，请检查 AstrBot 文本模型；原视频已保留。")
                save(); return
            job.update(message="旁白稿已完成，准备逐段配音。", retry_stage="", recoverable=False); save(); return
        clip = next((clip for clip in config["clips"] if clip["phase"] != "done"), None)
        if clip is None:
            try:
                for path, expected in self.artifacts(job):
                    if identity(path) != expected:
                        raise VoiceoverError("后期素材已被修改，请先核对文件。")
                destination = await self.render(job, max_bytes, save)
                config["complete"] = True
                job.update(phase="ready", cleanup_identity=identity(destination), message="配音与字幕处理完成，等待投稿。", recoverable=False, retry_stage="")
            except Exception as exc:
                job.update(phase="failed", recoverable=True, retry_stage="voice", message=str(exc) if isinstance(exc, VoiceoverError) else "配音合成失败，可重试后期处理，原视频和已生成配音保留。")
            save(); return
        index = config["clips"].index(clip)
        suffix = f"-voice-{index+1:02d}.wav"
        if clip["phase"] == "requesting":
            job.update(phase="review", recoverable=False, message="上次配音受理结果不明，请到语音服务商核对；不会重复收费提交。")
            save(); return
        try:
            client = SpeechClient(config, self.key(config))
        except VoiceoverError as exc:
            job.update(phase="failed", recoverable=True, retry_stage="voice", message=str(exc)); save(); return
        if clip["phase"] == "queued":
            clip["phase"] = "requesting"
            job["message"] = f"正在合成第 {index+1}/{len(config['clips'])} 段旁白。"
            save()
            try:
                result = await client.synthesize(clip["text"])
            except SpeechRejected as exc:
                clip["phase"] = "rejected"
                job.update(phase="failed", recoverable=True, retry_stage="voice", message=str(exc)); save(); return
            except Exception:
                job.update(phase="review", recoverable=False, message="配音受理结果不明，请到语音服务商核对；不会重复提交。")
                save(); return
            try:
                if "content" in result:
                    artifact = self.store(job, suffix, result["content"])
                    config["artifacts"].append(artifact)
                    clip.update(phase="downloaded", identity=artifact["identity"])
                else:
                    clip.update(phase="downloading", url=result["url"])
            except Exception:
                job.update(phase="review", recoverable=False, message="配音已受理，但本地保存未完成，请核对语音服务记录；不会自动重新合成。")
                save(); return
            save()
        try:
            if clip["phase"] == "downloading":
                content = await client.download(clip["url"])
                artifact = self.store(job, suffix, content)
                config["artifacts"].append(artifact)
                clip.update(phase="downloaded", identity=artifact["identity"])
                clip.pop("url", None)
                save()
            path = self.path(job, suffix)
            if identity(path) != clip["identity"]:
                raise VoiceoverError("配音文件已被修改，请先核对素材。")
            clip.update(duration=await audio_duration(path), phase="done")
            job["message"] = f"已完成 {index+1}/{len(config['clips'])} 段配音。"
        except Exception as exc:
            job.update(phase="failed", recoverable=True, retry_stage="voice", message=str(exc) if isinstance(exc, VoiceoverError) else "配音下载或校验失败；重试会复用原配音结果，不会重新生成视频。")
        save()

    def resume(self, job: dict) -> None:
        config = job["voiceover"]
        if any(clip["phase"] == "requesting" for clip in config.get("clips", [])):
            raise VoiceoverError("存在受理状态不明的配音，请先到语音服务商核对，不会重复提交。")
        settings = self.settings()
        key = settings["profiles"].get(config["provider"] + ":" + config["region"], {}).get("key")
        if not settings["enabled"] or not key:
            raise VoiceoverError("请先开启配音，并配置任务所用语音服务和地区的 Key。")
        # Explicit retry may adopt a replacement Key; automatic ticks never do.
        config["key_revision"] = revision(key)
        for clip in job["voiceover"].get("clips", []):
            if clip["phase"] == "rejected":
                clip["phase"] = "queued"
