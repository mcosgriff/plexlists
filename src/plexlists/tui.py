"""Textual TUI: browse shows and playlists, check matches, build, generate posters.

Plex calls block, so they run in thread workers and post results back to the UI.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import typer
from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Static,
    Tree,
)
from textual.widgets.option_list import Option

from plexlists import auth, config
from plexlists.models import BUILTIN_DIR, Show, discover
from plexlists.report import (
    entry_details,
    entry_status,
    match_summary,
    next_unwatched,
    result_lines,
    watched_progress,
)
from plexlists.service import (
    ApplyResult,
    MatchResult,
    PlaylistTimes,
    Session,
    ShowNotFoundError,
    cached_playlist_times,
)
from plexlists.sync import find_image


def connect_session() -> Session:
    """Connect using the saved login (or PLEX_URL/PLEX_TOKEN). Tests replace this."""
    plex = auth.connect(
        os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN"), warn=lambda _m: None
    )
    return Session(plex)


def has_login() -> bool:
    """Whether there's a saved login (or PLEX_URL + PLEX_TOKEN) to connect with unasked."""
    if config.load_config().get("client_id"):
        return True
    return bool(os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN"))


def hours(minutes: int) -> str:
    return f"~{round(minutes / 60)}h"


@dataclass(frozen=True)
class Nav:
    slug: str
    key: str | None = None  # None: the show itself


# ===================================================================== modals


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss(False)", "Cancel")]

    def __init__(self, question: str, detail: str = "", yes: str = "Yes") -> None:
        super().__init__()
        self.question, self.detail, self.yes = question, detail, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="title")
            if self.detail:
                yield Static(self.detail, classes="detail")
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()  # Enter shouldn't delete by accident

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class LoginScreen(ModalScreen[bool]):
    """Runs the Plex device login: show the approval URL, wait, pick a server."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.pending: auth.PendingLogin | None = None
        self.account: Any = None
        self.servers: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("Log in to Plex", classes="title")
            yield Static("Contacting plex.tv…", id="login-msg", classes="detail")
            yield LoadingIndicator(id="login-wait")
            yield OptionList(id="servers")
            with Horizontal(classes="buttons"):
                yield Button("Open in browser", id="open", disabled=True)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#servers").display = False
        self.start()

    @work(thread=True, exclusive=True)
    def start(self) -> None:
        msg = self.query_one("#login-msg", Static)
        if not config.keyring_backend()[0]:
            self.app.call_from_thread(
                msg.update,
                "[red]No OS keychain is available.[/red] Run [bold]plexlists login --store "
                "file[/bold] in a terminal instead.",
            )
            return
        try:
            self.pending = auth.begin_login()
        except auth.AuthError as exc:
            self.app.call_from_thread(msg.update, f"[red]{escape(str(exc))}[/red]")
            return
        url = self.pending.url
        self.app.call_from_thread(self.show_url, url)
        try:
            self.account, self.servers = auth.wait_for_approval(self.pending)
        except auth.AuthError as exc:
            self.app.call_from_thread(msg.update, f"[red]{escape(str(exc))}[/red]")
            return
        if len(self.servers) == 1:
            self.app.call_from_thread(self.finish, self.servers[0])
        else:
            self.app.call_from_thread(self.choose_server)

    def show_url(self, url: str) -> None:
        self.query_one("#login-msg", Static).update(
            "Approve [bold]plexlists[/bold] in your browser. If it didn't open, use this link:\n\n"
            f"[link={url}]{escape(url)}[/link]\n\nWaiting for approval…"
        )
        self.query_one("#open", Button).disabled = False
        typer.launch(url)

    def choose_server(self) -> None:
        self.query_one("#login-wait").display = False
        self.query_one("#login-msg", Static).update("Approved. Which server?")
        servers = self.query_one("#servers", OptionList)
        servers.display = True
        for i, s in enumerate(self.servers):
            label = s.name + ("" if s.owned else "  (shared with you)")
            servers.add_option(Option(label, id=str(i)))
        servers.focus()

    @on(OptionList.OptionSelected, "#servers")
    def server_chosen(self, event: OptionList.OptionSelected) -> None:
        self.finish(self.servers[int(event.option.id or 0)])

    def finish(self, resource: Any) -> None:
        assert self.pending is not None
        auth.complete_login(self.pending, self.account, resource, config.Store.keyring)
        self.app.notify(f"Logged in as {self.account.username} using {resource.name}.")
        self.dismiss(True)

    @on(Button.Pressed, "#open")
    def open_browser(self) -> None:
        if self.pending:
            typer.launch(self.pending.url)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self.pending:
            auth.cancel_login(self.pending)
        self.dismiss(False)


class AccountScreen(ModalScreen[str]):
    """Login status with Log in / Log out. Dismisses with 'login', 'logout' or ''."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss('')", "Close")]

    def compose(self) -> ComposeResult:
        cfg = config.load_config()
        logged_in = bool(cfg.get("client_id")) and bool(
            config.load_secrets(cfg.get("store", config.Store.keyring))
        )
        with Vertical(classes="dialog"):
            yield Label("Plex account", classes="title")
            yield Static(self.describe(cfg, logged_in), classes="detail")
            with Horizontal(classes="buttons"):
                if logged_in:
                    yield Button("Log out", variant="error", id="logout")
                yield Button(
                    "Log in again" if logged_in else "Log in", variant="primary", id="login"
                )
                yield Button("Close", id="close")

    @staticmethod
    def describe(cfg: dict[str, Any], logged_in: bool) -> str:
        if not logged_in:
            env = (
                " (PLEX_URL/PLEX_TOKEN are set and will be used)"
                if os.environ.get("PLEX_TOKEN")
                else ""
            )
            return f"Not logged in{env}."
        lines = [
            f"User:    {escape(str(cfg.get('username', '?')))}",
            f"Server:  {escape(str(cfg.get('server_name', '?')))}",
        ]
        secrets = config.load_secrets(cfg.get("store", config.Store.keyring)) or {}
        if exp := auth.jwt_expiry(secrets.get("jwt", "")):
            lines.append(f"Token:   renews automatically (current one expires {exp:%Y-%m-%d})")
        lines.append(f"Config:  {escape(str(config.config_file()))}")
        return "\n".join(lines)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss("" if event.button.id == "close" else str(event.button.id))


