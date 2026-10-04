"""Command-line interface.

plexlists shows                         # available shows
plexlists <show> list | show | build | posters | poster | pull-posters | remove
plexlists login | status | logout | paths | new
"""

import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from plexlists import __version__, auth, config
from plexlists.models import Show, discover
from plexlists.report import result_lines
from plexlists.service import ApplyResult, MatchResult, Session, ShowNotFoundError
from plexlists.sync import find_image

console = Console()
err_console = Console(stderr=True)

CONTEXT = {"help_option_names": ["-h", "--help"]}


def hours(minutes: int) -> str:
    return f"~{round(minutes / 60)}h"


def fail(exc: Exception | str) -> typer.Exit:
    err_console.print(f"[red]{exc}[/red]")
    return typer.Exit(1)


def connect_plex(url: str | None, token: str | None) -> Any:
    """Connect or exit with a readable error. Tests replace this function."""
    try:
        return auth.connect(url, token, warn=lambda m: err_console.print(f"[yellow]{m}[/yellow]"))
    except auth.AuthError as exc:
        raise fail(exc) from None
    except Exception as exc:
        raise fail(f"Couldn't connect to Plex: {type(exc).__name__}: {escape(str(exc))}") from None


def connect_user(plex: Any, user: str) -> Any:
    """Connect to the same server as another user, or exit. Tests replace this function."""
    try:
        return auth.connect_as(plex, user)
    except auth.AuthError as exc:
        raise fail(exc) from None
    except Exception as exc:
        raise fail(
            f"Couldn't connect as {escape(user)}: {type(exc).__name__}: {escape(str(exc))}"
        ) from None


def sessions_for(plex: Any, users: list[str] | None) -> list[tuple[str | None, Session]]:
    """(user, session) to act on: you (None) by default, otherwise each named user."""
    if not users:
        return [(None, Session(plex))]
    return [(u, Session(connect_user(plex, u))) for u in users]


def print_result(show: Show, result: MatchResult, out: ApplyResult) -> None:
    for line in result_lines(show, result, out):
        console.print(line)


# --------------------------------------------------------------------- shared options

UrlOpt = Annotated[
    str | None,
    typer.Option(
        envvar=["PLEX_URL", "PLEXAPI_AUTH_SERVER_BASEURL"],
        help="Connect to this server URL instead of discovering it from your account. "
        "Prefer the https://….plex.direct:32400 address.",
        show_default=False,
        rich_help_panel="Plex connection",
    ),
]
TokenOpt = Annotated[
    str | None,
    typer.Option(
        envvar=["PLEX_TOKEN", "PLEXAPI_AUTH_SERVER_TOKEN"],
        help="Legacy Plex token to use instead of [cyan]login[/cyan] (requires --url). "
        "Not recommended: it never expires, and on the command line it's visible to "
        "other processes.",
        show_default=False,
        rich_help_panel="Plex connection",
    ),
]
AllOpt = Annotated[bool, typer.Option("--all", "-a", help="Every playlist instead of naming them.")]
CollectionOpt = Annotated[
    bool,
    typer.Option(
        "--collection",
        "-c",
        help="Work with collections in the show's library instead of playlists. Every "
        "user of the server sees a collection; films from a movie library are left out.",
    ),
]
UsersOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--user",
        "-u",
        help="Act on this user's playlists instead of yours: a managed or shared user of "
        "your Plex account. Repeat for several. Only the server's owner can do this.",
        show_default=False,
    ),
]
PostersOpt = Annotated[
    Path | None,
    typer.Option(
        "--posters-dir",
        help="Folder of poster/art images named by playlist key "
        "[dim](default: <config dir>/posters/<show>)[/dim].",
        file_okay=False,
        show_default=False,
    ),
]


# --------------------------------------------------------------------- per-show commands


def title_env(show: Show) -> str:
    return f"PLEXLISTS_{show.slug.upper().replace('-', '_')}_TITLE"


