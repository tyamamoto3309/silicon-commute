"""Generate app icons and podcast cover art (run once; outputs go to site/icons/)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "site" / "icons"
OUT.mkdir(parents=True, exist_ok=True)
NAVY = (11, 36, 71)
NAVY2 = (18, 52, 98)
COPPER = (214, 139, 60)
TEAL = (99, 209, 189)
WHITE = (255, 255, 255)
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def chip(d: ImageDraw.ImageDraw, cx: float, cy: float, size: float, wave=True):
    """A microchip whose die shows a sound wave."""
    s = size
    body = [cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2]
    pin_len, pin_w, pins = s * 0.16, s * 0.055, 5
    for i in range(pins):
        off = -s * 0.32 + i * (s * 0.64 / (pins - 1))
        for x0, y0, x1, y1 in (
            (cx + off - pin_w / 2, cy - s / 2 - pin_len, cx + off + pin_w / 2, cy - s / 2),
            (cx + off - pin_w / 2, cy + s / 2, cx + off + pin_w / 2, cy + s / 2 + pin_len),
            (cx - s / 2 - pin_len, cy + off - pin_w / 2, cx - s / 2, cy + off + pin_w / 2),
            (cx + s / 2, cy + off - pin_w / 2, cx + s / 2 + pin_len, cy + off + pin_w / 2),
        ):
            d.rounded_rectangle([x0, y0, x1, y1], radius=pin_w / 2, fill=COPPER)
    d.rounded_rectangle(body, radius=s * 0.14, fill=NAVY2, outline=COPPER, width=max(2, int(s * 0.045)))
    if wave:
        heights = [0.18, 0.36, 0.58, 0.36, 0.72, 0.44, 0.24]
        bw = s * 0.06
        gap = s * 0.035
        total = len(heights) * bw + (len(heights) - 1) * gap
        x = cx - total / 2
        for i, h in enumerate(heights):
            hh = s * 0.62 * h
            d.rounded_rectangle([x, cy - hh / 2, x + bw, cy + hh / 2], radius=bw / 2, fill=TEAL if i == 4 else WHITE)
            x += bw + gap


def icon(size: int, pad: float, name: str, radius=True):
    img = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(img)
    chip(d, size / 2, size / 2, size * (0.56 - pad))
    img.save(OUT / name, optimize=True)


def cover(size=1600):
    img = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(img)
    # subtle circuit traces
    for i in range(9):
        y = 180 + i * 150
        d.line([(0, y), (size * 0.18, y), (size * 0.24, y + 60)], fill=NAVY2, width=10)
        d.line([(size, y + 40), (size * 0.82, y + 40), (size * 0.76, y + 100)], fill=NAVY2, width=10)
    chip(d, size / 2, size * 0.36, size * 0.34)
    f1 = ImageFont.truetype(SANS_B, int(size * 0.052))
    f2 = ImageFont.truetype(SANS_B, int(size * 0.105))
    f3 = ImageFont.truetype(CJK, int(size * 0.043))
    f4 = ImageFont.truetype(SANS_B, int(size * 0.034))

    def center(text, y, font, fill):
        w = d.textlength(text, font=font)
        d.text(((size - w) / 2, y), text, font=font, fill=fill)

    center("THE", size * 0.60, f1, COPPER)
    center("SILICON", size * 0.655, f2, WHITE)
    center("COMMUTE", size * 0.765, f2, WHITE)
    center("GAFAM × 半導体 ｜ 英日対訳", size * 0.885, f3, TEAL)
    d.rectangle([size * 0.42, size * 0.955, size * 0.58, size * 0.962], fill=COPPER)
    img.save(OUT / "cover.png", optimize=True)


if __name__ == "__main__":
    icon(192, 0.0, "icon-192.png")
    icon(512, 0.0, "icon-512.png")
    icon(512, 0.12, "icon-maskable-512.png")
    icon(180, 0.0, "apple-touch-icon.png")
    cover()
    print("icons written to", OUT)
