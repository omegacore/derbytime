"""Building rounds/heats from the roster, and standings from approved heats.

Each den races in one or more *passes*: a pass is a group Round holding one
full lane rotation of that den's entries. The order of passes (Round.seq) is
the race order and can be rearranged at any time; standings combine all of a
den's passes.
"""
from __future__ import annotations

import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, col, delete, select

from ..core import scheduler, scoring
from ..models import (
    ACTIVE,
    ON_TRACK,
    Entry,
    EntryStatus,
    Event,
    EventStatus,
    Group,
    Heat,
    HeatLane,
    HeatStatus,
    InspectionStatus,
    Racer,
    Round,
    RoundKind,
    Track,
    utcnow,
)
from . import Actor, ServiceError, views
from .audit import audit
from .views import entry_info

FINAL_SEQ = 1000
NO_DEN = "Open"


def eligible_entries(session: Session, event_id: int) -> list[tuple[Entry, Racer]]:
    return list(
        session.exec(
            select(Entry, Racer)
            .join(Racer, Racer.id == Entry.racer_id)
            .where(
                Entry.event_id == event_id,
                Entry.checked_in == True,  # noqa: E712
                Entry.inspection_status != InspectionStatus.failed,
                col(Entry.status).is_(None),
                col(Racer.inactive_since).is_(None),
            )
            .order_by(Entry.number)
        )
    )


def entries_with_status(session: Session, event_id: int, *statuses: EntryStatus) -> set[int]:
    return set(session.exec(select(Entry.id).where(Entry.event_id == event_id, col(Entry.status).in_(statuses))))


def out_of_race(session: Session, event_id: int) -> set[int]:
    """Entries that race no more: withdrawn, disqualified, or the scout went inactive."""
    return entries_with_status(session, event_id, *EntryStatus) | set(
        session.exec(
            select(Entry.id)
            .join(Racer, Racer.id == Entry.racer_id)
            .where(Entry.event_id == event_id, col(Racer.inactive_since).is_not(None))
        )
    )


def event_tracks(session: Session, event_id: int) -> list[Track]:
    return list(session.exec(select(Track).where(Track.event_id == event_id, Track.active == True).order_by(Track.id)))  # noqa: E712


# Heats that have happened (or are happening) are never touched by a rebuild.
RACED = (HeatStatus.staged, HeatStatus.armed, HeatStatus.pending, HeatStatus.approved)


# --- den passes ---------------------------------------------------------------


def den_name(session: Session, group_id: int | None) -> str:
    g = session.get(Group, group_id) if group_id else None
    return g.name if g else NO_DEN


def pass_name(den: str, n: int) -> str:
    return den if n == 1 else f"{den} · run {n}"


def den_entries(session: Session, event_id: int) -> dict[int | None, list[int]]:
    by_group: dict[int | None, list[int]] = defaultdict(list)
    for entry, racer in eligible_entries(session, event_id):
        by_group[racer.group_id].append(entry.id)
    return by_group


def group_rounds(session: Session, event_id: int) -> list[Round]:
    """The event's den passes in race order."""
    return list(
        session.exec(
            select(Round).where(Round.event_id == event_id, Round.kind == RoundKind.group).order_by(Round.seq, Round.id)
        )
    )


def _run_number(session: Session, rnd: Round) -> int:
    """1 for a den's first run, 2 for its second, … (by creation order)."""
    ids = sorted(r.id for r in group_rounds(session, rnd.event_id) if r.group_id == rnd.group_id)
    return ids.index(rnd.id) + 1 if rnd.id in ids else 1


def _lineup_order(event_id: int, rnd: Round, run: int, entry_ids: list[int]) -> list[int]:
    """Run 1 goes in car-number order; later runs shuffle (deterministically)
    so scouts meet different opponents while still running every lane once."""
    ids = list(entry_ids)
    if rnd.kind == RoundKind.group and run > 1:
        random.Random(f"{event_id}-{rnd.group_id}-{run}").shuffle(ids)
    return ids


