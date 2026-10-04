"""A tiny in-memory stand-in for the parts of plexapi that plexlists uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plexlists.titles import norm


@dataclass
class Item:
    ratingKey: int  # noqa: N815
    title: str
    seasonNumber: int | None = None  # noqa: N815
    index: int = 0
    year: int | None = None
    playlistItemID: int | None = None  # noqa: N815


class FakePlaylist:
    def __init__(self, server: FakeServer, rating_key: int, title: str, items: list[Item]):
        self.server = server
        self.ratingKey = rating_key
        self.title = title
        self.key = f"/playlists/{rating_key}"
        self.smart = False
        self.summary = ""
        self.entries: list[tuple[int, Item]] = []
        self.posters: list[str] = []
        self.arts: list[str] = []
        self.addItems(items)

    @property
    def leafCount(self) -> int:  # noqa: N802
        return len(self.entries)

    def keys(self) -> list[int]:
        return [i.ratingKey for _, i in self.entries]

    def addItems(self, items: list[Item]) -> None:  # noqa: N802
        for i in items:
            self.server.next_pid += 1
            self.entries.append((self.server.next_pid, i))

    def editSummary(self, text: str) -> None:  # noqa: N802
        self.summary = text

    def uploadPoster(self, filepath: str) -> None:  # noqa: N802
        self.posters.append(filepath)

    def uploadArt(self, filepath: str) -> None:  # noqa: N802
        self.arts.append(filepath)

    def delete(self) -> None:
        self.server.pls.remove(self)


@dataclass
class Section:
    type: str
    items: list[Any]

    def search(self, title: str) -> list[Any]:
        return [i for i in self.items if norm(title) in norm(i.title)]


@dataclass
class FakeShow:
    title: str
    eps: list[Item]

    def episodes(self) -> list[Item]:
        return self.eps


@dataclass
class _Library:
    secs: list[Section]

    def sections(self) -> list[Section]:
        return self.secs


class _Session:
    delete, put = "DELETE", "PUT"


@dataclass
class FakeServer:
    show: FakeShow
    movies: list[Item] = field(default_factory=list)
    machineIdentifier: str = "machine-1"  # noqa: N815
    friendlyName: str = "TestServer"  # noqa: N815
    _baseurl: str = "https://test.plex.direct:32400"

    def __post_init__(self) -> None:
        self.library = _Library([Section("show", [self.show]), Section("movie", self.movies)])
        self.pls: list[FakePlaylist] = []
        self.next_pid = 0
        self.next_rk = 1000
        self._session = _Session()

    def playlists(self, **_: Any) -> list[FakePlaylist]:
        return list(self.pls)

    def createPlaylist(self, title: str, items: list[Item]) -> FakePlaylist:  # noqa: N802
        self.next_rk += 1
        pl = FakePlaylist(self, self.next_rk, title, items)
        self.pls.append(pl)
        return pl

    def _pl(self, path: str) -> FakePlaylist:
        return next(p for p in self.pls if path.startswith(p.key + "/"))

    def fetchItems(self, path: str) -> list[Item]:  # noqa: N802
        return [
            Item(i.ratingKey, i.title, i.seasonNumber, i.index, playlistItemID=pid)
            for pid, i in self._pl(path).entries
        ]

    def query(self, path: str, method: str) -> None:
        pl = self._pl(path)
        parts = path.split("?")[0].split("/")
        pid = int(parts[4])
        if method == "DELETE":
            pl.entries = [e for e in pl.entries if e[0] != pid]
        elif parts[-1] == "move":
            entry = next(e for e in pl.entries if e[0] == pid)
            pl.entries.remove(entry)
            after = int(path.split("after=")[1]) if "after=" in path else None
            idx = (
                0
                if after is None
                else next(n for n, e in enumerate(pl.entries) if e[0] == after) + 1
            )
            pl.entries.insert(idx, entry)
