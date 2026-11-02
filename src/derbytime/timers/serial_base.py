"""Shared plumbing for USB serial timers.

Subclasses only implement ``parse_line`` (and optionally ``arm_bytes``); this
base class handles connecting, hotplug reconnects with backoff, connected /
disconnected status, and logging every raw line with a timestamp to
``<data_dir>/serial/track<id>-YYYYMMDD.log``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .base import PendingResult, TimerSource

log = logging.getLogger(__name__)


class SerialTimerSource(TimerSource):
    default_baudrate = 9600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        log_dir = Path(self.settings.get("data_dir", "data")) / "serial"
        self.log_dir = log_dir

    # -- to implement per device -------------------------------------------
    def parse_line(self, line: str) -> dict[int, float | None] | None:
        """Return lane -> seconds when ``line`` completes a race, else None."""
        raise NotImplementedError

    def arm_bytes(self) -> bytes | None:
        """Bytes to send when the heat is armed (e.g. reset/mask lanes)."""
        return None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if not self.settings.get("port"):
            self.set_status(False, f"no serial port configured for {self.kind}")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self.set_status(False, "stopped")

    async def arm(self, heat_id, lanes, existing=None) -> None:
        await super().arm(heat_id, lanes, existing)
        data = self.arm_bytes()
        if data and self._writer:
            self._writer.write(data)
            await self._writer.drain()

    def log_raw(self, direction: str, line: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        path = self.log_dir / f"track{self.track_id}-{now:%Y%m%d}.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='milliseconds')} {direction} {line}\n")

    async def _run(self) -> None:
        import serial_asyncio  # imported lazily so the app runs without hardware

        port = self.settings["port"]
        baud = int(self.settings.get("baudrate", self.default_baudrate))
        backoff = 1.0
        while True:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baud)
            except Exception as exc:
                self.set_status(False, f"waiting for {port}: {exc.__class__.__name__}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            self._writer = writer
            self.set_status(True, f"connected on {port}")
            self.log_raw("--", f"connected {port} @ {baud}")
            try:
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("port closed")
                    line = raw.decode("ascii", errors="replace").rstrip("\r\n")
                    self.log_raw("<<", line)
                    await self._handle_line(line)
            except asyncio.CancelledError:
                writer.close()
                raise
            except Exception as exc:
                self.log_raw("--", f"disconnected: {exc!r}")
                self.set_status(False, f"disconnected from {port}")
                self._writer = None
                try:
                    writer.close()
                except Exception:
                    pass
                await asyncio.sleep(1.0)

    async def _handle_line(self, line: str) -> None:
        try:
            times = self.parse_line(line)
        except NotImplementedError:
            return
        except Exception:
            log.exception("could not parse %r", line)
            return
        if times is None:
            return
        if self.armed_heat is None:
            log.warning("track %s: result with no armed heat: %r", self.track_id, line)
            return
        heat_id = self.armed_heat
        times = {l: t for l, t in times.items() if l in self.armed_lanes}
        await self.disarm()
        await self.emit(PendingResult(self.track_id, heat_id, times, self.kind, line))
