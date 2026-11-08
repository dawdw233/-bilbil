"""Read QQ Bilibili shares without trusting card captions or arbitrary redirect URLs."""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

import aiohttp
from astrbot.api.event import filter

MAX_CARD = 16384
BVID = re.compile(r"BV[0-9A-Za-z]{10}")
HOSTS = {"www.bilibili.com", "bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"}
URL_KEYS = {"url", "qqdocurl", "jumpurl", "jump_url", "pcjumpurl", "androidjumpurl", "iosjumpurl"}


class CardReadError(ValueError):
    pass


def video_url(value) -> str:
    if not isinstance(value, str) or len(value) > MAX_CARD:
        return ""
    value = html.unescape(value).strip().replace("\\/", "/")
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("https", "http") or parsed.hostname not in HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return ""
        path = unquote(parsed.path)
        if parsed.hostname in ("b23.tv", "www.b23.tv"):
            return "https://b23.tv" + path.rstrip("/") if re.fullmatch(r"/[A-Za-z0-9]{1,64}/?", path) else ""
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", path)
        return "https://www.bilibili.com/video/" + match[1] if match else ""
    except ValueError:
        return ""


def text_url(text):
    if not isinstance(text, str) or len(text) > MAX_CARD:
        return ""
    for value in re.findall(r"https?://[^\s<>\"']+", html.unescape(text).replace("\\/", "/"))[:12]:
        found = video_url(value.rstrip(").,，。！!？?】》"))
        if found:
            return found
    return ""


def json_url(payload):
    if isinstance(payload, str):
        if len(payload) > MAX_CARD:
            return ""
        try:
            payload = json.loads(payload)
        except ValueError:
            try:
                payload = json.loads(html.unescape(payload))
            except (ValueError, RecursionError):
                return ""
        except RecursionError:
            return ""
    queue = [(payload, 0)]
    for _ in range(128):
        if not queue:
            break
        node, depth = queue.pop()
        if depth > 8:
            continue
        if isinstance(node, dict):
            for key, value in list(node.items())[:32]:
                if str(key).lower() in URL_KEYS:
                    found = video_url(value)
                    if found:
                        return found
                if isinstance(value, (dict, list)):
                    queue.append((value, depth+1))
        elif isinstance(node, list):
            queue.extend((value, depth+1) for value in node[:32])
    return ""


def xml_url(text):
    if not isinstance(text, str) or len(text) > MAX_CARD:
        return ""
    text = text.strip()
    if not text.startswith("<"):
        text = html.unescape(text)
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        return ""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return ""
    for node in list(root.iter())[:64]:
        for key, value in node.attrib.items():
            if key.lower() in URL_KEYS:
                found = video_url(value)
                if found:
                    return found
    return ""


def raw_url(text):
    if not isinstance(text, str) or len(text) > MAX_CARD:
        return ""
    matches = re.findall(r"\[CQ:(json|xml),data=(.*?)\]", text, re.S)[:4]
    for kind, payload in matches:
        found = json_url(payload) if kind == "json" else xml_url(payload)
        if found:
            return found
    if matches:
        return ""
    if html.unescape(text).lstrip().startswith("<"):
        return xml_url(text)
    return text_url(text)


def share_url(event):
    """Top-level shares only: do not read URLs hidden in quoted/forwarded messages."""
    try:
        components = event.get_messages()
    except (AttributeError, TypeError):
        components = []
    for component in list(components or [])[:20]:
        kind = component.get("type", "") if isinstance(component, dict) else getattr(component, "type", "")
        kind = str(getattr(kind, "value", kind)).lower()
        data = component.get("data", {}) if isinstance(component, dict) else None
        if kind == "json":
            payload = data.get("data", data) if isinstance(data, dict) else getattr(component, "data", data)
            found = json_url(payload)
        elif kind == "share":
            found = video_url(data.get("url")) if isinstance(data, dict) else video_url(getattr(component, "url", ""))
        elif kind in ("plain", "text", "unknown", "xml"):
            text = data.get("text", data.get("data", "")) if isinstance(data, dict) else getattr(component, "text", getattr(component, "data", ""))
            found = raw_url(text)
        else:
            continue
        if found:
            return found
    # OneBot can retain an unsupported XML/JSON segment only in raw_message.
    # Restrict this fallback to top-level share segments, not reply/forward content.
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw, dict):
        parts = raw.get("message", raw.get("raw_message", []))
    else:
        parts = raw
    if isinstance(parts, list):
        for segment in parts[:20]:
            if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                continue
            data, kind = segment["data"], segment.get("type")
            found = (json_url(data.get("data")) if kind == "json" else
                     xml_url(data.get("data")) if kind == "xml" else
                     video_url(data.get("url")) if kind == "share" else "")
            if found:
                return found
    elif isinstance(parts, str) and ("[CQ:json," in parts or "[CQ:xml," in parts):
        return raw_url(parts)
    # Some QQ adapters expose CQ shares as raw text rather than components.
    return raw_url(getattr(event, "message_str", "")) if not components else ""


