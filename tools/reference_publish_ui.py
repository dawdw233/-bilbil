"""Render a chapter card with a separate illustration, text, and subtitle band."""

import math
from statistics import median

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


def illustration_matte(art):
    """Remove the flat or smoothly graded backdrop typical of these illustrations."""
    w, h = art.size
    sample = art.load()
    inset = max(1, w // 192)
    backdrop = Image.new("RGB", (2, h))
    rows = []
    for y in range(h):
        left = tuple(int(median(sample[x, y][c] for x in range(inset))) for c in range(3))
        right = tuple(int(median(sample[w - x - 1, y][c] for x in range(inset))) for c in range(3))
        rows.append((left, right))
    def similarity(a, b):
        return max(abs(x - y) for x, y in zip(a, b))

    # The upper border verifies that the image has a single backdrop. Keep full scenes intact.
    left, right = rows[0]
    if similarity(left, right) > 24 or any(similarity(left, sample[x, 0]) > 28 for x in range(0, w, max(1, w // 32))):
        return None
    neutral = min(left + right) >= 228

    def valid(color):
        return min(color) >= 222 if neutral else color[2] >= color[0] + 20

    if not valid(left) or not valid(right):
        return None

    last = rows[0]
    backdrop_end = h
    for y, (left, right) in enumerate(rows):
        a = valid(left) and similarity(left, last[0]) <= 22
        b = valid(right) and similarity(right, last[1]) <= 22
        if a and b and similarity(left, right) > 30:
            a = similarity(left, last[0]) <= similarity(right, last[1])
            b = not a
        if not a and not b and y > h * 0.7:
            backdrop_end = min(backdrop_end, y)
        if a or b:
            last = (left if a else right, right if b else left)
        backdrop.putpixel((0, y), last[0])
        backdrop.putpixel((1, y), last[1])

    if neutral and similarity(*rows[0]) <= 6:
        backdrop = Image.new("RGB", art.size, rows[0][0])
    else:
        backdrop = backdrop.filter(ImageFilter.MedianFilter(5)).resize(art.size, Image.Resampling.BILINEAR)
    red, green, blue = ImageChops.difference(art, backdrop).split()
    distance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    # Keep the foreground line art even if it borders the same coloured backdrop.
    # The wide, flat backdrop and enclosed pockets both fall inside the colour range.
    tolerance = 8 if neutral else 24
    flat = distance.point(lambda p: 255 if p <= tolerance else 0)
    border = ((x, y) for x in (0, w - 1) for y in range(h))
    for x, y in border:
        if flat.getpixel((x, y)) == 255:
            ImageDraw.floodfill(flat, (x, y), 128)

    # A loose strand can close off a small patch of background. Test only the
    # outside of the silhouette and never use white face or clothing as seeds.
    # Require a nearly exact match so shadows and dark lines stay opaque.
    if neutral:
        edge = round(w * 0.15)
        for x in (0, w - edge):
            box = (x, round(h * 0.56), x + edge, round(h * 0.90))
            edge_distance = distance.crop(box)
            pockets = edge_distance.point(lambda p: 255 if p <= 15 else 0)
            for y in range(0, pockets.height, 4):
                for col in range(0, pockets.width, 4):
                    if edge_distance.getpixel((col, y)) <= 8 and pockets.getpixel((col, y)) == 255:
                        ImageDraw.floodfill(pockets, (col, y), 128)
            fill = pockets.point(lambda p: 255 if p == 128 else 0)
            flat.paste(128, box, fill)

    matte = flat.point(lambda p: 0 if p == 128 else 255)
    # Drop the colour fringe just outside the outline, then smooth at pixel scale.
    matte = matte.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.75))

    img = art.convert("RGBA")
    if backdrop_end < h:
        # Leave furniture below the subject intact. The compositing fade handles this base.
        matte.paste(255, (0, backdrop_end, w, h))
    img.putalpha(matte)
    return img


def place_art(canvas, path, px, *, compact=False):
    with Image.open(path) as source:
        has_alpha = "A" in source.getbands() and source.getchannel("A").getextrema()[0] < 255
        art = ImageOps.contain(source.convert("RGBA" if has_alpha else "RGB"),
                               (px(645 if compact else 850), px(846)), Image.Resampling.LANCZOS)
    if not has_alpha:
        cutout = illustration_matte(art)
        if cutout is not None:
            art = cutout
    w, h = art.size

    mask = Image.new("L", (w, h))
    d = ImageDraw.Draw(mask)
    cutout = "A" in art.getbands()
    for y in range(h):
        vertical = min(1, (h - 1 - y) / px(82))
        if not cutout:
            vertical = min(vertical, y / px(32))
        d.line((0, y, w, y), fill=round(255 * vertical))
    feather = px(105 if cutout else 55)
    start = int(h * 0.86) if cutout else 0
    for x in range(min(feather, w // 2)):
        factor = x / feather
        column = mask.crop((x, start, x + 1, h)).point(lambda p: round(p * factor))
        mask.paste(column, (x, start))
        mask.paste(column, (w - x - 1, start))
    if cutout:
        mask = ImageChops.multiply(mask, art.getchannel("A"))
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
