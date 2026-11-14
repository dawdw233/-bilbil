#!/usr/bin/env python3
"""参考投稿 · 独立视频渲染工具。

人格卡绑定：口播稿、小标题、投稿标题/简介/标签全部由「鲸汐」人格卡驱动。
流程：--plan（人格卡写稿）→ --audio（整篇一次配音）→ ASR 对齐（服务器上单独跑）
      → --video（选插画 + 本地 ComfyUI + 卡片 + 字幕）
      → --upload（仅自己可见，需单独执行）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from reference_publish_art import art_for_episode, bridge_profile, local_art
from reference_publish_ui import make_card, W, H

PLUGIN = "/AstrBot/data/plugins/astrbot_plugin_bili_scroll"
DATA = Path("/AstrBot/data/plugin_data/astrbot_plugin_bili_scroll")
WORK = DATA / "reference_publish" / "attempt-004"
FONTS = DATA / "fonts" / "wqy-microhei.ttc"
REF_IMAGE = DATA / "reference_images" / "大肥鱼三维图-37656c06be80a0ff.jpg"
BREEZE_KEY_FILE = Path("/AstrBot/data/plugin_data/astrbot_plugin_breezeblue_tts/breeze_key.txt")
BREEZE_BASE = "https://api.breeze.blue"
VOICE = "voc_u3nxkd5578wc"          # 大肥鱼（机灵版）
CMD_CONFIG = Path("/AstrBot/data/cmd_config.json")
ASTRBOT_DB = Path("/AstrBot/data/data_v4.db")
PERSONA_ID = "鲸汐"
PROVIDER_ID = "openrouter/deepseek-v4.1-flash"
FPS = 30

# ---------------- 人格卡与模型 ----------------

def load_persona() -> str:
    try:
        db = sqlite3.connect(f"file:{ASTRBOT_DB}?mode=ro", uri=True)
        row = db.execute("select system_prompt from personas where persona_id=?", (PERSONA_ID,)).fetchone()
        db.close()
        if row and row[0] and str(row[0]).strip():
            return str(row[0]).strip()
    except Exception as exc:
        print("读人格卡失败：", type(exc).__name__, exc, flush=True)
    fallback = DATA.parent.parent / "_jingxi_persona.md"
    return fallback.read_text(encoding="utf-8").strip()


def load_provider() -> dict:
    config = json.loads(CMD_CONFIG.read_text(encoding="utf-8-sig"))
    provider = next((row for row in config.get("provider") or [] if row.get("id") == PROVIDER_ID), None)
    if not provider:
        raise RuntimeError(f"找不到模型 {PROVIDER_ID}")
    source_id = provider.get("provider_source_id") or ""
    source = next((row for row in config.get("provider_sources") or [] if row.get("id") == source_id), None)
    if not source or not source.get("key"):
        raise RuntimeError(f"找不到 {PROVIDER_ID} 的接口配置")
    key = source["key"]
    if isinstance(key, list):
        key = next((item for item in key if isinstance(item, str) and item.strip()), "")
    if not key:
        raise RuntimeError("接口 Key 为空")
    headers = dict(source.get("custom_headers") or {}) if isinstance(source.get("custom_headers"), dict) else {}
    return {"api_base": str(source.get("api_base") or "").rstrip("/"),
            "key": str(key), "model": str(provider.get("model") or ""),
            "headers": {str(k): str(v) for k, v in headers.items()}}


def llm_chat(system: str, user: str, provider: dict, *, max_tokens: int = 4096) -> str:
    payload = {
        "model": provider["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.85,
        "max_tokens": max_tokens, "stream": False,
    }
    request = urllib.request.Request(
        provider["api_base"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + provider["key"],
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                 "HTTP-Referer": "https://github.com/AstrBotDevs/AstrBot",
                 "X-OpenRouter-Title": "AstrBot",
                 "X-OpenRouter-Categories": "general-chat,personal-agent",
                 **(provider.get("headers") or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"模型接口 HTTP {exc.code}") from None
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if not content:
        print("空内容；字段：", sorted(message), "finish:", choice.get("finish_reason"),
              "usage:", body.get("usage"), flush=True)
    return content


PLAN_RULES = """
你现在要为一期 B 站视频写口播稿和投稿文案。这期视频的画面是「文字卡 + 你的形象」，声音是你的配音，所以稿子必须是能直接念出来的口语。

