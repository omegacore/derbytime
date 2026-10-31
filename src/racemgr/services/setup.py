"""Creating events and tracks with defaults from config.toml."""
from __future__ import annotations

from sqlmodel import Session

from ..config import Settings
from ..models import Event, EventType, ScoringMode, Track
from ..timers import REGISTRY
from . import Actor, ServiceError
from .audit import audit


def create_event(
    session: Session,
    settings: Settings,
    name: str,
    type: EventType | str,
    actor: Actor,
    scoring_mode: str | None = None,
    event_date: str | None = None,
) -> Event:
    type = EventType(type)
    d = settings.type_defaults(type.value)
    if not name.strip():
        raise ServiceError("Event name is required.")
    ev = Event(
        name=name.strip(),
        type=type,
        scoring_mode=ScoringMode(scoring_mode or d.scoring_mode),
        event_date=event_date or None,
        dnf_time=d.dnf_time,
        finalists_per_group=d.finalists_per_group,
    )
    session.add(ev)
    session.flush()
    audit(session, actor, "event.create", "event", ev.id, after={"name": ev.name, "type": type.value})
    return ev


def add_track(
    session: Session,
    settings: Settings,
    event: Event,
    name: str,
    actor: Actor,
    lane_count: int | None = None,
    timing_source: str | None = None,
) -> Track:
    d = settings.type_defaults(event.type.value)
    source = timing_source or d.timing_source
    if source not in REGISTRY:
        raise ServiceError(f"Unknown timing source {source!r}.")
    lanes = lane_count or d.lane_count
    if not 1 <= lanes <= 12:
        raise ServiceError("Lane count must be between 1 and 12.")
    t = Track(
        event_id=event.id,
        name=name.strip() or "Track",
        lane_count=lanes,
        timing_source=source,
        min_time=d.min_time,
        max_time=d.max_time,
    )
    session.add(t)
    session.flush()
    audit(session, actor, "track.create", "track", t.id, after={"name": t.name, "lanes": lanes, "source": source})
    return t
