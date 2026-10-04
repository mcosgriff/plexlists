"""Human-readable summaries of match/build results (rich markup), for the CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.markup import escape

from plexlists.models import Show
from plexlists.service import ApplyResult, EntryResult, EntryStatus, MatchResult


def match_summary(result: MatchResult) -> str:
    n = len(result.missing)
    return "[green]all matched[/green]" if not n else f"[yellow]{n} missing[/yellow]"


def entry_status(show: Show, r: EntryResult) -> str:
    """One cell describing how an entry matched, e.g. '✓', '✓ Descent (1) + (2)', '✗ missing'."""
    if r.status == EntryStatus.matched:
        titles = [i.title for i in r.items]
        if len(titles) == 1 and titles[0] == show.display(r.ep):
            return "[green]✓[/green]"
        return f"[green]✓[/green] [dim]{escape(' + '.join(titles))}[/dim]"
    if r.status == EntryStatus.no_film:
        return "[dim]not in movie library[/dim]"
    hint = f" [dim](closest: {escape(', '.join(r.suggestions))})[/dim]" if r.suggestions else ""
    return f"[red]✗ missing[/red]{hint}"


@dataclass(frozen=True)
class EntryDetails:
    """What Plex knows about the item(s) an entry matched, as display strings ("" = unknown)."""

    episode: str  # "S2E16", "S3E26 + S4E01", or a film's year
    aired: str
    length: str
    watched: str  # "✓", "—", or "1/2" for a partly watched two-parter
    summary: str


def _code(item: Any) -> str:
    season, number = getattr(item, "seasonNumber", None), getattr(item, "index", None)
    if season is None or not number:
        return str(getattr(item, "year", None) or "")
    return f"S{season}E{number:02d}"


def _minutes(ms: int) -> str:
    h, m = divmod(round(ms / 60000), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def entry_details(r: EntryResult) -> EntryDetails | None:
    """Details for a matched entry, or None if it didn't match anything in Plex."""
    if not r.items:
        return None
    aired = getattr(r.items[0], "originallyAvailableAt", None)
    durations = [getattr(i, "duration", None) or 0 for i in r.items]
    played = sum(bool(getattr(i, "isPlayed", False)) for i in r.items)
    watched = "✓" if played == len(r.items) else "—" if not played else f"{played}/{len(r.items)}"
    summaries = [s for i in r.items if (s := (getattr(i, "summary", None) or "").strip())]
    return EntryDetails(
        episode=" + ".join(c for i in r.items if (c := _code(i))),
        aired=f"{aired:%Y-%m-%d}" if aired else "",
        length=_minutes(sum(durations)) if all(durations) else "",
        watched=watched,
        summary="\n".join(summaries),
    )


def watched_progress(result: MatchResult) -> tuple[int, int]:
    """(watched, total) over the Plex items a playlist matched."""
    items = result.items
    return sum(bool(getattr(i, "isPlayed", False)) for i in items), len(items)


def next_unwatched(result: MatchResult) -> int | None:
    """Index of the first entry with something left to watch, or None if there isn't one."""
    for n, r in enumerate(result.entries):
        if r.items and not all(getattr(i, "isPlayed", False) for i in r.items):
            return n
    return None


def result_lines(show: Show, result: MatchResult, out: ApplyResult | None = None) -> list[str]:
    """Summary line plus details for one playlist. `out` is None for a match-only check."""
    name = escape(show.plex_name(result.key))
    if out is not None and out.skipped:
        return [f"[bold]{name}[/bold]: [yellow]skipped — {escape(out.skipped)}[/yellow]"]
    action = f" — {escape(out.action)}" if out is not None else ""
    lines = [f"[bold]{name}[/bold]: {len(result.items)} items, {match_summary(result)}{action}"]
    for r in result.missing:
        hint = (
            f"  [dim](closest in Plex: {escape(', '.join(r.suggestions))})[/dim]"
            if r.suggestions
            else ""
        )
        lines.append(f"   [yellow]MISS[/yellow] S{r.ep.season} {escape(r.ep.title)}{hint}")
    for r in result.films_skipped:
        lines.append(f"   [dim]skip {escape(show.display(r.ep))} (not in a movie library)[/dim]")
    if out is not None:
        lines += [f"   [cyan]{escape(msg)}[/cyan]" for msg in out.artwork]
        if out.artwork_error:
            lines.append(f"   [yellow]artwork upload failed: {escape(out.artwork_error)}[/yellow]")
    return lines
