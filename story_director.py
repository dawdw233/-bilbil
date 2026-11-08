"""General-purpose shot planning and local, identity-checked frame handoff."""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
import stat
import tempfile
from pathlib import Path

from PIL import Image

from .video_compose import CompositionError, probe_video, run_media


def file_identity(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CompositionError("镜头素材不是普通文件，已停止处理。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def directed_shot(row: dict, duration: int) -> dict:
    """Legacy plans remain valid; a new structured shot must be complete."""
    fields = {"beat": 100, "start_state": 180, "end_state": 180, "camera": 100,
              "sound": 160, "narration": 36}
    if not any(key in row for key in fields):
        return {}
    result = {}
    for key, limit in fields.items():
        value = row.get(key)
        if not isinstance(value, str) or len(value.strip()) > limit or (key != "narration" and not value.strip()):
            raise CompositionError("分镜方案缺少动作承接、机位、声音或旁白安排，尚未开始付费生成。")
        result[key] = re.sub(r"[\x00-\x1f]+", " ", value.strip())
    offset = row.get("voice_start", 0)
    if type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= duration - .5:
        raise CompositionError("分镜旁白起点超出当前镜头，尚未开始付费生成。")
    window = min(12.0, duration - offset)
    if len(result["narration"]) > max(1, min(36, int(window * 2.8))):
        raise CompositionError("分镜旁白过长，请缩短文稿；尚未开始付费生成。")
    result["voice_start"] = float(offset)
    return result


def native_sound(provider: str, model: str) -> bool:
    return ((provider == "wan" and model.startswith(("wan2.5-", "wan2.6-")))
            or (provider == "ark" and model.startswith(("doubao-seedance-2-", "doubao-seedance-1-5-pro"))))


def shot_prompt(job: dict, index: int) -> str:
    shot = job["shots"][index]
    provider, model = job["provider"], job["model"]
    budget = 1500 if provider == "wan" and model.startswith(("wan2.6-", "wan2.5-")) else 800 if provider == "wan" else 2000
    parts = [f"第 {index+1}/{len(job['shots'])} 段，{shot['duration']} 秒。", f"统一人物、场景与画风：{job['continuity']}"]
    if job.get("shot_link") == "continue" and index:
        parts.append("输入首帧是上一镜实际结束画面。从此状态继续动作，不重演之前情节；保持主体、道具与光线一致，平稳调整机位。")
    if shot.get("start_state"):
        parts += [f"起始状态：{shot['start_state']}", f"结束状态：{shot['end_state']}", f"机位与运镜：{shot['camera']}"]
    # Sound instructions precede the optional prose so provider prompt limits
    # cannot truncate the no-duplicate-narration constraint.
    if native_sound(provider, model):
        parts.append(f"画面同步环境音与动作音：{shot.get('sound', '与可见动作自然匹配的环境声')}。不生成旁白、对白、歌词或文字字幕，解说由后期单独添加。")
    parts.append("只呈现当前镜头的动作，不提前表演后续情节。不生成文字字幕。")
    prefix = "\n".join(parts) + "\n当前动作："
    if len(prefix) >= budget - 80:
        raise CompositionError("分镜统一设定和镜头描述过长，请缩短后重新策划。")
    return prefix + shot["prompt"][:budget-len(prefix)]


def frame_path(data_dir: Path, job_id: str, index: int) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or type(index) is not int or not 1 <= index <= 11:
        raise CompositionError("承接帧任务编号无效。")
    folder = data_dir / "story_frames"
    target = folder / f"ai-{job_id}-{index+1:02d}.jpg"
    if folder.is_symlink() or folder.resolve() != data_dir.resolve() / "story_frames" or target.is_symlink():
        raise CompositionError("承接帧路径存在目录跳转，已停止处理。")
    return target


async def handoff_frame(data_dir: Path, job: dict, index: int) -> dict:
    """Extract at the composition cut point, not beyond the trimmed clip."""
    previous, shot = job["shots"][index-1], job["shots"][index]
    media = data_dir / "media"
    source = media / f"ai-{job['id']}-shot-{index:02d}.mp4"
    if (media.is_symlink() or media.resolve() != data_dir.resolve() / "media"
            or previous.get("phase") != "done" or source.name != previous.get("file")
            or file_identity(source) != previous.get("identity")):
        raise CompositionError("上一镜素材缺失或已被修改，未提交下一镜。")
    target = frame_path(data_dir, job["id"], index)
    record = shot.get("handoff")
    if record and (not target.is_file() or file_identity(target) != record.get("identity")):
        raise CompositionError("承接帧已被修改或删除，请恢复原文件后重试。")
    if not target.exists():
        target.parent.mkdir(exist_ok=True)
        info = await probe_video(source)
        if info["duration"] < previous["duration"] - .5:
            raise CompositionError("上一镜实际时长不足，未提交下一镜。")
        moment = max(0, min(info["duration"], previous["duration"]) - .08)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".jpg", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            await run_media("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                            "-ss", f"{moment:.6f}", "-frames:v", "1", "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease",
                            "-q:v", "2", str(temporary), timeout=60)
            if file_identity(source) != previous["identity"]:
                raise CompositionError("提取承接帧期间上一镜被修改，已停止处理。")
            # Exclusive publication never overwrites a frame created elsewhere.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    content = target.read_bytes()
    if not 128 <= len(content) <= 10 * 1024 * 1024:
        raise CompositionError("承接帧大小无效，未提交下一镜。")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "JPEG" or min(image.size) < 300:
                raise ValueError()
            image.verify()
    except (OSError, ValueError) as exc:
        raise CompositionError("承接帧不能作为视频首帧，请检查上一镜画面。") from exc
    digest = hashlib.sha256(content).hexdigest()
    if record and digest != record.get("sha256"):
        raise CompositionError("承接帧内容已变化，已停止处理。")
    return {"record": {"identity": file_identity(target), "sha256": digest},
            "data_url": "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii")}


def narration_clips(job: dict, duration: float) -> list[dict] | None:
    """Use the film script, with deliberate silence and per-shot offsets."""
    shots = job.get("shots", [])
    if not shots or not all("narration" in shot for shot in shots):
        return None
    total = sum(shot["duration"] for shot in shots)
    if abs(total - duration) > .25:
        raise CompositionError("成片时长与分镜方案不符，不能按镜头添加旁白。")
    clips, start = [], 0.0
    for shot in shots:
        # Revalidate persisted scripts before any TTS call.
        parsed = directed_shot(shot, shot["duration"])
        offset = parsed["voice_start"]
        if parsed["narration"]:
            text = parsed["narration"].translate(str.maketrans({"{": "（", "}": "）", "<": "＜", ">": "＞", "\\": "／"}))
            clips.append({"text": text, "start": start+offset, "window": min(12.0, shot["duration"]-offset), "phase": "queued"})
        start += shot["duration"]
    return clips
