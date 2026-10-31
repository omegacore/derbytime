from collections import Counter

import pytest

from racemgr.core.scheduler import (
    QueuedHeat,
    is_blocked,
    lane_rotation,
    natural_key,
    plan_run,
    plan_tracks,
    snake_order,
)


def lanes_run(heats, entry):
    return sorted(lane for h in heats for lane, e in enumerate(h) if e == entry)


@pytest.mark.parametrize("n", range(1, 26))
@pytest.mark.parametrize("lanes", [2, 3, 4, 6, 8])
def test_every_entry_runs_every_lane_once(n, lanes):
    entries = list(range(100, 100 + n))
    heats = lane_rotation(entries, lanes)
    assert len(heats) == max(n, lanes)
    for h in heats:
        assert len(h) == lanes
        real = [e for e in h if e is not None]
        assert len(real) == len(set(real)), "entry twice in one heat"
    for e in entries:
        assert lanes_run(heats, e) == list(range(lanes))


def test_byes_fill_short_fields():
    heats = lane_rotation(["a", "b"], 4)
    assert len(heats) == 4
    assert all(h.count(None) == 2 for h in heats)


def test_no_byes_when_enough_entries():
    heats = lane_rotation(list(range(10)), 4)
    assert all(None not in h for h in heats)


def test_runs_are_spread_out():
    # With 12 entries on 4 lanes, nobody should race in consecutive heats.
    heats = lane_rotation(list(range(12)), 4)
    for e in range(12):
        idx = [i for i, h in enumerate(heats) if e in h]
        gaps = [b - a for a, b in zip(idx, idx[1:])]
        assert min(gaps) >= 2


def test_empty_and_invalid():
    assert lane_rotation([], 4) == []
    with pytest.raises(ValueError):
        lane_rotation([1, 1], 2)
    with pytest.raises(ValueError):
        lane_rotation([1], 0)


def test_natural_key_orders_numbers_numerically():
    names = ["Den 10", "den 2", "Den 9", "Den 1", "Arrow"]
    assert sorted(names, key=natural_key) == ["Arrow", "Den 1", "den 2", "Den 9", "Den 10"]
    assert sorted(["7 Webelos", "10 AOL", "3 Tigers"], key=natural_key) == ["3 Tigers", "7 Webelos", "10 AOL"]


def test_snake_order():
    assert snake_order(["a", "b", "c"], 1) == ["a", "b", "c"]
    assert snake_order(["a", "b", "c"], 2) == ["a", "b", "c", "c", "b", "a"]
    assert snake_order(["a", "b"], 3) == ["a", "b", "b", "a", "a", "b"]
    with pytest.raises(ValueError):
        snake_order(["a"], 0)


def check_plan(heats, tracks, entries):
    """Shared invariants: lanes valid for their track, nobody twice in a heat."""
    for ti, lanes in heats:
        assert set(lanes) <= set(tracks[ti])
        assert len(set(lanes.values())) == len(lanes)
    return Counter(e for _, lanes in heats for e in lanes.values())


@pytest.mark.parametrize("n", [2, 3, 5, 9, 10, 17])
def test_plan_run_uses_every_track_with_full_rotation(n):
    entries = list(range(n))
    tracks = [[1, 2], [1, 2]]  # two regatta gutters
    heats = plan_run(entries, tracks)
    runs = check_plan(heats, tracks, entries)
    assert set(runs.values()) == {2}
    used = {ti for ti, _ in heats}
    assert used == {0, 1}  # both gutters used for one den
    per_track = Counter(ti for ti, _ in heats)
    assert abs(per_track[0] - per_track[1]) <= 1
    for e in entries:  # every boat runs each lane once
        assert sorted(l for _, lanes in heats for l, x in lanes.items() if x == e) == [1, 2]


def test_plan_run_single_track_matches_rotation():
    heats = plan_run(list(range(10)), [[1, 2, 3, 4]])
    assert [[lanes.get(l) for l in (1, 2, 3, 4)] for _, lanes in heats] == lane_rotation(list(range(10)), 4)


def test_plan_run_skips_lanes_out_of_service():
    tracks = [[1, 3, 4]]  # lane 2 broke
    heats = plan_run(list(range(9)), tracks)
    runs = check_plan(heats, tracks, range(9))
    assert set(runs.values()) == {3}
    assert all(2 not in lanes for _, lanes in heats)


def test_plan_run_mixed_lane_counts():
    tracks = [[1, 2, 3, 4], [1, 2, 3]]
    heats = plan_run(list(range(8)), tracks)
    runs = check_plan(heats, tracks, range(8))
    assert set(runs.values()) == {4}
    assert {ti for ti, _ in heats} == {0, 1}


def test_plan_run_finishes_a_partly_raced_run():
    # 8 cars, 4 lanes; the first half of the run happened, then lane 4 broke
    entries = list(range(8))
    done = lane_rotation(entries, 4)[:4]
    used = {e: Counter() for e in entries}
    for lineup in done:
        for pos, e in enumerate(lineup):
            used[e][pos + 1] += 1
    need = {e: 4 - sum(used[e].values()) for e in entries}
    heats = plan_run(entries, [[1, 2, 3]], need, used)
    runs = check_plan(heats, [[1, 2, 3]], entries)
    assert all(runs[e] == need[e] for e in entries if need[e])
    for e in entries:  # nobody repeats a lane if a fresh one was available
        total = Counter(used[e])
        for _, lanes in heats:
            for l, x in lanes.items():
                if x == e:
                    total[l] += 1
        assert max(total.values()) - min(total.get(l, 0) for l in (1, 2, 3)) <= 1


