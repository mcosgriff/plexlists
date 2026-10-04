from pathlib import Path

import pytest
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static, Tree

from plexlists import config, tui
from plexlists.tui import ConfirmScreen, Nav, PlexlistsApp
from tests.fakeplex import FakeServer


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, make_server, shows) -> FakeServer:
    srv: FakeServer = make_server(shows["tng"])
    monkeypatch.setattr(tui, "connect_session", lambda: tui.Session(srv))
    return srv


async def select(app: PlexlistsApp, pilot, nav: Nav) -> None:
    app.query_one("#nav", Tree).move_cursor(app.nodes[nav])
    await pilot.pause()


async def settle(app: PlexlistsApp, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


def cell(app: PlexlistsApp, row: int, col: int) -> str:
    value = app.query_one("#table", DataTable).get_cell_at(Coordinate(row, col))
    return getattr(value, "plain", str(value))


async def test_starts_with_shows_and_playlists() -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        assert Nav("tng") in app.nodes and Nav("xfiles") in app.nodes
        assert Nav("tng", "borg") in app.nodes
        await select(app, pilot, Nav("tng", "borg"))
        table = app.query_one("#table", DataTable)
        assert table.row_count == 8
        assert cell(app, 7, 2) == "Star Trek: First Contact"
        assert "not logged in" in app.sub_title


async def test_show_view_lists_playlists_and_enter_opens_one() -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("xfiles"))
        table = app.query_one("#table", DataTable)
        assert table.row_count == 10
        table.focus()
        await pilot.press("down", "enter")  # second row: monsters
        assert app.current == Nav("xfiles", "monsters")


async def test_check_marks_episodes(server) -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("tng", "q"))
        await pilot.press("c")
        await settle(app, pilot)
        assert ("tng", "q") in app.results
        assert cell(app, 1, 4) == "✓"  # Hide and Q
        assert "all matched" in str(app.query_one("#summary", Static).render())
        assert server.pls == []  # checking never changes Plex
        assert "connected to TestServer" in app.sub_title


async def test_build_whole_show_then_rebuild(server, shows) -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("tng"))
        await pilot.press("b")
        await settle(app, pilot)
        assert len(server.pls) == len(shows["tng"].playlists)
        assert all(a == "created" for a in app.actions.values())
        await pilot.press("b")
        await settle(app, pilot)
        assert all(a == "unchanged" for a in app.actions.values())
        assert "unchanged" in cell(app, 0, 5)


async def test_missing_episode_shows_suggestion(server) -> None:
    for e in server.show.eps:
        if e.title == "Hollow Pursuits":
            e.title = "Hollow Persuits"
    app = PlexlistsApp()
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, Nav("tng", "holodeck"))
        await pilot.press("c")
        await settle(app, pilot)
        assert "missing" in cell(app, 2, 4) and "Hollow Persuits" in cell(app, 2, 4)
        label = app.nodes[Nav("tng", "holodeck")].label.plain
        assert "1✗" in label


async def test_remove_asks_first(server) -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("tng", "borg"))
        await pilot.press("b")
        await settle(app, pilot)
        assert len(server.pls) == 1
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # Cancel has focus by default
        await pilot.pause()
        assert len(server.pls) == 1
        await pilot.press("x")
        await pilot.pause()
        await pilot.click("#yes")
        await settle(app, pilot)
        assert server.pls == []
        assert app.actions[("tng", "borg")] == "removed"


async def test_posters_generated_then_confirm_replace(config_dir: Path) -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("xfiles", "funny"))
        await pilot.press("p")
        await settle(app, pilot)
        poster = config_dir / "posters" / "xfiles" / "funny.jpg"
        assert poster.is_file()
        assert "poster: funny.jpg" in str(app.query_one("#summary", Static).render())
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)


async def test_connection_error_is_reported(monkeypatch) -> None:
    def boom() -> tui.Session:
        raise tui.auth.AuthError("Not logged in. Run plexlists login first.")

    monkeypatch.setattr(tui, "connect_session", boom)
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await select(app, pilot, Nav("tng", "borg"))
        await pilot.press("c")
        await settle(app, pilot)
        assert app.session is None
        assert ("tng", "borg") not in app.results


async def test_reload_picks_up_new_user_show(config_dir: Path) -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        shows_dir = config_dir / "shows"
        shows_dir.mkdir(parents=True)
        (shows_dir / "ds9.toml").write_text(
            '[show]\ntitle = "Star Trek: Deep Space Nine"\nlabel = "DS9"\n'
            '[[playlists]]\nkey = "dominion"\nname = "Dominion War"\n'
            'episodes = [[2, "The Jem\'Hadar"]]\n'
        )
        await pilot.press("r")
        await pilot.pause()
        assert Nav("ds9", "dominion") in app.nodes


async def test_account_screen_opens() -> None:
    app = PlexlistsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert type(app.screen).__name__ == "AccountScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_created_time_comes_from_plex_and_is_remembered(server, config_dir: Path) -> None:
    config.save_config({"server_id": server.machineIdentifier})
    app = PlexlistsApp()
    async with app.run_test(size=(160, 40)) as pilot:
        await select(app, pilot, Nav("tng"))
        assert app.times is None  # never asked this server
        assert cell(app, 1, 6) == "—"
        await select(app, pilot, Nav("tng", "borg"))
        await pilot.press("b")
        await settle(app, pilot)
        summary = str(app.query_one("#summary", Static).render())
        assert "created 2026-10-03 21:14" in summary
        await select(app, pilot, Nav("tng", "q"))
        assert "not in Plex" in str(app.query_one("#summary", Static).render())
        await select(app, pilot, Nav("tng"))
        assert cell(app, 1, 6) == "2026-10-03"

    again = PlexlistsApp()  # a new run shows the saved times before connecting
    async with again.run_test(size=(160, 40)) as pilot:
        await select(again, pilot, Nav("tng"))
        assert again.session is None
        assert cell(again, 1, 6) == "2026-10-03"
        assert cell(again, 0, 6) == "—"
