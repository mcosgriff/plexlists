"""Plex operations shared by the CLI and the TUI: match, build, remove.

Nothing in here prints. Functions return results that the front end renders.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from plexlists import config
from plexlists.matching import Library
from plexlists.models import Ep, Show
from plexlists.sync import (
    file_hash,
    find_films,
    find_image,
    find_show,
    sync_artwork,
    sync_playlist,
)


class EntryStatus(StrEnum):
    matched = "matched"
    missing = "missing"  # an episode that isn't in Plex (or is ambiguous)
    no_film = "no_film"  # a film that isn't in any movie library: skipped, not an error


@dataclass
class EntryResult:
    ep: Ep
    items: list[Any]
    suggestions: list[str] = field(default_factory=list)

    @property
    def status(self) -> EntryStatus:
        if self.items:
            return EntryStatus.matched
        return EntryStatus.no_film if self.ep.film else EntryStatus.missing


@dataclass
class MatchResult:
    key: str
    entries: list[EntryResult]

    @property
    def items(self) -> list[Any]:
        """Matched Plex items in order, without duplicates (a combined two-parter can
        satisfy two entries)."""
        seen: set[Any] = set()
        out = []
        for r in self.entries:
            for item in r.items:
                if item.ratingKey not in seen:
                    seen.add(item.ratingKey)
                    out.append(item)
        return out

    @property
    def missing(self) -> list[EntryResult]:
        return [r for r in self.entries if r.status == EntryStatus.missing]

    @property
    def films_skipped(self) -> list[EntryResult]:
        return [r for r in self.entries if r.status == EntryStatus.no_film]


@dataclass
class ApplyResult:
    action: str  # "created", "updated (+1)", "unchanged", "would create", …
    artwork: list[str] = field(default_factory=list)
    artwork_error: str | None = None
    skipped: str | None = None  # why nothing was done, if so


@dataclass(frozen=True)
class PlaylistTimes:
    """When Plex says a playlist was created and last changed (server-local time)."""

    created: datetime | None
    updated: datetime | None


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    with contextlib.suppress(TypeError, ValueError):
        return datetime.fromisoformat(value)
    return None


def cached_playlist_times(server_id: str) -> dict[str, PlaylistTimes] | None:
    """Playlist times as of the last connection to this server, or None if never seen."""
    saved = config.read_json(config.playlists_state_file()).get(server_id)
    if not isinstance(saved, dict):
        return None
    return {
        name: PlaylistTimes(_stamp(t.get("created")), _stamp(t.get("updated")))
        for name, t in saved.items()
        if isinstance(t, dict)
    }


class ShowNotFoundError(LookupError):
    pass


def _image_url(item: Any, attr: str) -> str | None:
    """A Plex item's thumbUrl/artUrl, falling back to the other one."""
    other = "thumbUrl" if attr == "artUrl" else "artUrl"
    return getattr(item, attr, None) or getattr(item, other, None)


