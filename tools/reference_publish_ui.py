"""Render a chapter card with a separate illustration, text, and subtitle band."""

import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

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


def background(width, height, px):
    img = Image.new("RGB", (width, height))
    d = ImageDraw.Draw(img)
    for y in range(height):
        t = math.sin(math.pi * y / height) ** 2
        d.line((0, y, width, y), fill=(round(7 + 4 * t), round(13 + 8 * t), round(25 + 14 * t)))
    glow = Image.new("RGBA", img.size)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((width - px(935), px(25), width - px(40), px(906)), fill=(30, 79, 135, 67))
    gd.ellipse(tuple(map(px, (218, 180, 430, 392))), fill=(97, 160, 211, 20))
    gd.ellipse(tuple(map(px, (-20, 589, 210, 820))), fill=(69, 104, 175, 23))
    return Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(px(87)))).convert("RGB")


def place_art(canvas, path, px, *, compact=False):
    with Image.open(path) as source:
        art = ImageOps.contain(source.convert("RGB"), (px(645 if compact else 850), px(846)),
                               Image.Resampling.LANCZOS)
    # Feather the boundary only; retain the illustration's contrast and colour in the centre.
    w, h = art.size
    # A few generations copy the reference sheet's nearly white background.
    # Only key out highlights connected to the outer edge of the illustration.
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    white = [(x, y) for x, y in corners if min(art.getpixel((x, y))) >= 238]
    subject = None
    if len(white) >= 3:
        red, green, blue = art.split()
        matte = ImageChops.darker(ImageChops.darker(red, green), blue).point(lambda p: 255 if p >= 237 else 0)
        for corner in white:
            ImageDraw.floodfill(matte, corner, 128)
        subject = matte.point(lambda p: 0 if p == 128 else 255).filter(ImageFilter.GaussianBlur(0.6))

    mask = Image.new("L", (w, h))
    d = ImageDraw.Draw(mask)
    for y in range(h):
        vertical = min(1, y / px(32), (h - 1 - y) / px(82))
        d.line((0, y, w, y), fill=round(255 * vertical))
    feather = px(55)
    for x in range(min(feather, w // 2)):
        factor = x / feather
        left = mask.crop((x, 0, x + 1, h)).point(lambda p: round(p * factor))
        mask.paste(left, (x, 0))
        mask.paste(left, (w - x - 1, 0))
    if subject is not None:
        mask = ImageChops.multiply(mask, subject)
    canvas.paste(art, (px(1080 if compact else 1445) - w // 2, px(71) + (px(846) - h) // 2), mask)


def make_card(fonts, figure, kicker, segment, index, total, *, cover_title=None, upload_cover=False, height=H):
    def px(length):
        return round(length * height / H)

    def point(*coordinates):
        return tuple(map(px, coordinates))

    canvas = background(px(1440 if upload_cover else W), height, px)
    place_art(canvas, figure, px, compact=upload_cover)
    d = ImageDraw.Draw(canvas)
    small = ImageFont.truetype(fonts, px(26))
    d.ellipse(point(108, 77, 117, 86), fill=ACCENT)
    d.text(point(136, 65), kicker[:32], font=small, fill=SECONDARY)
    d.text((canvas.width - px(112), px(65)), "鲸汐制作笔记", font=small, fill=SECONDARY, anchor="ra")

    if cover_title:
        d.text(point(106, 258), "鲸汐 · 本期主题", font=ImageFont.truetype(fonts, px(32)), fill=ACCENT)
        width, maximum = (630, 88) if upload_cover else (875, 106)
        family, lines = fit_text(cover_title, fonts, px(maximum), px(42), px(width), 2)
        y = px(325)
    else:
        numerals = ImageFont.truetype(fonts, px(128))
        text_gradient(canvas, point(98, 162), f"{index:02d}", numerals, ACCENT, (100, 118, 204))
        family, lines = fit_text(segment["title"], fonts, px(81), px(46), px(875), 2)
        y = px(315)
    for i, line in enumerate(lines):
        text_gradient(canvas, (px(101), y), line, family, INK, (157, 208, 245) if i else INK)
        y += round(family.size * 1.21)

    if not cover_title:
        d.line((px(104), y + px(10), px(184), y + px(10)), fill=(118, 179, 217), width=px(3))
        for j, bullet in enumerate(segment["bullets"][:4]):
            yy = y + px(40 + j * 48)
            dpi, rows = fit_text([bullet], fonts, px(33), px(25), px(818), 1)
            d.ellipse((px(108), yy + px(16), px(115), yy + px(23)), fill=ACCENT)
            d.text((px(133), yy), rows[0], font=dpi, fill=SECONDARY)

    if not cover_title:
        # The narrator's subtitles have their own band below both art and chapter text.
        d.line(point(108, 1005, 1808, 1005), fill=(47, 69, 94), width=px(2))
        for j in range(total):
            start = round(108 + j * (1700 / total))
            end = round(108 + (j + 1) * (1700 / total)) - 7
            d.line(point(start, 1014, end, 1014), fill=ACCENT if j < index else (43, 63, 89), width=px(4))
    return canvas
