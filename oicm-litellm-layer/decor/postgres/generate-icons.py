#!/usr/bin/env python3
"""Generates the ADEO EDGE wordmark used by the Postgres build variant.

Run from this directory: python3 generate-icons.py

The proxy serves these through UI_LOGO_PATH and LITELLM_FAVICON_URL, which
Dockerfile.postgres sets to the paths below. Kept as a script rather than
committed binaries alone so the artwork is reproducible and reviewable.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SLATE = (42, 60, 74)
ACCENT = (86, 129, 173)
WHITE = (255, 255, 255)
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD, size)


def _centered(draw: ImageDraw.ImageDraw, text: str, font, cx: float, cy: float, fill) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right - left) / 2 - left, cy - (bottom - top) / 2 - top), text, font=font, fill=fill)


def wordmark(size: int) -> Image.Image:
    """ADEO over EDGE, stacked, sized to fill a square canvas."""
    image = Image.new("RGB", (size, size), SLATE)
    draw = ImageDraw.Draw(image)

    adeo_font = _font(int(size * 0.30))
    edge_font = _font(int(size * 0.30))
    _centered(draw, "ADEO", adeo_font, size / 2, size * 0.38, WHITE)
    _centered(draw, "EDGE", edge_font, size / 2, size * 0.68, ACCENT)

    bar_h = max(2, size // 40)
    draw.rectangle((size * 0.20, size * 0.535, size * 0.80, size * 0.535 + bar_h), fill=ACCENT)
    return image


def monogram(size: int) -> Image.Image:
    """AE monogram, because the wordmark is unreadable at favicon sizes."""
    image = Image.new("RGB", (size, size), SLATE)
    draw = ImageDraw.Draw(image)
    _centered(draw, "AE", _font(max(9, int(size * 0.46))), size / 2, size / 2, WHITE)
    return image


def main() -> None:
    here = Path(__file__).resolve().parent
    wordmark(256).save(here / "edge-logo.png")
    monogram(32).save(here / "edge-favicon.png")
    for name in ("edge-logo.png", "edge-favicon.png"):
        with Image.open(here / name) as im:
            print(f"{name}: {im.format} {im.size} {im.mode}")


if __name__ == "__main__":
    main()
