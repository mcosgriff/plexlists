from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from plexlists.models import Show, discover
from plexlists.titles import split_part
from tests.fakeplex import FakeServer, FakeShow, Item


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets an empty config dir, so nothing touches the real keychain or files."""
    d = tmp_path / "config"
    monkeypatch.setenv("PLEXLISTS_CONFIG_DIR", str(d))
    for var in (
        "PLEX_URL",
        "PLEX_TOKEN",
        "PLEXAPI_AUTH_SERVER_BASEURL",
        "PLEXAPI_AUTH_SERVER_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield d


@pytest.fixture(scope="session")
def shows() -> dict[str, Show]:
    found, errors = discover(None)
    assert not errors
    return found


def library_for(show: Show, style: str) -> list[Item]:
    """A fake Plex library holding every episode a show's playlists reference.

    style="tmdb": parts as "Title (1)", long episodes as one combined file.
    style="tvdb": parts as "Title, Part II", long episodes split in two, accents.
    """
    refs: dict[str, set[tuple[int | None, int | None]]] = {}
    for p in show.playlists.values():
        for e in p.episodes:
            if not e.film:
                base, part = split_part(e.title)
                refs.setdefault(base, set()).add((e.season, part))
    eps: list[Item] = []
    for base, parts in refs.items():
        numbered = sorted((s or 0, p) for s, p in parts if p is not None)
        season = next(iter(parts))[0]
        if base in ("Encounter at Farpoint", "All Good Things..."):
            titles = [base] if style == "tmdb" else [f"{base} ({k})" for k in (1, 2)]
            for t in titles:
                eps.append(Item(len(eps) + 1, t, season, len(eps) + 1))
        elif numbered:
            for s, p in numbered:
                t = f"{base} ({p})" if style == "tmdb" else f"{base}, Part {'I' * p}"
                eps.append(Item(len(eps) + 1, t, s, len(eps) + 1))
        else:
            t = base
            if style == "tvdb":
                t = {
                    "Deja Q": "Déjà Q",
                    "Folie a Deux": "Folie à Deux",
                    "Amor Fati": "The Sixth Extinction II: Amor Fati",
                }.get(base, base)
            eps.append(Item(len(eps) + 1, t, season, len(eps) + 1))
    return eps


def film_items(show: Show) -> list[Item]:
    return [Item(9000 + i, f.name, year=f.year) for i, f in enumerate(show.films.values())]


@pytest.fixture
def make_server():
    def _make(show: Show, style: str = "tmdb", films: bool = True) -> FakeServer:
        return FakeServer(
            FakeShow(show.title, library_for(show, style)), film_items(show) if films else []
        )

    return _make