def test_plan_run_late_arrival_gets_full_set():
    entries = ["a", "b", "c", "late"]
    need = {"a": 1, "b": 1, "c": 1, "late": 4}
    used = {"a": {1: 1, 2: 1, 3: 1}, "b": {1: 1, 2: 1, 3: 1}, "c": {1: 1, 2: 1, 3: 1}}
    heats = plan_run(entries, [[1, 2, 3, 4]], need, used)
    runs = check_plan(heats, [[1, 2, 3, 4]], entries)
    assert runs == {"a": 1, "b": 1, "c": 1, "late": 4}
    assert sorted(l for _, lanes in heats for l, x in lanes.items() if x == "late") == [1, 2, 3, 4]


@pytest.mark.parametrize("seed", range(40))
def test_plan_run_random_scenarios_meet_every_need(seed):
    import random

    rng = random.Random(seed)
    entries = list(range(rng.randint(1, 14)))
    tracks = [sorted(rng.sample(range(1, 7), rng.randint(1, 6))) for _ in range(rng.randint(1, 3))]
    need = {e: rng.randint(0, 5) for e in entries}
    used = {e: {l: rng.randint(0, 1) for l in range(1, 7)} for e in entries}
    heats = plan_run(entries, tracks, need, used)
    runs = check_plan(heats, tracks, entries)
    assert {e: runs.get(e, 0) for e in entries} == need
    assert all(lanes for _, lanes in heats)  # no empty heats
    # lanes are only left empty when there weren't enough cars owed runs
    assert len(heats) <= sum(need.values())


def test_plan_run_nothing_needed_and_bad_input():
    assert plan_run(["a"], [[1, 2]], need={"a": 0}) == []
    assert plan_run([], [[1, 2]]) == []
    with pytest.raises(ValueError):
        plan_run(["a"], [[]])


def ids(plan):
    return {t: [h.heat_id for h in q] for t, q in plan.items()}


def test_plan_tracks_shares_one_queue():
    q = [QueuedHeat.of(h, [h]) for h in range(1, 7)]
    # both free: tracks take turns from the front of the queue
    assert ids(plan_tracks(q, [1, 2], on_track={})) == {1: [1, 3, 5], 2: [2, 4, 6]}
    # track 1 still racing: free track 2 takes the front heat
    assert ids(plan_tracks(q, [1, 2], on_track={1: {99}}))[2][0] == 1
    # the track asking to stage gets first pick
    assert ids(plan_tracks(q, [1, 2], on_track={}, first=2))[2][0] == 1


def test_plan_tracks_slow_track_doesnt_hold_others_up():
    q = [QueuedHeat.of(h, [h]) for h in range(1, 7)]
    # track 1 is still racing heat 1; track 2 is free and takes heat 3...
    assert ids(plan_tracks(q[2:], [1, 2], on_track={1: {1}}))[2][0] == 3
    # ...races it, and is back before track 1 is done: it takes heat 4 too
    assert ids(plan_tracks(q[3:], [1, 2], on_track={1: {1}}, first=2))[2][0] == 4


def test_plan_tracks_never_puts_racer_on_two_tracks():
    q = [QueuedHeat.of(10, [1, 2]), QueuedHeat.of(11, [3, 4]), QueuedHeat.of(12, [5, 6])]
    # racer 1 is racing on track 1 right now
    plan = plan_tracks(q, [1, 2], on_track={1: {1, 9}})
    assert plan[2][0].heat_id == 11
    assert plan[1][0].heat_id == 10  # own track: no conflict with itself


def test_plan_tracks_avoids_racing_same_scout_side_by_side():
    q = [QueuedHeat.of(10, [1, 2]), QueuedHeat.of(11, [1, 5]), QueuedHeat.of(12, [6, 7])]
    plan = plan_tracks(q, [1, 2], on_track={})
    assert ids(plan) == {1: [10, 11], 2: [12]}


def test_plan_tracks_other_event_forecast_keeps_race_order():
    # the same scouts in a derby (tracks 1, 2) and a regatta (track 3) that hasn't started
    derby = [QueuedHeat.of(h, [h], tracks=[1, 2]) for h in (1, 2, 3, 4)]
    regatta = [QueuedHeat.of(10 + h, [h], tracks=[3]) for h in (2, 4)]
    plan = plan_tracks(regatta + derby, [1, 2, 3], on_track={})
    assert ids(plan) == {1: [1, 3], 2: [2, 4], 3: [12, 14]}
    # but a scout actually racing elsewhere is never planned next
    plan = plan_tracks(regatta + derby, [1, 2, 3], on_track={3: {2}})
    assert ids(plan)[1][0] == 1 and ids(plan)[2][0] == 3


def test_plan_tracks_respects_allowed_tracks():
    q = [QueuedHeat.of(10, [1], tracks=[1]), QueuedHeat.of(11, [2], tracks=[1]), QueuedHeat.of(12, [3])]
    plan = plan_tracks(q, [1, 2], on_track={})
    assert ids(plan) == {1: [10, 11], 2: [12]}
    # a heat no planned track can race is left out
    assert ids(plan_tracks([QueuedHeat.of(10, [1], tracks=[3])], [1, 2], on_track={})) == {1: [], 2: []}


def test_plan_tracks_blocked_heat_still_listed():
    # racer 1 is on a track outside the plan (another event's switched-off track)
    q = [QueuedHeat.of(10, [1], tracks=[1]), QueuedHeat.of(11, [2], tracks=[1])]
    plan = plan_tracks(q, [1], on_track={5: {1}})
    assert ids(plan) == {1: [11, 10]}


def test_is_blocked():
    h = QueuedHeat.of(1, [1, 2])
    assert is_blocked(h, 1, {1: {1}, 2: {2}}) == {2}
    assert is_blocked(h, 1, {}) == set()
