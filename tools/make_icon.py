"""Draw the app icon (an original design: gold "HD" on a deep-blue tile).

    uv run python tools/make_icon.py

Writes src/eq_hd/gui/icon.png (256 px) and icon.ico (16-256 px). Uses DejaVu Serif
Bold (free licence); pass another .ttf as the first argument to try a different face.
"""

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

S = 1024  # drawn large, then scaled down
FONT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
OUT = Path(__file__).resolve().parents[1] / "src" / "eq_hd" / "gui"


def vertical_gradient(size, top, bottom):
    w, h = size
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        g.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((w, h))


def rounded_mask(size, inset, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((inset, inset, size - 1 - inset, size - 1 - inset), radius, fill=255)
    return m


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    outer = rounded_mask(S, 24, 190)
    gold = vertical_gradient((S, S), (246, 214, 128), (176, 120, 40))
    img.paste(gold, (0, 0), outer)                                        # gold rim
    inner = rounded_mask(S, 70, 150)
    img.paste(vertical_gradient((S, S), (22, 44, 96), (8, 16, 40)), (0, 0), inner)  # blue tile
    glow = Image.new("L", (S, S), 0)                                      # soft light behind the letters
    ImageDraw.Draw(glow).ellipse((170, 220, 854, 800), fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img.paste((70, 120, 210), (0, 0), ImageChops.multiply(glow, inner))

    font = ImageFont.truetype(FONT, 470)
    text = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(text)
    box = d.textbbox((0, 0), "HD", font=font)
    x = (S - (box[2] - box[0])) // 2 - box[0]
    y = (S - (box[3] - box[1])) // 2 - box[1] + 20
    d.text((x, y), "HD", font=font, fill=255)
    shadow = text.filter(ImageFilter.GaussianBlur(14))
    img.paste((0, 0, 0), (14, 22), shadow.point(lambda v: v * 0.8))       # drop shadow
    img.paste(vertical_gradient((S, S), (255, 236, 170), (196, 136, 46)), (0, 0), text)  # gold letters

    spark = Image.new("L", (S, S), 0)                                     # a small four-point sparkle
    cx, cy, r = 806, 214, 92
    ImageDraw.Draw(spark).polygon([(cx, cy - r), (cx + 18, cy - 18), (cx + r, cy), (cx + 18, cy + 18),
                                   (cx, cy + r), (cx - 18, cy + 18), (cx - r, cy), (cx - 18, cy - 18)], fill=255)
    img.paste((255, 246, 214), (0, 0), spark)
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    S2 = Image.new("RGBA", (256 + 128 + 64 + 32 + 16 + 5 * 12, 256), (40, 40, 40, 255))
    xo = 0
    for n in (256, 128, 64, 32, 16):
        S2.paste(big.resize((n, n), Image.LANCZOS), (xo, 0), big.resize((n, n), Image.LANCZOS))
        xo += n + 12
    if len(sys.argv) > 2:  # optional: a strip of all sizes, for checking how it reads small
        S2.save(sys.argv[2])
    print("wrote", OUT / "icon.png", OUT / "icon.ico")
