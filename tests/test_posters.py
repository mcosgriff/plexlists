from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest
from PIL import Image

from plexlists import cli
from plexlists.posters import import_image, read_source, render_poster
from plexlists.service import Session
from tests.fakeplex import FakeServer
from tests.test_cli import invoke


def image_bytes(color: str, size: tuple[int, int] = (640, 360), fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def with_poster_from(show, key: str, value: str):
    p = dataclasses.replace(show.playlists[key], poster_from=value)
    return dataclasses.replace(show, playlists={**show.playlists, key: p})


@pytest.fixture
def server(make_server, shows) -> FakeServer:
    """A fake TNG server where every episode, film and the show itself has artwork."""
    srv: FakeServer = make_server(shows["tng"])
    srv.show.artUrl = "show-art"
    for item in [*srv.show.eps, *srv.movies]:
        item.thumbUrl = f"thumb-{item.title}"
        item.artUrl = f"art-{item.title}"
        srv._session.images[item.thumbUrl] = image_bytes("#ff0000")
        srv._session.images[item.artUrl] = image_bytes("#ff0000")
    srv._session.images["show-art"] = image_bytes("#00ff00")
    return srv


def test_backdrop_defaults_to_first_episode(server, shows) -> None:
    assert Session(server).backdrop(shows["tng"], "borg") is not None
    assert server._session.fetched == ["thumb-Q Who"]


@pytest.mark.parametrize(
    ("poster_from", "url"),
    [
        ("I, Borg", "thumb-I, Borg"),
        ("first_contact", "art-Star Trek: First Contact"),
        ("show", "show-art"),
        ("Not an Episode", "show-art"),
    ],
)
def test_backdrop_follows_poster_from(server, shows, poster_from: str, url: str) -> None:
    show = with_poster_from(shows["tng"], "borg", poster_from)
    Session(server).backdrop(show, "borg")
    assert server._session.fetched == [url]


def test_backdrop_is_none_without_artwork(make_server, shows) -> None:
    assert Session(make_server(shows["tng"])).backdrop(shows["tng"], "borg") is None


def test_render_poster_shows_the_backdrop(tmp_path: Path, shows) -> None:
    plain, photo = tmp_path / "plain.jpg", tmp_path / "photo.jpg"
    render_poster(shows["tng"], "borg", plain)
    render_poster(shows["tng"], "borg", photo, backdrop=image_bytes("#ff0000"))
    spot = (500, 330)  # between the label and the playlist name
    red0 = Image.open(plain).getchannel("R").getpixel(spot)
    red1 = Image.open(photo).getchannel("R").getpixel(spot)
    assert Image.open(photo).size == (1000, 1000)
    assert isinstance(red0, int) and isinstance(red1, int)
    assert red1 > red0 + 60


def test_posters_command_uses_plex_artwork(monkeypatch, server, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "connect_plex", lambda url, token: server)
    out = invoke("tng", "posters", "borg", "q", "--posters-dir", str(tmp_path))
    assert "plain" not in out
    assert server._session.fetched == ["thumb-Q Who", "thumb-Encounter at Farpoint"]
    server._session.fetched.clear()
    invoke("tng", "posters", "borg", "--posters-dir", str(tmp_path), "--force", "--plain")
    assert server._session.fetched == []


def test_posters_command_falls_back_when_not_logged_in(tmp_path: Path) -> None:
    out = invoke("tng", "posters", "borg", "--posters-dir", str(tmp_path))
    assert "plain title cards" in out
    assert (tmp_path / "borg.jpg").is_file()


def test_import_image_crops_and_replaces_other_formats(tmp_path: Path) -> None:
    (tmp_path / "borg.png").write_bytes(image_bytes("#000000"))
    import_image(image_bytes("#336699", (1600, 900)), tmp_path / "borg.jpg")
    import_image(image_bytes("#336699", (800, 800)), tmp_path / "borg-art.jpg", art=True)
    assert Image.open(tmp_path / "borg.jpg").size == (1000, 1000)
    assert Image.open(tmp_path / "borg-art.jpg").size == (1920, 1080)
    assert not (tmp_path / "borg.png").exists()


def test_poster_command_imports_a_file(tmp_path: Path) -> None:
    src = tmp_path / "download.png"
    src.write_bytes(image_bytes("#336699", (1200, 800)))
    folder = tmp_path / "posters"
    args = ("tng", "poster", "borg", str(src), "--posters-dir", str(folder))
    assert "wrote" in invoke(*args)
    assert Image.open(folder / "borg.jpg").size == (1000, 1000)
    assert "already exists" in invoke(*args)
    assert "wrote" in invoke(*args, "--force")
    assert "wrote" in invoke(*args, "--art")
    assert Image.open(folder / "borg-art.jpg").size == (1920, 1080)


def test_poster_command_reports_bad_input(tmp_path: Path) -> None:
    notimg = tmp_path / "notes.txt"
    notimg.write_text("hello")
    folder = str(tmp_path / "posters")
    assert "isn't an image" in invoke("tng", "poster", "borg", str(notimg), "--posters-dir", folder)
    missing = str(tmp_path / "nope.png")
    assert "Couldn't read" in invoke("tng", "poster", "borg", missing, "--posters-dir", folder)
    assert "Unknown playlist" in invoke("tng", "poster", "nope", str(notimg))


def test_read_source_downloads_urls(monkeypatch) -> None:
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        return io.BytesIO(b"data")

    monkeypatch.setattr("plexlists.posters.urllib.request.urlopen", urlopen)
    assert read_source("https://example.com/a.jpg") == b"data"
    assert seen == ["https://example.com/a.jpg"]
