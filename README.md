# plexlists

Build curated TV show playlists in Plex, matched by episode title.

- **Title matching:** works whether your rips use DVD or aired order. Two-parters
  match as separate parts ("Descent (1)", "Part II") or one combined file.
- **In-place updates:** rebuilding adds, removes and reorders items without
  recreating the playlist, so custom posters survive.
- **Posters:** generate title cards over artwork from your Plex library, or
  import your own image and have it cropped to fit.
- **Shows as TOML:** ships with *The X-Files*, *Star Trek: The Next Generation*,
  *Star Trek: Enterprise*, *Babylon 5*, *Defiance*, *Stargate SG-1* and
  *Stargate Atlantis*; add your own without writing Python.
- **Interactive app:** run `plexlists` with no command for a Textual TUI to browse,
  check, build and manage posters. Every action is also a CLI command for scripting.
- **Secure login:** Plex's JWT device flow, with the key in your OS keychain
  and HTTPS-only connections.

## Install

```sh
uv tool install ~/Development/plexlists     # or: uv tool install git+https://…
plexlists --help
```

From the project folder without installing: `uv run plexlists …`

## Interactive app

```sh
plexlists
```

Shows and playlists are listed on the left. Select a show to act on all of its
playlists, or select a single playlist.

The header shows the connection to Plex: 🟢 connected, ⚪ not connected yet (or
not logged in), 🔴 the last attempt failed.

Each playlist shows when it was created and last updated in Plex. Plex keeps
those times itself, per server; plexlists reads them whenever it connects and
remembers the last answer, so they're shown as soon as the app opens.

Opening a playlist lists each entry's episode number, air date, length and
whether you've watched it, with Plex's summary of the highlighted episode
underneath. With a saved login this loads by itself; otherwise press `c` once.

| Key | Action |
|---|---|
| `c` | Check: match episodes against your library without changing Plex |
| `b` | Build: create or update playlists, and upload changed posters |
| `p` | Generate title-card posters from Plex artwork (asks before replacing existing ones) |
| `o` | Open the posters folder in Finder |
| `e` | Edit the show file in `$EDITOR` (copies a built-in show to your folder first) |
| `x` | Delete playlists from Plex (asks first) |
| `r` | Reload show files and re-read your Plex library |
| `a` | Account: log in or out |
| `g` | Hide or show the log |
| `q` | Quit |

## Command line

```sh
plexlists login                       # once: approve this device on plex.tv
plexlists shows                       # available shows
plexlists tng list                    # playlists for a show
plexlists tng show borg               # episodes in a playlist
plexlists tng build --all --dry-run   # check matches and what would change
plexlists tng posters --all           # optional: generate title-card posters
plexlists tng poster borg ~/borg.png  # or use your own image (file or URL)
plexlists tng build --all             # create / update playlists in Plex
plexlists tng remove borg             # delete a playlist from Plex
plexlists status                      # login state and token expiry
```

Tab completion: `plexlists --install-completion`.

## Posters

Put images in `<config dir>/posters/<show>/`, named by playlist key:

| File | Used as |
|---|---|
| `borg.jpg` | Poster. Square, around 1000×1000. |
| `borg-art.jpg` | Background art. 16:9. |

`png` and `webp` also work. `build` uploads an image when it's new or changed.
`plexlists paths` shows where `<config dir>` is (on macOS,
`~/Library/Application Support/plexlists`).

You don't have to place the files by hand:

```sh
plexlists tng posters --all                  # title cards over artwork from Plex
plexlists tng posters --all --plain          # text on a gradient, no Plex needed
plexlists tng poster borg ~/Downloads/x.png  # your own image, cropped to a square
plexlists tng poster borg https://…/x.jpg --art   # …or to 16:9 background art
```

`posters` skips playlists that already have an image, and `poster` refuses to
replace one, unless you pass `--force`.

A generated poster uses the still of the playlist's first episode. To choose,
set `poster_from` on the playlist in the show file:

```toml
[[playlists]]
key = "borg"
poster_from = "Q Who"          # an episode title: its still
# poster_from = "first_contact"  # a key from [films]: the film's art
# poster_from = "show"           # the show's background
```

If Plex can't be reached or has no artwork, the poster falls back to the plain
gradient.

## Adding a show

```sh
plexlists new ds9 --title "Star Trek: Deep Space Nine"   # from a template
plexlists new tng --title x --copy tng                   # customize a built-in show
```

This writes `<config dir>/shows/<name>.toml`. A file there with the same name as a
built-in show replaces it. The format is documented at the top of
`src/plexlists/models.py`, and `src/plexlists/shows/` has full examples.

## Environment variables

| Variable | Purpose |
|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | Server URL / legacy token instead of `login` (not recommended) |
| `PLEXLISTS_<SHOW>_TITLE` | Show title in your Plex library, e.g. `PLEXLISTS_TNG_TITLE` |
| `PLEXLISTS_CONFIG_DIR` | Use a different config directory |

## Development

```sh
uv sync                 # create .venv with dev tools
uv run pytest           # tests (offline, against a fake Plex server; includes TUI tests)
uv run textual run --dev plexlists.tui:PlexlistsApp   # TUI with live CSS reload
uv run ruff check .     # lint
uv run ruff format .    # format
uv run ty check         # type check
```

| Module | Contents |
|---|---|
| `models.py` | Show file format and loader |
| `titles.py`, `matching.py` | Title normalization and episode matching |
| `sync.py` | Finding shows and films in Plex, in-place playlist sync, artwork upload |
| `posters.py` | Generated posters |
| `auth.py`, `config.py` | Plex login, keychain storage, config paths |
| `service.py` | Plex operations shared by the CLI and TUI |
| `report.py` | Result formatting |
| `cli.py` | Typer CLI |
| `tui.py` | Textual app |
