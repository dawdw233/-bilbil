"""Private server-side image library for image-to-video jobs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import stat
import tempfile
from pathlib import Path

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ReferenceError(ValueError):
    """A safe error about a reference image, never containing its contents."""


class ReferenceLibrary:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "reference_images"
        self._cache = {}

    def ensure(self) -> None:
        if self.path.is_symlink() or self.path.resolve().parent != self.data_dir.resolve():
            raise ReferenceError("参考图文件夹不能是符号链接或目录跳转。")
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReferenceError("无法创建或读取 reference_images 文件夹，请检查服务器权限。") from exc

    def _read(self, name: str) -> tuple[dict, bytes]:
        self.ensure()
        if not isinstance(name, str) or not name or Path(name).name != name or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
            raise ReferenceError("参考图文件名无效。")
        path = self.path / name
        if path.suffix.lower() not in SUFFIXES or path.is_symlink() or path.resolve().parent != self.path.resolve():
            raise ReferenceError("参考图必须是文件夹内的 PNG、JPG 或 WebP 图片，不能使用符号链接。")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE_BYTES:
                    raise ReferenceError("参考图必须是非空普通文件，且不超过 10 MB。")
                content = stream.read(MAX_IMAGE_BYTES + 1)
            if len(content) > MAX_IMAGE_BYTES:
                raise ReferenceError("参考图超过 10 MB。")
        except ReferenceError:
            raise
        except OSError as exc:
            raise ReferenceError("参考图无法读取，请检查文件是否存在及服务器权限。") from exc
        return self._inspect(name, content), content

    @staticmethod
    def _inspect(name: str, content: bytes) -> dict:
        if not 0 < len(content) <= MAX_IMAGE_BYTES:
            raise ReferenceError("参考图必须是非空图片，且不超过 10 MB。")
        try:
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                mime = MIMES.get(image.format)
                if not mime or getattr(image, "n_frames", 1) != 1:
                    raise ReferenceError("参考图需使用静态 PNG、JPG 或 WebP 图片。")
                if not (300 < min(width, height) and max(width, height) <= 4096 and 0.4 <= width / height <= 2.5):
                    raise ReferenceError("参考图短边须大于 300 像素，长边不超过 4096 像素，宽高比在 2:5～5:2 之间。")
                alpha = "A" in image.getbands() or "transparency" in image.info
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()  # Detect truncated JPEG/WebP pixel data as well as invalid headers.
        except ReferenceError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ReferenceError("参考图无法读取或图片已损坏，请重新放入有效图片。") from exc
        return {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                "mime": mime, "width": width, "height": height, "alpha": alpha}

    def upload(self, name: str, image: str) -> dict:
        """Validate bytes first, then atomically add an image without overwriting."""
        if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255
                or Path(name).name != name or "/" in name or "\\" in name
                or any(ord(c) < 32 for c in name) or Path(name).suffix.lower() not in SUFFIXES):
            raise ReferenceError("请选择 PNG、JPG 或 WebP 图片，文件名不能包含路径。")
        if not isinstance(image, str) or len(image) > 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 40:
            raise ReferenceError("参考图不能超过 10 MB。")
        header, separator, encoded = image.partition(",")
        if not separator or header not in {"data:" + mime + ";base64" for mime in MIMES.values()}:
            raise ReferenceError("上传内容不是支持的图片数据。")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ReferenceError("上传图片数据不完整，请重新选择图片。") from exc
        metadata = self._inspect(name, content)
        if header != "data:" + metadata["mime"] + ";base64":
            raise ReferenceError("图片声明格式与实际内容不一致，请重新导出图片。")
        self.ensure()
        # Content suffix makes retries idempotent and preserves pictures used by jobs.
        stem = Path(name).stem.strip(" .") or "reference"
        while len(stem.encode("utf-8")) > 100:
            stem = stem[:-1]
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[metadata["mime"]]
        saved_name = stem + "-" + metadata["sha256"][:16] + suffix
        destination = self.path / saved_name
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path, prefix=".upload-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
                created = True
            except FileExistsError:
                existing, _ = self._read(saved_name)
                if existing["sha256"] != metadata["sha256"]:
                    raise ReferenceError("同名参考图已存在且内容不同，请修改图片名称后上传。")
                created = False
        except OSError as exc:
            raise ReferenceError("参考图保存失败，请检查服务器磁盘空间和文件夹权限。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"uploaded": True, "created": created, "name": saved_name}

    def scan(self) -> dict:
        files, invalid = [], []
        try:
            self.ensure()
            names = sorted((p.name for p in self.path.iterdir() if p.suffix.lower() in SUFFIXES), key=lambda name: (name.casefold(), name))
            for name in names:
                try:
                    path = self.path / name
                    info = path.lstat()
                    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
                    cached = self._cache.get(name)
                    if cached is None or cached[0] != signature:
                        metadata, _ = self._read(name)
                        self._cache[name] = (signature, metadata)
                    else:
                        metadata = cached[1]
                    files.append(dict(metadata))
                except (ReferenceError, OSError) as exc:
                    invalid.append({"name": name, "message": str(exc) if isinstance(exc, ReferenceError) else "文件无法读取。"})
            self._cache = {name: value for name, value in self._cache.items() if name in names}
        except (ReferenceError, OSError) as exc:
            return {"directory": str(self.path), "count": 0, "files": [], "invalid_count": 0,
                    "invalid": [], "error": str(exc) if isinstance(exc, ReferenceError) else "无法读取参考图文件夹。"}
        return {"directory": str(self.path), "count": len(files), "files": files,
                "invalid_count": len(invalid), "invalid": invalid, "error": ""}

    def choose(self, jobs: list[dict]) -> dict:
        state = self.scan()
        if state["error"]:
            raise ReferenceError(state["error"])
        if not state["files"]:
            raise ReferenceError("reference_images 文件夹中没有可用参考图，请先放入有效图片。")
        last = next((job["reference_image"]["name"] for job in reversed(jobs) if job.get("reference_image")), "")
        key = lambda name: (name.casefold(), name)
        chosen = next((item for item in state["files"] if key(item["name"]) > key(last)), state["files"][0])
        # Reopen instead of trusting cached bytes when pinning a job.
        return self._read(chosen["name"])[0]

    def load(self, expected: dict, *, provider: str) -> dict:
        actual, content = self._read(expected.get("name"))
        if actual["sha256"] != expected.get("sha256"):
            raise ReferenceError("任务的参考图已被替换，请恢复原图后再重试；不会使用新图片继续生成。")
        if provider == "wan" and actual["alpha"]:
            raise ReferenceError("万相参考图不支持透明通道，请改用不透明 JPG 图片。")
        return {**actual, "data_url": "data:" + actual["mime"] + ";base64," + base64.b64encode(content).decode("ascii")}
