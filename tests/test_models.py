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
    assert s.playlists["fav"].poster_from == ""


def test_poster_from_is_read() -> None:
    playlist = {"key": "fav", "name": "Favorites", "episodes": [[1, "Pilot"]]}
    data = {"show": {"title": "Test Show"}, "playlists": [{**playlist, "poster_from": "Pilot"}]}
    s = parse_show(data, "test")
    assert s.playlists["fav"].poster_from == "Pilot"


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
        (lambda d: d["playlists"][0].update(poster_from=3), "'poster_from' must be a str"),
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


def test_builtin_playlists_pick_distinct_poster_artwork(shows) -> None:
    for show in shows.values():
        titles = {e.title for p in show.playlists.values() for e in p.episodes if not e.film}
        picks = [p.poster_from for p in show.playlists.values()]
        assert all(picks), show.slug
        assert len(set(picks)) == len(picks), f"{show.slug}: repeated poster_from"
        for pick in picks:
            assert pick == "show" or pick in show.films or pick in titles, (show.slug, pick)
