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
        }
        for e, r, g in rows
    }


def lanes_for(session: Session, heat_ids: list[int]) -> dict[int, list[HeatLane]]:
    out: dict[int, list[HeatLane]] = defaultdict(list)
    if heat_ids:
        for hl in session.exec(select(HeatLane).where(col(HeatLane.heat_id).in_(heat_ids)).order_by(HeatLane.lane)):
            out[hl.heat_id].append(hl)
    return out


def heat_views(session: Session, heats: list[Heat]) -> list[dict[str, Any]]:
    if not heats:
        return []
    lanes = lanes_for(session, [h.id for h in heats])
    entries = entry_info(session, {hl.entry_id for ls in lanes.values() for hl in ls if hl.entry_id})
    rounds = {r.id: r for r in session.exec(select(Round).where(col(Round.id).in_({h.round_id for h in heats})))}
    tracks = {t.id: t for t in session.exec(select(Track).where(col(Track.id).in_({h.track_id for h in heats})))}
    events = {e.id: e for e in session.exec(select(Event).where(col(Event.id).in_({r.event_id for r in rounds.values()})))}
    out = []
    for h in heats:
        t = tracks[h.track_id]
        lane_rows = []
        for hl in lanes[h.id]:
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
        r = rounds[h.round_id]
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
                "track_name": t.name,
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


def scheduled_heats(session: Session, track_ids: list[int]) -> dict[int, list[Heat]]:
    out: dict[int, list[Heat]] = {t: [] for t in track_ids}
    rows = session.exec(
        select(Heat)
        .join(Round, Round.id == Heat.round_id)
        .where(col(Heat.track_id).in_(track_ids), Heat.status == HeatStatus.scheduled)
        .order_by(Round.seq, Heat.seq)
    )
    for h in rows:
        out[h.track_id].append(h)
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


def plan_all(session: Session, limit: int | None = None) -> dict[int, list[Heat]]:
    """Every active track's queue, ordered so index 0 is the next heat to stage.

    Planning is global (all active events) because racers are shared across
    events: a scout racing a car can't simultaneously sail a boat.
    """
    tracks = active_tracks(session)
    sched = scheduled_heats(session, [t.id for t in tracks])
    all_ids = [h.id for hs in sched.values() for h in hs]
    rbh = racers_by_heat(session, all_ids)
    queues = {tid: [QueuedHeat(h.id, frozenset(rbh[h.id])) for h in hs] for tid, hs in sched.items()}
    planned = plan_tracks(queues, on_track_racers(session))
    by_id = {h.id: h for hs in sched.values() for h in hs}
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
    up = heat_views(session, plan[:upcoming])
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
