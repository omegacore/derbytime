import pytest

from racemgr.core.scoring import (
    Mode,
    RunResult,
    compute_standings,
    is_outlier,
    place_heat,
    points_for,
    runoff_order,
    select_finalists,
    tie_blocks,
)


def test_place_heat_with_ties_and_dnf():
    places = place_heat({1: 3.1, 2: 3.0, 3: 3.1, 4: None})
    assert places == {2: 1, 1: 2, 3: 2, 4: 4}


def test_place_heat_ignores_float_noise():
    assert place_heat({1: 3.0001, 2: 3.0004}) == {1: 1, 2: 1}


def test_outlier():
    assert is_outlier(1.0, 2, 6)
    assert is_outlier(7.0, 2, 6)
    assert not is_outlier(3.0, 2, 6)
    assert not is_outlier(None, 2, 6)


def test_points_for():
    assert points_for(1, 4, False) == 4
    assert points_for(4, 4, False) == 1
    assert points_for(1, 4, True) == 0


def runs(data):
    """data: {entry: [(time, place, field)]}"""
    return [RunResult(e, t, p, f) for e, rs in data.items() for (t, p, f) in rs]


def test_average_time_with_dnf_penalty():
    st = compute_standings(
        runs({"a": [(3.0, 1, 2), (3.2, 1, 2)], "b": [(3.1, 2, 2), (None, 2, 2)]}),
        Mode.average_time,
        dnf_time=9.0,
    )
    assert [s.entry_id for s in st] == ["a", "b"]
    assert st[0].average == pytest.approx(3.1)
    assert st[1].average == pytest.approx(6.05)
    assert st[1].best == 3.1


def test_best_time():
    st = compute_standings(
        runs({"a": [(3.0, 1, 2), (4.0, 2, 2)], "b": [(3.5, 2, 2), (3.1, 1, 2)]}),
        "best_time",
        dnf_time=9.0,
    )
    assert [s.entry_id for s in st] == ["a", "b"]
    assert st[0].score == 3.0


def test_points_mode_higher_is_better():
    st = compute_standings(
        runs({"a": [(3.0, 1, 4), (3.3, 2, 4)], "b": [(3.1, 2, 4), (3.0, 1, 4)], "c": [(3.5, 4, 4), (None, 4, 4)]}),
        Mode.points,
        dnf_time=9.0,
    )
    assert [(s.entry_id, s.points) for s in st] == [("a", 7), ("b", 7), ("c", 1)]
    assert st[0].tied and st[1].tied and st[0].rank == st[1].rank == 1
    assert st[2].rank == 3


def test_entries_without_runs_rank_last():
    st = compute_standings(runs({"a": [(3.0, 1, 1)]}), Mode.average_time, 9.0, entries=["a", "z"])
    assert [s.entry_id for s in st] == ["a", "z"]
    assert st[1].score is None and not st[1].tied


def test_ties_and_runoff_resolution():
    data = runs({"a": [(3.0, 1, 3)], "b": [(3.0, 1, 3)], "c": [(3.2, 3, 3)]})
    st = compute_standings(data, Mode.average_time, 9.0)
    assert tie_blocks(st, within=3) == [["a", "b"]] or tie_blocks(st, within=3) == [["b", "a"]]
    assert tie_blocks(st, within=0) == []

    runoff = compute_standings(runs({"a": [(3.05, 2, 2)], "b": [(3.01, 1, 2)]}), Mode.average_time, 9.0)
    st2 = compute_standings(data, Mode.average_time, 9.0, tiebreak=runoff_order(runoff))
    assert [(s.entry_id, s.rank, s.tied) for s in st2] == [("b", 1, False), ("a", 2, False), ("c", 3, False)]


def test_select_finalists_and_cutoff_tie():
    st = compute_standings(
        runs({"a": [(3.0, 1, 4)], "b": [(3.1, 2, 4)], "c": [(3.2, 3, 4)], "d": [(3.2, 3, 4)]}),
        Mode.average_time,
        9.0,
    )
    pick = select_finalists(st, 2)
    assert pick.finalists == ["a", "b"] and pick.blocking_tie is None
    pick = select_finalists(st, 3)
    assert pick.finalists == ["a", "b"]
    assert sorted(pick.blocking_tie) == ["c", "d"]
    pick = select_finalists(st, 4)
    assert sorted(pick.finalists) == ["a", "b", "c", "d"] and pick.blocking_tie is None