def make_show_app(show: Show) -> typer.Typer:
    keys_help = ", ".join(show.playlists)
    app = typer.Typer(
        help=f"Playlists for [bold]{escape(show.title)}[/bold] ({len(show.playlists)} playlists).",
        no_args_is_help=True,
        rich_markup_mode="rich",
        context_settings=CONTEXT,
    )

    def resolve(keys: list[str] | None, all_: bool) -> list[str]:
        keys = list(show.playlists) if all_ else (keys or [])
        if not keys:
            raise typer.BadParameter("Name one or more playlists, or use --all.")
        bad = [k for k in keys if k not in show.playlists]
        if bad:
            raise typer.BadParameter(
                f"Unknown playlist(s): {', '.join(bad)}. Run `plexlists {show.slug} list`."
            )
        return keys

    def complete_keys(incomplete: str) -> list[tuple[str, str]]:
        return [(k, p.name) for k, p in show.playlists.items() if k.startswith(incomplete)]

    KeysArg = Annotated[  # noqa: N806  (type alias)
        list[str] | None,
        typer.Argument(
            help=f"One or more playlist keys: {keys_help}. Omit when using --all.",
            autocompletion=complete_keys,
            show_default=False,
        ),
    ]

    @app.command("list")
    def list_cmd() -> None:
        """
        Show this show's playlists.

        Each playlist's key (what you pass to the other commands), its name in
        Plex, item count, approximate runtime, whether a poster image exists, and
        a short description. Doesn't need a Plex connection.
        """
        folder = config.posters_dir(show.slug)
        t = Table(title=f"{escape(show.label)} playlists")
        t.add_column("Key", style="bold cyan")
        t.add_column("Name in Plex")
        t.add_column("Items", justify="right")
        t.add_column("Runtime", justify="right")
        t.add_column("Poster", justify="center")
        t.add_column("About", style="dim")
        for k, p in show.playlists.items():
            t.add_row(
                k,
                escape(show.plex_name(k)),
                str(len(p.episodes)),
                hours(show.runtime(k)),
                "✓" if find_image(folder, k) else "[dim]—[/dim]",
                escape(p.description),
            )
        console.print(t)

    @app.command("show")
    def show_cmd(
        key: Annotated[
            str,
            typer.Argument(
                help=f"Playlist key. One of: {keys_help}.",
                autocompletion=complete_keys,
                show_default=False,
            ),
        ],
    ) -> None:
        """
        List the episodes in a playlist, in viewing order with notes.

        Doesn't need a Plex connection.
        """
        k = resolve([key], False)[0]
        p = show.playlists[k]
        t = Table(
            title=f"{escape(show.plex_name(k))} — {len(p.episodes)} items, {hours(show.runtime(k))}"
        )
        t.add_column("#", justify="right", style="dim")
        t.add_column("S", justify="right")
        t.add_column("Title", style="bold")
        t.add_column("Note", style="dim")
        for i, e in enumerate(p.episodes, 1):
            t.add_row(
                str(i), "film" if e.film else str(e.season), escape(show.display(e)), escape(e.note)
            )
        console.print(t)

    @app.command()
    def build(
        keys: KeysArg = None,
        all_: AllOpt = False,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                "-n",
                help="Report matches and what would change, without changing Plex.",
            ),
        ] = False,
        posters_dir: PostersOpt = None,
        no_artwork: Annotated[
            bool, typer.Option("--no-artwork", help="Don't upload posters or background art.")
        ] = False,
        force_artwork: Annotated[
            bool,
            typer.Option(
                "--force-artwork",
                help="Re-upload posters and art even if unchanged (e.g. after picking a "
                "different poster in Plex Web).",
            ),
        ] = False,
        show_title: Annotated[
            str,
            typer.Option(
                envvar=title_env(show),
                help="The show's title as it appears in your Plex TV library.",
                rich_help_panel="Plex connection",
            ),
        ] = show.title,
        users: UsersOpt = None,
        collection: CollectionOpt = False,
        url: UrlOpt = None,
        token: TokenOpt = None,
    ) -> None:
        """
        Create or update playlists in Plex.

        Matches each playlist's episodes by title (and any films in your movie
        libraries), then creates the playlist or updates an existing one in place:
        adds, removes and reorders items without recreating it, so its poster and
        art stay. Unchanged playlists aren't touched.

        Also uploads [bold]<key>.jpg[/bold] / [bold]<key>-art.jpg[/bold] from the posters
        folder when they're new or changed. Episodes that can't be found are listed
        with the closest titles in your library.
        """
        keys = resolve(keys, all_)
        if collection and users:
            raise typer.BadParameter("A collection is shared by every user: drop --user.")

        with console.status("Connecting to Plex..."):
            targets = sessions_for(connect_plex(url, token), users)

        total_missing = 0
        for user, session in targets:
            if user:
                console.print(f"\n[bold]For {escape(user)}[/bold]")
            try:
                session.library(show, show_title)
            except ShowNotFoundError as exc:
                raise fail(
                    f"{escape(str(exc))} Pass --show-title or set {title_env(show)} "
                    "to the title Plex shows."
                ) from None
            existing = session.existing_playlists()
            for key in keys:
                result = session.match(show, key, show_title)
                total_missing += len(result.missing)
                if collection:
                    out = session.apply_collection(
                        show,
                        result,
                        title=show_title,
                        dry_run=dry_run,
                        artwork=not no_artwork,
                        force_artwork=force_artwork,
                        posters_dir=posters_dir,
                    )
                else:
                    out = session.apply(
                        show,
                        result,
                        dry_run=dry_run,
                        artwork=not no_artwork,
                        force_artwork=force_artwork,
                        posters_dir=posters_dir,
                        existing=existing,
                    )
                print_result(show, result, out)

        if total_missing:
            src = show.source or f"{show.slug}.toml"
            console.print(
                "\n[dim]Missing episodes usually mean Plex titled them differently, or they're "
                f"from seasons you don't have. To fix titles, edit {escape(str(src))} "
                f"(or copy it to {escape(str(config.user_shows_dir()))}/ and edit that).[/dim]"
            )

    @app.command()
    def posters(
        keys: KeysArg = None,
        all_: AllOpt = False,
        posters_dir: PostersOpt = None,
        force: Annotated[
            bool,
            typer.Option("--force", "-f", help="Overwrite existing images, including your own."),
        ] = False,
        plain: Annotated[
            bool,
            typer.Option("--plain", help="Don't use artwork from Plex: text on a gradient only."),
        ] = False,
        no_art: Annotated[
            bool, typer.Option("--no-art", help="Don't write background art, only posters.")
        ] = False,
        show_title: Annotated[
            str,
            typer.Option(
                envvar=title_env(show),
                help="The show's title as it appears in your Plex TV library.",
                rich_help_panel="Plex connection",
            ),
        ] = show.title,
        url: UrlOpt = None,
        token: TokenOpt = None,
    ) -> None:
        """
        Generate title-card posters.

        Writes a square [bold]<key>.jpg[/bold] to the posters folder: the playlist name,
        description and runtime over artwork from your Plex library. The artwork is
        the playlist's [bold]poster_from[/bold] in the show file (an episode title, a film
        key or "show"), otherwise the still of its first episode. With --plain, or
        when Plex can't be reached, the background is a gradient in the show's colors.

        The same artwork, without the text, is also saved as the 16:9 background
        [bold]<key>-art.jpg[/bold] if the playlist doesn't have one (or with --force).

        Playlists that already have an image are skipped unless you pass --force,
        so your own artwork is safe. Run [cyan]build[/cyan] afterwards to upload them.
        """
        from plexlists.posters import import_image, render_poster

        keys = resolve(keys, all_)
        folder = posters_dir or config.posters_dir(show.slug)
        todo = []
        for k in keys:
            img = find_image(folder, k)
            if img and not force:
                console.print(f"[dim]skip {k} ({img.name} exists)[/dim]")
            else:
                todo.append(k)

        session = None
        if todo and not plain:
            try:
                with console.status("Connecting to Plex..."):
                    session = Session(connect_plex(url, token))
                    session.library(show, show_title)
            except (typer.Exit, ShowNotFoundError) as exc:
                if isinstance(exc, ShowNotFoundError):
                    err_console.print(f"[red]{escape(str(exc))}[/red]")
                session = None
                err_console.print("[yellow]Writing plain title cards instead.[/yellow]")

        for k in todo:
            backdrop, note = None, ""
            if session is not None:
                try:
                    backdrop = session.backdrop(show, k, show_title)
                    note = "" if backdrop else " [dim](no artwork in Plex: plain)[/dim]"
                except Exception as exc:
                    note = f" [yellow](artwork failed: {type(exc).__name__}: plain)[/yellow]"
            render_poster(show, k, folder / f"{k}.jpg", backdrop=backdrop)
            console.print(f"[green]wrote[/green] {escape(str(folder / (k + '.jpg')))}{note}")
            if backdrop and not no_art and (force or not find_image(folder, f"{k}-art")):
                import_image(backdrop, folder / f"{k}-art.jpg", art=True)
                console.print(f"[green]wrote[/green] {escape(str(folder / (k + '-art.jpg')))}")
        if todo:
            target = "--all" if all_ else " ".join(keys)
            console.print(
                f"\nRun [cyan]plexlists {show.slug} build {target}[/cyan] to upload them."
            )

    @app.command("pull-posters")
    def pull_posters(
        keys: KeysArg = None,
        all_: AllOpt = False,
        posters_dir: PostersOpt = None,
        force: Annotated[
            bool, typer.Option("--force", "-f", help="Replace images in the posters folder.")
        ] = False,
        url: UrlOpt = None,
        token: TokenOpt = None,
    ) -> None:
        """
        Save posters and art you chose in Plex into the posters folder.

        If you picked or uploaded a playlist's artwork in Plex itself, this copies it
        to [bold]<key>.jpg[/bold] / [bold]<key>-art.jpg[/bold] so it's kept with your other
        posters and can be uploaded to another server. Plex's automatic collage
        isn't saved.
        """
        keys = resolve(keys, all_)
        folder = posters_dir or config.posters_dir(show.slug)
        with console.status("Connecting to Plex..."):
            session = Session(connect_plex(url, token))
            existing = session.existing_playlists()
        for k in keys:
            try:
                lines = session.pull_artwork(show, k, folder, force=force, existing=existing)
            except Exception as exc:
                lines = [f"[yellow]failed: {escape(f'{type(exc).__name__}: {exc}')}[/yellow]"]
            console.print(f"[bold]{escape(show.plex_name(k))}[/bold]: {', '.join(lines)}")

    @app.command()
    def poster(
        key: Annotated[
            str,
            typer.Argument(
                help=f"Playlist key: {keys_help}.",
                autocompletion=complete_keys,
                show_default=False,
            ),
        ],
        source: Annotated[
            str, typer.Argument(help="An image file or an http(s) URL.", show_default=False)
        ],
        art: Annotated[
            bool,
            typer.Option("--art", help="Use it as the 16:9 background art, not the poster."),
        ] = False,
        posters_dir: PostersOpt = None,
        force: Annotated[
            bool, typer.Option("--force", "-f", help="Replace an image that's already there.")
        ] = False,
    ) -> None:
        """
        Use your own image as a playlist's poster or background art.

        Crops the image from its center to a square (or to 16:9 with --art), resizes
        it and saves it under the right name in the posters folder. Run
        [cyan]build[/cyan] afterwards to upload it.
        """
        from PIL import UnidentifiedImageError

        from plexlists.posters import import_image, read_source

        (key,) = resolve([key], False)
        folder = posters_dir or config.posters_dir(show.slug)
        stem = f"{key}-art" if art else key
        old = find_image(folder, stem)
        if old and not force:
            raise fail(f"{escape(str(old))} already exists. Pass --force to replace it.")
        try:
            import_image(read_source(source), folder / f"{stem}.jpg", art=art)
        except UnidentifiedImageError:
            raise fail(f"{escape(source)} isn't an image Pillow can read.") from None
        except (OSError, ValueError) as exc:
            raise fail(f"Couldn't read {escape(source)}: {escape(str(exc))}") from None
        console.print(f"[green]wrote[/green] {escape(str(folder / (stem + '.jpg')))}")
        console.print(f"Run [cyan]plexlists {show.slug} build {key}[/cyan] to upload it.")

    @app.command()
    def remove(
        keys: KeysArg = None,
        all_: AllOpt = False,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")
        ] = False,
        users: UsersOpt = None,
        collection: CollectionOpt = False,
        show_title: Annotated[
            str,
            typer.Option(
                envvar=title_env(show),
                help="The show's title as it appears in your Plex TV library.",
                rich_help_panel="Plex connection",
            ),
        ] = show.title,
        url: UrlOpt = None,
        token: TokenOpt = None,
    ) -> None:
        """
        Delete playlists from Plex.

        Only touches playlists (or, with --collection, collections in the show's
        library) whose name exactly matches one of this show's playlists.
        Episodes and watch history aren't affected.
        """
        keys = resolve(keys, all_)
        if collection and users:
            raise typer.BadParameter("A collection is shared by every user: drop --user.")
        kind = "collection" if collection else "playlist"
        targets: list[tuple[str | None, Any]] = []
        with console.status("Connecting to Plex..."):
            for user, session in sessions_for(connect_plex(url, token), users):
                if collection:
                    try:
                        cols = session.collections(show, show_title)
                    except ShowNotFoundError as exc:
                        raise fail(escape(str(exc))) from None
                    names = [show.plex_name(k) for k in keys]
                    targets += [(None, cols[n]) for n in names if n in cols]
                else:
                    targets += [(user, pl) for pl in session.find_playlists(show, keys)]
        if not targets:
            console.print(f"None of those {kind}s exist in Plex.")
            return
        for user, pl in targets:
            owner = f" [dim]for {escape(user)}[/dim]" if user else ""
            count = pl.childCount if collection else pl.leafCount
            console.print(f"  • {escape(pl.title)} [dim]({count} items)[/dim]{owner}")
        if not yes and not typer.confirm(f"Delete {len(targets)} {kind}(s) from Plex?"):
            raise typer.Abort()
        for _, pl in targets:
            pl.delete()
        console.print(f"[green]Deleted {len(targets)} {kind}(s).[/green]")

    return app


