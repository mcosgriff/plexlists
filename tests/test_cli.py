import json
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

from plexlists import cli, config
from plexlists.matching import Library
from tests.fakeplex import FakePlaylist, FakeServer

runner = CliRunner()


def invoke(*args: str) -> str:
    result = runner.invoke(
        cli.make_app(), list(args), catch_exceptions=False, env={"COLUMNS": "300"}
    )
    return result.output


@pytest.fixture
def plex(monkeypatch: pytest.MonkeyPatch, make_server, shows):
    """Point the CLI at a fake TNG server."""
    server: FakeServer = make_server(shows["tng"])
    monkeypatch.setattr(cli, "connect_plex", lambda url, token: server)
    return server


def use(monkeypatch: pytest.MonkeyPatch, server: FakeServer) -> None:
    monkeypatch.setattr(cli, "connect_plex", lambda url, token: server)


def test_help_and_offline_commands() -> None:
    assert "tng" in invoke("--help")
    assert "Weekend Binge" in invoke("tng", "list")
    assert "First Contact" in invoke("tng", "show", "borg")
    assert "built-in" in invoke("shows")
    assert "Config dir" in invoke("paths")
    assert "plexlists" in invoke("--version")


@pytest.mark.parametrize("style", ["tmdb", "tvdb"])
@pytest.mark.parametrize("slug", ["tng", "xfiles"])
def test_build_all_then_rebuild_is_noop(monkeypatch, make_server, shows, slug, style) -> None:
    show = shows[slug]
    server = make_server(show, style)
    use(monkeypatch, server)
    out = invoke(slug, "build", "--all", "--no-artwork")
    assert "MISS" not in out
    assert len(server.pls) == len(show.playlists)
    assert all(p.summary for p in server.pls)
    out = invoke(slug, "build", "--all", "--no-artwork")
    assert out.count("unchanged") == len(show.playlists)


def test_dry_run_changes_nothing(plex) -> None:
    out = invoke("tng", "build", "--all", "--dry-run")
    assert "would create" in out
    assert plex.pls == []


def test_rebuild_repairs_in_place(plex, shows) -> None:
    invoke("tng", "build", "borg", "--no-artwork")
    pl = plex.pls[0]
    original_key = pl.ratingKey
    random.Random(1).shuffle(pl.entries)
    pl.entries.pop()
    pl.addItems([plex.show.eps[40]])
    out = invoke("tng", "build", "borg", "--no-artwork")
    assert "updated" in out
    assert len(plex.pls) == 1 and plex.pls[0].ratingKey == original_key
    lib = Library(plex.show.eps, {"first_contact": plex.movies[1]})
    want = [i.ratingKey for e in shows["tng"].playlists["borg"].episodes for i in lib.match(e)]
    assert pl.keys() == want


def test_missing_film_is_skipped_not_missing(monkeypatch, make_server, shows) -> None:
    server = make_server(shows["tng"], films=False)
    use(monkeypatch, server)
    out = invoke("tng", "build", "films", "--no-artwork")
    assert "skip Star Trek: Nemesis" in out
    assert "MISS" not in out


def test_artwork_uploads_only_when_changed(plex, tmp_path: Path) -> None:
    folder = tmp_path / "posters"
    invoke("tng", "posters", "borg", "--posters-dir", str(folder))
    assert (folder / "borg.jpg").is_file()

    assert "poster uploaded" in invoke("tng", "build", "borg", "--posters-dir", str(folder))
    assert "uploaded" not in invoke("tng", "build", "borg", "--posters-dir", str(folder))
    (folder / "borg-art.png").write_bytes((folder / "borg.jpg").read_bytes())
    assert "art uploaded" in invoke("tng", "build", "borg", "--posters-dir", str(folder))
    invoke("tng", "build", "borg", "--posters-dir", str(folder), "--force-artwork")
    pl = plex.pls[0]
    assert len(pl.posters) == 2 and len(pl.arts) == 2


