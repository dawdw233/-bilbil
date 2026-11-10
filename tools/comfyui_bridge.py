"""Expose a local ComfyUI H3 workflow through the plugin's Videos API adapter."""

import argparse
import io
import logging
import re
import secrets
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import aiohttp
from aiohttp import web
from PIL import Image

MODEL = "comfyui-h3"
AUDIO_MODEL = "comfyui-h3-audio"
UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
OUTPUT_FOLDER = "bili-robot"
MAX_VIDEO = 100 * 1024 * 1024
MAX_REFERENCE = 10 * 1024 * 1024
# 成片统一放大到 1080p 竖屏；显存/时长撑不住时把它改成 None，就按生成分辨率出片。
UPSCALE = (1080, 1920)
# 总步数：第 2 步切到高分辨率（SelfLift transition_step=2），所以是 2 步低分辨率 + STEPS-2 步高分辨率。
# Turbo LoRA 官方按 4 步蒸馏；4 → 8 是「更细一点、时间翻倍」的折中，再往上收益变小。
STEPS = 8
SECONDS = ("4", "8", "10", "12")
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
        "\n\nKeep facial proportions consistent across frames, but let the face act: "
        "the mouth and jaw should follow the action naturally — chewing, biting, talking "
        "and swallowing must read clearly on screen, do not freeze the expression."
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


class ComfyBridge:
    def __init__(self, state_dir, token, comfy_port, width=None, height=None):
        self.token = token
        self.base = f"http://127.0.0.1:{comfy_port}"
        self.width, self.height = width, height
        self.database = state_dir / "comfyui-jobs.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, prefix TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            yield db

    async def comfy_json(self, path, *, json_body=None, form=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            method = "GET" if json_body is None and form is None else "POST"
            async with session.request(method, self.base + path, json=json_body, data=form, allow_redirects=False) as response:
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
            ("CLIPLoader", "clip_name", ENCODER),
            ("VAELoader", "vae_name", VIDEO_VAE),
            ("VAELoader", "vae_name", AUDIO_VAE),
            ("LoraLoaderModelOnly", "lora_name", LORA),
        )
        nodes = {}
        for name, field, filename in checks:
            if name not in nodes:
                nodes[name] = await self.comfy_json("/object_info/" + name)
            choices = nodes[name].get(name, {}).get("input", {}).get("required", {}).get(field, [[]])[0]
            if filename not in choices:
                raise web.HTTPServiceUnavailable(text=f"ComfyUI 未找到模型文件 {filename}。")
        required_nodes = ["MiniMaxH3ReferenceToVideo", "SelfLiftH3Sampler", "MiniMaxH3SigmaShift",
                          "MiniMaxChunkFeedForward", "ConditioningZeroOut", "CreateVideo", "SaveVideo"]
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
            raise web.HTTPBadRequest(text="请选择 comfyui-h3 或 comfyui-h3-audio，时长须为 4、8、10 或 12 秒。")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise web.HTTPBadRequest(text="视频描述为空或过长。")
        seconds = int(seconds)
        width, height = self.width, self.height
        if width is None or height is None:
            stats = await self.comfy_json("/system_stats")
            width, height = portrait_resolution(stats.get("devices", []), seconds)
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
            form = aiohttp.FormData()
            form.add_field("image", content, filename=filename, content_type="image/png")
            form.add_field("subfolder", OUTPUT_FOLDER)
            form.add_field("type", "input")
            result = await self.comfy_json("/upload/image", form=form)
            if result.get("name") != filename or result.get("subfolder") != OUTPUT_FOLDER:
                raise web.HTTPServiceUnavailable(text="无法核对 ComfyUI 上传的参考图。")
            uploaded = f"{OUTPUT_FOLDER}/{filename}"

        frames = 5 + 17 * round((seconds * 24 - 5) / 17)
        graph = h3_workflow(prompt, frames, width, height, f"{OUTPUT_FOLDER}/{prefix}", uploaded,
                            audio=model == AUDIO_MODEL)
        result = await self.comfy_json("/prompt", json_body={"prompt": graph, "client_id": prefix})
        job = result.get("prompt_id", "")
        if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f-]{36}", job):
            raise web.HTTPServiceUnavailable(text="未确认 ComfyUI 任务是否已受理。")
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?, ?)", (job, prefix))
        LOG.info("Accepted ComfyUI job %s (%s, %dx%d, %d frames)", job, model, width, height, frames)
        return web.json_response({"id": job, "status": "queued"}, status=202)

    async def outcome(self, job):
        with self.connect() as db:
            row = db.execute("SELECT prefix FROM jobs WHERE id = ?", (job,)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        history = await self.comfy_json("/history/" + job)
        result = history.get(job)
        if not result:
            return "in_progress", None
        status = result.get("status", {})
        if status.get("status_str") == "error":
            for kind, data in status.get("messages", []):
                if kind == "execution_error":
                    LOG.warning("Job %s failed at %s (%s)", job, data.get("node_type"), data.get("exception_type"))
            return "failed", None
        outputs = result.get("outputs", {}).get("15", {})
        for files in outputs.values():
            for item in files if isinstance(files, list) else []:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename", "")
                if (item.get("type") == "output" and item.get("subfolder") == OUTPUT_FOLDER
                        and re.fullmatch(re.escape(row[0]) + r"_[0-9]+_?\.mp4", filename)):
                    return "completed", {"filename": filename, "subfolder": OUTPUT_FOLDER, "type": "output"}
        return ("failed", None) if status.get("completed") else ("in_progress", None)

    async def status(self, request):
        job = request.match_info["job"]
        status, _ = await self.outcome(job)
        return web.json_response({"id": job, "status": status})

    async def content(self, request):
        status, params = await self.outcome(request.match_info["job"])
        if status != "completed":
            raise web.HTTPConflict(text="ComfyUI 成片尚不可下载。")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80)) as session:
            async with session.get(self.base + "/view", params=params, allow_redirects=False) as upstream:
                size = upstream.content_length
                if upstream.status != 200 or size is None or not 1024 <= size <= MAX_VIDEO:
                    raise web.HTTPServiceUnavailable(text="ComfyUI 成片不存在或超过 100 MB。")
                reply = web.StreamResponse(headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
                await reply.prepare(request)
                async for chunk in upstream.content.iter_chunked(128 * 1024):
                    await reply.write(chunk)
                await reply.write_eof()
                return reply

    def app(self):
        app = web.Application(middlewares=[self.guard], client_max_size=12 * 1024 * 1024)
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
