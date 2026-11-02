"""公开说话的敏感信息闸 —— 评论 / 回复在发出去之前过一遍。

为什么要有它：提示词里写「别透露账号」只是嘱咐，模型偶尔还是会照着素材里的
数字复述。**这是一道硬闸：命中就不发（fail closed）**，日志里写清楚命中什么。

拦这几类：
    · 机器人自己的 QQ（SnowLuma 登录的那个）
    · 使用者的 QQ（config 的 owner_qq，调用方传进来）
    · 服务器公网 IP（内置 + 任何 IPv4 字面量）
    · 长数字串（7 位以上，QQ / UID / 手机号）
    · 敏感词（服务器、管理员、ssh、微信… 见 SENSITIVE_WORDS）

刻意**不拦**：普通小数字（「62 万播放」「2026 年」「3.5G」）、B站 昵称、
视频 BV 号 —— 那些是内容，拦了反而让评论变得像背稿。
"""

from __future__ import annotations

import re

# 机器人自己的 QQ（一号一机，写死比读平台配置可靠：平台配置可能被换掉）
BOT_QQ = "3823343158"
# 服务器公网 IP：泄漏了就等于把门牌号发在评论区
SERVER_IP = "47.111.106.9"

# 7 位以上连续数字：QQ / UID / 手机号都在这个范围；「62 万」这种不会命中
_LONG_DIGITS_RE = re.compile(r"\d{7,}")
_IP_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
# 这些词出现在一条沙雕视频的评论/回复里，基本只可能是它在说自己的事。
#
# ⚠️ 别把「服务器 / 管理员 / 机器人 / 端口 / root / 部署」加回来 —— 10-26 实测：
#    「这服务器也太卡了」「管理员又在乱踢人」「这手速跟机器人一样」这些**正常评论**
#    全被当成泄露拦掉，游戏/科技视频下会大面积误杀。
#    真正要防的「服务器 IP」有 IPv4 正则和 SERVER_IP 兜着，不需要靠这个词。
SENSITIVE_WORDS = (
    "qq",
    "扣扣",
    "企鹅号",
    "微信",
    "v信",
    "阿里云",
    "机房",
    "ssh",
    "公网",
    "ip地址",
    "密码",
    "后台接口",
    "插件数据",
    "本ai",
    "作为ai",
)

_SPACE_RE = re.compile(r"[\s\u3000\u200b]+")


def normalize(text) -> str:
    """抹掉空格 / 全角点，防止「382 334 3158」「47.111 。106.9」绕过。"""
    raw = str(text or "").lower()
    raw = _SPACE_RE.sub("", raw)
    return raw.replace("．", ".").replace("。", ".").replace("·", ".")


def leak_reason(text, secrets=()) -> str:
    """命中就返回原因（中文，能直接写日志）；干净返回空串。

    secrets 里可以放字符串，也可以放 (值, 标签)：
        ["2719734725"] 或 [("2719734725", "管理员 QQ")]
    """
    if not str(text or "").strip():
        return ""
    norm = normalize(text)

    for value, label in ((BOT_QQ, "机器人 QQ"), (SERVER_IP, "服务器 IP")):
        if value and value in norm:
            return f"含{label}"
    for item in secrets or ():
        if isinstance(item, (tuple, list)):
            value, label = (item + ("联系方式",))[:2]
        else:
            value, label = item, "联系方式"
        value = str(value or "").strip()
        if value and value in norm:
            return f"含{label}"

    match = _IP_RE.search(norm)
    if match:
        return f"出现 IP 地址（{match.group(0)}）"
    match = _LONG_DIGITS_RE.search(norm)
    if match:
        return f"出现长数字串（{match.group(0)[:8]}…），像 QQ/UID"
    for word in SENSITIVE_WORDS:
        if word in norm:
            return f"出现敏感词「{word}」"
    return ""


def describe() -> str:
    """给日志/文档用的一句话。"""
    return (
        f"敏感闸：机器人QQ {BOT_QQ} / 服务器IP {SERVER_IP} / 使用者QQ（配置）/"
        f"任何 IPv4 / 7 位以上数字 / {len(SENSITIVE_WORDS)} 个敏感词"
    )