def test_posters_default_folder_and_no_overwrite(config_dir: Path) -> None:
    invoke("xfiles", "posters", "--all")
    folder = config_dir / "posters" / "xfiles"
    assert len(list(folder.glob("*.jpg"))) == 10
    assert "exists" in invoke("xfiles", "posters", "funny")
    assert "✓" in invoke("xfiles", "list")


def test_miss_shows_suggestion(monkeypatch, make_server, shows) -> None:
    server = make_server(shows["tng"])
    for e in server.show.eps:
        if e.title == "Hollow Pursuits":
            e.title = "Hollow Persuits"
    use(monkeypatch, server)
    out = invoke("tng", "build", "holodeck", "--dry-run")
    assert "MISS S3 Hollow Pursuits" in out
    assert "closest in Plex: Hollow Persuits" in out


def test_smart_playlist_with_same_name_is_left_alone(plex) -> None:
    smart = FakePlaylist(plex, 1, "TNG: The Borg", [])
    smart.smart = True
    plex.pls.append(smart)
    out = invoke("tng", "build", "borg")
    assert "smart playlist" in out
    assert plex.pls == [smart]


def test_remove_only_touches_own_playlists(plex) -> None:
    invoke("tng", "build", "--all", "--no-artwork")
    plex.pls.append(FakePlaylist(plex, 1, "My Own List", []))
    out = invoke("tng", "remove", "--all", "--yes")
    assert "Deleted 11" in out
    assert [p.title for p in plex.pls] == ["My Own List"]


def test_new_show_from_template_loads(config_dir: Path) -> None:
    out = invoke("new", "ds9", "--title", "Star Trek: Deep Space Nine")
    assert "Created" in out
    assert (config_dir / "shows" / "ds9.toml").is_file()
    out = invoke("ds9", "list")
    assert "Star Trek: Deep Space Nine: Favorites" in out
    assert "already exists" in invoke("new", "ds9", "--title", "x")


def test_new_copy_overrides_builtin(config_dir: Path) -> None:
    invoke("new", "tng", "--title", "unused", "--copy", "tng")
    assert str(config_dir / "shows" / "tng.toml") in invoke("shows")
    src = config_dir / "shows" / "tng.toml"
    src.write_text(src.read_text().replace('prefix = "TNG: "', 'prefix = "Trek: "'))
    assert "Trek: Weekend Binge" in invoke("tng", "list")


def test_status_when_logged_out() -> None:
    assert "no" in invoke("status")