# --------------------------------------------------------------------- root app

APP_HELP = """
Build curated TV show playlists in your Plex library.

Run [cyan]plexlists[/cyan] with no command for the interactive app, or use the commands
below for scripting.

Episodes are matched by [bold]title[/bold], not SxxEyy numbers, so DVD vs. aired order
doesn't matter, and two-parters work as separate parts or one combined file.
Rebuilding updates playlists [bold]in place[/bold], so custom posters survive.

Shows are TOML files: built-in ones ship with plexlists, and you can add your
own to [cyan]<config dir>/shows/[/cyan]. See [cyan]plexlists new[/cyan] and
[cyan]plexlists paths[/cyan].
"""

APP_EPILOG = """
[bold]Getting started[/bold]

  plexlists login                      [dim]# once: approve this device in Plex[/dim]
  plexlists shows                      [dim]# what's available[/dim]
  plexlists tng build --all --dry-run  [dim]# check which episodes match[/dim]
  plexlists tng posters --all          [dim]# optional: generate title-card posters[/dim]
  plexlists tng build --all

[bold]Environment variables[/bold] (all optional)

  [bold]PLEX_URL[/bold] / [bold]PLEX_TOKEN[/bold]           server URL / legacy token
                                  instead of login
  [bold]PLEXLISTS_<SHOW>_TITLE[/bold]          show title in Plex, e.g. PLEXLISTS_TNG_TITLE
  [bold]PLEXLISTS_CONFIG_DIR[/bold]            use a different config directory
"""