硬性要求：
1. 6 到 8 段，每段一个屏幕卡片；每段口播 45 到 110 字，全篇 480 到 650 字。
2. 每段给 title（1～2 行小标题，每行不超过 14 字）和 bullets（1～4 条要点，每条不超过 18 字）。
3. 口播按人格卡的说话方式写：自称、语气、对观众的称呼都照人格卡来；可以傲娇、可以嘴硬，但内容要真诚、信息量要够，别演过头。
4. 系列名用「鲸汐 · 制作笔记」，口播按人格卡自称。
5. 围绕当期主题组织成清晰的步骤或章节。只用提供的事实；不确定时不要声称做过实测。
6. 每段口播结尾不要用「好的」「那么」「接下来我们」这类播报腔；不要写画面提示、BGM、字幕说明。
7. 口播里不要念英文品牌：说「刷视频的那套插件」而不是 AstrBot，说「微风语音」而不是 Breeze；技术名词用中文（「投稿参数」而不是「字段」）；画面卡片不受此限。
8. 投稿文案：title 不超过 80 字、口语但不标题党；desc 150～400 字，写清这期讲什么 + 本片由 AI 生成（画面、配音、字幕）；tags 用中文逗号分隔、6～9 个、不要重复「AI生成」。
9. tts_instructions 写一句给语音合成的中文语气指令，2～3 个分句，体现人格卡的语气（例如傲娇但认真），不要出现角色扮演以外的要求。

只返回一个 JSON 对象，不要 Markdown 代码块，结构：
{"kicker": "系列名 · 期数", "tts_instructions": "…",
 "segments": [{"title": ["…"], "bullets": ["…"], "text": "…"}],
 "upload": {"title": "…", "desc": "…", "tags": "…"}}
