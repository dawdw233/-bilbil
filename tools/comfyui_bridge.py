"""Expose a local ComfyUI H3 workflow through the plugin's Videos API adapter.

长片按 5 秒一段顺序生成，段间用上一段尾帧当下一段首帧（FL2VA），最后用 PyAV 拼成一条。
"""

import argparse
import asyncio
import io
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import aiohttp
from aiohttp import web
from PIL import Image

from reference_art import ReferenceArt

MODEL = "comfyui-h3"
AUDIO_MODEL = "comfyui-h3-audio"
UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
FL2VA = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
FL2V_LORA = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
OUTPUT_FOLDER = "bili-robot"
MAX_VIDEO = 100 * 1024 * 1024
MAX_REFERENCE = 10 * 1024 * 1024

# 成片统一放大到 1080p 竖屏；显存/时长撑不住时把它改成 None，就按生成分辨率出片。
UPSCALE = (1080, 1920)
# 总步数：第 2 步切到高分辨率（SelfLift transition_step=2），所以是 2 步低分辨率 + STEPS-2 步高分辨率。
STEPS = 8
# 这台机器（8 GB 显存 / 15 GB 内存）实测单段 5 秒稳、8 秒跑不完、10 秒采样 OOM。
# 长片按 5 秒一段串行生成，段间用尾帧接续，最后拼接；总时长上限见 MAX_SECONDS。
SEGMENT_SECONDS = 5
SEGMENT_FRAMES = 5 + 17 * round((SEGMENT_SECONDS * 24 - 5) / 17)   # 124 帧 = 5.167 秒
MAX_SECONDS = 60
SECONDS = tuple(str(value) for value in range(SEGMENT_SECONDS, MAX_SECONDS + 1, SEGMENT_SECONDS))
SEGMENT_TIMEOUT = 40 * 60
# PyAV 辅助脚本跑在 ComfyUI 自带的 python 里（那边有 av，本机没有 ffmpeg.exe）
COMFY_PYTHON = os.environ.get(
    "COMFY_PYTHON", r"C:/Users/syhsz/Downloads/ComfyUI-aki-v3.2 (1)/ComfyUI-aki-v3.2/python/python.exe"
)
HELPER_DIR = Path(os.environ.get("COMFY_HELPER_DIR", r"C:/work/qqbot/comfyui-link"))

LOG = logging.getLogger(__name__)


def portrait_resolution(devices, seconds):
    """生成分辨率：够用的机器直接 720p 竖屏（736×1312），不够才降级。"""
    # Total VRAM is stable across jobs, even when ComfyUI still caches weights.
    vram = max((device.get("vram_total", 0) for device in devices
                if device.get("type") != "cpu"), default=0)
    # 9:16 且 32 对齐的 720p 竖屏：736×1312（720×1280 不是 32 的倍数）
    vram_gib = vram / (1024 ** 3)
    if vram_gib >= 7.5:
        return 736, 1312
    if vram_gib >= 5:
        return 448, 800
    return 288, 512