class QQBiliCardFilter(filter.CustomFilter):
    def filter(self, event, cfg):
        if event.get_platform_name() not in ("aiocqhttp", "qq_official", "qqofficial", "qq"):
            return False
        own_id = getattr(event, "get_self_id", lambda: None)()
        if own_id is not None and str(event.get_sender_id()) == str(own_id):
            return False
        if not event.is_private_chat() and not getattr(event, "is_at_or_wake_command", False):
            return False
        url = share_url(event)
        if url:
            event.set_extra("bili_share_url", url)
        return bool(url)


async def resolve_share(url):
    current = video_url(url)
    if not current:
        raise CardReadError("这张卡片没有可读取的 B站视频链接。")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        for _ in range(3):
            match = BVID.search(urlsplit(current).path)
            if match:
                return match[0]
            async with session.get(current, allow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status in (301, 302, 303, 307, 308):
                    target = video_url(urljoin(current, response.headers.get("Location", "")))
                    if not target:
                        raise CardReadError("B站短链跳转地址无法识别，请发送完整视频链接。")
                    current = target
                    continue
                raise CardReadError("B站短链暂时无法展开，请发送完整视频链接。")
    match = BVID.search(urlsplit(current).path)
    if match:
        return match[0]
    raise CardReadError("B站短链跳转次数过多，请发送完整视频链接。")


def read_object(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and len(raw) <= 200000:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
    return {}


class CardReader:
    def __init__(self):
        self._messages, self._last, self._cache = {}, {}, {}

    def claim(self, event):
        now = time.monotonic()
        self._messages = {key: expiry for key, expiry in self._messages.items() if expiry > now}
        self._last = {key: expiry for key, expiry in self._last.items() if expiry > now}
        scope = str(event.unified_msg_origin) + ":" + str(event.get_sender_id())
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        key = scope + ":" + message_id if message_id else None
        if (key and key in self._messages) or scope in self._last:
            return False
        if len(self._messages) >= 128:
            del self._messages[next(iter(self._messages))]
        if len(self._last) >= 64:
            del self._last[next(iter(self._last))]
        if key:
            self._messages[key] = now + 120
        self._last[scope] = now + 10
        return True

    async def read(self, url, info, subtitle):
        async with asyncio.timeout(25):
            bvid = await resolve_share(url)
            cached = self._cache.get(bvid)
            if cached and cached[0] > time.monotonic():
                return dict(cached[1])
            details = read_object(await asyncio.wait_for(info(bvid), timeout=8))
            if details.get("bvid") != bvid or not isinstance(details.get("title"), str) or details.get("error"):
                raise CardReadError("暂时读不到这条视频，可能已删除、受限或需要重新登录 B站。")
            try:
                raw_subtitle = read_object(await asyncio.wait_for(subtitle(bvid), timeout=8))
                text = str(raw_subtitle.get("text") or "").strip() if not raw_subtitle.get("error") else ""
                if len(text) < 40 and re.fullmatch(r"字幕(?:由.+(?:提供|制作)|(?:制作|组)[:：].+)[。.]?", text):
                    text = ""
                status = "可用字幕" if text else "无可用字幕，仅依据简介"
            except Exception:
                text, status = "", "字幕读取失败，仅依据简介"
            material = {"bvid": bvid, "url": "https://www.bilibili.com/video/" + bvid,
                        "title": details["title"][:160], "author": str(details.get("author") or "")[:60],
                        "description": str(details.get("description") or "")[:600],
                        "subtitle_status": status, "subtitle_excerpt": text[:5000], "subtitle_truncated": len(text) > 5000,
                        "visual_analysis": False}
            if len(self._cache) >= 32:
                del self._cache[next(iter(self._cache))]
            self._cache[bvid] = (time.monotonic()+300, material)
            return dict(material)
