"""Normalize and concatenate locally generated shots without invoking a shell."""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from pathlib import Path


class CompositionError(Exception):
    """A local composition failure safe to show in the dashboard."""


async def run_media(*args: str, timeout: int = 300, cwd: str | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
    )
    try:
        async with asyncio.timeout(timeout):
            output, _error = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise CompositionError("视频处理失败，请检查 FFmpeg 编码器和分镜文件；已保留原始片段。")
    return output


async def probe_video(path: Path) -> dict:
    raw = await run_media(
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path), timeout=30,
    )
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
        if not math.isfinite(duration) or duration <= 0 or not any(item["codec_type"] == "video" for item in streams):
            raise ValueError()
    except (ValueError, KeyError, TypeError) as exc:
        raise CompositionError("无法读取有效的分镜视频，请检查原始片段。") from exc
    return {"duration": duration, "streams": streams, "audio": any(item["codec_type"] == "audio" for item in streams)}


async def compose_video(data_dir: Path, job_id: str, durations: list[int], max_bytes: int) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not 2 <= len(durations) <= 12:
        raise CompositionError("分镜任务参数无效。")
    if any(type(value) is not int or not 1 <= value <= 15 for value in durations):
        raise CompositionError("分镜时长无效。")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise CompositionError("分镜合成需要服务器安装 ffmpeg 和 ffprobe。")
    root = data_dir.resolve()
    media = data_dir / "media"
    work = data_dir / (".compose-" + job_id)
    final = media / f"ai-{job_id}.mp4"
    if (media.is_symlink() or media.resolve() != root / "media" or work.is_symlink()
            or work.resolve() != root / work.name or final.is_symlink()):
        raise CompositionError("分镜合成目录已变化，已停止处理。")
    sources = [media / f"ai-{job_id}-shot-{index + 1:02d}.mp4" for index in range(len(durations))]
    if any(path.is_symlink() or path.resolve() != media.resolve() / path.name or not path.is_file() for path in sources):
        raise CompositionError("原始分镜文件缺失或路径已变化，请恢复文件后重新合成。")
    work.mkdir(exist_ok=True)
    outputs = [work / f"part-{index:02d}.mp4" for index in range(len(durations))]
    manifest, assembled = work / "concat.txt", work / "assembled.mp4"
    if any(path.is_symlink() for path in [*outputs, manifest, assembled]):
        raise CompositionError("合成临时文件路径异常，已停止处理。")
    try:
        for source, destination, duration in zip(sources, outputs, durations):
            info = await probe_video(source)
            if info["duration"] < duration - .5:
                raise CompositionError("分镜实际时长明显不足，已保留片段供核对。")
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "1", "-i", str(source)]
            if not info["audio"]:
                args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            args += [
                "-map", "0:v:0", "-map", "0:a:0" if info["audio"] else "1:a:0",
                "-vf", f"scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=0.5,trim=duration={duration},setpts=PTS-STARTPTS",
                "-af", f"aresample=48000,apad,atrim=duration={duration},asetpts=PTS-STARTPTS",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "4M", "-bufsize", "8M",
                "-threads", "2", "-pix_fmt", "yuv420p", "-video_track_timescale", "90000", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "-b:a", "128k", "-t", str(duration), "-movflags", "+faststart", str(destination),
            ]
            await run_media(*args)
        manifest.write_text("".join(f"file '{path.name}'\n" for path in outputs), encoding="utf-8")
        await run_media(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1",
            "-i", str(manifest), "-c", "copy", "-movflags", "+faststart", str(assembled), timeout=120,
        )
        result = await probe_video(assembled)
        if abs(result["duration"] - sum(durations)) > .2 or not 1024 <= assembled.stat().st_size <= max_bytes:
            raise CompositionError("合成结果的时长或大小不符合要求，已保留原始片段。")
        assembled.replace(final)
        return final
    finally:
        # Only these generated temporary files are removable; never recursively remove a computed path.
        for path in [*outputs, manifest, assembled]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass
