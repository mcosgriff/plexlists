"""Human-readable summaries of match/build results (rich markup), for the CLI and TUI."""

from __future__ import annotations

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
