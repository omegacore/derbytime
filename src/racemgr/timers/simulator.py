from __future__ import annotations

import asyncio
import random

from .base import PendingResult, TimerSource


class SimulatorTimer(TimerSource):
    """Fake timer for demos and testing: emits plausible times after a delay.

    Occasionally emits a DNF or an outlier so the timekeeper's flagging can be
    exercised (see [timers.simulator] in config).
    """

    kind = "simulator"
    label = "Simulator"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._rng = random.Random(self.settings.get("seed"))

    async def start(self) -> None:
        self.set_status(True, "simulator ready")

    async def stop(self) -> None:
        await self.disarm()
        self.set_status(False, "stopped")

    async def arm(self, heat_id, lanes, existing=None) -> None:
        await super().arm(heat_id, lanes, existing)
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._run(heat_id, list(lanes)))

    async def disarm(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await super().disarm()

    def generate(self, lanes: list[int]) -> dict[int, float | None]:
        s = self.settings
        mean = float(s.get("mean_time", 3.2))
        spread = float(s.get("spread", 0.25))
        times: dict[int, float | None] = {}
        for lane in lanes:
            r = self._rng.random()
            if r < float(s.get("dnf_chance", 0.0)):
                times[lane] = None
            elif r < float(s.get("dnf_chance", 0.0)) + float(s.get("outlier_chance", 0.0)):
                times[lane] = round(mean * self._rng.choice([0.4, 2.5]), 4)
            else:
                times[lane] = round(max(0.5, self._rng.gauss(mean, spread)), 4)
        return times

    async def _run(self, heat_id: int, lanes: list[int]) -> None:
        await asyncio.sleep(float(self.settings.get("delay_seconds", 3.0)))
        if self.armed_heat != heat_id:
            return
        times = self.generate(lanes)
        raw = " ".join(f"{chr(64 + l)}={'DNF' if t is None else f'{t:.4f}'}" for l, t in sorted(times.items()))
        self.armed_heat = None
        await self.emit(PendingResult(self.track_id, heat_id, times, self.kind, raw))
