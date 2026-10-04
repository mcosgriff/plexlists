"""Textual TUI: browse shows and playlists, check matches, build, generate posters.

Plex calls block, so they run in thread workers and post results back to the UI.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
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
from plexlists.report import entry_status, match_summary, result_lines
from plexlists.service import (
    ApplyResult,
    MatchResult,
    PlaylistTimes,
    Session,
    cached_playlist_times,
)
from plexlists.sync import find_image


def connect_session() -> Session:
    """Connect using the saved login (or PLEX_URL/PLEX_TOKEN). Tests replace this."""
    plex = auth.connect(
        os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN"), warn=lambda _m: None
    )
    return Session(plex)


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
    #nav { width: 34; border-right: solid $panel; padding-right: 1; }
    #main { width: 1fr; }
    #summary { height: auto; padding: 0 1 1 1; }
    #table { height: 1fr; }
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
        Binding("o", "open_posters", "Posters folder"),
        Binding("e", "edit_show", "Edit show"),
        Binding("x", "remove", "Remove"),
        Binding("r", "reload", "Reload"),
        Binding("a", "account", "Account"),
        Binding("g", "toggle_log", "Log", show=False),
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
        self.nodes: dict[Nav, Any] = {}
        self.current: Nav | None = None

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            tree: Tree[Nav] = Tree("Shows", id="nav")
            tree.show_root = False
            yield tree
            with Vertical(id="main"):
                yield Static(id="summary")
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        if config.import_legacy_login():
            self.notify("Imported your login from xfiles.py.")
        self.load_cached_times()
        self.load_shows()
        self.update_subtitle()
        self.query_one("#nav", Tree).focus()

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

    def update_subtitle(self, connected: str | None = None) -> None:
        if connected:
            self.sub_title = f"connected to {connected}"
            return
        cfg = config.load_config()
        if cfg.get("client_id"):
            self.sub_title = f"{cfg.get('username', '?')} @ {cfg.get('server_name', '?')}"
        elif os.environ.get("PLEX_TOKEN"):
            self.sub_title = "using PLEX_TOKEN"
        else:
            self.sub_title = "not logged in — press a"

    def load_shows(self) -> None:
        self.shows, errors = discover(config.user_shows_dir())
        for e in errors:
            self.log_line(f"[yellow]Skipping show file: {escape(e)}[/yellow]")
            self.notify(e, title="Show file error", severity="warning", timeout=8)
        tree = self.query_one("#nav", Tree)
        previous = self.current
        tree.clear()
        self.nodes = {}
        for slug, show in self.shows.items():
            node = tree.root.add(Text(show.label, style="bold"), data=Nav(slug), expand=True)
            self.nodes[Nav(slug)] = node
            for key in show.playlists:
                leaf = node.add_leaf(self.playlist_label(show, key), data=Nav(slug, key))
                self.nodes[Nav(slug, key)] = leaf
        target = self.nodes.get(previous) if previous else None
        if target is None and self.nodes:
            target = next(iter(self.nodes.values()))
        if target is not None:
            tree.move_cursor(target)
            self.show_nav(target.data)

    def playlist_label(self, show: Show, key: str) -> Text:
        label = Text(show.playlists[key].name)
        result = self.results.get((show.slug, key))
        if result is not None:
            label.append(
                "  ✓" if not result.missing else f"  {len(result.missing)}✗",
                style="green" if not result.missing else "yellow",
            )
        return label

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
        if event.node.data is not None:
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
        self.query_one("#summary", Static).update(
            f"[bold]{escape(show.title)}[/bold]  [dim]{len(show.playlists)} playlists · "
            f"{escape(src)}[/dim]\n"
            "[dim]c check all · b build all · p posters for all · enter opens a playlist[/dim]"
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Key", "Name in Plex", "Items", "Runtime", "Poster", "Plex", "Created")
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
        poster = find_image(config.posters_dir(show.slug), key)
        poster_s = f"poster: {escape(poster.name)}" if poster else "no poster"
        self.query_one("#summary", Static).update(
            f"[bold]{escape(show.plex_name(key))}[/bold]\n"
            f"{escape(p.description)}\n"
            f"[dim]{len(p.episodes)} items · {hours(show.runtime(key))} · {poster_s}"
            f"{self.times_summary(show, key)} · Plex: [/dim]{self.plex_cell(show, key)}"
        )
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "S", "Title", "Note", "In Plex")
        result = self.results.get((show.slug, key))
        for i, e in enumerate(p.episodes):
            status = (
                Text.from_markup(entry_status(show, result.entries[i]))
                if result
                else Text("—", style="dim")
            )
            table.add_row(
                str(i + 1),
                "film" if e.film else str(e.season),
                show.display(e),
                Text(e.note, style="dim"),
                status,
            )

    def refresh_view(self) -> None:
        self.show_nav(self.current)

    @on(DataTable.RowSelected, "#table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        if self.current and self.current.key is None and event.row_key.value:
            node = self.nodes.get(Nav(self.current.slug, str(event.row_key.value)))
            if node is not None:
                self.query_one("#nav", Tree).move_cursor(node)
                self.show_nav(node.data)

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
        if self.session is not None:
            return self.session
        self.call_from_thread(self.log_line, "Connecting to Plex…")
        try:
            self.session = connect_session()
        except auth.AuthError as exc:
            self.call_from_thread(self.report_error, str(exc), "Can't connect")
            return None
        except Exception as exc:
            self.call_from_thread(
                self.report_error, f"{type(exc).__name__}: {exc}", "Can't connect"
            )
            return None
        self.call_from_thread(self.update_subtitle, self.session.server_name)
        return self.session

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

    def store_result(self, show: Show, result: MatchResult, out: ApplyResult | None) -> None:
        self.results[(show.slug, result.key)] = result
        if out is not None:
            self.actions[(show.slug, result.key)] = (out.skipped and "skipped") or out.action
        for line in result_lines(show, result, out):
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
        from plexlists.posters import render_poster

        session = self.session
        try:  # artwork from Plex is a bonus: without a connection, write plain cards
            if session is None:
                session = self.session = connect_session()
                self.call_from_thread(self.update_subtitle, session.server_name)
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
        note = f" {plain} without artwork from Plex." if plain else ""
        self.call_from_thread(
            self.log_line,
            f"Wrote {len(keys)} poster(s) to {escape(str(folder))}.{note} Build to upload them.",
        )
        self.call_from_thread(self.notify, f"Generated {len(keys)} poster(s).")
        self.call_from_thread(self.refresh_view)

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
        if self.session is not None:
            self.session.forget()
        self.load_shows()
        self.notify("Reloaded show files.")

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
            self.load_cached_times()
            self.update_subtitle()
            self.refresh_view()

    @work(thread=True, exclusive=True, group="plex")
    def do_logout(self) -> None:
        msg = auth.logout()
        self.session = None
        self.call_from_thread(self.notify, msg)
        self.call_from_thread(self.log_line, escape(msg))
        self.call_from_thread(self.update_subtitle)


def run() -> None:
    PlexlistsApp().run()