# ===================================================================== app


class PlexlistsApp(App[None]):
    TITLE = "plexlists"
    CSS = """
    #side { width: 42; border-right: solid $panel; padding-right: 1; }
    #nav { height: 1fr; }
    #filter.hidden { display: none; }
    #main { width: 1fr; }
    #summary { height: auto; padding: 0 1 1 1; }
    #table { height: 1fr; }
    #detail { height: auto; max-height: 7; border-top: solid $panel; padding: 0 1; }
    #detail.hidden { display: none; }
    #log { height: 8; border-top: solid $panel; padding: 0 1; }
    #log.hidden { display: none; }
    ModalScreen { align: center middle; }
    .dialog {
        width: 64; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    .dialog.wide { width: 90; }
    .dialog .title { text-style: bold; margin-bottom: 1; }
    .dialog .detail { margin-bottom: 1; }
    .dialog LoadingIndicator { height: 3; }
    .dialog OptionList { height: auto; max-height: 10; margin-bottom: 1; }
    .buttons { height: auto; align-horizontal: right; }
    .buttons Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "check", "Check"),
        Binding("b", "build", "Build"),
        Binding("p", "poster", "Poster"),
        Binding("v", "view_poster", "View poster"),
        Binding("o", "open_posters", "Posters folder"),
        Binding("e", "edit_show", "Edit show"),
        Binding("x", "remove", "Remove"),
        Binding("r", "reload", "Reload"),
        Binding("a", "account", "Account"),
        Binding("slash", "filter", "Filter", key_display="/"),
        Binding("z", "toggle_fold", "Collapse/expand all"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("g", "toggle_log", "Log"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.shows: dict[str, Show] = {}
        self.session: Session | None = None
        self.results: dict[tuple[str, str], MatchResult] = {}
        self.actions: dict[tuple[str, str], str] = {}
        # Plex playlist name -> times, as of the last time we asked. None = never asked.
        self.times: dict[str, PlaylistTimes] | None = None
        # Shows whose episode details we stop loading unasked ("*" = can't connect at all).
        self.no_auto: set[str] = set()
        self.absent: set[str] = set()  # shows the connected server doesn't have
        self.scan: tuple[int, int] | None = None  # background check progress
        self.filter_text = ""  # lowercase; narrows the tree to matching playlists
        # After a rebuild, until the cursor is back: the tree's first node, whose
        # automatic highlight must not change the view.
        self.restoring: Nav | None = None
        self.connect_lock = threading.Lock()
        self.nodes: dict[Nav, Any] = {}
        self.current: Nav | None = None

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="side"):
                yield Input(
                    placeholder="Filter playlists and episodes", id="filter", classes="hidden"
                )
                tree: Tree[Nav] = Tree("Shows", id="nav")
                tree.show_root = False
                yield tree
            with Vertical(id="main"):
                yield Static(id="summary")
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static(id="detail", classes="hidden")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        if config.import_legacy_login():
            self.notify("Imported your login from xfiles.py.")
        self.load_cached_times()
        self.load_shows()
        self.update_subtitle()
        self.query_one("#nav", Tree).focus()
        if has_login():
            self.connect_in_background()

    @work(thread=True, exclusive=True, group="connect")
    def connect_in_background(self) -> None:
        """Connect without being asked, then check every playlist against Plex quietly,
        so the header, playlist times, match marks and episode details fill in by themselves."""
        session = self.get_session()
        if session is None:
            self.no_auto.add("*")
            return
        with contextlib.suppress(Exception):
            self.call_from_thread(self.set_times, session.playlist_times())
        shows = list(self.shows.values())
        for n, show in enumerate(shows):
            self.call_from_thread(self.set_scan, (n, len(shows)))
            try:
                results = [session.match(show, key) for key in show.playlists]
            except ShowNotFoundError:
                self.no_auto.add(show.slug)
                self.call_from_thread(self.mark_absent, show)
                continue
            except Exception as exc:
                self.no_auto.add(show.slug)
                self.call_from_thread(
                    self.log_line,
                    f"[yellow]Couldn't check {escape(show.title)}: "
                    f"{escape(f'{type(exc).__name__}: {exc}')}[/yellow]",
                )
                continue
            self.call_from_thread(self.store_scan, show, results)
        self.call_from_thread(self.set_scan, None)

    def set_scan(self, progress: tuple[int, int] | None) -> None:
        """How far the background check has got: (shows done, shows in total), or None."""
        self.scan = progress
        self.update_subtitle(self.session.server_name if self.session else None)

    def mark_absent(self, show: Show) -> None:
        """The server doesn't have this show: dim it in the tree."""
        self.absent.add(show.slug)
        self.log_line(f"[dim]{escape(show.title)} isn't in your Plex library.[/dim]")
        node = self.nodes.get(Nav(show.slug))
        if node is not None:
            node.set_label(self.show_label(show))
        self.refresh_labels(show.slug)
        if self.current and self.current.slug == show.slug:
            self.refresh_view()

    def show_label(self, show: Show) -> Text:
        if show.slug in self.absent:
            return Text(f"{show.label}  (not in Plex)", style="dim")
        return Text(show.label, style="bold")

    def store_scan(self, show: Show, results: list[MatchResult]) -> None:
        """Results of the background check for one show. Anything checked meanwhile wins."""
        if self.shows.get(show.slug) is not show:
            return  # show files were reloaded while this ran
        for result in results:
            self.results.setdefault((show.slug, result.key), result)
        self.refresh_labels(show.slug)
        if self.current and self.current.slug == show.slug:
            self.refresh_view()

    def load_cached_times(self) -> None:
        """What the server said last time, until we connect and ask again."""
        server_id = config.load_config().get("server_id")
        self.times = cached_playlist_times(server_id) if server_id else None

    def set_times(self, times: dict[str, PlaylistTimes]) -> None:
        self.times = times
        self.refresh_view()

    def created_cell(self, show: Show, key: str) -> Text:
        t = (self.times or {}).get(show.plex_name(key))
        if t is None or t.created is None:
            return Text("—", style="dim")
        return Text(f"{t.created:%Y-%m-%d}")

    def times_summary(self, show: Show, key: str) -> str:
        if self.times is None:
            return ""
        t = self.times.get(show.plex_name(key))
        if t is None:
            return " · not in Plex"
        parts = [
            f"{label} {when:%Y-%m-%d %H:%M}"
            for label, when in (("created", t.created), ("updated", t.updated))
            if when is not None
        ]
        return "".join(f" · {p}" for p in parts)

    def update_subtitle(self, connected: str | None = None, state: str = "idle") -> None:
        """Header status: 🟢 connected, 🟡 connecting, 🔴 couldn't connect, ⚪ not connected."""
        if connected:
            checking = f" · checking shows {self.scan[0] + 1}/{self.scan[1]}…" if self.scan else ""
            self.sub_title = f"🟢 connected to {connected}{checking}"
            return
        icon, state = {
            "connecting": ("🟡", "connecting…"),
            "failed": ("🔴", "can't connect"),
        }.get(state, ("⚪", "not connected"))
        cfg = config.load_config()
        if cfg.get("client_id"):
            who = f"{cfg.get('username', '?')} @ {cfg.get('server_name', '?')}"
            self.sub_title = f"{icon} {state} · {who}"
        elif os.environ.get("PLEX_TOKEN"):
            self.sub_title = f"{icon} {state} · using PLEX_TOKEN"
        else:
            self.sub_title = "⚪ not logged in — press a"

    def load_shows(self) -> None:
        self.shows, errors = discover(config.user_shows_dir())
        for e in errors:
            self.log_line(f"[yellow]Skipping show file: {escape(e)}[/yellow]")
            self.notify(e, title="Show file error", severity="warning", timeout=8)
        self.build_tree()

    def matching_episode(self, show: Show, key: str) -> int | None:
        """Index of the first episode in a playlist whose title contains the filter text."""
        if not self.filter_text:
            return None
        titles = (show.display(e).lower() for e in show.playlists[key].episodes)
        return next((n for n, t in enumerate(titles) if self.filter_text in t), None)

    def visible_playlists(self, show: Show) -> list[str]:
        """The show's playlists that pass the filter: by show, playlist or episode name."""
        text = self.filter_text
        if not text or text in show.label.lower() or text in show.title.lower():
            return list(show.playlists)
        return [
            key
            for key, p in show.playlists.items()
            if text in key or text in p.name.lower() or self.matching_episode(show, key) is not None
        ]

    def build_tree(self) -> None:
        tree = self.query_one("#nav", Tree)
        previous = self.current
        # Shows start collapsed. A rebuild keeps the ones that were open, plus the one
        # holding the selected playlist; a filter opens everything it matched.
        opened = {n.data.slug for n in tree.root.children if n.data and n.is_expanded}
        if previous and previous.key:
            opened.add(previous.slug)
        tree.clear()
        self.nodes = {}
        for slug, show in self.shows.items():
            keys = self.visible_playlists(show)
            if not keys:
                continue
            expand = bool(self.filter_text) or slug in opened
            node = tree.root.add(self.show_label(show), data=Nav(slug), expand=expand)
            self.nodes[Nav(slug)] = node
            for key in keys:
                leaf = node.add_leaf(self.playlist_label(show, key), data=Nav(slug, key))
                self.nodes[Nav(slug, key)] = leaf
        target = self.nodes.get(previous) if previous else None
        if target is None and self.nodes:
            target = next(iter(self.nodes.values()))
        if target is not None:
            # The new nodes have no lines until the tree redraws, so the cursor can only
            # be put back afterwards. Until then, ignore the highlight of the first node.
            self.restoring = next(iter(self.nodes))
            self.show_nav(target.data)
            self.call_after_refresh(self.restore_cursor, target.data)

    def restore_cursor(self, nav: Nav) -> None:
        first, self.restoring = self.restoring, None
        tree = self.query_one("#nav", Tree)
        node = self.nodes.get(nav)  # None if the tree was rebuilt again meanwhile
        moved = tree.cursor_node is not None and tree.cursor_node.data != first
        if node is not None and not moved:  # don't undo a move made in the meantime
            tree.move_cursor(node)

    def action_toggle_fold(self) -> None:
        """Collapse every show in the tree, or expand them all if they're already collapsed."""
        tree = self.query_one("#nav", Tree)
        shows = list(tree.root.children)
        if any(node.is_expanded for node in shows):
            # A playlist's row is about to disappear: move to its show first.
            if (
                self.current
                and self.current.key
                and (node := self.nodes.get(Nav(self.current.slug)))
            ):
                tree.move_cursor(node)
            for node in shows:
                node.collapse()
        else:
            for node in shows:
                node.expand()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.remove_class("hidden")
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        if box.has_class("hidden"):
            return
        box.value = ""
        box.add_class("hidden")
        self.query_one("#nav", Tree).focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip().lower()
        self.build_tree()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.query_one("#nav", Tree).focus()

    def playlist_label(self, show: Show, key: str) -> Text:
        label = Text(show.playlists[key].name, style="dim" if show.slug in self.absent else "")
        result = self.results.get((show.slug, key))
        if result is not None:
            label.append(
                "  ✓" if not result.missing else f"  {len(result.missing)}✗",
                style="green" if not result.missing else "yellow",
            )
            label.append(f"  {self.watched_text(result)}", style="dim")
        return label

    @staticmethod
    def watched_text(result: MatchResult | None) -> str:
        """'12/27' watched, or '' before the playlist has been checked against Plex."""
        if result is None or not result.items:
            return ""
        return "{}/{}".format(*watched_progress(result))

    def refresh_labels(self, slug: str) -> None:
        show = self.shows.get(slug)
        if not show:
            return
        for key in show.playlists:
            node = self.nodes.get(Nav(slug, key))
            if node is not None:
                node.set_label(self.playlist_label(show, key))

    # ------------------------------------------------------------- views

    @on(Tree.NodeHighlighted, "#nav")
    def nav_highlighted(self, event: Tree.NodeHighlighted[Nav]) -> None:
        # Rebuilding the tree queues a highlight for its first node; only the node the
        # cursor ends up on counts.
        if self.restoring is not None and event.node.data == self.restoring:
            return
        if event.node.data is not None and event.node is event.control.cursor_node:
            self.show_nav(event.node.data)

    def show_nav(self, nav: Nav | None) -> None:
        if nav is None or nav.slug not in self.shows:
            return
        self.current = nav
        show = self.shows[nav.slug]
        if nav.key is None or nav.key not in show.playlists:
            self.render_show(show)
        else:
            self.render_playlist(show, nav.key)

    def render_show(self, show: Show) -> None:
        src = "built-in" if show.source and BUILTIN_DIR in show.source.parents else str(show.source)
        absent = "  [yellow]not in your Plex library[/yellow]" if show.slug in self.absent else ""
        self.query_one("#summary", Static).update(
            f"[bold]{escape(show.title)}[/bold]  [dim]{len(show.playlists)} playlists · "
            f"{escape(src)}[/dim]{absent}\n"
            "[dim]c check all · b build all · p posters for all · enter opens a playlist[/dim]"
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Key", "Name in Plex", "Items", "Runtime", "Poster", "Plex", "Created", "Watched"
        )
        self.show_detail(None)
        folder = config.posters_dir(show.slug)
        for key, p in show.playlists.items():
            table.add_row(
                key,
                show.plex_name(key),
                str(len(p.episodes)),
                hours(show.runtime(key)),
                "✓" if find_image(folder, key) else Text("—", style="dim"),
                Text.from_markup(self.plex_cell(show, key)),
                self.created_cell(show, key),
                self.watched_text(self.results.get((show.slug, key))) or Text("—", style="dim"),
                key=key,
            )

    def plex_cell(self, show: Show, key: str) -> str:
        parts = []
        if (result := self.results.get((show.slug, key))) is not None:
            parts.append(match_summary(result))
        if action := self.actions.get((show.slug, key)):
            parts.append(escape(action))
        return " · ".join(parts) or "[dim]not checked[/dim]"

    def render_playlist(self, show: Show, key: str) -> None:
        p = show.playlists[key]
        result = self.results.get((show.slug, key))
        watched = f" · {w} watched" if (w := self.watched_text(result)) else ""
        poster = find_image(config.posters_dir(show.slug), key)
        poster_s = f"poster: {escape(poster.name)}" if poster else "no poster"
        self.query_one("#summary", Static).update(
            f"[bold]{escape(show.plex_name(key))}[/bold]\n"
            f"{escape(p.description)}\n"
            f"[dim]{len(p.episodes)} items · {hours(show.runtime(key))} · {poster_s}"
            f"{self.times_summary(show, key)}{watched} · Plex: [/dim]{self.plex_cell(show, key)}"
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "S", "E", "Title", "Note", "In Plex", "Aired", "Length", "Watched")
        blank = Text("—", style="dim")
        up_next = next_unwatched(result) if result else None
        for i, e in enumerate(p.episodes):
            status = Text.from_markup(entry_status(show, result.entries[i])) if result else blank
            d = entry_details(result.entries[i]) if result else None
            info: list[Any] = [d.aired, d.length, d.watched] if d else [""] * 3
            if i == up_next:
                info[2] = Text("▶ next", style="bold")
            table.add_row(
                str(i + 1),
                "film" if e.film else str(e.season),
                (d.number if d else "") or blank,
                show.display(e),
                Text(e.note, style="dim"),
                status,
                *(v or blank for v in info),
            )
        self.show_detail(0)
        if (found := self.matching_episode(show, key)) is not None:
            table.move_cursor(row=found)  # the episode the filter was looking for
        if result is None and self.can_auto_load(show):
            self.load_details(show, key)

    def can_auto_load(self, show: Show) -> bool:
        """Whether to fetch episode details unasked: only with a connection or a saved login."""
        if self.no_auto & {"*", show.slug}:
            return False
        return self.session is not None or has_login()

    @on(DataTable.RowHighlighted, "#table")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail(event.cursor_row)

    def show_detail(self, row: int | None) -> None:
        """Under the table: Plex's summary of the highlighted episode, once it's known."""
        detail = self.query_one("#detail", Static)
        nav = self.current
        result = self.results.get((nav.slug, nav.key)) if nav and nav.key else None
        entry = None
        if result is not None and row is not None and 0 <= row < len(result.entries):
            entry = result.entries[row]
        d = entry_details(entry) if entry else None
        if nav is None or entry is None or d is None or not d.summary:
            detail.add_class("hidden")
            return
        title = self.shows[nav.slug].display(entry.ep)
        facts = " · ".join(v for v in (d.episode, d.aired, d.length) if v)
        detail.update(f"[bold]{escape(title)}[/bold]  [dim]{facts}[/dim]\n{escape(d.summary)}")
        detail.remove_class("hidden")

    def refresh_view(self) -> None:
        """Redraw the current view in place, keeping the table cursor where it was."""
        table = self.query_one("#table", DataTable)
        row = table.cursor_row
        self.show_nav(self.current)
        if 0 < row < table.row_count:
            table.move_cursor(row=row)

    @on(DataTable.RowSelected, "#table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        if self.current and self.current.key is None and event.row_key.value:
            node = self.nodes.get(Nav(self.current.slug, str(event.row_key.value)))
            if node is not None:
                if node.parent is not None:
                    node.parent.expand()  # its row only exists once the show is open
                self.show_nav(node.data)
                self.call_after_refresh(self.query_one("#nav", Tree).move_cursor, node)

    def log_line(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    # ------------------------------------------------------------- targets

    def targets(self) -> tuple[Show, list[str]] | None:
        """The show and playlist keys the current selection refers to."""
        if self.current is None or self.current.slug not in self.shows:
            self.notify("Select a show or playlist first.", severity="warning")
            return None
        show = self.shows[self.current.slug]
        keys = [self.current.key] if self.current.key else list(show.playlists)
        return show, keys

    # ------------------------------------------------------------- Plex work (threads)

    def get_session(self) -> Session | None:
        """Called from worker threads."""
        with self.connect_lock:  # two workers asking at once share one connection
            if self.session is not None:
                return self.session
            self.call_from_thread(self.log_line, "Connecting to Plex…")
            self.call_from_thread(self.update_subtitle, None, "connecting")
            try:
                self.session = connect_session()
            except auth.AuthError as exc:
                self.call_from_thread(self.report_error, str(exc), "Can't connect")
                self.call_from_thread(self.update_subtitle, None, "failed")
                return None
            except Exception as exc:
                self.call_from_thread(
                    self.report_error, f"{type(exc).__name__}: {exc}", "Can't connect"
                )
                self.call_from_thread(self.update_subtitle, None, "failed")
                return None
            self.call_from_thread(self.connected, self.session.server_name)
            return self.session

    def connected(self, server_name: str) -> None:
        self.update_subtitle(server_name)
        self.log_line(f"Connected to {escape(server_name)}.")

    def report_error(self, message: str, title: str = "Error") -> None:
        self.log_line(f"[red]{escape(message)}[/red]")
        plain = Text.from_markup(message).plain
        self.notify(plain, title=title, severity="error", timeout=10)

    def action_check(self) -> None:
        if t := self.targets():
            self.run_plex(t[0], t[1], build=False)

    def action_build(self) -> None:
        if t := self.targets():
            self.run_plex(t[0], t[1], build=True)

    @work(thread=True, exclusive=True, group="plex")
    def run_plex(self, show: Show, keys: list[str], build: bool) -> None:
        session = self.get_session()
        if session is None:
            return
        verb = "Building" if build else "Checking"
        self.call_from_thread(self.log_line, f"[bold]{verb}[/bold] {len(keys)} playlist(s)…")
        try:
            session.library(show)
            existing = session.existing_playlists() if build else None
            for key in keys:
                result = session.match(show, key)
                out: ApplyResult | None = None
                if build:
                    out = session.apply(show, result, existing=existing)
                self.call_from_thread(self.store_result, show, result, out)
            self.call_from_thread(self.set_times, session.playlist_times())
        except Exception as exc:
            self.call_from_thread(self.report_error, f"{type(exc).__name__}: {exc}")
            return
        missing = sum(len(self.results[(show.slug, k)].missing) for k in keys)
        done = "Built" if build else "Checked"
        msg = f"{done} {len(keys)} playlist(s)" + (f", {missing} missing" if missing else "")
        self.call_from_thread(self.notify, msg, severity="warning" if missing else "information")

    @work(thread=True, exclusive=True, group="details")
    def load_details(self, show: Show, key: str) -> None:
        """Match one playlist quietly so its episode details can be shown."""
        session = self.get_session()
        if session is None:
            self.no_auto.add("*")
            return
        try:
            result = session.match(show, key)
        except Exception as exc:
            self.no_auto.add(show.slug)
            self.call_from_thread(self.report_error, f"{type(exc).__name__}: {exc}")
            return
        self.call_from_thread(self.store_result, show, result, None, True)

    def store_result(
        self, show: Show, result: MatchResult, out: ApplyResult | None, quiet: bool = False
    ) -> None:
        self.results[(show.slug, result.key)] = result
        if out is not None:
            self.actions[(show.slug, result.key)] = (out.skipped and "skipped") or out.action
        for line in [] if quiet else result_lines(show, result, out):
            self.log_line(line)
        self.refresh_labels(show.slug)
        self.refresh_view()

    def action_remove(self) -> None:
        if not (t := self.targets()):
            return
        show, keys = t
        names = "\n".join(f"• {escape(show.plex_name(k))}" for k in keys)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.do_remove(show, keys)

        self.push_screen(
            ConfirmScreen(
                f"Delete {len(keys)} playlist(s) from Plex?",
                f"{names}\n\nEpisodes and watch history aren't affected.",
                yes="Delete",
            ),
            confirmed,
        )

    @work(thread=True, exclusive=True, group="plex")
    def do_remove(self, show: Show, keys: list[str]) -> None:
        session = self.get_session()
        if session is None:
            return
        try:
            targets = session.find_playlists(show, keys)
            for pl in targets:
                pl.delete()
            self.call_from_thread(self.set_times, session.playlist_times())
        except Exception as exc:
            self.call_from_thread(self.report_error, f"{type(exc).__name__}: {exc}")
            return

        def done() -> None:
            for k in keys:
                self.actions[(show.slug, k)] = "removed"
            self.log_line(f"Deleted {len(targets)} playlist(s) from Plex.")
            self.notify(f"Deleted {len(targets)} playlist(s).")
            self.refresh_view()

        self.call_from_thread(done)

    # ------------------------------------------------------------- local actions

    def action_poster(self) -> None:
        if not (t := self.targets()):
            return
        show, keys = t
        folder = config.posters_dir(show.slug)
        existing = [k for k in keys if find_image(folder, k)]

        def go(overwrite: bool | None) -> None:
            todo = keys if overwrite else [k for k in keys if k not in existing]
            if todo:
                self.make_posters(show, todo, folder)
            else:
                self.notify("Nothing to do: every poster already exists.")

        if existing:
            names = ", ".join(existing)
            self.push_screen(
                ConfirmScreen(
                    "Replace existing posters?",
                    f"{escape(names)} already have an image in {escape(str(folder))}.",
                    yes="Replace",
                ),
                go,
            )
        else:
            go(False)

    @work(thread=True, exclusive=True, group="posters")
    def make_posters(self, show: Show, keys: list[str], folder: Path) -> None:
        from plexlists.posters import import_image, render_poster

        session = self.session
        try:  # artwork from Plex is a bonus: without a connection, write plain cards
            if session is None:
                session = self.session = connect_session()
                self.call_from_thread(self.connected, session.server_name)
            session.library(show)
        except Exception:
            session = None
        plain = 0
        for k in keys:
            backdrop = None
            if session is not None:
                with contextlib.suppress(Exception):
                    backdrop = session.backdrop(show, k)
            plain += backdrop is None
            render_poster(show, k, folder / f"{k}.jpg", backdrop=backdrop)
            if backdrop and not find_image(folder, f"{k}-art"):  # background art, if none yet
                import_image(backdrop, folder / f"{k}-art.jpg", art=True)
        note = f" {plain} without artwork from Plex." if plain else ""
        self.call_from_thread(
            self.log_line,
            f"Wrote {len(keys)} poster(s) to {escape(str(folder))}.{note} Build to upload them.",
        )
        self.call_from_thread(self.notify, f"Generated {len(keys)} poster(s).")
        self.call_from_thread(self.refresh_view)

    def action_view_poster(self) -> None:
        """Open the selected playlist's poster in the system image viewer."""
        nav = self.current
        if nav is None or nav.key is None:
            self.notify("Select a playlist to view its poster.", severity="warning")
            return
        poster = find_image(config.posters_dir(nav.slug), nav.key)
        if poster is None:
            self.notify("No poster yet. Press p to generate one.", severity="warning")
            return
        typer.launch(str(poster))
        self.notify(f"Opened {poster.name}")

    def action_open_posters(self) -> None:
        if self.current is None:
            return
        folder = config.posters_dir(self.current.slug)
        folder.mkdir(parents=True, exist_ok=True)
        typer.launch(str(folder))
        self.notify(f"Opened {folder}")

    def action_edit_show(self) -> None:
        if self.current is None or self.current.slug not in self.shows:
            return
        show = self.shows[self.current.slug]
        path = show.source
        if path is None:
            return
        if BUILTIN_DIR in path.parents:
            dest = config.user_shows_dir() / path.name
            if not dest.exists():
                config.ensure_private_dir(dest.parent)
                shutil.copyfile(path, dest)
                self.notify(f"Copied the built-in show to {dest} so your edits are kept.")
            path = dest
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with self.suspend():
            subprocess.run([*editor.split(), str(path)], check=False)
        self.action_reload()

    def action_reload(self) -> None:
        """Re-read show files and forget cached Plex episode lists."""
        self.results.clear()
        self.actions.clear()
        self.no_auto.clear()
        self.absent.clear()
        if self.session is not None:
            self.session.forget()
        self.load_shows()
        self.notify("Reloaded show files.")
        if has_login():
            self.connect_in_background()

    def action_toggle_log(self) -> None:
        self.query_one("#log").toggle_class("hidden")

    def action_account(self) -> None:
        def chosen(choice: str | None) -> None:
            if choice == "login":
                self.push_screen(LoginScreen(), self.after_login)
            elif choice == "logout":
                self.do_logout()

        self.push_screen(AccountScreen(), chosen)

    def after_login(self, ok: bool | None) -> None:
        if ok:
            self.session = None
            self.no_auto.clear()
            self.absent.clear()
            self.load_cached_times()
            self.update_subtitle()
            self.load_shows()  # redraws the tree without the old server's marks
            self.connect_in_background()

    @work(thread=True, exclusive=True, group="plex")
    def do_logout(self) -> None:
        msg = auth.logout()
        self.session = None
        self.call_from_thread(self.notify, msg)
        self.call_from_thread(self.log_line, escape(msg))
        self.call_from_thread(self.update_subtitle)


def run() -> None:
    PlexlistsApp().run()