def test_imports_legacy_login(monkeypatch, tmp_path: Path) -> None:
    """A login made by the old xfiles.py (file store) is copied over once."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        json.dumps(
            {
                "client_id": "abc",
                "store": "file",
                "server_id": "s1",
                "server_name": "TheLab",
                "username": "matthew",
            }
        )
    )
    (legacy / "secrets.json").write_text(json.dumps({"private_key": "k", "public_key": "p"}))
    (legacy / "artwork.json").write_text(json.dumps({"m/1/poster": "hash"}))
    new = tmp_path / "new"
    monkeypatch.delenv("PLEXLISTS_CONFIG_DIR")
    monkeypatch.setattr(
        config.typer, "get_app_dir", lambda name: str(legacy if name == "xfiles" else new)
    )

    assert config.import_legacy_login() is True
    assert config.load_config()["server_name"] == "TheLab"
    assert config.load_secrets("file") == {"private_key": "k", "public_key": "p"}
    assert config.read_json(config.artwork_state_file()) == {"m/1/poster": "hash"}
    assert oct((new / "secrets.json").stat().st_mode & 0o777) == "0o600"
    assert config.import_legacy_login() is False  # only once


def test_no_command_opens_tui(monkeypatch) -> None:
    from plexlists import tui

    opened = []
    monkeypatch.setattr(tui, "run", lambda: opened.append(True))
    invoke()
    invoke("tui")
    assert opened == [True, True]


def test_build_and_remove_for_other_users(monkeypatch, plex, make_server, shows) -> None:
    theirs = {name: make_server(shows["tng"]) for name in ("alice", "bob")}
    monkeypatch.setattr(cli, "connect_user", lambda server, user: theirs[user])
    out = invoke("tng", "build", "borg", "--no-artwork", "-u", "alice", "-u", "bob")
    assert "For alice" in out and "For bob" in out
    assert [p.title for p in theirs["alice"].pls] == ["TNG: The Borg"]
    assert [p.title for p in theirs["bob"].pls] == ["TNG: The Borg"]
    assert plex.pls == []  # yours are left alone

    out = invoke("tng", "remove", "borg", "--yes", "--user", "alice")
    assert "for alice" in out and "Deleted 1" in out
    assert theirs["alice"].pls == [] and len(theirs["bob"].pls) == 1


def test_build_and_remove_as_collection(plex, shows) -> None:
    out = invoke("tng", "build", "borg", "--collection", "--no-artwork")
    assert "TNG: The Borg" in out and "created" in out
    assert plex.pls == []  # no playlist was made
    (col,) = plex.show.library_section.colls
    wanted = [e.title for e in shows["tng"].playlists["borg"].episodes if not e.film]
    assert [i.title for i in col.items()] == wanted  # in order, without First Contact
    assert col.summary == shows["tng"].playlists["borg"].description

    assert "unchanged" in invoke("tng", "build", "borg", "-c", "--no-artwork")
    col.entries.reverse()
    col.entries.pop()
    assert "updated" in invoke("tng", "build", "borg", "-c", "--no-artwork")
    assert [i.title for i in col.items()] == wanted

    assert "drop --user" in invoke("tng", "build", "borg", "-c", "--user", "alice")
    assert "Deleted 1 collection" in invoke("tng", "remove", "borg", "-c", "--yes")
    assert plex.show.library_section.colls == []


def test_build_all_builds_what_the_server_has(plex, shows) -> None:
    out = invoke("build-all", "--no-artwork")
    assert len(plex.pls) == len(shows["tng"].playlists)
    assert "The X-Files: not in your Plex library" in out
    assert invoke("build-all", "--no-artwork", "--quiet").strip() == ""  # nothing changed

    plex.pls[0].delete()
    out = invoke("build-all", "--no-artwork", "--quiet")
    assert out.count("created") == 1 and "X-Files" not in out


def test_build_all_exits_nonzero_on_errors(monkeypatch, plex) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("server went away")

    monkeypatch.setattr(plex, "createPlaylist", boom)
    result = runner.invoke(cli.make_app(), ["build-all", "--no-artwork"], env={"COLUMNS": "300"})
    assert result.exit_code == 1
    assert "server went away" in result.output and "had errors" in result.output


def test_list_and_show_with_plex_details(plex) -> None:
    for e in plex.show.eps:
        if e.title == "Q Who":
            e.seasonNumber, e.index, e.duration, e.isPlayed = 2, 16, 45 * 60 * 1000, True
    assert "Created" not in invoke("tng", "list")  # offline by default
    invoke("tng", "build", "borg", "--no-artwork")

    out = invoke("tng", "list", "--plex")
    borg = next(line for line in out.splitlines() if "TNG: The Borg" in line)
    assert "all matched" in borg and "2026-10-03" in borg and "1/8" in borg
    data = next(line for line in out.splitlines() if "TNG: Data" in line)
    assert "2026-10-03" not in data  # not built, so no created date

    out = invoke("tng", "show", "borg", "--plex")
    q_who = next(line for line in out.splitlines() if "Q Who" in line)
    assert "S2E16" in q_who and "45m" in q_who and "✓" in q_who
    assert "▶ next" in next(line for line in out.splitlines() if "Both Worlds (1)" in line)
    assert "all matched · 1/8 watched" in out
