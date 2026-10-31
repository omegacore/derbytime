"""Database models.

Racers and Groups are pack-wide and persist across events; each event gets its
own Entry (car or boat) per racer. Track IDs are globally unique so track URLs
need no event prefix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventType(str, Enum):
    derby = "derby"
    regatta = "regatta"


class ScoringMode(str, Enum):
    average_time = "average_time"
    best_time = "best_time"
    points = "points"


class EventStatus(str, Enum):
    setup = "setup"
    active = "active"
    done = "done"


class InspectionStatus(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"


class RoundKind(str, Enum):
    group = "group"
    final = "final"
    runoff = "runoff"


class HeatStatus(str, Enum):
    scheduled = "scheduled"
    staged = "staged"
    armed = "armed"
    pending = "pending"
    approved = "approved"
    rerun = "rerun"


# Heats that occupy a track (at most one staged/armed per track; pending ones
# wait for the head timekeeper but no longer block the track).
ON_TRACK = (HeatStatus.staged, HeatStatus.armed)
ACTIVE = (HeatStatus.staged, HeatStatus.armed, HeatStatus.pending)


class Role(str, Enum):
    admin = "admin"
    head_timekeeper = "head_timekeeper"
    track_operator = "track_operator"
    lane_official = "lane_official"
    checkin = "checkin"


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    type: EventType
    scoring_mode: ScoringMode
    status: EventStatus = EventStatus.setup
    event_date: Optional[str] = None
    dnf_time: float = 9.999
    finalists_per_group: int = 3
    created_at: datetime = Field(default_factory=utcnow)


class Group(SQLModel, table=True):
    """A den or rank. Pack-wide, shared by all events."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    rank: Optional[str] = None
    sort_order: int = 0


class Racer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    first_name: str
    last_name: str = ""
    group_id: Optional[int] = Field(default=None, foreign_key="group.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Entry(SQLModel, table=True):
    """A racer's car (derby) or boat (regatta) in one event."""

    __table_args__ = (
        UniqueConstraint("event_id", "number"),
        UniqueConstraint("event_id", "racer_id"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    racer_id: int = Field(foreign_key="racer.id", index=True)
    number: int
    name: Optional[str] = None
    weight_oz: Optional[float] = None
    checked_in: bool = False
    inspection_status: InspectionStatus = InspectionStatus.pending
    inspection_notes: Optional[str] = None
    # check-in photo of the car or boat: a file name under <data_dir>/photos
    photo_path: Optional[str] = None


class Track(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    name: str
    lane_count: int
    timing_source: str
    # Hardware overrides for this track (e.g. serial port); merged over the
    # [timers.<source>] section of config.toml.
    timer_settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    min_time: float
    max_time: float
    active: bool = True
    # lanes out of service (e.g. a broken lane); the scheduler skips them
    disabled_lanes: Optional[list[int]] = Field(default=None, sa_column=Column(JSON))

    @property
    def usable_lanes(self) -> list[int]:
        off = set(self.disabled_lanes or [])
        return [l for l in range(1, self.lane_count + 1) if l not in off]


class Round(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    name: str
    kind: RoundKind
    group_id: Optional[int] = Field(default=None, foreign_key="group.id")
    # runoff rounds break ties in their parent round
    parent_round_id: Optional[int] = Field(default=None, foreign_key="round.id")
    seq: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Heat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key="round.id", index=True)
    track_id: int = Field(foreign_key="track.id", index=True)
    # Order within the track queue; reruns get a fractional seq so they slot
    # in right after the heat they replace.
    seq: float
    number: int  # human-facing heat number within the round
    status: HeatStatus = Field(default=HeatStatus.scheduled, index=True)
    rerun_of_id: Optional[int] = Field(default=None, foreign_key="heat.id")
    staged_at: Optional[datetime] = None
    armed_at: Optional[datetime] = None
    results_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    # Shared start for human-timed heats (regatta): the earliest start press by
    # the operator or any lane official, in server-clock epoch milliseconds.
    start_ms: Optional[float] = None
    start_source: Optional[str] = None


class HeatLane(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("heat_id", "lane"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    heat_id: int = Field(foreign_key="heat.id", index=True)
    lane: int
    entry_id: Optional[int] = Field(default=None, foreign_key="entry.id", index=True)  # None = bye
    raw_time: Optional[float] = None
    final_time: Optional[float] = None
    dnf: bool = False
    place: Optional[int] = None
    source: Optional[str] = None
    edit_reason: Optional[str] = None
    # client-generated id so retried phone submissions are idempotent
    submission_id: Optional[str] = None
    # lane official's STOP press (server-clock epoch ms); time = stop - heat start
    stop_ms: Optional[float] = None


class Official(SQLModel, table=True):
    """A device that joined via QR code."""

    id: Optional[int] = Field(default=None, primary_key=True)
    role: Role
    label: str = ""
    # the event this official works; None = all events (admins)
    event_id: Optional[int] = Field(default=None, foreign_key="event.id")
    track_id: Optional[int] = Field(default=None, foreign_key="track.id")
    lane: Optional[int] = None
    device_token_hash: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen: Optional[datetime] = None
    revoked: bool = False


class JoinToken(SQLModel, table=True):
    """Short-lived token encoded in a QR code; redeeming it creates an Official."""

    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    role: Role
    label: str = ""
    # the event this official works; None = all events (admins)
    event_id: Optional[int] = Field(default=None, foreign_key="event.id")
    track_id: Optional[int] = Field(default=None, foreign_key="track.id")
    lane: Optional[int] = None
    expires_at: datetime
    max_uses: int = 1
    uses: int = 0
    revoked: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    official_id: Optional[int] = Field(default=None, foreign_key="official.id")
    actor: str = ""
    action: str
    entity_type: str
    entity_id: Optional[int] = None
    before: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    after: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    reason: Optional[str] = None