def _round_tracks(session: Session, rnd: Round, tracks: list[Track]) -> list[Track]:
    """Den runs use every active track; runoffs and finals stay on one track."""
    if rnd.kind == RoundKind.group:
        return tracks
    tid = session.exec(select(Heat.track_id).where(Heat.round_id == rnd.id)).first()
    return [next((t for t in tracks if t.id == tid), tracks[0])]


def _build_round_heats(
    session: Session,
    event: Event,
    rnd: Round,
    tracks: list[Track],
    members: list[int],
    start_seq: float = 0,
) -> dict[str, int]:
    """(Re)build the unraced heats of one run.

    Raced heats (staged, armed, pending, approved) are kept. Every member is
    then owed runs up to the run's length: a full lane rotation for a fresh
    run, or the length the run started with if it's already under way (so all
    cars in a run race the same number of times, even if a lane broke).
    """
    heats = list(session.exec(select(Heat).where(Heat.round_id == rnd.id)))
    lanes = views.lanes_for(session, [h.id for h in heats])
    raced = [h for h in heats if h.status in RACED]
    unraced = [h for h in heats if h.status == HeatStatus.scheduled]
    width = max(len(t.usable_lanes) for t in tracks)

    def runs_in(hs: list[Heat]) -> Counter:
        return Counter(hl.entry_id for h in hs for hl in lanes[h.id] if hl.entry_id)

    planned_before = runs_in(raced + unraced)
    target = max(planned_before.values(), default=width) if raced else width
    done = runs_in(raced)
    used: dict[int, Counter] = defaultdict(Counter)
    for h in raced:
        for hl in lanes[h.id]:
            if hl.entry_id:
                used[hl.entry_id][hl.lane] += 1
    need = {e: max(0, target - done[e]) for e in members}

    unraced_ids = [h.id for h in unraced]
    if unraced_ids:
        session.exec(delete(HeatLane).where(col(HeatLane.heat_id).in_(unraced_ids)))
        session.exec(delete(Heat).where(col(Heat.id).in_(unraced_ids)))

    if raced:
        base_seq = max(h.seq for h in raced)
    elif heats:
        base_seq = min(h.seq for h in heats) - 1
    else:
        base_seq = start_seq
    base_num = max((h.number for h in raced), default=0)
    order = _lineup_order(event.id, rnd, _run_number(session, rnd), members)
    plan = scheduler.plan_run(order, [t.usable_lanes for t in tracks], need, used)
    for i, (ti, lineup) in enumerate(plan, start=1):
        track = tracks[ti]
        heat = Heat(round_id=rnd.id, track_id=track.id, seq=base_seq + i, number=base_num + i)
        session.add(heat)
        session.flush()
        for lane in track.usable_lanes:
            session.add(HeatLane(heat_id=heat.id, lane=lane, entry_id=lineup.get(lane)))
    return {"kept": len(raced), "removed": len(unraced), "added": len(plan)}


def _round_members(session: Session, rnd: Round, den_map: dict[int | None, list[int]]) -> list[int]:
    if rnd.kind == RoundKind.group:
        return list(den_map.get(rnd.group_id, []))
    # runoffs and finals keep the entries they were created with (less any
    # that have been withdrawn or disqualified since)
    out = out_of_race(session, rnd.event_id)
    rows = session.exec(
        select(HeatLane.entry_id, Entry.number)
        .join(Heat, Heat.id == HeatLane.heat_id)
        .join(Entry, Entry.id == HeatLane.entry_id)
        .where(Heat.round_id == rnd.id, Heat.status != HeatStatus.rerun)
    ).all()
    return [e for e, _ in sorted(set(rows), key=lambda r: r[1]) if e not in out]


def _new_run(session: Session, event: Event, group_id: int | None, n: int, seq: int) -> Round:
    rnd = Round(
        event_id=event.id,
        name=pass_name(den_name(session, group_id), n),
        kind=RoundKind.group,
        group_id=group_id,
        seq=seq,
    )
    session.add(rnd)
    session.flush()
    return rnd


