"""Local ComfyUI illustration jobs for the separate reference-video renderer."""

import asyncio
import io
import logging
import re
import uuid

import aiohttp
from aiohttp import web
from PIL import Image

ART_FOLDER = "reference-publish"
ART_TIMEOUT = 30 * 60
MAX_IMAGE = 10 * 1024 * 1024
LOG = logging.getLogger(__name__)


def still_workflow(build_graph, prompt, tag, reference):
    """Use the installed Ref2VA weights as a source of single illustrations."""
    graph = build_graph(prompt, 22, 544, 640, tag, reference)
    graph["6"]["inputs"]["prompt"] = (
        "Use <Picture 1> only for the appearance of one character. Preserve the hair, eye colour, "
        "outfit and chibi proportions. Do not show the sheet, panels, any side views or labels. "
        "One clean anime editorial illustration, character fully in frame, centered, natural hands, "
        "deep navy studio setting, soft cyan rim light, restrained palette, no writing or logos. "
        + prompt
    )
    graph["8"]["inputs"]["steps"] = 4
    for key in ("14", "15", "19"):
        graph.pop(key, None)
    graph["20"] = {"class_type": "ImageFromBatch", "inputs": {
        "image": ["12", 0], "batch_index": 0, "length": 1,
    }}
    graph["21"] = {"class_type": "SaveImage", "inputs": {
        "images": ["20", 0], "filename_prefix": f"{ART_FOLDER}/{tag}",
    }}
    return graph


class ReferenceArt:
    def __init__(self, bridge, build_graph):
        self.bridge = bridge
        self.build_graph = build_graph
        self.jobs = {}
        self.task = None

    async def create(self, request):
        if self.task is not None and not self.task.done():
            raise web.HTTPConflict(text="本机正在生成参考投稿插画。")
        if request.content_type != "multipart/form-data":
            raise web.HTTPBadRequest(text="需要描述和 PNG 角色参考图。")
        form = await request.post()
        prompt, ref = form.get("prompt"), form.get("input_reference")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 800:
            raise web.HTTPBadRequest(text="插画描述为空或过长。")
        if not isinstance(ref, web.FileField):
            raise web.HTTPBadRequest(text="缺少 PNG 角色参考图。")
        content = ref.file.read(MAX_IMAGE + 1)
        try:
            with Image.open(io.BytesIO(content)) as im:
                if im.format != "PNG" or max(im.size) > 2048 or len(content) > MAX_IMAGE:
                    raise ValueError
                im.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise web.HTTPBadRequest(text="参考图必须是有效的 PNG，且不超过 10 MB 和 2048 像素。") from None
        if self.task is not None and not self.task.done():
            raise web.HTTPConflict(text="本机正在生成参考投稿插画。")
        while len(self.jobs) >= 12:
            del self.jobs[next(iter(self.jobs))]
        job = uuid.uuid4().hex
        self.jobs[job] = {"status": "in_progress"}
        self.task = asyncio.create_task(self.run(job, prompt.strip(), content))
        return web.json_response({"id": job, "status": "in_progress"}, status=202)

    async def run(self, job, prompt, content):
        try:
            reference = await self.bridge.upload_image(content, f"art-{job}.png")
            graph = still_workflow(self.build_graph, prompt, job, reference)
            queued = await self.bridge.comfy_json("/prompt", json_body={"prompt": graph, "client_id": job})
            pid = queued.get("prompt_id", "")
            if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f-]{36}", pid):
                raise RuntimeError("ComfyUI 未确认插画任务。")
            async with asyncio.timeout(ART_TIMEOUT):
                while True:
                    await asyncio.sleep(4)
                    try:
                        history = await self.bridge.comfy_json("/history/" + pid)
                    except (aiohttp.ClientError, TimeoutError, web.HTTPServiceUnavailable):
                        continue
                    record = history.get(pid, {})
                    if record.get("status", {}).get("status_str") == "error":
                        raise RuntimeError("ComfyUI 插画生成失败；请查看本机执行日志。")
                    for item in record.get("outputs", {}).get("21", {}).get("images", []):
                        if (item.get("type") == "output" and item.get("subfolder") == ART_FOLDER
                                and re.fullmatch(job + r"_\d+_\.png", item.get("filename", ""))):
                            self.jobs[job] = {"status": "completed", "image": item}
                            return
        except Exception as exc:
            LOG.exception("Reference illustration %s failed", job)
            reason = exc.text if isinstance(exc, web.HTTPException) else str(exc)
            self.jobs[job] = {"status": "failed", "error": reason[:250] or "等待本机插画超时。"}

    async def status(self, request):
        job = self.jobs.get(request.match_info["job"])
        if job is None:
            raise web.HTTPNotFound()
        return web.json_response({key: value for key, value in job.items() if key != "image"})

    async def content(self, request):
        job = self.jobs.get(request.match_info["job"])
        if job is None:
            raise web.HTTPNotFound()
        if job["status"] != "completed":
            raise web.HTTPConflict(text="插画还未生成。")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(self.bridge.base + "/view", params=job["image"],
                                   allow_redirects=False) as response:
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text="无法读取本机插画。")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(128 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_IMAGE:
                        raise web.HTTPServiceUnavailable(text="本机插画超出大小限制。")
        if not chunks.startswith(b"\x89PNG\r\n\x1a\n"):
            raise web.HTTPServiceUnavailable(text="本机未返回有效 PNG。")
        return web.Response(body=bytes(chunks), content_type="image/png")

    def register(self, app):
        app.router.add_post("/v1/reference-images", self.create)
        app.router.add_get("/v1/reference-images/{job:[0-9a-f]{32}}", self.status)
        app.router.add_get("/v1/reference-images/{job:[0-9a-f]{32}}/content", self.content)
