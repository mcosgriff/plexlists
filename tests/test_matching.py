import pytest

from plexlists.matching import Library
from plexlists.models import Ep
from tests.conftest import film_items, library_for
from tests.fakeplex import Item


@pytest.mark.parametrize("style", ["tmdb", "tvdb"])
@pytest.mark.parametrize("slug", ["tng", "xfiles"])
def test_every_entry_matches(shows, slug: str, style: str) -> None:
    show = shows[slug]
    films = dict(zip(show.films, film_items(show), strict=True))
    lib = Library(library_for(show, style), films)
    for p in show.playlists.values():
        for e in p.episodes:
            assert lib.match(e), f"{slug}/{p.key}: {e.title} ({style})"


def test_combined_file_vs_split_parts() -> None:
    combined = Library([Item(1, "Encounter at Farpoint", 1, 1)])
    split = Library(
        [Item(1, "Encounter at Farpoint (1)", 1, 1), Item(2, "Encounter at Farpoint (2)", 1, 2)]
    )
    ep = Ep(1, "Encounter at Farpoint")
    assert [i.ratingKey for i in combined.match(ep)] == [1]
    assert [i.ratingKey for i in split.match(ep)] == [1, 2]
    # Asking for a specific part of a combined file gives the combined file.
    assert [i.ratingKey for i in combined.match(Ep(1, "Encounter at Farpoint (2)"))] == [1]


def test_part_spanning_seasons() -> None:
    lib = Library(
        [
            Item(1, "The Best of Both Worlds, Part I", 3, 26),
            Item(2, "The Best of Both Worlds, Part II", 4, 1),
        ]
    )
    assert [i.ratingKey for i in lib.match(Ep(4, "The Best of Both Worlds (2)"))] == [2]


def test_ambiguous_title_does_not_guess() -> None:
    lib = Library([Item(1, "Home", 4, 2), Item(2, "Home", 4, 3)])
    assert lib.match(Ep(4, "Home")) == []


def test_season_breaks_ties() -> None:
    lib = Library([Item(1, "Home", 2, 2), Item(2, "Home", 4, 3)])
    assert [i.ratingKey for i in lib.match(Ep(4, "Home"))] == [2]


def test_fuzzy_substring_fallback() -> None:
    lib = Library([Item(1, "The Sixth Extinction II: Amor Fati", 7, 2)])
    assert lib.match(Ep(7, "Amor Fati"))


def test_suggestions_for_typos() -> None:
    lib = Library([Item(1, "Peak Performance", 2, 21)])
    assert lib.match(Ep(2, "Peek Performence")) == []
    assert lib.suggest(Ep(2, "Peek Performence")) == ["Peak Performance"]
