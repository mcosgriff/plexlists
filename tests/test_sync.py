import random

import pytest

from plexlists.sync import plan_sync


def apply(current: list[int], wanted: list[int]) -> list[int]:
    """Replay a plan with Plex semantics: remove, append adds, then move-after."""
    plan = plan_sync(current, wanted)
    order = [k for k in current if k not in plan.remove] + plan.add
    for k, after in plan.moves:
        order.remove(k)
        order.insert(0 if after is None else order.index(after) + 1, k)
    return order


def test_no_changes() -> None:
    assert plan_sync([1, 2, 3], [1, 2, 3]).empty


@pytest.mark.parametrize("seed", range(500))
def test_plan_reaches_wanted_order(seed: int) -> None:
    rng = random.Random(seed)
    pool = list(range(30))
    rng.shuffle(pool)
    current = pool[: rng.randint(0, 20)]
    rng.shuffle(pool)
    wanted = pool[: rng.randint(1, 20)]
    assert apply(current, wanted) == wanted


def test_summary() -> None:
    plan = plan_sync([1, 2, 3, 4], [4, 1, 2, 5])
    assert plan.summary() == "+1, -1, 1 moved"
