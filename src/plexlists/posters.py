"""Posters: generated title cards, and your own images cropped to the right shape."""

from __future__ import annotations

import io
import textwrap
import urllib.request
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from plexlists.models import Show
from plexlists.sync import IMAGE_EXTS

POSTER_SIZE = (1000, 1000)
ART_SIZE = (1920, 1080)
MAX_DOWNLOAD = 40 * 1024 * 1024

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


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    return ImageOps.exif_transpose(img).convert("RGB")


def _shade(size: int) -> Image.Image:
    """How much of the gradient shows over a backdrop: heavy behind the text, light between."""
    stops = ((0.0, 215), (0.3, 120), (0.42, 150), (0.62, 150), (0.8, 215), (1.0, 240))
    col = Image.new("L", (1, size))
    for y in range(size):
        t = y / (size - 1)
        (t0, a0), (t1, a1) = next((lo, hi) for lo, hi in pairwise(stops) if t <= hi[0])
        col.putpixel((0, y), round(a0 + (a1 - a0) * (t - t0) / (t1 - t0)))
    return col.resize((size, size))


def render_poster(
    show: Show, key: str, path: Path, size: int = 1000, backdrop: bytes | None = None
) -> None:
    """Write a title card: text on a gradient, over `backdrop` (image bytes) if given."""
    top, bottom, accent, fg = (_rgb(c) for c in show.colors)
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):  # vertical gradient
        t = y / (size - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        d.line([(0, y), (size, y)], fill=color)
    if backdrop is not None:
        photo = ImageOps.fit(_open(backdrop), (size, size))
        img = Image.composite(img, photo, _shade(size))
        d = ImageDraw.Draw(img)

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

    _save(img, path)


def _save(img: Image.Image, path: Path) -> None:
    """Save as path (a .jpg), removing the same image in any other format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for ext in IMAGE_EXTS:
        other = path.with_suffix(ext)
        if other != path and other.is_file():
            other.unlink()
    img.save(path, quality=92)


# --------------------------------------------------------------------- your own images


def read_source(source: str) -> bytes:
    """Image bytes from a file path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "plexlists"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError(f"image is larger than {MAX_DOWNLOAD // (1024 * 1024)} MB")
        return data
    return Path(source).expanduser().read_bytes()


def save_image(data: bytes, path: Path) -> None:
    """Save image bytes as path (a .jpg) at their own size and shape."""
    _save(_open(data), path)


def import_image(data: bytes, path: Path, art: bool = False) -> None:
    """Crop image bytes to a square poster (or 16:9 background art) and save as path."""
    _save(ImageOps.fit(_open(data), ART_SIZE if art else POSTER_SIZE), path)
