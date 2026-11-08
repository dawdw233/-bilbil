"""Temporary, bounded keyframes from first-party Bilibili playback streams."""
from __future__ import annotations

import base64
import math
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .creative_learning import LearningError
from .video_compose import run_media

MAX_BYTES = 40 * 1024 * 1024


def stream_url(data):
    rows = data.get("dash", {}).get("video", [])
    if rows:
        rows = sorted(rows, key=lambda row: (row.get("height", 9999), row.get("bandwidth", 99999999)))
        value = rows[0].get("baseUrl") or rows[0].get("base_url") or ""
    else:
        rows = data.get("durl", [])
        if len(rows) != 1:
            raise LearningError("没有可用的单段视频流，已跳过学习。")
        value = rows[0].get("url", "")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host.endswith("." + suffix) for suffix in ("bilivideo.com", "bilivideo.cn"))):
        raise LearningError("视频流不是已支持的 B站 CDN 地址，已跳过学习。")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


async def download_stream(url, path, bvid):
    headers = {"Referer": f"https://www.bilibili.com/video/{bvid}/", "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in (412, 429):
                return {"code": response.status, "message": "视频流触发风控，停止学习。"}
            if response.status != 200 or (response.content_length or 0) > MAX_BYTES:
                raise LearningError("视频下载失败或超过 40 MB，已跳过学习。")
            total = 0
            with path.open("wb") as stream:
                async for chunk in response.content.iter_chunked(128 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise LearningError("视频超过 40 MB，已停止学习下载。")
                    stream.write(chunk)
            if total < 1024:
                raise LearningError("视频流内容无效，已跳过学习。")


async def extract_frames(path, count):
    raw = await run_media("ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                          "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path), timeout=10)
    duration = float(raw.strip())
    if not math.isfinite(duration) or not 2 <= duration <= 121:
        raise LearningError("实际视频时长不在学习范围内。")
    samples = []
    for index in range(count):
        start, end = duration * .05, min(duration * .9, duration - .5)
        stamp = round(start + (end - start) * index / (count - 1), 3)
        target = path.parent / f"frame-{index}.jpg"
        await run_media("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
                        "-protocol_whitelist", "file,pipe", "-ss", str(stamp), "-i", str(path),
                        "-frames:v", "1", "-filter_threads", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                        "-threads", "1", "-pix_fmt", "yuvj420p", "-q:v", "4", str(target), timeout=10)
        content = target.read_bytes()
        if not 100 <= len(content) <= 512 * 1024:
            raise LearningError("关键帧无效或过大，已跳过学习。")
        samples.append({"time": stamp, "image": "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")})
    return samples


async def video_frames(info, count, call, bili):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise LearningError("关键帧学习需要服务器安装 FFmpeg 和 ffprobe。")
    source = await call(bili.bili_learning_stream, info["bvid"])
    url = stream_url(source)
    # System-created directory; no source videos or images enter the experience library.
    with tempfile.TemporaryDirectory(prefix="bili-learning-") as folder:
        path = Path(folder) / "clip.mp4"
        result = await call(download_stream, url, path, info["bvid"])
        if isinstance(result, dict) and result.get("code"):
            raise LearningError("视频流触发风控，已停止学习。")
        return await extract_frames(path, count)
