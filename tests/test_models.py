from pathlib import Path

import pytest

from plexlists.models import ShowFileError, discover, parse_show

MINIMAL = {
    "show": {"title": "Test Show"},
    "playlists": [{"key": "fav", "name": "Favorites", "episodes": [[1, "Pilot"]]}],
}


def test_builtin_shows_load(shows) -> None:
    assert set(shows) >= {"tng", "xfiles"}
    assert len(shows["tng"].playlists) == 11
    assert len(shows["xfiles"].playlists) == 10


def test_minimal_defaults() -> None:
    s = parse_show(MINIMAL, "test")
    assert s.label == "Test Show"
    assert s.prefix == "Test Show: "
    assert s.plex_name("fav") == "Test Show: Favorites"
    assert s.runtime("fav") == 44


def test_long_episode_counts_double(shows) -> None:
    tng = shows["tng"]
    farpoint = tng.playlists["binge"].episodes[0]
    assert farpoint.title == "Encounter at Farpoint"
    assert tng.minutes(farpoint) == 90


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("show"), "missing [show]"),
        (lambda d: d["show"].pop("title"), "missing 'title'"),
        (lambda d: d["playlists"][0].update(key="Bad Key"), "key must be"),
        (lambda d: d["playlists"].append(dict(d["playlists"][0])), "duplicate key"),
        (lambda d: d["playlists"][0].update(episodes=[["1", "Pilot"]]), "expected [season"),
        (lambda d: d["playlists"][0].update(episodes=[{"film": "nope"}]), "unknown film"),
        (lambda d: d["show"].update(colors={"top": "red"}), "#rrggbb"),
        (lambda d: d.update(playlists=[]), "no [[playlists]]"),
    ],
)
def test_validation_errors(mutate, message: str) -> None:
    import copy

    data = copy.deepcopy(MINIMAL)
    mutate(data)
    with pytest.raises(ShowFileError, match=message.replace("[", r"\[")):
        parse_show(data, "test")


def test_user_dir_overrides_builtin_and_reports_bad_files(tmp_path: Path) -> None:
    (tmp_path / "tng.toml").write_text(
        '[show]\ntitle = "My TNG"\n[[playlists]]\nkey = "a"\nname = "A"\n'
        'episodes = [[1, "Pilot"]]\n'
    )
    (tmp_path / "broken.toml").write_text("this is = = not toml")
    shows, errors = discover(tmp_path)
    assert shows["tng"].title == "My TNG"
    assert "xfiles" in shows
    assert len(errors) == 1
    assert "broken.toml" in errors[0]
