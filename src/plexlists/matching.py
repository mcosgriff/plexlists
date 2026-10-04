"""Title-based episode matching.

Episodes are matched by title rather than SxxEyy, so DVD-order vs. aired-order
numbering doesn't matter. Multi-part episodes work whether Plex has them as
separate parts ("Descent (1)", "Descent, Part II") or as one combined file.
"""

from __future__ import annotations

import difflib
from typing import Any, Protocol

from plexlists.models import Ep
from plexlists.titles import norm, split_part


class EpisodeLike(Protocol):
    title: str
    seasonNumber: int | None  # noqa: N815  (plexapi's attribute name)
    index: int | None


def _order(e: EpisodeLike) -> tuple[int, int]:
    return (e.seasonNumber or 0, e.index or 0)


class Library:
    """Matches playlist entries to Plex items. Takes plain objects, so it's testable."""

    def __init__(self, episodes: list[Any], films: dict[str, Any] | None = None) -> None:
        self.films = films or {}
        self.titles: dict[str, str] = {}  # norm(full title) -> title, for suggestions
        self.index: dict[str, list[tuple[int | None, Any]]] = {}  # norm(base) -> [(part, ep)]
        for e in episodes:
            base, part = split_part(e.title)
            self.index.setdefault(norm(base), []).append((part, e))
            self.titles[norm(e.title)] = e.title

    def match(self, ep: Ep) -> list[Any]:
        """Plex items for one entry: [] if missing or ambiguous, several for a multi-part title."""
        if ep.film:
            m = self.films.get(ep.film)
            return [m] if m is not None else []
        base, part = split_part(ep.title)
        q = norm(base)
        cands = self.index.get(q)
        if not cands:
            # Fuzzy fallback: "Amor Fati" vs "The Sixth Extinction II: Amor Fati"
            cands = [
                c for k, cs in self.index.items() for c in cs if q in k or (len(k) >= 6 and k in q)
            ]
        if not cands:
            return []
        pool = [c for c in cands if c[1].seasonNumber == ep.season] or cands

        if part is not None:
            exact = [e for p, e in pool if p == part] or [e for p, e in cands if p == part]
            if len(exact) == 1:
                return exact
            combined = [e for p, e in pool if p is None]  # two-parter stored as one file
            return combined if len(combined) == 1 and not exact else []

        if len(pool) == 1:
            return [pool[0][1]]
        # Several parts of one story in this season: take them all, in order.
        bases = {norm(split_part(e.title)[0]) for _, e in pool}
        if len(bases) == 1 and all(p is not None for p, _ in pool):
            ordered = sorted(pool, key=lambda c: (c[0] or 0, _order(c[1])))
            return [e for _, e in ordered]
        return []

    def suggest(self, ep: Ep, n: int = 2) -> list[str]:
        """Closest Plex titles to an entry that didn't match."""
        q = norm(split_part(ep.title)[0])
        hits = difflib.get_close_matches(q, list(self.titles), n=n, cutoff=0.6)
        return [self.titles[h] for h in hits]