def version_callback(value: bool) -> None:
    if value:
        console.print(f"plexlists {__version__}")
        raise typer.Exit()


def make_app() -> typer.Typer:
    shows, errors = discover(config.user_shows_dir())

    app = typer.Typer(
        help=APP_HELP,
        epilog=APP_EPILOG,
        rich_markup_mode="rich",
        no_args_is_help=False,
        context_settings=CONTEXT,
    )

    @app.callback(invoke_without_command=True)
    def root(
        ctx: typer.Context,
        version: Annotated[
            bool | None,
            typer.Option(
                "--version",
                "-V",
                callback=version_callback,
                is_eager=True,
                help="Show the version and exit.",
            ),
        ] = None,
    ) -> None:
        if config.import_legacy_login():
            err_console.print(
                "[dim]Imported your login from xfiles.py; no need to log in again.[/dim]"
            )
        if ctx.invoked_subcommand is None:
            from plexlists.tui import run

            run()  # the TUI reports show file errors itself
            raise typer.Exit()
        for e in errors:
            err_console.print(f"[yellow]Skipping show file: {escape(e)}[/yellow]")

    for slug, show in shows.items():
        app.add_typer(make_show_app(show), name=slug, rich_help_panel="Shows")

    @app.command(rich_help_panel="General")
    def tui() -> None:
        """Open the interactive app (same as running [cyan]plexlists[/cyan] with no command)."""
        from plexlists.tui import run

        run()

    @app.command("shows", rich_help_panel="General")
    def shows_cmd() -> None:
        """List available shows and where each one is defined."""
        t = Table(title="Shows")
        t.add_column("Show", style="bold cyan")
        t.add_column("Title in Plex")
        t.add_column("Playlists", justify="right")
        t.add_column("Source", style="dim")
        builtin = Path(__file__).parent
        for slug, s in shows.items():
            src = "built-in" if s.source and builtin in s.source.parents else str(s.source)
            t.add_row(slug, escape(s.title), str(len(s.playlists)), escape(src))
        console.print(t)
        console.print(
            f'[dim]Add your own: plexlists new <name> --title "…"  '
            f"(saved in {escape(str(config.user_shows_dir()))})[/dim]"
        )

    @app.command(rich_help_panel="General")
    def new(
        slug: Annotated[
            str,
            typer.Argument(
                help="Short name for the command line, e.g. [cyan]ds9[/cyan].", show_default=False
            ),
        ],
        title: Annotated[
            str,
            typer.Option(
                "--title", "-t", help="The show's title as it appears in Plex.", show_default=False
            ),
        ],
        copy: Annotated[
            str | None,
            typer.Option(
                help="Start from an existing show's file instead of the template "
                "(e.g. to customize a built-in show).",
                show_default=False,
            ),
        ] = None,
    ) -> None:
        """
        Create a show file to fill in with playlists.

        Writes [bold]<config dir>/shows/<slug>.toml[/bold] from a commented template,
        or from an existing show with --copy. A user file with the same name as a
        built-in show replaces it.
        """
        from plexlists.models import KEY_RE

        if not KEY_RE.match(slug):
            raise fail("Name must be lowercase letters, digits, - or _.")
        dest = config.user_shows_dir() / f"{slug}.toml"
        if dest.exists():
            raise fail(f"{dest} already exists.")
        if copy:
            if copy not in shows or shows[copy].source is None:
                raise fail(f"Unknown show '{copy}'. See `plexlists shows`.")
            src_path = shows[copy].source
            assert src_path is not None
            text = src_path.read_text(encoding="utf-8")
        else:
            text = TEMPLATE.replace("{title}", title.replace('"', '\\"'))
        config.ensure_private_dir(dest.parent)
        dest.write_text(text, encoding="utf-8")
        console.print(f"[green]Created[/green] {escape(str(dest))}")
        console.print(f"Edit it, then try [cyan]plexlists {slug} list[/cyan].")

    @app.command(rich_help_panel="General")
    def paths() -> None:
        """Show where plexlists keeps its config, show files and posters."""
        t = Table(show_header=False, box=None)
        t.add_column(style="bold")
        t.add_column()
        t.add_row("Config dir", str(config.app_dir()))
        t.add_row("Your shows", str(config.user_shows_dir()))
        t.add_row("Posters", str(config.app_dir() / "posters") + "/<show>/")
        t.add_row("Built-in shows", str(Path(__file__).parent / "shows"))
        console.print(t)

    # ---------------------------------------------------------------- auth commands

    @app.command(rich_help_panel="Account")
    def login(
        server: Annotated[
            str | None,
            typer.Option(
                "--server",
                "-s",
                help="Name of the Plex server to use. You'll be asked if there's more than one.",
                show_default=False,
            ),
        ] = None,
        store: Annotated[
            config.Store,
            typer.Option(
                help="Where to keep the device key and token. [bold]keyring[/bold]: macOS Keychain "
                "or Linux Secret Service. [bold]file[/bold]: a 0600 file in the config "
                "directory, for headless Linux machines."
            ),
        ] = config.Store.keyring,
        browser: Annotated[
            bool,
            typer.Option(
                "--browser/--no-browser",
                help="Open the approval page in your browser. Use --no-browser on a headless "
                "machine and open the printed link on any device.",
            ),
        ] = True,
        timeout: Annotated[
            int, typer.Option(help="Seconds to wait for you to approve the device.", min=30)
        ] = 300,
    ) -> None:
        """
        Authorize this machine with your Plex account (one time).

        Generates a device key locally, then you approve the device on plex.tv.
        The key and a 7-day token are stored in your OS keychain, and the token
        renews automatically. This device appears as [bold]plexlists (<hostname>)[/bold]
        under Plex Account → Authorized Devices, where you can revoke it anytime.

        Running login again replaces (and revokes) the previous device.
        """
        if store == config.Store.keyring and not config.keyring_backend()[0]:
            raise fail(
                "No OS keychain is available on this machine.\n"
                "  Linux desktop: make sure GNOME Keyring or KWallet is running and unlocked.\n"
                "  Headless Linux: use [cyan]plexlists login --store file[/cyan]."
            )
        try:
            pending = auth.begin_login(timeout)
            console.print("\nApprove [bold]plexlists[/bold] in your Plex account:\n")
            console.print(f"  [link={pending.url}]{pending.url}[/link]\n", soft_wrap=True)
            if browser:
                typer.launch(pending.url)
            with console.status(f"Waiting for approval (up to {timeout // 60} min)..."):
                account, servers = auth.wait_for_approval(pending)
            res = pick_server(servers, server)
        except auth.AuthError as exc:
            raise fail(exc) from None

        auth.complete_login(pending, account, res, store.value)
        console.print(
            f"[green]Logged in as [bold]{escape(account.username)}[/bold] "
            f"using server [bold]{escape(res.name)}[/bold].[/green]"
        )
        with console.status("Checking HTTPS connection..."):
            try:
                plex = auth.check_connection(pending, account, res)
                console.print(f"[green]Connected securely:[/green] {plex._baseurl}")
            except auth.AuthError as exc:
                console.print(f"[yellow]Logged in, but: {exc}[/yellow]")
        where = (
            config.keyring_backend()[1]
            if store == config.Store.keyring
            else str(config.secrets_file())
        )
        console.print(f"[dim]Secrets stored in: {escape(where)}[/dim]")

    @app.command(rich_help_panel="Account")
    def logout(
        keep_device: Annotated[
            bool,
            typer.Option(
                "--keep-device",
                help="Only delete local secrets; don't revoke the device at plex.tv.",
            ),
        ] = False,
    ) -> None:
        """
        Delete stored credentials and revoke this device at plex.tv.

        Removes the device key and token from the keychain (or secrets file),
        deletes the config, and removes this device from your Plex Authorized
        Devices so nothing issued to it keeps working.
        """
        with console.status("Signing out..."):
            msg = auth.logout(keep_device)
        console.print(msg)

    @app.command(rich_help_panel="Account")
    def status(
        check: Annotated[
            bool,
            typer.Option(
                "--check",
                "-c",
                help="Also renew the token if needed and test the HTTPS connection.",
            ),
        ] = False,
    ) -> None:
        """
        Show login state: account, server, where secrets are stored, token expiry.

        Makes no network requests unless [cyan]--check[/cyan] is given.
        """
        cfg = config.load_config()
        t = Table(show_header=False, box=None)
        t.add_column(style="bold")
        t.add_column()
        t.add_row("Config", str(config.config_file()))
        if not cfg.get("client_id"):
            t.add_row("Logged in", "[yellow]no[/yellow] — run [cyan]plexlists login[/cyan]")
            console.print(t)
            return
        store = cfg.get("store", config.Store.keyring)
        secrets = config.load_secrets(store)
        t.add_row(
            "Logged in",
            f"[green]yes[/green] as {escape(str(cfg.get('username')))}"
            if secrets
            else "[red]secrets missing[/red] — run [cyan]login[/cyan]",
        )
        t.add_row("Server", escape(str(cfg.get("server_name", "?"))))
        t.add_row(
            "Device", f"{auth.DEVICE_NAME} ({socket.gethostname()})  [dim]{cfg['client_id']}[/dim]"
        )
        t.add_row(
            "Secrets in",
            config.keyring_backend()[1]
            if store == config.Store.keyring
            else f"{config.secrets_file()} (0600)",
        )
        if secrets and secrets.get("jwt"):
            exp = auth.jwt_expiry(secrets["jwt"])
            if exp:
                left = exp - datetime.now(UTC)
                when = f"{exp:%Y-%m-%d %H:%M} UTC"
                t.add_row(
                    "Token expires",
                    f"{when} ({left.days}d)"
                    if left.total_seconds() > 0
                    else f"{when} [dim](expired; renews automatically on next use)[/dim]",
                )
        for var in (
            "PLEX_URL",
            "PLEX_TOKEN",
            "PLEXAPI_AUTH_SERVER_BASEURL",
            "PLEXAPI_AUTH_SERVER_TOKEN",
        ):
            if os.environ.get(var):
                t.add_row("Override", f"[yellow]{var} is set[/yellow]")
        console.print(t)
        if check and secrets:
            with console.status("Checking..."):
                plex = connect_plex(os.environ.get("PLEX_URL"), None)
            console.print(f"[green]OK:[/green] {escape(plex.friendlyName)} at {plex._baseurl}")

    return app


