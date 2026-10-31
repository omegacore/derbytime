from __future__ import annotations

from .base import PendingResult, TimerSource


class PhoneStopwatch(TimerSource):
    """Lane officials time each lane on their phone and submit elapsed times.

    Each lane reports independently; once every occupied lane has a time (or a
    DNF) the heat is emitted as a pending result. The web layer persists each
    lane time as it arrives, and passes those back in ``arm(existing=...)``
    after a restart, so no submission is lost.
    """

    kind = "phone"
    label = "Phone stopwatches"
    human_timed = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lane_times: dict[int, float | None] = {}

    async def start(self) -> None:
        self.set_status(True, "waiting for lane officials")

    async def stop(self) -> None:
        await self.disarm()
        self.set_status(False, "stopped")

    async def arm(self, heat_id, lanes, existing=None) -> None:
        await super().arm(heat_id, lanes, existing)
        self.lane_times = {l: t for l, t in (existing or {}).items() if l in lanes}
        await self._maybe_complete()

    async def disarm(self) -> None:
        self.lane_times = {}
        await super().disarm()

    @property
    def missing_lanes(self) -> list[int]:
        return [l for l in self.armed_lanes if l not in self.lane_times]

    async def submit_lane(self, heat_id: int, lane: int, seconds: float | None) -> bool:
        """Record one lane. Returns True if this completed the heat."""
        if heat_id != self.armed_heat or lane not in self.armed_lanes:
            return False
        self.lane_times[lane] = seconds
        return await self._maybe_complete()

    async def _maybe_complete(self) -> bool:
        if self.armed_heat is None or self.missing_lanes:
            return False
        heat_id, times = self.armed_heat, dict(self.lane_times)
        await self.disarm()
        await self.emit(PendingResult(self.track_id, heat_id, times, self.kind))
        return True
