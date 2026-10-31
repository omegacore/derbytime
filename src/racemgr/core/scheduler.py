"""Heat generation and multi-track queue planning.

Everything here works on plain ids so it can be unit-tested without a database.
"""
from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)

Lineup = list  # list[T | None], index = lane - 1, None = bye


def lane_rotation(entries: Sequence[T], lane_count: int) -> list[list[T | None]]:
    """Lane-balanced rotation: every entry runs every lane exactly once.

    With N entries and L lanes we build max(N, L) heats. Entry slots are laid
    out on a ring; lane ``l`` of heat ``h`` takes slot ``(h + l*s) mod M`` where
    the stride ``s = M // L`` spreads each entry's runs through the round
    instead of racing back-to-back. When N < L the ring is padded with byes so
    every entry still sees every lane.
    """
    if lane_count < 1:
        raise ValueError("lane_count must be >= 1")
    n = len(entries)
    if n == 0:
        return []
    if len(set(entries)) != n:
        raise ValueError("entries must be unique")
    m = max(n, lane_count)
    slots: list[T | None] = list(entries) + [None] * (m - n)
    stride = max(1, m // lane_count)
    heats = []
    for h in range(m):
        lineup = [slots[(h + lane * stride) % m] for lane in range(lane_count)]
        if any(x is not None for x in lineup):
            heats.append(lineup)
    return heats


def natural_key(name: str) -> tuple:
    """Sort key that orders embedded numbers numerically: 'Den 9' < 'Den 10'."""
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name.strip()))


def snake_order(keys: Sequence[T], passes: int) -> list[T]:
    """Run order for ``passes`` passes over ``keys``: forward, back, forward, …

    ``snake_order([a, b, c], 2) == [a, b, c, c, b, a]``.
    """
    if passes < 1:
        raise ValueError("passes must be >= 1")
    out: list[T] = []
    for p in range(passes):
        out += list(keys) if p % 2 == 0 else list(reversed(keys))
    return out


PlannedHeat = tuple[int, dict[int, K]]  # (index into tracks, {lane number: entry})


def plan_run(
    entries: Sequence[K],
    tracks: Sequence[Sequence[int]],
    need: Mapping[K, int] | None = None,
    used: Mapping[K, Mapping[int, int]] | None = None,
) -> list[PlannedHeat]:
    """Heats for one run of a den, spread over every track.

    ``tracks``: the usable lane numbers of each track (lanes out of service
    left out). ``need``: runs still owed to each entry (default: one per lane,
    i.e. a full rotation). ``used``: lane -> times already run this run, for
    runs that are being rebuilt part-way through.

    Heats alternate between tracks in order, so a den is raced on all tracks
    at once and finishes before the next run starts. When every track has the
    same number of usable lanes and the run is fresh, the classic rotation is
    used (every entry runs every lane exactly once). Otherwise heats are filled
    greedily: entries who are owed the most and have rested longest go first,
    and each gets the lane it has used least.
    """
    tracks = [list(t) for t in tracks]
    if not tracks or any(not t for t in tracks):
        raise ValueError("every track needs at least one usable lane")
    if len(set(entries)) != len(entries):
        raise ValueError("entries must be unique")
    used = {e: dict(used.get(e, {})) for e in entries} if used else {e: {} for e in entries}
    width = max(len(t) for t in tracks)
    need = {e: width for e in entries} if need is None else {e: max(0, int(need.get(e, 0))) for e in entries}

    fresh = not any(used[e] for e in entries)
    uniform = len({len(t) for t in tracks}) == 1
    if fresh and uniform and all(n == width for n in need.values()):
        out: list[PlannedHeat] = []
        for i, lineup in enumerate(lane_rotation(list(entries), width)):
            ti = i % len(tracks)
            out.append((ti, {tracks[ti][pos]: e for pos, e in enumerate(lineup) if e is not None}))
        return out
    return _fill_heats(list(entries), tracks, need, used)


def _fill_heats(entries: list[K], tracks: list[list[int]], need: dict[K, int], used: dict[K, dict[int, int]]) -> list[PlannedHeat]:
    order = {e: i for i, e in enumerate(entries)}
    remaining = {e: n for e, n in need.items() if n > 0}
    last: dict[K, int] = {e: -(10**6) for e in entries}
    window = len(tracks)  # heats running at about the same time
    out: list[PlannedHeat] = []
    i = 0
    while remaining:
        ti = i % len(tracks)
        lanes = tracks[ti]
        cands = sorted(
            remaining,
            key=lambda e: (i - last[e] <= window, -remaining[e], last[e], order[e]),
        )
        # consider a few more cars than lanes so a car whose missing lane is
        # taken can wait for the next heat instead of repeating a lane
        assignment = _assign_lanes(cands[: 2 * len(lanes)], lanes, used)
        for lane, e in assignment.items():
            used[e][lane] = used[e].get(lane, 0) + 1
            last[e] = i
            remaining[e] -= 1
            if remaining[e] == 0:
                del remaining[e]
        out.append((ti, assignment))
        i += 1
    return out


