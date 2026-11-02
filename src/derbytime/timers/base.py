"""Timing source interface.

A TimerSource belongs to one track. The track operator arms it for a heat and
it later emits a PendingResult; nothing it emits is final until the head
timekeeper approves it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class PendingResult:
    track_id: int
    heat_id: int
    times: dict[int, float | None]  # lane -> seconds; None = DNF
    source: str
    raw: str | None = None


@dataclass
class TimerStatus:
    connected: bool
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"connected": self.connected, "detail": self.detail, **self.extra}


ResultCallback = Callable[[PendingResult], Awaitable[None]]
StatusCallback = Callable[[int, TimerStatus], None]


class TimerSource(ABC):
    kind: ClassVar[str]
    label: ClassVar[str]
    # True when results come from people (lane-official phones) rather than hardware
    human_timed: ClassVar[bool] = False

    def __init__(
        self,
        track_id: int,
        lane_count: int,
        settings: dict[str, Any],
        on_result: ResultCallback,
        on_status: StatusCallback,
    ):
        self.track_id = track_id
        self.lane_count = lane_count
        self.settings = settings
        self._on_result = on_result
        self._on_status = on_status
        self.armed_heat: int | None = None
        self.armed_lanes: list[int] = []
        self._status = TimerStatus(False, "not started")

    @property
    def status(self) -> TimerStatus:
        return self._status

    def set_status(self, connected: bool, detail: str = "", **extra: Any) -> None:
        new = TimerStatus(connected, detail, extra)
        if new != self._status:
            self._status = new
            self._on_status(self.track_id, new)

    async def emit(self, result: PendingResult) -> None:
        await self._on_result(result)

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def arm(self, heat_id: int, lanes: list[int], existing: dict[int, float | None] | None = None) -> None:
        """Prepare for ``heat_id``; ``lanes`` are lanes with a racer (byes excluded).

        ``existing`` carries lane times already recorded (used to resume after a restart).
        """
        self.armed_heat = heat_id
        self.armed_lanes = list(lanes)

    async def disarm(self) -> None:
        self.armed_heat = None
        self.armed_lanes = []
