"""Title normalization shared by matching and runtime estimates."""

from __future__ import annotations

import re
import unicodedata


def norm(s: str) -> str:
    """Lowercase ASCII letters and digits only: 'Déjà Q!' -> 'dejaq'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s)


_PART_RE = re.compile(
    r"[\s,:\-]*(?:\((\d+)\)|\(?\bpart\s+(\d+|i{1,3}|one|two|three)\)?)\s*$", re.IGNORECASE
)
_PART_WORDS = {"i": 1, "ii": 2, "iii": 3, "one": 1, "two": 2, "three": 3}


def split_part(title: str) -> tuple[str, int | None]:
    """'Descent (1)' / 'Descent, Part II' / 'Descent: Part One' -> ('Descent', 1|2)."""
    m = _PART_RE.search(title)
    if not m:
        return title.strip(), None
    raw = (m.group(1) or m.group(2)).lower()
    return title[: m.start()].strip(), int(raw) if raw.isdigit() else _PART_WORDS[raw]
