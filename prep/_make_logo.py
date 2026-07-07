"""Generate a simple on-brand placeholder logo for the LinkedIn app (navy/gold)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

NAVY = (11, 31, 58)      # #0B1F3A  (BRAHMASTRA/VISION navy)
GOLD = (201, 162, 75)    # #C9A24B  (gold)
WHITE = (245, 245, 245)

SIZE = 512
img = Image.new("RGB", (SIZE, SIZE), NAVY)
d = ImageDraw.Draw(img)

# Inset gold ring for a finished look.
margin = 28
d.rounded_rectangle(
    [margin, margin, SIZE - margin, SIZE - margin],
    radius=64, outline=GOLD, width=8,
)


def load_font(size, bold=True):
    # Try common Windows fonts, fall back to PIL default.
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "seguisb.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def centered(draw, text, font, cy, fill):
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    w, h = r - l, b - t
    draw.text(((SIZE - w) / 2 - l, cy - h / 2 - t), text, font=font, fill=fill)


# Big gold "V" monogram, then the wordmark under it.
centered(d, "V", load_font(300), 230, GOLD)
centered(d, "VISION", load_font(64), 400, WHITE)
centered(d, "INSIGHT ENGINE", load_font(26), 452, GOLD)

out = Path(r"D:\Projects\ClaudeCode\Vision-LinkedIN\assets")
out.mkdir(exist_ok=True)
p = out / "vision_logo.png"
img.save(p, "PNG")
# Also a 300x300 version (LinkedIn's recommended size).
img.resize((300, 300), Image.LANCZOS).save(out / "vision_logo_300.png", "PNG")
print("saved:", p, "and vision_logo_300.png")
