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
UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
OUTPUT_FOLDER = "bili-robot"
MAX_VIDEO = 100 * 1024 * 1024
MAX_REFERENCE = 10 * 1024 * 1024
LOG = logging.getLogger(__name__)


def h3_workflow(prompt, frames, width, height, prefix, image=None):
    def node(class_type, **inputs):
        return {"class_type": class_type, "inputs": inputs}

    graph = {
        "1": node("UNETLoader", unet_name=UNET, weight_dtype="default"),
        "2": node("CLIPLoader", clip_name=ENCODER, type="minimax", device="default"),
        "3": node("VAELoader", vae_name=VIDEO_VAE),
        "4": node("VAELoader", vae_name=AUDIO_VAE),
        "5": node("LoraLoaderModelOnly", model=["1", 0], lora_name=LORA, strength_model=1),
        "6": node("MiniMaxH3ImageToVideo", clip=["2", 0], vae=["3", 0], prompt=prompt,
                  width=width, height=height, length=frames),
        "7": node("BasicGuider", model=["5", 0], conditioning=["6", 0]),
        "8": node("BasicScheduler", model=["5", 0], scheduler="simple", steps=4, denoise=1),
        "9": node("RandomNoise", noise_seed=secrets.randbits(48)),
        "10": node("KSamplerSelect", sampler_name="res_multistep"),
        "11": node("SamplerCustomAdvanced", noise=["9", 0], guider=["7", 0],
                   sampler=["10", 0], sigmas=["8", 0], latent_image=["6", 1]),
        "12": node("VAEDecode", samples=["11", 0], vae=["3", 0]),
        "13": node("VAEDecodeAudio", samples=["11", 0], vae=["4", 0]),
        "14": node("CreateVideo", images=["12", 0], audio=["13", 0], fps=24, bit_depth=8),
        "15": node("SaveVideo", video=["14", 0], filename_prefix=prefix, format="mp4", codec="auto"),
    }
    if image:
        graph["16"] = node("LoadImage", image=image)
        graph["6"]["inputs"]["first_frame"] = ["16", 0]
    return graph


class ComfyBridge:
    def __init__(self, state_dir, token, comfy_port, width, height):
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
        for name in ("MiniMaxH3ImageToVideo", "SaveVideo"):
            if name not in await self.comfy_json("/object_info/" + name):
                raise web.HTTPServiceUnavailable(text=f"ComfyUI 缺少节点 {name}。")
        return web.json_response({"data": [{"id": MODEL, "name": "本机 ComfyUI · MiniMax H3"}]})

    async def create(self, request):
        if request.content_type != "multipart/form-data":
            raise web.HTTPBadRequest(text="ComfyUI 网桥使用 Multipart 请求格式。")
        fields = await request.post()
        prompt = fields.get("prompt")
        seconds = fields.get("seconds")
        if fields.get("model") != MODEL or seconds not in ("4", "8", "12"):
            raise web.HTTPBadRequest(text="请选择 comfyui-h3 和 4、8 或 12 秒。")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise web.HTTPBadRequest(text="视频描述为空或过长。")
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

        frames = 5 + 17 * round((int(seconds) * 24 - 5) / 17)
        graph = h3_workflow(prompt, frames, self.width, self.height, f"{OUTPUT_FOLDER}/{prefix}", uploaded)
        result = await self.comfy_json("/prompt", json_body={"prompt": graph, "client_id": prefix})
        job = result.get("prompt_id", "")
        if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f-]{36}", job):
            raise web.HTTPServiceUnavailable(text="未确认 ComfyUI 任务是否已受理。")
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?, ?)", (job, prefix))
        LOG.info("Accepted ComfyUI job %s", job)
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
    parser.add_argument("--width", type=int, default=352)
    parser.add_argument("--height", type=int, default=192)
    args = parser.parse_args()
    if any(value < 64 or value > 1280 or value % 32 for value in (args.width, args.height)):
        parser.error("Width and height must be multiples of 32 between 64 and 1280")
    key = (args.state_dir / "bridge.key").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        parser.error("bridge.key must contain a randomly generated 64-character hexadecimal key")
    logging.basicConfig(level=logging.INFO)
    bridge = ComfyBridge(args.state_dir, key, args.comfy_port, args.width, args.height)
    web.run_app(bridge.app(), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