def _active_tracks_or_error(session: Session, event: Event) -> list[Track]:
    tracks = [t for t in event_tracks(session, event.id) if t.usable_lanes]
    if not tracks:
        raise ServiceError("Add at least one active track with a working lane first.")
    return tracks


def generate_group_rounds(session: Session, event: Event, actor: Actor, passes: int | None = None) -> list[Round]:
    """Build every den's runs. Dens are sorted by name (numbers compared
    numerically) and the race order snakes: down the list, back up, …
    Each run is spread over all of the event's active tracks."""
    if session.exec(select(Round).where(Round.event_id == event.id)).first():
        raise ServiceError("This event already has a schedule. Use Regenerate to rebuild it.")
    tracks = _active_tracks_or_error(session, event)
    by_group = den_entries(session, event.id)
    if not by_group:
        raise ServiceError("No checked-in entries to schedule.")
    if passes is None:
        passes = 2
    if not 1 <= passes <= 6:
        raise ServiceError("Runs per den must be between 1 and 6.")

    dens = sorted(by_group, key=lambda gid: scheduler.natural_key(den_name(session, gid)))
    rounds = []
    count: dict[int | None, int] = defaultdict(int)
    for seq, gid in enumerate(scheduler.snake_order(dens, passes), start=1):
        count[gid] += 1
        rnd = _new_run(session, event, gid, count[gid], seq)
        _build_round_heats(session, event, rnd, tracks, by_group[gid])
        rounds.append(rnd)
    event.status = EventStatus.active
    session.add(event)
    audit(session, actor, "schedule.generate", "event", event.id, after={"order": [r.name for r in rounds]})
    return rounds


def regenerate(session: Session, event: Event, actor: Actor, reason: str = "") -> dict[str, int]:
    """Rebuild every unraced heat from the current roster, tracks and lanes.

    Raced heats and their results stay. Late check-ins get full runs, scratched
    scouts lose their remaining heats, new or re-enabled tracks and lanes are
    used, and dens that weren't scheduled yet get the same number of runs as
    the others (added at the end of the race order).
    """
    rounds_all = list(session.exec(select(Round).where(Round.event_id == event.id).order_by(Round.seq, Round.id)))
    if not rounds_all:
        raise ServiceError("Generate a schedule first.")
    tracks = _active_tracks_or_error(session, event)
    den_map = den_entries(session, event.id)

    runs = group_rounds(session, event.id)
    per_den = Counter(r.group_id for r in runs)
    new_dens = sorted((g for g in den_map if g not in per_den), key=lambda g: scheduler.natural_key(den_name(session, g)))
    seq = max((r.seq for r in runs), default=0)
    for gid in new_dens:
        for n in range(1, max(per_den.values(), default=1) + 1):
            seq += 1
            rounds_all.append(_new_run(session, event, gid, n, seq))

    totals: Counter = Counter()
    for rnd in rounds_all:
        members = _round_members(session, rnd, den_map)
        totals.update(_build_round_heats(session, event, rnd, _round_tracks(session, rnd, tracks), members))
    _sync_runoff_seqs(session, event.id)
    if event.status == EventStatus.setup:
        event.status = EventStatus.active
        session.add(event)
    result = {"kept": totals["kept"], "removed": totals["removed"], "added": totals["added"], "new_dens": len(new_dens)}
    audit(session, actor, "schedule.regenerate", "event", event.id, after=result, reason=reason or None)
    return result


