"""Normalize administrator-configured endpoints without embedding credentials."""
from urllib.parse import urlsplit, urlunsplit


def api_base(value, *, websocket=False) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ValueError("API 地址格式不正确。")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        schemes = ("https", "http", "wss", "ws") if websocket else ("https", "http")
        if (parts.scheme not in schemes or not parts.hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.port == 0 or "\\" in value):
            raise ValueError()
        scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme) if websocket else parts.scheme
        return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    except (ValueError, TypeError) as exc:
        raise ValueError("请填写完整的 API 地址，不要在地址中附带 Key、查询参数或片段。") from exc


def api_endpoint(base: str, route: str) -> str:
    return base if base.endswith(route) else base + route