"""


def plan_script(topic: str) -> dict:
    persona = load_persona()
    provider = load_provider()
    user = f"本期主题、范围与已知事实：\n{topic.strip()}"
    print(f"人格卡 {len(persona)} 字，模型 {provider['model']}，开始写稿…", flush=True)
    text = llm_chat(persona + "\n\n---\n" + PLAN_RULES, user, provider)
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean
        clean = clean.rsplit("```", 1)[0]
    plan = json.loads(clean)
    return validate_plan(plan)


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise RuntimeError("模型返回的不是 JSON 对象")
    segments = plan.get("segments")
    if not isinstance(segments, list) or not 5 <= len(segments) <= 9:
        raise RuntimeError(f"段数不对：{len(segments) if isinstance(segments, list) else '非列表'}")
    total_chars = 0
    clean_segments = []
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise RuntimeError("分段格式不对")
        title = [str(line).strip() for line in segment.get("title") or [] if str(line).strip()]
        bullets = [str(line).strip() for line in segment.get("bullets") or [] if str(line).strip()]
        text = str(segment.get("text") or "").strip()
        if not title or len(title) > 2 or any(len(line) > 18 for line in title):
            raise RuntimeError(f"第 {index} 段小标题不合规：{title}")
        if not 1 <= len(bullets) <= 5 or any(len(line) > 22 for line in bullets):
            raise RuntimeError(f"第 {index} 段要点不合规：{bullets}")
        if not 20 <= len(text) <= 140:
            raise RuntimeError(f"第 {index} 段口播长度不合规：{len(text)}")
        total_chars += len(text)
        clean_segments.append({"title": title[:2], "bullets": bullets[:5], "text": text})
    if not 420 <= total_chars <= 700:
        raise RuntimeError(f"全篇口播 {total_chars} 字，超出 420～700 范围")
    upload = plan.get("upload") or {}
    title = str(upload.get("title") or "").strip()
    desc = str(upload.get("desc") or "").strip()
    tags = str(upload.get("tags") or "").strip()
    if not 1 <= len(title) <= 80:
        raise RuntimeError(f"投稿标题不合规：{title!r}")
    if not 120 <= len(desc) <= 600:
        raise RuntimeError(f"投稿简介长度不合规：{len(desc)}")
    if not tags:
        raise RuntimeError("标签为空")
    return {"kicker": str(plan.get("kicker") or "鲸汐 · 制作笔记").strip(),
            "tts_instructions": str(plan.get("tts_instructions") or "").strip()
            or "用自然、温和、连贯的中文口语朗读；语速平稳，句间留自然停顿。",
            "segments": clean_segments,
            "upload": {"title": title, "desc": desc, "tags": tags},
            "persona": PERSONA_ID, "total_chars": total_chars}


# ---------------- 基础工具 ----------------

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(f"命令失败 {cmd[0]}: {(proc.stderr or proc.stdout)[-400:]}")
    return proc


def duration_of(path: Path) -> float:
    proc = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)])
    return float(proc.stdout.strip())


def split_sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？!?\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    result = []
    for sentence in out:
        while len(sentence) > 48:
            cut = max(sentence.rfind("，", 0, 48), sentence.rfind("、", 0, 48))
            cut = cut + 1 if cut > 10 else 48
            result.append(sentence[:cut])
            sentence = sentence[cut:]
        if sentence:
            result.append(sentence)
    return result


def load_script() -> dict:
    path = WORK / "script.json"
    if not path.exists():
        raise RuntimeError("还没有 script.json，先跑 --plan")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------- 配音 ----------------

def breeze_key() -> str:
    key = BREEZE_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("breeze_key.txt 是空的")
    return key


async def tts_full(text: str, instructions: str, folder: Path, session) -> Path:
    digest = hashlib.md5((text + instructions).encode("utf-8")).hexdigest()[:12]
    target = folder / f"full-{digest}.mp3"
    if target.exists() and target.stat().st_size > 1024:
        print(f"复用配音 {target.name}", flush=True)
        return target
    async with session.post(f"{BREEZE_BASE}/v1/text-to-speech/{VOICE}",
                            params={"output_format": "mp3"},
                            json={"text": text, "instructions": instructions},
                            headers={"xi-api-key": breeze_key()}) as resp:
        if resp.status != 200:
            body = (await resp.read())[:200].decode("utf-8", "replace")
            raise RuntimeError(f"TTS 被拒 HTTP {resp.status}: {body}")
        audio = await resp.read()
    if len(audio) < 1024:
        raise RuntimeError("TTS 返回音频过小")
    target.write_bytes(audio)
    return target


async def build_audio() -> None:
    import aiohttp

    script = load_script()
    (WORK / "audio").mkdir(parents=True, exist_ok=True)
    speech = "".join(segment["text"] for segment in script["segments"])
    sentences = []
    for segment in script["segments"]:
        sentences.extend(split_sentences(segment["text"]))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        full_audio = await tts_full(speech, script["tts_instructions"], WORK / "audio", session)
    wav = WORK / "audio" / "full.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(full_audio),
         "-ar", "48000", "-ac", "1", str(wav)])
    total = duration_of(wav)
    (WORK / "sentences.json").write_text(json.dumps(sentences, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"配音 {total:.1f}s，{len(speech)} 字（{len(speech) / total:.2f} 字/秒），{len(sentences)} 句", flush=True)


# ---------------- 渲染 ----------------

def detect_silences(path: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(["ffmpeg", "-i", str(path), "-af", "silencedetect=noise=-40dB:d=0.15",
                           "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for line in (proc.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].strip()))
            except ValueError:
                pass
        elif "silence_end:" in line:
            try:
                ends.append(float(line.split("silence_end:")[1].split("|")[0].strip()))
            except ValueError:
                pass
    return [(a, b) for a, b in zip(starts, ends) if b > a]


def sentence_starts(total: float, silences: list[tuple[float, float]], rows: list[dict],
                    count: int) -> list[float]:
    pause_ends = [b for _a, b in silences]
    starts = {}
    for row in rows:
        index = int(row["index"]) - 1
        if not 0 <= index < count:
            continue
        moment = float(row["start"])
        if pause_ends:
            nearest = min(pause_ends, key=lambda value: abs(value - moment))
            if abs(nearest - moment) <= 0.6:
                moment = nearest
        starts[index] = moment
    if not count or len(starts) != count:
        raise RuntimeError("字幕对齐结果与口播句数不同，请重新对齐。")
    stamps = [0.0] + [starts[i] for i in range(1, count)]
    if any(not a < b for a, b in zip(stamps, stamps[1:] + [total])):
        raise RuntimeError("字幕时间顺序或音轨长度不匹配，请重新对齐。")
    return stamps


def wrap_subtitle(text: str, limit: int = 34) -> str:
    """按字数和标点折行，保证字幕处于底部独立区域。"""
    if len(text) <= limit:
        return text
    out, rest = [], text
    while len(rest) > limit and len(out) < 2:
        window = rest[:limit + 8]
        cut = max(window.rfind(ch) for ch in "，。！？、；：")
        cut = cut + 1 if cut >= int(limit * 0.55) else limit
        out.append(rest[:cut])
        rest = rest[cut:]
    out.append(rest)
    return "\\N".join(out)


def write_ass(path: Path, timeline: list[tuple[float, float, str]]) -> None:
    def stamp(seconds: float) -> str:
        cs = int(round(seconds * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,WenQuanYi Micro Hei,46,&H00FFFFFF,&H00FFFFFF,&H9A000000,&H9A000000,0,0,0,0,100,100,0,0,1,2.6,0,2,126,126,96,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    for start, end, text in timeline:
        text = text.replace("{", "｛").replace("}", "｝").replace(chr(92), "＼")
        if text:
            lines.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},Default,,0,0,0,,{wrap_subtitle(text)}")
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def build_video(art_count=4, *, cards_only=False) -> dict:
    script = load_script()
    segments = script["segments"]
    kicker = (script.get("kicker") or "鲸汐 · 制作笔记").replace("大肥鱼的工作台", "鲸汐的工作台")
    wav = WORK / "audio" / "full.wav"
    sentences = []
    owners = []
    for index, segment in enumerate(segments):
        for sentence in split_sentences(segment["text"]):
            sentences.append(sentence)
            owners.append(index)
    if not cards_only:
        if not wav.exists():
            raise RuntimeError("还没有配音，先跑 --audio")
        total = duration_of(wav)
        alignment_path = WORK / "asr_alignment.json"
        if not alignment_path.exists():
            raise RuntimeError("还没有 asr_alignment.json，先跑 ASR 对齐")
        stored = json.loads((WORK / "sentences.json").read_text(encoding="utf-8"))
        if stored != sentences:
            raise RuntimeError("稿件已改动，请重新配音并对齐。")
        rows = json.loads(alignment_path.read_text(encoding="utf-8"))
        starts = sentence_starts(total, detect_silences(wav), rows, len(sentences))
        ends = starts[1:] + [total]
        timeline = list(zip(starts, ends, sentences))
        boundaries = [round(starts[i] * FPS) for i, owner in enumerate(owners)
                      if i == 0 or owners[i - 1] != owner] + [round(total * FPS)]
        if any(a >= b for a, b in zip(boundaries, boundaries[1:])):
            raise RuntimeError("有章节短于一帧，请检查配音对齐。")

    provider = None
    persona = None
    transport = None

    def choose(system, user):
        nonlocal provider, persona
        if provider is None:
            provider, persona = load_provider(), load_persona()
        return llm_chat(persona + "\n\n" + system, user, provider, max_tokens=5000)

    def generate(prompt, picture):
        nonlocal transport
        if transport is None:
            transport = bridge_profile(DATA)
        return local_art(*transport, prompt, picture)

    board, art = art_for_episode(WORK, script, REF_IMAGE, art_count, choose, generate)
    (WORK / "cards").mkdir(parents=True, exist_ok=True)
    cover = make_card(FONTS, art[0], kicker, None, 0, len(segments), cover_title=board["cover_title"])
    cover.save(WORK / "cover.png")
    upload_cover = make_card(FONTS, art[0], kicker, None, 0, len(segments),
                             cover_title=board["cover_title"], upload_cover=True)
    upload_cover.save(WORK / "cover-4x3.png")
    clips = []

    def encode(card_path, name, frames):
        target = WORK / name
        run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS),
             "-i", str(card_path), "-frames:v", str(frames), "-vf", "format=yuv420p",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(target)])
        clips.append(target.name)

    for index, segment in enumerate(segments, 1):
        card = make_card(FONTS, art[board["chapters"][index - 1]], kicker, segment, index, len(segments))
        card_path = WORK / "cards" / f"{index:02d}.png"
        card.save(card_path)
        if cards_only:
            continue
        frames = boundaries[index] - boundaries[index - 1]
        if index == 1 and frames >= FPS:
            cover_frames = min(round(2.4 * FPS), frames // 3)
            encode(WORK / "cover.png", "intro.mp4", cover_frames)
            frames -= cover_frames
        encode(card_path, f"seg-{index:02d}.mp4", frames)
        print(f"  段 {index}：{frames / FPS:.1f}s", flush=True)

    if cards_only:
        return {"cover": str(WORK / "cover.png"), "cards": str(WORK / "cards")}
    concat_lines = "".join(f"file '{clip}'\n" for clip in clips)
    (WORK / "concat.txt").write_text(concat_lines, encoding="utf-8")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(WORK / "concat.txt"), "-c", "copy", str(WORK / "silent.mp4")])

    write_ass(WORK / "subs.ass", timeline)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(WORK / "silent.mp4"), "-i", str(wav),
         "-vf", f"subtitles={WORK / 'subs.ass'}:fontsdir={FONTS.parent}",
         "-af", f"afade=t=out:st={max(0.0, total - 1.0):.2f}:d=1.0",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(WORK / "final.mp4")])

    artifacts = {"video": str(WORK / "final.mp4"), "cover": str(WORK / "cover-4x3.png"),
                 "duration": round(duration_of(WORK / "final.mp4"), 2),
                 "segments": len(segments), "sentences": len(sentences),
                 "persona": script.get("persona"), "illustration_count": len(art), "upload": script["upload"]}
    (WORK / "artifacts.json").write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8")
    print("成片", artifacts["video"], Path(artifacts["video"]).stat().st_size, "字节", flush=True)
    return artifacts


# ---------------- 上传 ----------------

def upload(artifacts: dict) -> dict:
    sys.path.insert(0, PLUGIN)
    import bili_api

    bili_api.CRED_FILE = DATA / "bili_credential.json"
    meta = artifacts.get("upload") or {}

    async def go():
        raw = await bili_api.bili_upload_video(
            video_path=artifacts["video"],
            title=str(meta.get("title") or "")[:80],
            desc=str(meta.get("desc") or ""),
            tid=0,
            tags=str(meta.get("tags") or ""),
            cover_path=artifacts["cover"],
            cover_43=True,
            ai_declare=True,
            private=True,
        )
        return json.loads(raw)

    result = asyncio.run(go())
    record = {**artifacts, "upload_result": result, "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (WORK / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    global WORK, FONTS, REF_IMAGE
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true", help="用鲸汐人格卡写稿")
    parser.add_argument("--topic", help="本期主题和已知事实（与 --plan 一起使用）")
    parser.add_argument("--audio", action="store_true", help="整篇配音")
    parser.add_argument("--video", action="store_true", help="渲染成片")
    parser.add_argument("--upload", action="store_true", help="投稿（仅自己可见）")
    parser.add_argument("--work", type=Path, default=WORK, help="单期工作目录")
    parser.add_argument("--font", type=Path, default=FONTS, help="卡片与字幕字体")
    parser.add_argument("--reference", type=Path, default=REF_IMAGE, help="自己的角色设定图")
    parser.add_argument("--art-count", type=int, choices=range(1, 7), default=4, help="本期插画数量")
    parser.add_argument("--cards", action="store_true", help="只生成卡片供预览")
    args = parser.parse_args()
    if args.plan and not (args.topic and args.topic.strip()):
        parser.error("--plan 需要 --topic")
    if args.plan and (args.work / "script.json").is_file():
        parser.error("该目录已有口播稿，请为新一期选择其他 --work 目录")
    if args.upload and (args.plan or args.audio or args.cards or args.video):
        parser.error("--upload 必须在预览成片后单独运行")
    WORK, FONTS, REF_IMAGE = args.work.resolve(), args.font.resolve(), args.reference.resolve()
    WORK.mkdir(parents=True, exist_ok=True)

    if args.plan:
        plan = plan_script(args.topic)
        (WORK / "script.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"写稿完成：{len(plan['segments'])} 段、{plan['total_chars']} 字；人格卡 {plan['persona']}")
        for index, segment in enumerate(plan["segments"], 1):
            print(f"  {index:02d} {segment['title'][0]}  （{len(segment['text'])}字）")
        print("投稿标题：", plan["upload"]["title"])
    if args.audio:
        asyncio.run(build_audio())
    if args.video or args.cards:
        build_video(args.art_count, cards_only=not args.video)
    if args.upload:
        artifacts = json.loads((WORK / "artifacts.json").read_text(encoding="utf-8"))
        print("开始上传（仅自己可见）…", flush=True)
        print(json.dumps(upload(artifacts), ensure_ascii=False, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