def schedule_problems(session: Session, event: Event) -> dict[str, list[str]]:
    """Roster changes the schedule hasn't caught up with yet."""
    runs = group_rounds(session, event.id)
    if not runs:
        return {}
    den_map = den_entries(session, event.id)
    eligible = {e for ids in den_map.values() for e in ids}
    rows = session.exec(
        select(HeatLane.entry_id, Heat.status)
        .join(Heat, Heat.id == HeatLane.heat_id)
        .where(col(Heat.round_id).in_([r.id for r in runs]), Heat.status != HeatStatus.rerun)
    ).all()
    scheduled = {e for e, _ in rows if e}
    unraced = {e for e, st in rows if e and st == HeatStatus.scheduled}
    missing, scratched = eligible - scheduled, unraced - eligible
    info = entry_info(session, missing | scratched)

    def labels(ids: set[int]) -> list[str]:
        return [f"#{info[e]['number']} {info[e]['racer_name']}" for e in sorted(ids, key=lambda e: info[e]["number"])]

    out = {"missing": labels(missing), "scratched": labels(scratched)}
    return {k: v for k, v in out.items() if v}


def _round_started(session: Session, round_id: int) -> bool:
    return session.exec(
        select(Heat).where(Heat.round_id == round_id, col(Heat.status).in_([*ACTIVE, HeatStatus.approved, HeatStatus.rerun]))
    ).first() is not None


def _sync_runoff_seqs(session: Session, event_id: int) -> None:
    """Runoffs run right after the last pass of their den."""
    last = defaultdict(int)
    for r in group_rounds(session, event_id):
        last[r.group_id] = max(last[r.group_id], r.seq)
    for ro in session.exec(select(Round).where(Round.event_id == event_id, Round.kind == RoundKind.runoff)):
        parent = session.get(Round, ro.parent_round_id)
        if parent and parent.kind == RoundKind.group and parent.group_id in last:
            ro.seq = last[parent.group_id]
            session.add(ro)


def set_race_order(session: Session, event: Event, round_ids: list[int], actor: Actor) -> None:
    current = group_rounds(session, event.id)
    if sorted(round_ids) != sorted(r.id for r in current):
        raise ServiceError("The race order changed in the meantime. Reload and try again.", 409)
    by_id = {r.id: r for r in current}
    before = [r.name for r in current]
    for seq, rid in enumerate(round_ids, start=1):
        by_id[rid].seq = seq
        session.add(by_id[rid])
    _sync_runoff_seqs(session, event.id)
    audit(session, actor, "schedule.reorder", "event", event.id, before={"order": before}, after={"order": [by_id[i].name for i in round_ids]})


def add_pass(session: Session, event: Event, group_id: int | None, actor: Actor) -> Round:
    """Append another full run for a den to the end of the race order."""
    entries = den_entries(session, event.id).get(group_id)
    if not entries:
        raise ServiceError("That den has no checked-in entries in this event.")
    tracks = _active_tracks_or_error(session, event)
    existing = [r for r in group_rounds(session, event.id) if r.group_id == group_id]
    seq = max((r.seq for r in group_rounds(session, event.id)), default=0) + 1
    rnd = _new_run(session, event, group_id, len(existing) + 1, seq)
    _build_round_heats(session, event, rnd, tracks, entries)
    _sync_runoff_seqs(session, event.id)
    audit(session, actor, "schedule.add_pass", "round", rnd.id, after={"name": rnd.name})
    return rnd


def remove_pass(session: Session, rnd: Round, actor: Actor) -> None:
    if rnd.kind != RoundKind.group:
        raise ServiceError("Only den runs can be removed here.")
    if _round_started(session, rnd.id):
        raise ServiceError(f"{rnd.name} has already started racing and can't be removed.", 409)
    heat_ids = list(session.exec(select(Heat.id).where(Heat.round_id == rnd.id)))
    session.exec(delete(HeatLane).where(col(HeatLane.heat_id).in_(heat_ids)))
    session.exec(delete(Heat).where(col(Heat.id).in_(heat_ids)))
    session.delete(rnd)
    session.flush()
    # keep run numbers tidy: "Den 3 · run 3" becomes "run 2" if run 2 was removed
    same = [r for r in group_rounds(session, rnd.event_id) if r.group_id == rnd.group_id]
    for n, r in enumerate(sorted(same, key=lambda r: r.id), start=1):
        r.name = pass_name(den_name(session, rnd.group_id), n)
        session.add(r)
    _sync_runoff_seqs(session, rnd.event_id)
    audit(session, actor, "schedule.remove_pass", "round", rnd.id, before={"name": rnd.name})


