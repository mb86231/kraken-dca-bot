"""Generate PWA/home-screen icons for the DCA-Bot (design-matched).

Palette from web/static/css/app.css (dark theme): bg #0c0f18, accent #f7a21a,
green #3ed68e. Output goes to web/static/icons/.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent.parent / "web" / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

BG = (12, 15, 24, 255)          # #0c0f18
ACCENT = (247, 162, 26, 255)    # #f7a21a
GREEN = (62, 214, 142, 255)     # #3ed68e


def draw_icon(size: int, maskable: bool) -> Image.Image:
    """Render the icon at ``size`` px. Maskable keeps content in the safe zone."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if maskable:
        # Full-bleed background, OS applies the mask shape itself.
        d.rectangle([0, 0, size - 1, size - 1], fill=BG)
        scale = 0.5  # content within the 80% safe zone
    else:
        # Rounded-square background (radius ~22%) as the canonical shape.
        radius = int(size * 0.22)
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)
        scale = 0.62

    cx, cy = size / 2, size / 2

    # Diamond ring (brand mark ◈): outer and inner diamond outlines.
    outer_r = size * scale * 0.52
    inner_r = outer_r * 0.62
    stroke = max(2, int(size * 0.035))

    def diamond(r: float) -> list[tuple[float, float]]:
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]

    # Lift the diamond slightly so the bars fit below it.
    dy = -size * scale * 0.08
    outer = [(x, y + dy) for x, y in diamond(outer_r)]
    inner = [(x, y + dy) for x, y in diamond(inner_r)]
    d.polygon(outer, outline=ACCENT, width=stroke)
    d.polygon(inner, outline=ACCENT, width=stroke)

    # Three ascending DCA bars below the diamond.
    bar_w = size * scale * 0.16
    gap = bar_w * 0.45
    heights = [size * scale * 0.14, size * scale * 0.22, size * scale * 0.32]
    base_y = cy + size * scale * 0.75
    total_w = 3 * bar_w + 2 * gap
    x = cx - total_w / 2
    for h in heights:
        top = base_y - h
        d.rounded_rectangle(
            [x, top, x + bar_w, base_y],
            radius=bar_w * 0.25,
            fill=GREEN,
        )
        x += bar_w + gap

    return img


# Standard icons: rounded-square shape with transparent corners.
draw_icon(512, maskable=False).save(OUT / "icon-512.png")
draw_icon(192, maskable=False).save(OUT / "icon-192.png")
draw_icon(32, maskable=False).save(OUT / "icon-32.png")

# Maskable: full-bleed, content inside the safe zone.
draw_icon(512, maskable=True).save(OUT / "icon-512-maskable.png")

# Apple touch icon: full-bleed square, iOS rounds the corners itself.
draw_icon(180, maskable=True).convert("RGB").save(OUT / "apple-touch-icon.png")

for p in sorted(OUT.iterdir()):
    print(p.name, p.stat().st_size, "bytes")
