"""Shared render cache for public (anonymous) pages.

Every bus event and every write request bumps ``version``. Anonymous viewers
asking for the same URL at the same version share one render, and a per-URL
lock makes the first request render while the rest wait for it: 100 phones
refreshing after the same heat cost one render, not 100. Entries also expire
after ``ttl`` seconds as a backstop for changes made outside the web app
(e.g. the CLI).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send


class PageCache:
    def __init__(self, ttl: float = 5.0, max_entries: int = 2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.version = 0
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._entries: dict[str, tuple[int, float, Any]] = {}

    def bump(self) -> None:
        with self._lock:
            self.version += 1

    def get(self, key: str, render: Callable[[], Any]) -> Any:
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            version, now = self.version, time.monotonic()
            hit = self._entries.get(key)
            if hit and hit[0] == version and now - hit[1] < self.ttl:
                return _thaw(hit[2])
            value = render()
            frozen = _freeze(value)
            if frozen is not None:
                with self._lock:
                    if len(self._entries) >= self.max_entries:
                        self._entries.clear()
                        self._key_locks.clear()
                    # stored under the version read *before* rendering, so a
                    # change that lands mid-render forces the next re-render
                    self._entries[key] = (version, now, frozen)
            return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Response):
        if value.status_code != 200 or not hasattr(value, "body"):
            return None
        return ("response", value.body, list(value.raw_headers))
    return ("value", value)


def _thaw(frozen: tuple) -> Any:
    if frozen[0] == "response":
        r = Response(content=frozen[1])
        r.raw_headers = list(frozen[2])
        return r
    return frozen[1]


class BumpOnWrite:
    """ASGI middleware: any non-GET request may change data, so bump the
    cache version before (renders started during it are stale) and after."""

    def __init__(self, app: ASGIApp, cache: PageCache):
        self.app = app
        self.cache = cache

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        write = scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS")
        if write:
            self.cache.bump()
        try:
            await self.app(scope, receive, send)
        finally:
            if write:
                self.cache.bump()