def race_order(session: Session, event: Event) -> list[dict[str, Any]]:
    """Rows for the admin race-order list."""
    rows = []
    tracks = {t.id: t for t in session.exec(select(Track).where(Track.event_id == event.id))}
    for r in group_rounds(session, event.id):
        heats = session.exec(select(Heat.status, Heat.track_id).where(Heat.round_id == r.id, Heat.status != HeatStatus.rerun)).all()
        statuses = [s for s, _ in heats]
        done = sum(1 for s in statuses if s == HeatStatus.approved)
        live = any(s in ACTIVE for s in statuses)
        used = sorted({tid for _, tid in heats if tid in tracks})
        rows.append(
            {
                "round": r,
                "tracks": [tracks[tid].name for tid in used],
                "done": done,
                "total": len(statuses),
                "state": "done" if statuses and done == len(statuses) else "racing" if live or done else "waiting",
                "removable": not _round_started(session, r.id),
            }
        )
    return rows


# --- projected end time ------------------------------------------------------

PACE_SAMPLE = 8  # recent heat-to-heat gaps that set a track's measured pace
PACE_MIN_SAMPLES = 3  # fewer gaps than this: use the configured estimate
PACE_MAX_GAP = 600.0  # a longer gap is a break, not the racing pace


def track_pace(session: Session, track_id: int) -> float | None:
    """Median seconds between recent results on a track, or None if too few.

    The gap between one heat's results and the next covers the race itself and
    all the overhead around it (approving, loading cars, staging, re-runs).
    """
    stamps = list(
        session.exec(
            select(Heat.results_at)
            .where(Heat.track_id == track_id, col(Heat.results_at).is_not(None))
            .order_by(col(Heat.results_at).desc())
            .limit(PACE_SAMPLE + 1)
        )
    )
    gaps = [(a - b).total_seconds() for a, b in zip(stamps, stamps[1:])]
    gaps = [g for g in gaps if 0 < g <= PACE_MAX_GAP]
    return statistics.median(gaps) if len(gaps) >= PACE_MIN_SAMPLES else None


def _clock(dt: datetime) -> str:
    # the server runs at the event, so its local time is the event's
    return dt.astimezone().strftime("%I:%M %p").lstrip("0")