REPEAT_COST = 1000  # running a lane again outweighs any priority difference


def _assign_lanes(cands: list[K], lanes: list[int], used: dict[K, dict[int, int]]) -> dict[int, K]:
    """Pick cars for this heat and their lanes in one optimal assignment.

    ``cands`` is in priority order. Cost of car e on lane l = lane repeats
    (heavily weighted) + e's priority rank; extra cars beyond the lane count
    may sit out at no cost. Lanes are left empty only if there are fewer cars.
    """
    if not cands:
        return {}
    # Square matrix: rows are cars (+ "empty lane" placeholders when cars run
    # short), columns are lanes (+ "sits out" slots when there are extra cars).
    n = max(len(cands), len(lanes))

    def cell(r: int, c: int) -> int:
        if r >= len(cands) or c >= len(lanes):
            return 0  # an empty lane, or a car sitting this heat out
        return used[cands[r]].get(lanes[c], 0) * REPEAT_COST + r

    match = _hungarian([[cell(r, c) for c in range(n)] for r in range(n)])
    return {lanes[c]: cands[r] for r, c in enumerate(match) if r < len(cands) and c < len(lanes)}


def _hungarian(cost: list[list[int]]) -> list[int]:
    """Minimum-cost perfect assignment for a square matrix; returns column per row."""
    n = len(cost)
    INF = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (n + 1)
    p, way = [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used_col = [False] * (n + 1)
        while True:
            used_col[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, n + 1):
                if not used_col[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used_col[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    row_to_col = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            row_to_col[p[j] - 1] = j - 1
    return row_to_col


@dataclass(frozen=True)
class QueuedHeat:
    heat_id: int
    racer_ids: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def of(cls, heat_id: int, racer_ids: Iterable[int | None]) -> "QueuedHeat":
        return cls(heat_id, frozenset(r for r in racer_ids if r is not None))


def pick_next(queue: Sequence[QueuedHeat], busy: set[int], soft_busy: set[int] = frozenset()) -> int | None:
    """Index of the heat to run next, skipping heats with conflicting racers.

    ``busy`` racers are on another track right now (hard conflict).
    ``soft_busy`` racers are on deck elsewhere; avoid them if anything else is ready.
    Returns None when every heat has a hard conflict.
    """
    fallback = None
    for i, heat in enumerate(queue):
        if heat.racer_ids & busy:
            continue
        if not heat.racer_ids & soft_busy:
            return i
        if fallback is None:
            fallback = i
    return fallback


def plan_tracks(
    queues: Mapping[int, Sequence[QueuedHeat]],
    on_track: Mapping[int, set[int]],
) -> dict[int, list[QueuedHeat]]:
    """Shared scheduler across tracks.

    ``queues``: track id -> scheduled heats in nominal order.
    ``on_track``: track id -> racers currently staged/armed on that track.

    Returns each track's queue reordered so its first heat is the one to stage
    next. A racer on track A is never planned next on track B, and tracks are
    planned in id order so a racer on deck at one track is avoided (if
    possible) as the next heat on a later track.
    """
    plan: dict[int, list[QueuedHeat]] = {}
    on_deck_racers: set[int] = set()
    for track_id in sorted(queues):
        queue = list(queues[track_id])
        busy: set[int] = set()
        for other, racers in on_track.items():
            if other != track_id:
                busy |= racers
        idx = pick_next(queue, busy, on_deck_racers)
        if idx is not None:
            nxt = queue.pop(idx)
            queue.insert(0, nxt)
            on_deck_racers |= nxt.racer_ids
            plan[track_id] = queue
        else:
            plan[track_id] = queue
    return plan


def is_blocked(heat: QueuedHeat, track_id: int, on_track: Mapping[int, set[int]]) -> set[int]:
    """Racers in ``heat`` who are currently on a different track."""
    clash: set[int] = set()
    for other, racers in on_track.items():
        if other != track_id:
            clash |= heat.racer_ids & racers
    return clash
