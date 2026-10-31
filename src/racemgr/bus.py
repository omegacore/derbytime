"""In-process async event bus.

``publish`` is safe to call from the event loop or from worker threads (sync
FastAPI endpoints). Each subscriber gets its own queue and task, so a slow
subscriber never blocks publishers or other subscribers.

Event ``keys`` are routing tags such as ``"track-3"``, ``"heat"``,
``"racer-12"``; browser pages use them to decide which panels to refresh.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .models import utcnow

log = logging.getLogger(__name__)


@dataclass
class BusEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    ts: str = field(default_factory=lambda: utcnow().isoformat())

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "keys": self.keys, "ts": self.ts}


Handler = Callable[[BusEvent], Awaitable[None]]


class Subscription:
    def __init__(self, bus: "EventBus", handler: Handler, types: set[str] | None):
        self.bus = bus
        self.handler = handler
        self.types = types
        self.queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=1000)
        self.task: asyncio.Task | None = None

    def wants(self, ev: BusEvent) -> bool:
        return self.types is None or ev.type in self.types or ev.type.split(".")[0] in self.types

    async def run(self) -> None:
        while True:
            ev = await self.queue.get()
            try:
                await self.handler(ev)
            except Exception:  # keep the subscriber alive
                log.exception("bus subscriber failed on %s", ev.type)

    def cancel(self) -> None:
        self.bus._subs.discard(self)
        if self.task:
            self.task.cancel()


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        # called synchronously on every publish (the page cache's version bump)
        self.on_publish: Callable[[], None] | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def subscribe(self, handler: Handler, types: set[str] | None = None) -> Subscription:
        """Register a handler. ``types`` filters by full type or prefix ("heat")."""
        sub = Subscription(self, handler, types)
        self._subs.add(sub)
        loop = self._loop or asyncio.get_event_loop()
        sub.task = loop.create_task(sub.run())
        return sub

    def publish(self, type: str, data: dict[str, Any] | None = None, keys: list[str] | None = None) -> BusEvent:
        ev = BusEvent(type, data or {}, keys or [])
        if self.on_publish:
            self.on_publish()
        if self._loop is None or self._loop.is_closed():
            return ev  # no loop yet (CLI / tests without app): drop
        if threading.get_ident() == self._loop_thread:
            self._dispatch(ev)
        else:
            self._loop.call_soon_threadsafe(self._dispatch, ev)
        return ev

    def _dispatch(self, ev: BusEvent) -> None:
        for sub in list(self._subs):
            if sub.wants(ev):
                try:
                    sub.queue.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning("dropping %s for slow subscriber", ev.type)

    async def close(self) -> None:
        for sub in list(self._subs):
            sub.cancel()
