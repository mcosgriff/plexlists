"""Show and playlist definitions, loaded from TOML files.

A show file looks like this (see src/plexlists/shows/ for full examples):

    [show]
    title = "Star Trek: The Next Generation"   # as it appears in Plex
    label = "Star Trek: TNG"                    # short name for tables and posters
    prefix = "TNG: "                            # playlist name prefix in Plex
    episode_minutes = 45
    long_episodes = ["Encounter at Farpoint"]   # double length when stored as one file
    colors = { top = "#1a2140", bottom = "#05060f", accent = "#f2a93b", text = "#f4f1ea" }

    [films.first_contact]
    name = "Star Trek: First Contact"
    search = "First Contact"                    # word to search the movie library for
    year = 1996
    minutes = 111

    [[playlists]]
    key = "borg"
    name = "The Borg"
    description = "Every Borg episode in order, plus First Contact"
    poster_from = "Q Who"                       # optional: artwork for the generated poster
    episodes = [
        [2, "Q Who", "First contact with the Borg"],   # [season, title, note?]
        [3, "The Best of Both Worlds (1)"],
        { film = "first_contact" },                    # a film from [films]
    ]

The file name (minus .toml) is the show's key on the command line.

`poster_from` picks the Plex artwork behind a generated poster: an episode title
(its still), a key from [films] (the film's art), or "show" (the show's
background). Without it, the playlist's first episode is used.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexlists.titles import norm, split_part

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DEFAULT_COLORS = ("#1b1f2a", "#05070b", "#d4a017", "#f2f2f2")


class ShowFileError(ValueError):
    """A show file is malformed. The message names the file and the problem."""


@dataclass(frozen=True)
class Film:
    name: str
    search: str
    year: int
    minutes: int = 120


@dataclass(frozen=True)
class Ep:
    season: int | None
    title: str
    note: str = ""
    film: str | None = None  # key into Show.films; title is unused for films


@dataclass(frozen=True)
class Playlist:
    key: str
    name: str  # without the show prefix
    description: str
    episodes: tuple[Ep, ...]
    poster_from: str = ""  # episode title, film key or "show"; "" = first episode


@dataclass(frozen=True)
class Show:
    slug: str
    title: str
    label: str
    prefix: str
    playlists: dict[str, Playlist]
    films: dict[str, Film] = field(default_factory=dict)
    episode_minutes: int = 44
    long_episodes: frozenset[str] = frozenset()  # normalized titles
    colors: tuple[str, str, str, str] = DEFAULT_COLORS  # top, bottom, accent, text
    source: Path | None = None

    def plex_name(self, key: str) -> str:
        return self.prefix + self.playlists[key].name

    def display(self, e: Ep) -> str:
        return self.films[e.film].name if e.film else e.title

    def minutes(self, e: Ep) -> int:
        if e.film:
            return self.films[e.film].minutes
        base, part = split_part(e.title)
        double = part is None and norm(base) in self.long_episodes
        return self.episode_minutes * (2 if double else 1)

    def runtime(self, key: str) -> int:
        return sum(self.minutes(e) for e in self.playlists[key].episodes)


# --------------------------------------------------------------------- loading


def _req(table: dict[str, Any], key: str, typ: type, where: str) -> Any:
    if key not in table:
        raise ShowFileError(f"{where}: missing '{key}'")
    val = table[key]
    if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
        raise ShowFileError(f"{where}: '{key}' must be a {typ.__name__}, got {val!r}")
    return val


def _opt(table: dict[str, Any], key: str, typ: type, default: Any, where: str) -> Any:
    return _req(table, key, typ, where) if key in table else default


def _entry(raw: Any, where: str, films: dict[str, Film]) -> Ep:
    if isinstance(raw, dict):
        fkey = _req(raw, "film", str, where)
        if fkey not in films:
            raise ShowFileError(f"{where}: unknown film '{fkey}' (define it under [films.{fkey}])")
        return Ep(None, films[fkey].name, _opt(raw, "note", str, "", where), film=fkey)
    if isinstance(raw, list) and len(raw) in (2, 3):
        season, title, *rest = raw
        note = rest[0] if rest else ""
        if isinstance(season, int) and isinstance(title, str) and isinstance(note, str):
            return Ep(season, title, note)
    raise ShowFileError(
        f'{where}: expected [season, "title", "note"?] or {{ film = "key" }}, got {raw!r}'
    )


def parse_show(data: dict[str, Any], slug: str, source: Path | None = None) -> Show:
    name = str(source or slug)
    if not KEY_RE.match(slug):
        raise ShowFileError(f"{name}: file name must be lowercase letters, digits, - or _")
    show = data.get("show")
    if not isinstance(show, dict):
        raise ShowFileError(f"{name}: missing [show] table")
    w = f"{name} [show]"
    title = _req(show, "title", str, w)

    colors_raw = _opt(show, "colors", dict, {}, w)
    defaults = dict(zip(("top", "bottom", "accent", "text"), DEFAULT_COLORS, strict=True))
    colors = tuple(str(colors_raw.get(k, v)) for k, v in defaults.items())
    for c in colors:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            raise ShowFileError(f"{w}: colors must be #rrggbb, got {c!r}")

    films: dict[str, Film] = {}
    for fkey, f in _opt(data, "films", dict, {}, name).items():
        fw = f"{name} [films.{fkey}]"
        if not isinstance(f, dict):
            raise ShowFileError(f"{fw}: must be a table")
        films[fkey] = Film(
            name=_req(f, "name", str, fw),
            search=_opt(f, "search", str, _req(f, "name", str, fw), fw),
            year=_req(f, "year", int, fw),
            minutes=_opt(f, "minutes", int, 120, fw),
        )

    playlists: dict[str, Playlist] = {}
    for i, p in enumerate(_opt(data, "playlists", list, [], name), 1):
        pw = f"{name} playlist #{i}"
        if not isinstance(p, dict):
            raise ShowFileError(f"{pw}: must be a [[playlists]] table")
        key = _req(p, "key", str, pw)
        pw = f"{name} playlist '{key}'"
        if not KEY_RE.match(key):
            raise ShowFileError(f"{pw}: key must be lowercase letters, digits, - or _")
        if key in playlists:
            raise ShowFileError(f"{pw}: duplicate key")
        raw_eps = _req(p, "episodes", list, pw)
        if not raw_eps:
            raise ShowFileError(f"{pw}: episodes is empty")
        playlists[key] = Playlist(
            key=key,
            name=_req(p, "name", str, pw),
            description=_opt(p, "description", str, "", pw),
            episodes=tuple(_entry(e, f"{pw} entry {n}", films) for n, e in enumerate(raw_eps, 1)),
            poster_from=_opt(p, "poster_from", str, "", pw),
        )
    if not playlists:
        raise ShowFileError(f"{name}: no [[playlists]] defined")

    return Show(
        slug=slug,
        title=title,
        label=_opt(show, "label", str, title, w),
        prefix=_opt(show, "prefix", str, f"{title}: ", w),
        playlists=playlists,
        films=films,
        episode_minutes=_opt(show, "episode_minutes", int, 44, w),
        long_episodes=frozenset(norm(t) for t in _opt(show, "long_episodes", list, [], w)),
        colors=(colors[0], colors[1], colors[2], colors[3]),
        source=source,
    )


def load_show(path: Path) -> Show:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ShowFileError(f"{path}: invalid TOML: {exc}") from exc
    return parse_show(data, path.stem, path)


BUILTIN_DIR = Path(__file__).parent / "shows"


def discover(user_dir: Path | None) -> tuple[dict[str, Show], list[str]]:
    """All shows: built-in first, then the user's folder (which can override by name).

    Returns (shows, errors). A broken file is reported, not fatal, so one bad
    show file doesn't take down the whole CLI.
    """
    shows: dict[str, Show] = {}
    errors: list[str] = []
    dirs = [BUILTIN_DIR] + ([user_dir] if user_dir and user_dir.is_dir() else [])
    for d in dirs:
        for path in sorted(d.glob("*.toml")):
            try:
                shows[path.stem] = load_show(path)
            except ShowFileError as exc:
                errors.append(str(exc))
    return shows, errors