def _duration(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def projected_finish(session: Session, event: Event, heat_seconds: float, now: datetime | None = None) -> dict[str, Any] | None:
    """When the event's remaining heats should finish, or None if none remain.

    Tracks race in parallel, so the event ends when its slowest track does.
    Each track uses its measured pace once it has one, else `heat_seconds`
    (configured race time plus overhead). Heats on a track now finish there;
    queued heats are shared out the way the shared queue hands them out, each
    to whichever track that can race it would get to it first, so a faster
    track takes more of them.
    """
    now = now or utcnow()
    tracks = list(session.exec(select(Track).where(Track.event_id == event.id, Track.active == True).order_by(Track.id)))  # noqa: E712
    measured = {t.id: track_pace(session, t.id) for t in tracks}
    pace = {t.id: measured[t.id] or heat_seconds for t in tracks}
    count: Counter = Counter()
    busy = {t.id: 0.0 for t in tracks}
    for h in session.exec(select(Heat).where(col(Heat.track_id).in_(list(busy)), col(Heat.status).in_(ON_TRACK))):
        count[h.track_id] += 1
        busy[h.track_id] += pace[h.track_id]
    for _, fit in views.scheduled_heats(session, tracks):
        t = min(fit, key=lambda t: (busy[t.id] + pace[t.id], t.id))
        count[t.id] += 1
        busy[t.id] += pace[t.id]
    rows = []
    for t in tracks:
        remaining, left = count[t.id], busy[t.id]
        if not remaining:
            continue
        rows.append(
            {
                "track": t,
                "remaining": remaining,
                "pace": round(pace[t.id]),
                "measured": measured[t.id] is not None,
                "left": _duration(left),
                "end": _clock(now + timedelta(seconds=left)),
                "seconds": left,
            }
        )
    if not rows:
        return None
    left = max(r["seconds"] for r in rows)
    return {
        "tracks": rows,
        "remaining": sum(r["remaining"] for r in rows),
        "seconds": left,
        "left": _duration(left),
        "end": _clock(now + timedelta(seconds=left)),
    }


def clear_schedule(session: Session, event: Event, actor: Actor) -> None:
    rounds = list(session.exec(select(Round.id).where(Round.event_id == event.id)))
    if not rounds:
        return
    heat_ids = list(session.exec(select(Heat.id).where(col(Heat.round_id).in_(rounds))))
    raced = session.exec(
        select(Heat).where(
            col(Heat.id).in_(heat_ids),
            col(Heat.status).in_([HeatStatus.armed, HeatStatus.pending, HeatStatus.approved]),
        )
    ).first()
    if raced:
        raise ServiceError("Heats have already been raced; the schedule can't be cleared.")
    session.exec(delete(HeatLane).where(col(HeatLane.heat_id).in_(heat_ids)))
    session.exec(delete(Heat).where(col(Heat.id).in_(heat_ids)))
    session.exec(delete(Round).where(col(Round.parent_round_id).is_not(None), col(Round.id).in_(rounds)))
    session.exec(delete(Round).where(col(Round.id).in_(rounds)))
    event.status = EventStatus.setup
    session.add(event)
    audit(session, actor, "schedule.clear", "event", event.id)


# --- standings ---------------------------------------------------------------


def round_progress(session: Session, round_id: int) -> tuple[int, int]:
    """(approved heats, total heats) excluding superseded reruns."""
    statuses = list(session.exec(select(Heat.status).where(Heat.round_id == round_id, Heat.status != HeatStatus.rerun)))
    return sum(1 for s in statuses if s == HeatStatus.approved), len(statuses)


def round_runs(session: Session, round_ids: list[int], exclude: set[int] = frozenset()) -> tuple[list[scoring.RunResult[int]], set[int]]:
    """Approved runs of the rounds, and every entry in them. ``exclude``
    (disqualified entries) leaves entries out of the ranking; heat places and
    points of the others stay as raced."""
    rows = session.exec(
        select(HeatLane, Heat)
        .join(Heat, Heat.id == HeatLane.heat_id)
        .where(col(Heat.round_id).in_(round_ids))
    ).all()
    entries: set[int] = set()
    by_heat: dict[int, list[HeatLane]] = defaultdict(list)
    fields = Counter(heat.id for hl, heat in rows if hl.entry_id is not None)
    for hl, heat in rows:
        if hl.entry_id is None or hl.entry_id in exclude:
            continue
        if heat.status != HeatStatus.rerun:
            entries.add(hl.entry_id)
        if heat.status == HeatStatus.approved:
            by_heat[heat.id].append(hl)
    runs = []
    for heat_id, lanes in by_heat.items():
        field = fields[heat_id]
        for hl in lanes:
            runs.append(scoring.RunResult(hl.entry_id, None if hl.dnf else hl.final_time, hl.place or field, field))
    return runs, entries


def scoring_rounds(session: Session, rnd: Round) -> list[Round]:
    """Rounds scored together with ``rnd``: all passes of its den, or just itself."""
    if rnd.kind == RoundKind.group:
        return [r for r in group_rounds(session, rnd.event_id) if r.group_id == rnd.group_id]
    return [rnd]


def _runoffs_for(session: Session, rounds: list[Round]) -> list[Round]:
    ids = [r.id for r in rounds]
    return list(session.exec(select(Round).where(col(Round.parent_round_id).in_(ids)).order_by(Round.id)))


def round_standings(session: Session, rnd: Round, event: Event, exclude: set[int] | None = None) -> list[scoring.Standing[int]]:
    """Standings for ``rnd``'s den (all passes) or final, with runoffs breaking
    ties. Disqualified entries are left out unless ``exclude`` says otherwise."""
    if exclude is None:
        exclude = entries_with_status(session, event.id, EntryStatus.disqualified)
    rounds = scoring_rounds(session, rnd)
    runs, entries = round_runs(session, [r.id for r in rounds], exclude)
    tiebreak: dict[int, int] = {}
    offset = 0
    for ro in _runoffs_for(session, rounds):
        ro_runs, ro_entries = round_runs(session, [ro.id], exclude)
        ro_st = scoring.compute_standings(ro_runs, event.scoring_mode.value, event.dnf_time, entries=sorted(ro_entries))
        for eid, rank in scoring.runoff_order(ro_st).items():
            tiebreak[eid] = offset + rank
        offset += 1000
    return scoring.compute_standings(runs, event.scoring_mode.value, event.dnf_time, entries=sorted(entries), tiebreak=tiebreak)


def den_progress(session: Session, rnd: Round, with_runoffs: bool = True) -> tuple[int, int]:
    rounds = scoring_rounds(session, rnd)
    if with_runoffs:
        rounds = rounds + _runoffs_for(session, rounds)
    approved = total = 0
    for r in rounds:
        a, t = round_progress(session, r.id)
        approved, total = approved + a, total + t
    return approved, total


def event_standings(session: Session, event: Event) -> list[dict[str, Any]]:
    """One block per den (passes combined, in den-name order), then the final."""
    out = []
    seen: set = set()
    groups = sorted(
        {r.group_id: r for r in reversed(group_rounds(session, event.id))}.values(),
        key=lambda r: scheduler.natural_key(den_name(session, r.group_id)),
    )
    finals = session.exec(select(Round).where(Round.event_id == event.id, Round.kind == RoundKind.final).order_by(Round.seq)).all()
    dq = entries_with_status(session, event.id, EntryStatus.disqualified)
    gone = dq | entries_with_status(session, event.id, EntryStatus.withdrawn)
    for rnd in [*groups, *finals]:
        if (rnd.kind, rnd.group_id) in seen:
            continue
        seen.add((rnd.kind, rnd.group_id))
        st = round_standings(session, rnd, event, dq)
        dq_here = round_runs(session, [r.id for r in scoring_rounds(session, rnd)])[1] & dq if dq else set()
        info = entry_info(session, {s.entry_id for s in st} | dq_here)
        approved, total = den_progress(session, rnd, with_runoffs=False)
        cutoff = event.finalists_per_group if rnd.kind == RoundKind.group else 3
        complete = total > 0 and approved == total
        out.append(
            {
                "round": rnd,
                "name": den_name(session, rnd.group_id) if rnd.kind == RoundKind.group else rnd.name,
                "approved": approved,
                "total": total,
                "complete": complete,
                "rows": [{"s": s, **info[s.entry_id]} for s in st],
                "disqualified": sorted((info[e] for e in dq_here), key=lambda i: i["number"]),
                # only offer runoffs once a den has finished every pass, and
                # only between scouts still racing
                "ties": [[info[e] for e in block] for block in scoring.tie_blocks(still_racing(session, rnd, event, st, gone), cutoff)] if complete else [],
                "cutoff": cutoff,
            }
        )
    return out


def still_racing(session: Session, rnd: Round, event: Event, st: list[scoring.Standing[int]], gone: set[int]) -> list[scoring.Standing[int]]:
    """Standings re-ranked without withdrawn (and disqualified) entries, for
    finalist and runoff decisions."""
    if not gone & {s.entry_id for s in st}:
        return st
    return round_standings(session, rnd, event, gone)


def pack_standings(session: Session, event: Event) -> dict[str, Any]:
    """Every den's runs pooled into one pack-wide ranking (no runoffs)."""
    runs_all = group_rounds(session, event.id)
    dq = entries_with_status(session, event.id, EntryStatus.disqualified)
    runs, entries = round_runs(session, [r.id for r in runs_all], dq) if runs_all else ([], set())
    st = scoring.compute_standings(runs, event.scoring_mode.value, event.dnf_time, entries=sorted(entries))
    info = entry_info(session, {s.entry_id for s in st})
    approved = total = 0
    for rnd in runs_all:
        a, t = round_progress(session, rnd.id)
        approved, total = approved + a, total + t
    return {
        "round": None,
        "name": "Pack Overall",
        "approved": approved,
        "total": total,
        "complete": total > 0 and approved == total,
        "rows": [{"s": s, **info[s.entry_id]} for s in st],
    }


def create_runoff(session: Session, event: Event, parent: Round, entry_ids: list[int], track_id: int | None, actor: Actor) -> Round:
    if len(entry_ids) < 2:
        raise ServiceError("A runoff needs at least two entries.")
    tracks = _active_tracks_or_error(session, event)
    track = next((t for t in tracks if t.id == track_id), tracks[0])
    # attach to the den's last pass so it runs after the den finishes
    parent = max(scoring_rounds(session, parent), key=lambda r: r.seq)
    n = len(_runoffs_for(session, scoring_rounds(session, parent))) + 1
    den = den_name(session, parent.group_id) if parent.kind == RoundKind.group else parent.name
    rnd = Round(
        event_id=event.id,
        name=f"{den} runoff{'' if n == 1 else f' {n}'}",
        kind=RoundKind.runoff,
        parent_round_id=parent.id,
        group_id=parent.group_id,
        seq=parent.seq,
    )
    session.add(rnd)
    session.flush()
    last_seq = session.exec(select(Heat.seq).where(Heat.round_id == parent.id).order_by(col(Heat.seq).desc())).first() or 0
    _build_round_heats(session, event, rnd, [track], list(entry_ids), start_seq=float(last_seq))
    audit(session, actor, "round.runoff", "round", rnd.id, after={"parent": parent.id, "entries": entry_ids})
    return rnd


def create_final(session: Session, event: Event, track_id: int | None, actor: Actor) -> Round:
    if session.exec(select(Round).where(Round.event_id == event.id, Round.kind == RoundKind.final)).first():
        raise ServiceError("A final round already exists.")
    blocks = [b for b in event_standings(session, event) if b["round"].kind == RoundKind.group]
    if not blocks:
        raise ServiceError("Generate the den rounds first.")
    finalists: list[int] = []
    gone = entries_with_status(session, event.id, *EntryStatus)
    for b in blocks:
        approved, total = den_progress(session, b["round"])
        if approved != total:
            raise ServiceError(f"{b['name']}: {total - approved} heat(s) not approved yet.")
        st = still_racing(session, b["round"], event, [row["s"] for row in b["rows"]], gone)
        pick = scoring.select_finalists(st, event.finalists_per_group)
        if pick.blocking_tie:
            nums = ", ".join(f"#{i['number']}" for i in entry_info(session, set(pick.blocking_tie)).values())
            raise ServiceError(f"{b['name']}: tie at the finalist cutoff ({nums}). Run a runoff first.")
        finalists += pick.finalists
    if len(finalists) < 2:
        raise ServiceError("Not enough finalists.")
    tracks = _active_tracks_or_error(session, event)
    track = next((t for t in tracks if t.id == track_id), tracks[0])
    rnd = Round(event_id=event.id, name="Final", kind=RoundKind.final, seq=FINAL_SEQ)
    session.add(rnd)
    session.flush()
    # the final stays on one track so every finalist races the same lanes
    _build_round_heats(session, event, rnd, [track], finalists)
    audit(session, actor, "round.final", "round", rnd.id, after={"entries": finalists})
    return rnd
