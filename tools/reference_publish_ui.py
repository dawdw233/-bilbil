"""Render a chapter card with a separate illustration, text, and subtitle band."""

import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1920, 1080
INK = (235, 244, 255)
SECONDARY = (163, 187, 215)
ACCENT = (130, 210, 249)


def split_text(text, font, width):
    rows, row = [], ""
    for char in text:
        if row and font.getlength(row + char) > width:
            rows.append(row)
            row = char
        else:
            row += char
    return rows + ([row] if row else [])


def fit_text(lines, fonts, size, minimum, width, max_rows):
    for px in range(size, minimum - 1, -2):
        font = ImageFont.truetype(fonts, px)
        rows = [row for line in lines for row in split_text(line, font, width)]
        if len(rows) <= max_rows:
            return font, rows
    raise ValueError("卡片文字过长，超出显示区域。")


def text_gradient(canvas, pos, value, font, start, end):
    x, y, x2, y2 = font.getbbox(value)
    mask = Image.new("L", (max(1, x2 - x), max(1, y2 - y)))
    ImageDraw.Draw(mask).text((-x, -y), value, font=font, fill=255)
    grad = Image.new("RGB", (1, mask.height))
    for j in range(mask.height):
        grad.putpixel((0, j), tuple(round(a + (b - a) * j / max(1, mask.height - 1))
                                   for a, b in zip(start, end)))
    canvas.paste(grad.resize(mask.size), pos, mask)


def background(width):
    img = Image.new("RGB", (width, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = math.sin(math.pi * y / H) ** 2
        d.line((0, y, width, y), fill=(round(7 + 4 * t), round(13 + 8 * t), round(25 + 14 * t)))
    glow = Image.new("RGBA", img.size)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((width - 935, 25, width - 40, 906), fill=(30, 79, 135, 67))
    gd.ellipse((218, 180, 430, 392), fill=(97, 160, 211, 20))
    gd.ellipse((-20, 589, 210, 820), fill=(69, 104, 175, 23))
    return Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(87))).convert("RGB")


def place_art(canvas, path, *, compact=False):
    with Image.open(path) as source:
        art = ImageOps.contain(source.convert("RGB"), (645 if compact else 850, 846), Image.Resampling.LANCZOS)
    # Feather the boundary only; retain the illustration's contrast and colour in the centre.
    w, h = art.size
    mask = Image.new("L", (w, h))
    d = ImageDraw.Draw(mask)
    for y in range(h):
        vertical = min(1, y / 48, (h - 1 - y) / 124)
        d.line((0, y, w, y), fill=round(255 * vertical))
    for x in range(min(100, w // 2)):
        factor = x / 100
        left = mask.crop((x, 0, x + 1, h)).point(lambda p: round(p * factor))
        mask.paste(left, (x, 0))
        mask.paste(left, (w - x - 1, 0))
    canvas.paste(art, ((1080 if compact else 1445) - w // 2, 71 + (846 - h) // 2), mask)


def make_card(fonts, figure, kicker, segment, index, total, *, cover_title=None, upload_cover=False):
    canvas = background(1440 if upload_cover else W)
    place_art(canvas, figure, compact=upload_cover)
    d = ImageDraw.Draw(canvas)
    small = ImageFont.truetype(fonts, 26)
    d.ellipse((108, 77, 117, 86), fill=ACCENT)
    d.text((136, 65), kicker[:32], font=small, fill=SECONDARY)
    d.text((canvas.width - 112, 65), "鲸汐制作笔记", font=small, fill=SECONDARY, anchor="ra")

    if cover_title:
        d.text((106, 258), "鲸汐 · 本期主题", font=ImageFont.truetype(fonts, 32), fill=ACCENT)
        width, maximum = (630, 88) if upload_cover else (875, 106)
        family, lines = fit_text(cover_title, fonts, maximum, 42, width, 2)
        y = 325
    else:
        numerals = ImageFont.truetype(fonts, 128)
        text_gradient(canvas, (98, 162), f"{index:02d}", numerals, ACCENT, (100, 118, 204))
        family, lines = fit_text(segment["title"], fonts, 81, 46, 875, 2)
        y = 315
    for i, line in enumerate(lines):
        text_gradient(canvas, (101, y), line, family, INK, (157, 208, 245) if i else INK)
        y += round(family.size * 1.21)

    if not cover_title:
        d.line((104, y + 10, 184, y + 10), fill=(118, 179, 217), width=3)
        bullet_font = ImageFont.truetype(fonts, 33)
        for j, bullet in enumerate(segment["bullets"][:4]):
            yy = y + 40 + j * 48
            dpi, rows = fit_text([bullet], fonts, 33, 25, 818, 1)
            d.ellipse((108, yy + 16, 115, yy + 23), fill=ACCENT)
            d.text((133, yy), rows[0], font=dpi if dpi.size != 33 else bullet_font, fill=SECONDARY)

    if not cover_title:
        # The narrator's subtitles have their own band below both art and chapter text.
        d.line((108, 1005, 1808, 1005), fill=(47, 69, 94), width=2)
        for j in range(total):
            start = round(108 + j * (1700 / total))
            end = round(108 + (j + 1) * (1700 / total)) - 7
            d.line((start, 1014, end, 1014), fill=ACCENT if j < index else (43, 63, 89), width=4)
    return canvas
