"""Read-side helpers: queue planning and JSON-able snapshots of heats/tracks."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlmodel import Session, col, select

from ..core.scheduler import QueuedHeat, plan_tracks
from ..core.scoring import is_outlier
from ..timers import REGISTRY
from ..models import (
    ON_TRACK,
    Entry,
    Event,
    EventStatus,
    Group,
    Heat,
    HeatLane,
    HeatStatus,
    Racer,
    Round,
    RoundKind,
    Track,
)


def heat_started(heat: Heat, track: Track) -> bool:
    """Has this heat actually been raced (so a re-run makes sense)?

    Human-timed (regatta) heats start when someone presses START; timer
    tracks are running as soon as they're armed. Pending and approved heats
    have obviously raced.
    """
    if heat.status in (HeatStatus.pending, HeatStatus.approved):
        return True
    if heat.status != HeatStatus.armed:
        return False
    cls = REGISTRY.get(track.timing_source)
    return heat.start_ms is not None if cls and cls.human_timed else True


def vehicle_photo_url(entry: Entry) -> str | None:
    """Public URL of an entry's car/boat photo."""
    return f"/api/entry/{entry.id}/photo/entry?v={entry.photo_path}" if entry.photo_path else None


def entry_info(session: Session, entry_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not entry_ids:
        return {}
    rows = session.exec(
        select(Entry, Racer, Group)
        .join(Racer, Racer.id == Entry.racer_id)
        .join(Group, Group.id == Racer.group_id, isouter=True)
        .where(col(Entry.id).in_(entry_ids))
    ).all()
    return {
        e.id: {
            "entry_id": e.id,
            "number": e.number,
            "car_name": e.name or "",
            "photo_url": vehicle_photo_url(e),
            "racer_id": r.id,
            "racer_name": r.display_name,
            "group": g.name if g else "",
            "status": e.status.value if e.status else None,
            "status_reason": e.status_reason or "",
        }
        for e, r, g in rows
    }


def lanes_for(session: Session, heat_ids: list[int]) -> dict[int, list[HeatLane]]:
    out: dict[int, list[HeatLane]] = defaultdict(list)
    if heat_ids:
        for hl in session.exec(select(HeatLane).where(col(HeatLane.heat_id).in_(heat_ids)).order_by(HeatLane.lane)):
            out[hl.heat_id].append(hl)
    return out


def heat_views(session: Session, heats: list[Heat], track_id: int | None = None) -> list[dict[str, Any]]:
    """Snapshots of heats. ``track_id`` shows them as raced on that track
    (for a ``plan_all`` forecast); otherwise a den run still in the shared
    queue is shown as going to the next free track."""
    if not heats:
        return []
    lanes = lanes_for(session, [h.id for h in heats])
    entries = entry_info(session, {hl.entry_id for ls in lanes.values() for hl in ls if hl.entry_id})
    rounds = {r.id: r for r in session.exec(select(Round).where(col(Round.id).in_({h.round_id for h in heats})))}
    track_ids = {h.track_id for h in heats} | ({track_id} if track_id else set())
    tracks = {t.id: t for t in session.exec(select(Track).where(col(Track.id).in_(track_ids)))}
    events = {e.id: e for e in session.exec(select(Event).where(col(Event.id).in_({r.event_id for r in rounds.values()})))}
    track_count: dict[int, int] = defaultdict(int)
    for t in session.exec(select(Track).where(col(Track.event_id).in_(list(events)), Track.active == True)):  # noqa: E712
        track_count[t.event_id] += 1
    out = []
    for h in heats:
        t = tracks[track_id or h.track_id]
        r = rounds[h.round_id]
        shared = (
            track_id is None and h.status == HeatStatus.scheduled and r.kind == RoundKind.group and track_count[r.event_id] > 1
        )
        lane_rows = []
        for hl in lanes[h.id]:
            if hl.entry_id is None and hl.lane not in t.usable_lanes:
                continue  # forecast on a track without this lane
            info = entries.get(hl.entry_id) if hl.entry_id else None
            lane_rows.append(
                {
                    "lane": hl.lane,
                    "bye": hl.entry_id is None,
                    **(info or {}),
                    "raw_time": hl.raw_time,
                    "final_time": hl.final_time,
                    "dnf": hl.dnf,
                    "place": hl.place,
                    "source": hl.source,
                    "edit_reason": hl.edit_reason,
                    "stopped": hl.stop_ms is not None,
                    "outlier": is_outlier(hl.final_time, t.min_time, t.max_time),
                    "has_result": hl.final_time is not None or hl.dnf,
                }
            )
        out.append(
            {
                "id": h.id,
                "number": h.number,
                "status": h.status.value,
                "round_id": r.id,
                "round_name": r.name,
                "event_id": r.event_id,
                "event_type": events[r.event_id].type.value,
                "track_id": t.id,
                "track_name": "Next free track" if shared else t.name,
                "shared_queue": shared,
                "is_rerun": h.rerun_of_id is not None,
                "start_ms": h.start_ms,
                "started": heat_started(h, t),
                "start_source": h.start_source,
                "lanes": lane_rows,
                "racer_ids": [l["racer_id"] for l in lane_rows if not l["bye"]],
                "flagged": any(l["outlier"] for l in lane_rows),
            }
        )
    return out


def heat_view(session: Session, heat: Heat) -> dict[str, Any]:
    return heat_views(session, [heat])[0]


def active_tracks(session: Session) -> list[Track]:
    return list(
        session.exec(
            select(Track)
            .join(Event, Event.id == Track.event_id)
            .where(Track.active == True, Event.status == EventStatus.active)  # noqa: E712
            .order_by(Track.id)
        )
    )


def occupied_lanes(session: Session, heat_ids: list[int]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    if heat_ids:
        rows = session.exec(
            select(HeatLane.heat_id, HeatLane.lane).where(col(HeatLane.heat_id).in_(heat_ids), col(HeatLane.entry_id).is_not(None))
        )
        for heat_id, lane in rows:
            out[heat_id].add(lane)
    return out


def _fitting_tracks(heat: Heat, kind: RoundKind, event_tracks: list[Track], lanes: set[int]) -> list[Track]:
    """Tracks that may race ``heat``: for a den run, any of its event's active
    tracks with the heat's lanes in service (the shared queue); finals and
    runoffs stay on their own track."""
    own = [t for t in event_tracks if t.id == heat.track_id]
    if kind != RoundKind.group:
        return own
    fit = [t for t in event_tracks if lanes <= set(t.usable_lanes)]
    return fit or own


def heat_tracks(session: Session, heat: Heat) -> list[Track]:
    """Active tracks that can race this scheduled heat."""
    rnd = session.get(Round, heat.round_id)
    tracks = [t for t in active_tracks(session) if t.event_id == rnd.event_id]
    return _fitting_tracks(heat, rnd.kind, tracks, occupied_lanes(session, [heat.id])[heat.id])


def scheduled_heats(session: Session, tracks: list[Track]) -> list[tuple[Heat, list[Track]]]:
    """Every scheduled heat on these tracks' events in race order, with the
    tracks that can race it."""
    by_event: dict[int, list[Track]] = defaultdict(list)
    for t in tracks:
        by_event[t.event_id].append(t)
    rows = session.exec(
        select(Heat, Round.kind, Round.event_id)
        .join(Round, Round.id == Heat.round_id)
        .where(col(Round.event_id).in_(list(by_event)), Heat.status == HeatStatus.scheduled)
        .order_by(Round.seq, Heat.seq)
    ).all()
    lanes = occupied_lanes(session, [h.id for h, _, _ in rows])
    out = []
    for h, kind, event_id in rows:
        fit = _fitting_tracks(h, kind, by_event[event_id], lanes[h.id])
        if fit:
            out.append((h, fit))
    return out


def racers_by_heat(session: Session, heat_ids: list[int]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    if heat_ids:
        rows = session.exec(
            select(HeatLane.heat_id, Entry.racer_id)
            .join(Entry, Entry.id == HeatLane.entry_id)
            .where(col(HeatLane.heat_id).in_(heat_ids))
        )
        for heat_id, racer_id in rows:
            out[heat_id].add(racer_id)
    return out


def on_track_heats(session: Session) -> list[Heat]:
    return list(session.exec(select(Heat).where(col(Heat.status).in_(ON_TRACK))))


def on_track_racers(session: Session) -> dict[int, set[int]]:
    heats = on_track_heats(session)
    rbh = racers_by_heat(session, [h.id for h in heats])
    out: dict[int, set[int]] = defaultdict(set)
    for h in heats:
        out[h.track_id] |= rbh[h.id]
    return out


def plan_all(session: Session, limit: int | None = None, first: int | None = None) -> dict[int, list[Heat]]:
    """Forecast of every active track's upcoming heats; index 0 is the next
    heat to stage. Den runs share one queue per event, so these lists show
    where heats are expected to go, and a heat's ``track_id`` is only fixed
    when a track stages it. ``first`` gives one track first pick (the track
    asking to stage now).

    Planning is global (all active events) because racers are shared across
    events: a scout racing a car can't simultaneously sail a boat.
    """
    tracks = active_tracks(session)
    sched = scheduled_heats(session, tracks)
    rbh = racers_by_heat(session, [h.id for h, _ in sched])
    queue = [QueuedHeat.of(h.id, rbh[h.id], (t.id for t in fit)) for h, fit in sched]
    planned = plan_tracks(queue, [t.id for t in tracks], on_track_racers(session), first=first)
    by_id = {h.id: h for h, _ in sched}
    out = {}
    for tid, q in planned.items():
        heats = [by_id[qh.heat_id] for qh in q]
        out[tid] = heats[:limit] if limit else heats
    return out


def track_state(session: Session, track: Track, timer_status: dict | None = None, upcoming: int = 3) -> dict[str, Any]:
    current = session.exec(
        select(Heat).where(Heat.track_id == track.id, col(Heat.status).in_(ON_TRACK))
    ).first()
    pending = list(
        session.exec(select(Heat).where(Heat.track_id == track.id, Heat.status == HeatStatus.pending).order_by(Heat.results_at))
    )
    last = session.exec(
        select(Heat)
        .where(Heat.track_id == track.id, Heat.status == HeatStatus.approved)
        .order_by(col(Heat.approved_at).desc())
    ).first()
    plan = plan_all(session).get(track.id, [])
    blocked_ids: set[int] = set()
    if plan:
        others = on_track_racers(session)
        busy = set().union(*(r for t, r in others.items() if t != track.id)) if others else set()
        rbh = racers_by_heat(session, [h.id for h in plan[:upcoming]])
        blocked_ids = {h.id for h in plan[:upcoming] if rbh[h.id] & busy}
    up = heat_views(session, plan[:upcoming], track_id=track.id)
    for hv in up:
        hv["blocked"] = hv["id"] in blocked_ids
    return {
        "track": {
            "id": track.id,
            "name": track.name,
            "event_id": track.event_id,
            "lane_count": track.lane_count,
            "timing_source": track.timing_source,
            "min_time": track.min_time,
            "max_time": track.max_time,
        },
        "timer": timer_status or {"connected": False, "detail": "not running"},
        "current": heat_view(session, current) if current else None,
        "pending": heat_views(session, pending),
        "upcoming": up,
        "remaining": len(plan),
        "last": heat_view(session, last) if last else None,
    }
