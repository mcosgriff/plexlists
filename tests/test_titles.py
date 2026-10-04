import pytest

from plexlists.titles import norm, split_part


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Descent (1)", ("Descent", 1)),
        ("Descent, Part II", ("Descent", 2)),
        ("Time's Arrow: Part One", ("Time's Arrow", 1)),
        ("Chain of Command - Part 2", ("Chain of Command", 2)),
        ("All Good Things... (2)", ("All Good Things...", 2)),
        ("The Royale", ("The Royale", None)),
        ("Counterpart", ("Counterpart", None)),  # "part" inside a word isn't a part number
    ],
)
def test_split_part(title: str, expected: tuple[str, int | None]) -> None:
    assert split_part(title) == expected


def test_norm_strips_accents_and_punctuation() -> None:
    assert norm("Déjà Q") == norm("Deja Q") == "dejaq"
    assert norm("Jose Chung's From Outer Space") == "josechungsfromouterspace"
    assert norm("Q & A") == "qanda"
