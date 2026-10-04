"""Generated title-card posters: an original starting point you can replace anytime."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from plexlists.models import Show

FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
    "/Library/Fonts/Arial Bold.ttf",
    "Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int) -> Any:
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)  # Pillow >= 10.1 ships a scalable default


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _fit_lines(
    draw: ImageDraw.ImageDraw, text: str, width: int, max_size: int, min_size: int, max_lines: int
) -> tuple[Any, list[str]]:
    """Largest font size at which `text` wraps into <= max_lines lines within width."""
    size = max_size
    while size >= min_size:
        font = _font(size)
        avg = draw.textlength("abcdefghijklmnopqrstuvwxyz", font=font) / 26
        lines = textwrap.wrap(text, width=max(1, int(width / avg)))
        if len(lines) <= max_lines and all(draw.textlength(ln, font=font) <= width for ln in lines):
            return font, lines
        size -= 4
    font = _font(min_size)
    return font, textwrap.wrap(text, width=max(1, int(width / (min_size * 0.55))))[:max_lines]


def render_poster(show: Show, key: str, path: Path, size: int = 1000) -> None:
    top, bottom, accent, fg = (_rgb(c) for c in show.colors)
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):  # vertical gradient
        t = y / (size - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        d.line([(0, y), (size, y)], fill=color)

    m = int(size * 0.09)
    p = show.playlists[key]

    # Show label in spaced caps, with an accent rule under it
    d.text((m, m), " ".join(show.label.upper()), font=_font(int(size * 0.038)), fill=accent)
    rule_y = m + int(size * 0.075)
    d.rectangle([m, rule_y, m + int(size * 0.18), rule_y + max(4, size // 160)], fill=accent)

    # Playlist name, as large as fits in 3 lines, centered vertically
    font, lines = _fit_lines(d, p.name, size - 2 * m, int(size * 0.13), int(size * 0.06), 3)
    lh = int(font.size * 1.12)
    y = int(size * 0.5) - (lh * len(lines)) // 2
    for line in lines:
        d.text((m, y), line, font=font, fill=fg)
        y += lh

    # Description and stats at the bottom
    small = _font(int(size * 0.032))
    step = int(small.size * 1.4)
    desc = textwrap.wrap(p.description, width=int((size - 2 * m) / (small.size * 0.52)))[:2]
    y = size - m - step * (len(desc) + 1)
    dim = tuple(round(c * 0.75) for c in fg)
    for line in desc:
        d.text((m, y), line, font=small, fill=dim)
        y += step
    n = len(p.episodes)
    stats = f"{n} {'item' if n == 1 else 'items'}  ·  ~{round(show.runtime(key) / 60)}h"
    d.text((m, y), stats, font=small, fill=accent)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=92)