def h3_workflow(prompt, frames, width, height, prefix, image=None, *, audio=False):
    """构建 Ref2VA 图：SelfLift 两段采样（第 2 步升分辨率），出片前放大到 1080p。

    节点接法照抄 `_preview/YZ金鱼-MiniMax H3-本机8GB兼容版.json`，单段 4 步 Turbo LoRA。
    """
    def node(class_type, **inputs):
        return {"class_type": class_type, "inputs": inputs}

    prompt += (
        "\n\nKeep facial proportions consistent across frames, but let the face act. "
        "When the subject eats, drinks or talks, the mouth must open and close rhythmically, "
        "several times in a row: lips part, jaw drops, lips close again — never hold one "
        "mouth shape for the whole shot. Chewing should read on the lips, jaw and cheeks."
    )
    graph = {
        "1": node("UNETLoader", unet_name=UNET, weight_dtype="default"),
        "2": node("CLIPLoader", clip_name=ENCODER, type="minimax", device="default"),
        "3": node("VAELoader", vae_name=VIDEO_VAE),
        "4": node("VAELoader", vae_name=AUDIO_VAE),
        "5": node("LoraLoaderModelOnly", model=["1", 0], lora_name=LORA, strength_model=1),
        "17": node("MiniMaxH3SigmaShift", model=["5", 0], shift_video=12.0, shift_audio=3.0),
        "18": node("MiniMaxChunkFeedForward", model=["17", 0], chunks=2, seq_threshold=4096),
        "6": node("MiniMaxH3ReferenceToVideo", clip=["2", 0], vae=["3", 0], audio_vae=["4", 0],
                  prompt=prompt, width=width, height=height, length=frames, ref_image_size="match"),
        "7": node("ConditioningZeroOut", conditioning=["6", 0]),
        "8": node("BasicScheduler", model=["18", 0], scheduler="simple", steps=STEPS, denoise=1),
        "10": node("KSamplerSelect", sampler_name="euler"),
        "11": node("SelfLiftH3Sampler", model=["18", 0], positive=["6", 0], negative=["7", 0],
                   vae=["3", 0], latent_image=["6", 1], sampler=["10", 0], sigmas=["8", 0],
                   seed=secrets.randbits(48), cfg=1.0, transition_step=2, lowres_scale=0.6,
                   rho=0.6, w_min=0.6, w_max=1.0, upscaler_model="none",
                   highres_tiling=False, upscaler_unload=True),
        "12": node("VAEDecode", samples=["11", 0], vae=["3", 0]),
        "14": node("CreateVideo", fps=24, bit_depth=8),
        "15": node("SaveVideo", video=["14", 0], filename_prefix=prefix, format="mp4", codec="auto"),
    }
    if UPSCALE:
        target_w, target_h = UPSCALE
        graph["19"] = node("ImageScale", image=["12", 0], upscale_method="lanczos",
                           width=target_w, height=target_h, crop="center")
        graph["14"]["inputs"]["images"] = ["19", 0]
    else:
        graph["14"]["inputs"]["images"] = ["12", 0]
    if image:
        graph["16"] = node("LoadImage", image=image)
        inputs = graph["6"]["inputs"]
        inputs["ref_images.ref_image_0"] = ["16", 0]
        inputs["prompt"] = (
            "Use <Picture 1> only to fix the subject's appearance (hair, outfit, colours). "
            "Any front, side, and back views of the same subject describe one character. "
            "Do not copy the reference's pose, framing or closed-mouth expression, and never show "
            "the reference sheet, grids, split screens, labels or overlays in the shot. "
            "Frame the action as described below — close enough that the face and mouth are clearly visible.\n\n"
            + prompt
        )
    if audio:
        graph["13"] = node("VAEDecodeAudio", samples=["11", 0], vae=["4", 0])
        graph["14"]["inputs"]["audio"] = ["13", 0]
    return graph


def h3_continuation(prompt, frames, width, height, prefix, first_frame):
    """第二段起：用 FL2VA 接上一段的尾帧，保持同一个镜头继续。"""
    def node(class_type, **inputs):
        return {"class_type": class_type, "inputs": inputs}

    prompt += (
        "\n\nKeep the body pose, the running direction and the camera angle from the first frame; "
        "do not reset or restart the action. Keep facial proportions consistent across frames."
    )
    graph = {
        "1": node("UNETLoader", unet_name=FL2VA, weight_dtype="default"),
        "2": node("CLIPLoader", clip_name=ENCODER, type="minimax", device="default"),
        "3": node("VAELoader", vae_name=VIDEO_VAE),
        "4": node("VAELoader", vae_name=AUDIO_VAE),
        "5": node("LoraLoaderModelOnly", model=["1", 0], lora_name=FL2V_LORA, strength_model=1),
        "17": node("MiniMaxH3SigmaShift", model=["5", 0], shift_video=12.0, shift_audio=3.0),
        "18": node("MiniMaxChunkFeedForward", model=["17", 0], chunks=2, seq_threshold=4096),
        "6": node("MiniMaxH3ImageToVideo", clip=["2", 0], vae=["3", 0], prompt=prompt,
                  width=width, height=height, length=frames, first_frame=["16", 0]),
        "16": node("LoadImage", image=first_frame),
        "7": node("ConditioningZeroOut", conditioning=["6", 0]),
        "8": node("BasicScheduler", model=["18", 0], scheduler="simple", steps=STEPS, denoise=1),
        "10": node("KSamplerSelect", sampler_name="euler"),
        "11": node("SelfLiftH3Sampler", model=["18", 0], positive=["6", 0], negative=["7", 0],
                   vae=["3", 0], latent_image=["6", 1], sampler=["10", 0], sigmas=["8", 0],
                   seed=secrets.randbits(48), cfg=1.0, transition_step=2, lowres_scale=0.6,
                   rho=0.6, w_min=0.6, w_max=1.0, upscaler_model="none",
                   highres_tiling=False, upscaler_unload=True),
        "12": node("VAEDecode", samples=["11", 0], vae=["3", 0]),
        "14": node("CreateVideo", fps=24, bit_depth=8),
        "15": node("SaveVideo", video=["14", 0], filename_prefix=prefix, format="mp4", codec="auto"),
    }
    if UPSCALE:
        target_w, target_h = UPSCALE
        graph["19"] = node("ImageScale", image=["12", 0], upscale_method="lanczos",
                           width=target_w, height=target_h, crop="center")
        graph["14"]["inputs"]["images"] = ["19", 0]
    else:
        graph["14"]["inputs"]["images"] = ["12", 0]
    return graph