def pick_server(servers: list[Any], wanted: str | None) -> Any:
    if not servers:
        raise auth.AuthError("No Plex Media Servers found on this account.")
    if wanted:
        match = [s for s in servers if s.name.lower() == wanted.lower()]
        if not match:
            names = ", ".join(s.name for s in servers)
            raise auth.AuthError(f"No server named '{wanted}'. Available: {names}")
        return match[0]
    if len(servers) == 1:
        return servers[0]
    for i, s in enumerate(servers, 1):
        shared = "" if s.owned else " [dim](shared with you)[/dim]"
        console.print(f"  [bold]{i}[/bold]. {escape(s.name)}{shared}")
    while True:
        n = typer.prompt("Which server?", type=int)
        if 1 <= n <= len(servers):
            return servers[n - 1]
        console.print(f"Enter a number from 1 to {len(servers)}.")


TEMPLATE = """# plexlists show file. The file name is the show's name on the command line.
# Episodes are matched by title, so season numbers only break ties.
# Docs: run `plexlists --help`, or look at the built-in shows (`plexlists paths`).

[show]
title = "{title}"            # exactly as it appears in your Plex TV library
label = "{title}"            # short name for tables and generated posters
prefix = "{title}: "         # playlist names in Plex start with this
episode_minutes = 44
# long_episodes = ["Pilot"]  # double-length episodes stored as one file
colors = { top = "#1b1f2a", bottom = "#05070b", accent = "#d4a017", text = "#f2f2f2" }

# Films from your movie library can be part of playlists:
# [films.the_movie]
# name = "The Movie"
# search = "Movie"           # distinctive word to search for
# year = 1999
# minutes = 120

[[playlists]]
key = "favorites"            # used on the command line
name = "Favorites"           # shown in Plex after the prefix
description = "My favorite episodes"
# poster_from = "Pilot"      # artwork for the generated poster: episode, film key or "show"
episodes = [
    [1, "Pilot", "an optional note"],     # [season, "title", "note"?]
    # [2, "Some Two-Parter (1)"],         # parts: "(1)", "Part II", "Part One" all work
    # { film = "the_movie" },
]
"""


def main() -> None:
    make_app()()
