"""Find things in Plex and keep playlists in sync with their definitions, in place.

Updating in place (add, remove, reorder) instead of delete-and-recreate keeps a
playlist's identity, so posters and art survive a rebuild.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plexlists.models import Film
from plexlists.titles import norm

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def find_show(plex: Any, title: str) -> Any | None:
    for section in plex.library.sections():
        if section.type == "show":
            hits = [s for s in section.search(title=title) if norm(s.title) == norm(title)]
            if hits:
                return hits[0]
    return None


def find_films(plex: Any, films: dict[str, Film]) -> dict[str, Any]:
    """Locate films in any movie library, by search word + year."""
    found: dict[str, Any] = {}
    for section in plex.library.sections():
        if section.type != "movie":
            continue
        for key, f in films.items():
            if key in found:
                continue
            try:
                hits = section.search(title=f.search)
            except Exception:
                continue
            m = next(
                (m for m in hits if m.year == f.year and norm(f.search) in norm(m.title)), None
            )
            if m is not None:
                found[key] = m
    return found


@dataclass(frozen=True)
class SyncPlan:
    remove: list[Any]
    add: list[Any]
    moves: list[tuple[Any, Any | None]]  # (key, after_key or None for "to the top")

    @property
    def empty(self) -> bool:
        return not (self.remove or self.add or self.moves)

    def summary(self) -> str:
        parts = [
            f"+{len(self.add)}" if self.add else "",
            f"-{len(self.remove)}" if self.remove else "",
            f"{len(self.moves)} moved" if self.moves else "",
        ]
        return ", ".join(p for p in parts if p)


def plan_sync(current: list[Any], wanted: list[Any]) -> SyncPlan:
    """Steps that turn the `current` key order into `wanted`, mirroring Plex semantics:
    removes first, then adds append at the end, then each move puts a key after another."""
    wanted_set, current_set = set(wanted), set(current)
    remove = [k for k in current if k not in wanted_set]
    add = [k for k in wanted if k not in current_set]
    order = [k for k in current if k in wanted_set] + add
    moves: list[tuple[Any, Any | None]] = []
    for i, k in enumerate(wanted):
        if order[i] != k:
            moves.append((k, wanted[i - 1] if i else None))
            order.remove(k)
            order.insert(i, k)
    return SyncPlan(remove, add, moves)


def playlist_items(plex: Any, pl: Any) -> list[Any]:
    """Fresh list of a playlist's items (each with .playlistItemID), bypassing plexapi's cache."""
    return plex.fetchItems(f"{pl.key}/items")


def sync_playlist(plex: Any, pl: Any, items: list[Any], dry_run: bool) -> str:
    """Make an existing playlist hold exactly `items`, in order. Returns a short status."""
    current = playlist_items(plex, pl)
    plan = plan_sync([i.ratingKey for i in current], [i.ratingKey for i in items])
    if plan.empty:
        return "unchanged"
    if dry_run:
        return f"would update ({plan.summary()})"

    pid = {i.ratingKey: i.playlistItemID for i in current}
    for k in plan.remove:
        plex.query(f"{pl.key}/items/{pid[k]}", method=plex._session.delete)
    if plan.add:
        by_key = {i.ratingKey: i for i in items}
        pl.addItems([by_key[k] for k in plan.add])
    if plan.moves:
        pid = {i.ratingKey: i.playlistItemID for i in playlist_items(plex, pl)}
        for k, after in plan.moves:
            q = f"{pl.key}/items/{pid[k]}/move" + (f"?after={pid[after]}" if after else "")
            plex.query(q, method=plex._session.put)
    return f"updated ({plan.summary()})"


# --------------------------------------------------------------------- artwork


def find_image(folder: Path, stem: str) -> Path | None:
    for ext in IMAGE_EXTS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def sync_artwork(
    plex: Any,
    pl: Any | None,
    key: str,
    folder: Path,
    state: dict[str, str],
    force: bool,
    dry_run: bool,
) -> list[str]:
    """Upload <key>.* as the poster and <key>-art.* as the background, if new or changed.

    `state` maps "<server>/<playlist>/<kind>" to the hash of the last upload; it's
    updated in place.
    """
    done = []
    for kind, stem in (("poster", key), ("art", f"{key}-art")):
        img = find_image(folder, stem)
        if img is None:
            continue
        h = file_hash(img)
        skey = f"{plex.machineIdentifier}/{pl.ratingKey if pl else 'new'}/{kind}"
        if not force and pl is not None and state.get(skey) == h:
            continue
        if dry_run or pl is None:
            done.append(f"would upload {kind} {img.name}")
            continue
        upload = pl.uploadPoster if kind == "poster" else pl.uploadArt
        upload(filepath=str(img))
        state[skey] = h
        done.append(f"{kind} uploaded ({img.name})")
    return done