class Session:
    """A connected Plex server plus per-show caches of its episodes."""

    def __init__(self, plex: Any) -> None:
        self.plex = plex
        self._libraries: dict[tuple[str, str], Library] = {}
        self._show_objs: dict[tuple[str, str], Any] = {}

    @property
    def server_name(self) -> str:
        return str(getattr(self.plex, "friendlyName", "Plex"))

    def library(self, show: Show, title: str | None = None) -> Library:
        title = title or show.title
        cache_key = (show.slug, title)
        if cache_key not in self._libraries:
            show_obj = find_show(self.plex, title)
            if show_obj is None:
                raise ShowNotFoundError(f"Couldn't find '{title}' in any TV library.")
            films = find_films(self.plex, show.films) if show.films else {}
            self._libraries[cache_key] = Library(show_obj.episodes(), films)
            self._show_objs[cache_key] = show_obj
        return self._libraries[cache_key]

    def forget(self, show: Show | None = None) -> None:
        """Drop cached episode lists (e.g. after adding episodes to Plex)."""
        if show is None:
            self._libraries.clear()
            self._show_objs.clear()
        else:
            self._libraries = {k: v for k, v in self._libraries.items() if k[0] != show.slug}
            self._show_objs = {k: v for k, v in self._show_objs.items() if k[0] != show.slug}

    def match(self, show: Show, key: str, title: str | None = None) -> MatchResult:
        lib = self.library(show, title)
        entries = []
        for ep in show.playlists[key].episodes:
            items = lib.match(ep)
            sugg = lib.suggest(ep) if not items and not ep.film else []
            entries.append(EntryResult(ep, items, sugg))
        return MatchResult(key, entries)

    def backdrop(self, show: Show, key: str, title: str | None = None) -> bytes | None:
        """Artwork from Plex to put behind a generated poster, or None if there isn't any.

        Uses the playlist's `poster_from` (an episode title, a film key or "show"),
        otherwise the still of the first episode Plex has, otherwise the show's art.
        """
        lib = self.library(show, title)
        show_obj = self._show_objs[(show.slug, title or show.title)]
        p = show.playlists[key]
        source: list[Any] = []
        if p.poster_from in show.films:
            source = [lib.films.get(p.poster_from)]
        elif p.poster_from and p.poster_from != "show":
            source = lib.match(Ep(None, p.poster_from))
        elif not p.poster_from:
            source = next((m for e in p.episodes if not e.film and (m := lib.match(e))), [])
        film = p.poster_from in show.films
        urls = [_image_url(i, "artUrl" if film else "thumbUrl") for i in source if i is not None]
        urls += [_image_url(show_obj, "artUrl"), _image_url(show_obj, "thumbUrl")]
        url = next((u for u in urls if u), None)
        if url is None:
            return None
        resp = self.plex._session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def existing_playlists(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for pl in self.plex.playlists(playlistType="video"):
            found.setdefault(pl.title, pl)
        return found

    def playlist_times(self) -> dict[str, PlaylistTimes]:
        """Created/updated times of every video playlist, by name, straight from Plex.

        Plex is the record of these; they're also saved per server so the TUI can
        show them before it connects.
        """
        times = {
            name: PlaylistTimes(
                _stamp(getattr(pl, "addedAt", None)), _stamp(getattr(pl, "updatedAt", None))
            )
            for name, pl in self.existing_playlists().items()
        }
        state = config.read_json(config.playlists_state_file())
        state[str(self.plex.machineIdentifier)] = {
            name: {k: v.isoformat() if v else None for k, v in vars(t).items()}
            for name, t in times.items()
        }
        config.write_json(config.playlists_state_file(), state)
        return times

    def apply(
        self,
        show: Show,
        result: MatchResult,
        *,
        dry_run: bool = False,
        artwork: bool = True,
        force_artwork: bool = False,
        posters_dir: Path | None = None,
        existing: dict[str, Any] | None = None,
    ) -> ApplyResult:
        """Create the playlist or update it in place, then sync its artwork."""
        key = result.key
        name, description = show.plex_name(key), show.playlists[key].description
        if existing is None:
            existing = self.existing_playlists()
        pl = existing.get(name)
        if pl is not None and pl.smart:
            return ApplyResult(
                "skipped",
                skipped="a smart playlist with this name exists. Rename or delete it in Plex.",
            )
        items = result.items
        if not items:
            return ApplyResult("skipped", skipped="nothing matched")

        if pl is None:
            action = "would create" if dry_run else "created"
            if not dry_run:
                pl = self.plex.createPlaylist(name, items=items)
                existing[name] = pl
        else:
            action = sync_playlist(self.plex, pl, items, dry_run)
        if pl is not None and not dry_run and (pl.summary or "") != description:
            with contextlib.suppress(Exception):
                pl.editSummary(description)

        out = ApplyResult(action)
        if artwork:
            folder = posters_dir or config.posters_dir(show.slug)
            state = config.read_json(config.artwork_state_file())
            try:
                out.artwork = sync_artwork(
                    self.plex, pl, key, folder, state, force_artwork, dry_run
                )
            except Exception as exc:
                out.artwork_error = f"{type(exc).__name__}: {exc}"
            if not dry_run:
                config.write_json(config.artwork_state_file(), state)
        return out

    def pull_artwork(
        self,
        show: Show,
        key: str,
        folder: Path,
        *,
        force: bool = False,
        existing: dict[str, Any] | None = None,
    ) -> list[str]:
        """Save the poster and art a playlist has in Plex into the posters folder.

        Only artwork someone chose is saved: Plex's automatic collage isn't. What's
        saved is recorded as already uploaded, so `build` doesn't send it straight back.
        """
        from plexlists.posters import save_image

        if existing is None:
            existing = self.existing_playlists()
        pl = existing.get(show.plex_name(key))
        if pl is None:
            return ["not in Plex"]
        thumb = getattr(pl, "thumb", None)
        if thumb == getattr(pl, "composite", None):
            thumb = None  # the automatic collage of episode stills
        state = config.read_json(config.artwork_state_file())
        done = []
        for kind, stem, path in (("poster", key, thumb), ("art", f"{key}-art", pl.art)):
            if not path:
                continue
            if (old := find_image(folder, stem)) and not force:
                done.append(f"{kind} skipped ({old.name} exists)")
                continue
            resp = self.plex._session.get(self.plex.url(path, includeToken=True), timeout=30)
            resp.raise_for_status()
            dest = folder / f"{stem}.jpg"
            save_image(resp.content, dest)
            state[f"{self.plex.machineIdentifier}/{pl.ratingKey}/{kind}"] = file_hash(dest)
            done.append(f"{kind} saved ({dest.name})")
        config.write_json(config.artwork_state_file(), state)
        return done or ["no custom artwork in Plex"]

    def find_playlists(self, show: Show, keys: list[str]) -> list[Any]:
        names = {show.plex_name(k) for k in keys}
        return [pl for pl in self.plex.playlists() if pl.title in names]
