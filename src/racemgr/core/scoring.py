"""Heat placing, standings, tie detection and finalist selection."""
from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)

# Timers report to the thousandth; compare at that precision so float noise
# never creates or hides a tie.
PRECISION = 3


class Mode(str, Enum):
    average_time = "average_time"
    best_time = "best_time"
    points = "points"


def _q(t: float) -> float:
    return round(t, PRECISION)


def place_heat(times: Mapping[K, float | None]) -> dict[K, int]:
    """Competition ranking (1,2,2,4) by time; None = DNF, placed after finishers."""
    finishers = sorted((_q(t), k) for k, t in times.items() if t is not None)
    places: dict[K, int] = {}
    prev, prev_place = None, 0
    for i, (t, k) in enumerate(finishers, start=1):
        if t != prev:
            prev, prev_place = t, i
        places[k] = prev_place
    dnf_place = len(finishers) + 1
    for k, t in times.items():
        if t is None:
            places[k] = dnf_place
    return places


def is_outlier(t: float | None, min_time: float, max_time: float) -> bool:
    return t is not None and not (min_time <= t <= max_time)


@dataclass(frozen=True)
class RunResult(Generic[K]):
    entry_id: K
    time: float | None  # None = DNF
    place: int
    field_size: int  # entries (not byes) in the heat


def points_for(place: int, field_size: int, dnf: bool) -> int:
    return 0 if dnf else max(0, field_size - place + 1)


@dataclass
class Standing(Generic[K]):
    entry_id: K
    rank: int
    score: float | None  # mode's primary score; None if no valid runs
    runs: int
    best: float | None
    average: float | None
    points: int
    tied: bool = False


def compute_standings(
    runs: Sequence[RunResult[K]],
    mode: Mode | str,
    dnf_time: float,
    entries: Sequence[K] = (),
    tiebreak: Mapping[K, int] | None = None,
) -> list[Standing[K]]:
    """Rank entries for one round.

    ``entries`` lists entries that should appear even with no runs yet.
    ``tiebreak`` maps entry -> runoff finishing order (1 = best); entries
    tied on score are ordered by it and no longer count as tied.
    """
    mode = Mode(mode)
    by_entry: dict[K, list[RunResult[K]]] = {e: [] for e in entries}
    for r in runs:
        by_entry.setdefault(r.entry_id, []).append(r)

    rows: list[Standing[K]] = []
    for entry_id, rs in by_entry.items():
        times = [r.time for r in rs if r.time is not None]
        counted = [r.time if r.time is not None else dnf_time for r in rs]
        best = min(times) if times else None
        avg = fmean(counted) if counted else None
        pts = sum(points_for(r.place, r.field_size, r.time is None) for r in rs)
        if mode is Mode.average_time:
            score = avg
        elif mode is Mode.best_time:
            score = best
        else:
            score = float(pts) if rs else None
        rows.append(Standing(entry_id, 0, score, len(rs), best, avg, pts))

    def primary(s: Standing) -> tuple:
        if s.score is None:
            return (1, 0.0)
        return (0, -s.score if mode is Mode.points else _q(s.score))

    tb = tiebreak or {}
    big = 10**9
    rows.sort(key=lambda s: (primary(s), tb.get(s.entry_id, big), str(s.entry_id)))

    # Competition ranking. Rows share a rank when their primary score matches
    # and the runoff did not separate them.
    for i, s in enumerate(rows):
        if i > 0 and primary(s) == primary(rows[i - 1]) and tb.get(s.entry_id, big) == tb.get(rows[i - 1].entry_id, big):
            s.rank = rows[i - 1].rank
        else:
            s.rank = i + 1
    counts: dict[int, int] = {}
    for s in rows:
        counts[s.rank] = counts.get(s.rank, 0) + 1
    for s in rows:
        s.tied = counts[s.rank] > 1 and s.score is not None
    return rows


def tie_blocks(standings: Sequence[Standing[K]], within: int) -> list[list[K]]:
    """Groups of tied entries whose shared rank is within the top ``within``."""
    blocks: dict[int, list[K]] = {}
    for s in standings:
        if s.tied and s.rank <= within:
            blocks.setdefault(s.rank, []).append(s.entry_id)
    return [blocks[r] for r in sorted(blocks)]


@dataclass
class FinalistPick(Generic[K]):
    finalists: list[K]
    blocking_tie: list[K] | None  # tie straddling the cutoff; run a runoff first


def select_finalists(standings: Sequence[Standing[K]], k: int) -> FinalistPick[K]:
    ranked = [s for s in standings if s.score is not None]
    finalists = [s.entry_id for s in ranked if s.rank <= k]
    if len(finalists) > k:
        cutoff_rank = max(s.rank for s in ranked if s.rank <= k)
        tie = [s.entry_id for s in ranked if s.rank == cutoff_rank]
        return FinalistPick([s.entry_id for s in ranked if s.rank < cutoff_rank], tie)
    return FinalistPick(finalists, None)


def runoff_order(standings: Sequence[Standing[K]]) -> dict[K, int]:
    """Turn runoff-round standings into a tiebreak map for the parent round."""
    return {s.entry_id: s.rank for s in standings if s.score is not None}