class ComfyBridge:
    def __init__(self, state_dir, token, comfy_port, width=None, height=None):
        self.token = token
        self.base = f"http://127.0.0.1:{comfy_port}"
        self.width, self.height = width, height
        self.state_dir = state_dir
        self.database = state_dir / "comfyui-jobs.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, prefix TEXT NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'queued', file TEXT, total INTEGER NOT NULL DEFAULT 1, "
                "done INTEGER NOT NULL DEFAULT 0, error TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, ddl in (("state", "TEXT NOT NULL DEFAULT 'queued'"), ("file", "TEXT"),
                              ("total", "INTEGER NOT NULL DEFAULT 1"),
                              ("done", "INTEGER NOT NULL DEFAULT 0"), ("error", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        self.tasks: set[asyncio.Task] = set()

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            yield db

    async def comfy_json(self, path, *, json_body=None, form=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            method = "GET" if json_body is None and form is None else "POST"
            async with session.request(method, self.base + path, json=json_body, data=form,
                                       allow_redirects=False) as response:
                if response.status == 400:
                    raise web.HTTPBadRequest(text="ComfyUI 拒绝了工作流输入，请检查模型文件和本机日志。")
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text="ComfyUI 暂时无法完成请求。")
                return await response.json()

    @web.middleware
    async def guard(self, request, handler):
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, f"Bearer {self.token}".encode()):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except (aiohttp.ClientError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="本机 ComfyUI 暂时无法连接。") from None

    async def models(self, request):
        await self.comfy_json("/system_stats")
        checks = (
            ("UNETLoader", "unet_name", UNET),
            ("UNETLoader", "unet_name", FL2VA),
            ("CLIPLoader", "clip_name", ENCODER),
            ("VAELoader", "vae_name", VIDEO_VAE),
            ("VAELoader", "vae_name", AUDIO_VAE),
            ("LoraLoaderModelOnly", "lora_name", LORA),
            ("LoraLoaderModelOnly", "lora_name", FL2V_LORA),
        )
        nodes = {}
        for name, field, filename in checks:
            if name not in nodes:
                nodes[name] = await self.comfy_json("/object_info/" + name)
            choices = [
                option for group in nodes[name].get(name, {}).get("input", {}).get("required", {}).get(field, [[]])
                for option in (group if isinstance(group, list) else [])
            ]
            if filename not in choices:
                raise web.HTTPServiceUnavailable(text=f"ComfyUI 未找到模型文件 {filename}。")
        required_nodes = ["MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo", "SelfLiftH3Sampler",
                          "MiniMaxH3SigmaShift", "MiniMaxChunkFeedForward", "ConditioningZeroOut",
                          "CreateVideo", "SaveVideo"]
        if UPSCALE:
            required_nodes.append("ImageScale")
        for name in required_nodes:
            if name not in await self.comfy_json("/object_info/" + name):
                raise web.HTTPServiceUnavailable(text=f"ComfyUI 缺少节点 {name}。")
        return web.json_response({"data": [
            {"id": MODEL, "name": "本机 ComfyUI · MiniMax H3 多参（无声）"},
            {"id": AUDIO_MODEL, "name": "本机 ComfyUI · MiniMax H3 多参（原生音频）"},
        ]})

    async def create(self, request):
        if request.content_type != "multipart/form-data":
            raise web.HTTPBadRequest(text="ComfyUI 网桥使用 Multipart 请求格式。")
        fields = await request.post()
        prompt = fields.get("prompt")
        seconds = fields.get("seconds")
        model = fields.get("model")
        if model not in (MODEL, AUDIO_MODEL) or seconds not in SECONDS:
            allowed = "、".join(SECONDS)
            raise web.HTTPBadRequest(
                text=f"请选择 comfyui-h3 或 comfyui-h3-audio；时长可选 {allowed} 秒"
                     f"（按 {SEGMENT_SECONDS} 秒一段串行生成）。"
            )
        seconds = int(seconds)
        if model == AUDIO_MODEL and seconds != SEGMENT_SECONDS:
            raise web.HTTPBadRequest(text=f"原生音频目前只支持单段 {SEGMENT_SECONDS} 秒。")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise web.HTTPBadRequest(text="视频描述为空或过长。")
        width, height = self.width, self.height
        if width is None or height is None:
            stats = await self.comfy_json("/system_stats")
            width, height = portrait_resolution(stats.get("devices", []), seconds)
        job = str(uuid.uuid4())
        prefix = uuid.uuid4().hex
        uploaded = None
        reference = fields.get("input_reference")
        if reference is not None:
            if not isinstance(reference, web.FileField):
                raise web.HTTPBadRequest(text="参考图必须为 PNG 文件。")
            content = reference.file.read(MAX_REFERENCE + 1)
            try:
                with Image.open(io.BytesIO(content)) as image:
                    if image.format != "PNG" or max(image.size) > 2048 or len(content) > MAX_REFERENCE:
                        raise ValueError()
                    image.verify()
            except (OSError, ValueError, Image.DecompressionBombError):
                raise web.HTTPBadRequest(text="参考图必须为有效的 PNG，大小不超过 10 MB。") from None
            filename = prefix + ".png"
            uploaded = await self.upload_image(content, filename)

        total = max(1, seconds // SEGMENT_SECONDS)
        with self.connect() as db:
            db.execute("INSERT INTO jobs (id, prefix, state, total, done) VALUES (?, ?, 'queued', ?, 0)",
                       (job, prefix, total))
        task = asyncio.create_task(self.run_job(
            job=job, prefix=prefix, prompt=prompt, width=width, height=height,
            segments=total, reference=uploaded, audio=model == AUDIO_MODEL,
        ))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        LOG.info("Accepted ComfyUI job %s (%s, %dx%d, %d x %ds)", job, model, width, height,
                 total, SEGMENT_SECONDS)
        return web.json_response({"id": job, "status": "queued"}, status=202)

    async def upload_image(self, content, filename):
        form = aiohttp.FormData()
        form.add_field("image", content, filename=filename, content_type="image/png")
        form.add_field("subfolder", OUTPUT_FOLDER)
        form.add_field("type", "input")
        form.add_field("overwrite", "true")
        result = await self.comfy_json("/upload/image", form=form)
        if result.get("name") != filename:
            raise web.HTTPServiceUnavailable(text="无法核对 ComfyUI 上传的图片。")
        return f"{result.get('subfolder') or OUTPUT_FOLDER}/{filename}"

    def set_state(self, job, **values):
        allowed = {key: value for key, value in values.items() if key in
                   ("state", "file", "done", "error")}
        if not allowed:
            return
        columns = ", ".join(f"{key} = ?" for key in allowed)
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*allowed.values(), job))

    async def run_job(self, *, job, prefix, prompt, width, height, segments, reference, audio):
        self.set_state(job, state="running")
        try:
            files = []
            first_frame = reference
            for index in range(segments):
                tag = f"{prefix}-s{index + 1:02d}"
                if index == 0:
                    graph = h3_workflow(prompt, SEGMENT_FRAMES, width, height,
                                        f"{OUTPUT_FOLDER}/{tag}", reference, audio=audio)
                else:
                    graph = h3_continuation(prompt, SEGMENT_FRAMES, width, height,
                                            f"{OUTPUT_FOLDER}/{tag}", first_frame)
                result = await self.comfy_json("/prompt", json_body={"prompt": graph, "client_id": tag})
                prompt_id = result.get("prompt_id", "")
                if not isinstance(prompt_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", prompt_id):
                    raise RuntimeError("未确认 ComfyUI 任务是否已受理")
                path = await self.wait_segment(prompt_id, tag)
                files.append(path)
                self.set_state(job, done=index + 1)
                LOG.info("Segment %d/%d done for %s (%.1fs)", index + 1, segments, job,
                         path.stat().st_size / 1024 / 1024)
                if index + 1 < segments:
                    first_frame = await self.tail_frame(path, tag)
            target = self.state_dir / f"job-{job}.mp4"
            await self.concat(files, target)
            self.set_state(job, state="completed", file=str(target))
            LOG.info("Job %s completed: %s", job, target)
        except Exception as exc:  # 任何异常都落到 failed，别让任务永远 in_progress
            LOG.exception("Job %s failed", job)
            self.set_state(job, state="failed", error=str(exc)[:300])

    async def wait_segment(self, prompt_id, tag):
        deadline = asyncio.get_running_loop().time() + SEGMENT_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(10)
            history = await self.comfy_json("/history/" + prompt_id)
            record = history.get(prompt_id)
            if not record:
                continue
            status = record.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI 生成失败（{tag}）")
            for files in (record.get("outputs", {}).get("15", {}) or {}).values():
                for item in files if isinstance(files, list) else []:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("filename", "")
                    if (item.get("type") == "output" and item.get("subfolder") == OUTPUT_FOLDER
                            and name.startswith(tag) and name.endswith(".mp4")):
                        return await self.fetch_output(item)
        raise RuntimeError(f"等待成片超时（{tag}）")

    async def fetch_output(self, item):
        """把 ComfyUI 的输出文件下载到本地缓存目录，供后续拼接。"""
        cache = self.state_dir / "segments"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / item["filename"]
        if target.exists() and target.stat().st_size > 1024:
            return target
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(self.base + "/view", params=item, allow_redirects=False) as upstream:
                if upstream.status != 200:
                    raise RuntimeError("ComfyUI 成片拉取失败")
                data = await upstream.read()
        if len(data) < 1024 or data[4:8] != b"ftyp":
            raise RuntimeError("ComfyUI 返回的成片不是有效 MP4")
        target.write_bytes(data)
        return target

    async def tail_frame(self, path, tag):
        cache = self.state_dir / "segments"
        out = cache / f"{tag}-tail.png"
        await asyncio.to_thread(self.run_helper, "tail_frame_av.py", str(path), str(out))
        return await self.upload_image(out.read_bytes(), f"{tag}-tail.png")

    async def concat(self, files, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.run_helper, "concat_av.py", str(target), *[str(f) for f in files])
        if not target.exists() or target.stat().st_size < 1024:
            raise RuntimeError("拼接失败，没有产生成片")
        if target.stat().st_size > MAX_VIDEO:
            target.unlink(missing_ok=True)
            raise RuntimeError("成片超过 100 MB")

    def run_helper(self, script, *args):
        helper = HELPER_DIR / script
        if not helper.exists():
            raise RuntimeError(f"找不到辅助脚本 {helper}")
        python = COMFY_PYTHON if Path(COMFY_PYTHON).exists() else shutil.which("python")
        if not python:
            raise RuntimeError("找不到带 PyAV 的 python")
        result = subprocess.run([python, str(helper), *args], capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "辅助脚本失败")[-300:])

    def job_row(self, job):
        with self.connect() as db:
            return db.execute("SELECT id, prefix, state, file, total, done, error FROM jobs WHERE id = ?",
                              (job,)).fetchone()

    async def status(self, request):
        row = self.job_row(request.match_info["job"])
        if row is None:
            raise web.HTTPNotFound()
        state = row[2]
        status = "in_progress" if state in ("queued", "running") else state
        return web.json_response({"id": row[0], "status": status,
                                   "segments_done": row[5], "total_segments": row[4]})

    async def content(self, request):
        row = self.job_row(request.match_info["job"])
        if row is None:
            raise web.HTTPNotFound()
        if row[2] != "completed" or not row[3]:
            raise web.HTTPConflict(text="ComfyUI 成片尚不可下载。")
        path = Path(row[3])
        if not path.exists():
            raise web.HTTPServiceUnavailable(text="ComfyUI 成片不存在。")
        size = path.stat().st_size
        if not 1024 <= size <= MAX_VIDEO:
            raise web.HTTPServiceUnavailable(text="ComfyUI 成片异常。")
        reply = web.StreamResponse(headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
        await reply.prepare(request)
        with path.open("rb") as stream:
            while chunk := await asyncio.to_thread(stream.read, 128 * 1024):
                await reply.write(chunk)
        await reply.write_eof()
        return reply

    def app(self):
        app = web.Application(middlewares=[self.guard], client_max_size=12 * 1024 * 1024)
        ReferenceArt(self, h3_workflow).register(app)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/videos", self.create)
        app.router.add_get("/v1/videos/{job:[0-9a-f-]{36}}", self.status)
        app.router.add_get("/v1/videos/{job:[0-9a-f-]{36}}/content", self.content)
        return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=18188)
    parser.add_argument("--comfy-port", type=int, default=8188)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        parser.error("Pass both --width and --height to override automatic resolution")
    if args.width is not None and any(value < 64 or value > 1280 or value % 32 for value in (args.width, args.height)):
        parser.error("Width and height must be multiples of 32 between 64 and 1280")
    key = (args.state_dir / "bridge.key").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        parser.error("bridge.key must contain a randomly generated 64-character hexadecimal key")
    logging.basicConfig(level=logging.INFO)
    bridge = ComfyBridge(args.state_dir, key, args.comfy_port, args.width, args.height)
    web.run_app(bridge.app(), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
