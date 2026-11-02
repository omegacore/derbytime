"""WebSocket hub: forwards every bus event to connected browsers.

Payloads are small and the audience is a few hundred phones at most, so all
clients get all events and filter by ``keys`` client-side. The hub is just one
bus subscriber; a Web Push sender can later subscribe to ``alert`` events the
same way.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

from .bus import BusEvent, EventBus

log = logging.getLogger(__name__)


class WebSocketHub:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.clients: set[WebSocket] = set()
        self._sub = None

    def start(self) -> None:
        self._sub = self.bus.subscribe(self._broadcast)

    @property
    def count(self) -> int:
        return len(self.clients)

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "hello", "data": {"clients": self.count}, "keys": []}))
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text('{"type":"pong","data":{},"keys":[]}')
        except Exception:
            pass
        finally:
            self.clients.discard(ws)

    async def _broadcast(self, ev: BusEvent) -> None:
        if not self.clients:
            return
        text = json.dumps(ev.to_json(), default=str)
        dead = []

        async def send(ws: WebSocket):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=5)
            except Exception:
                dead.append(ws)

        await asyncio.gather(*(send(ws) for ws in list(self.clients)))
        for ws in dead:
            self.clients.discard(ws)
