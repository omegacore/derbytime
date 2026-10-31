# CLAUDE.md

Scout Race Manager (`racemgr`): a self-hosted FastAPI + HTMX app for Pinewood Derby / Rain Gutter Regatta races. One laptop runs the server; officials' and families' phones on the same Wi-Fi are the clients. README.md covers setup, the event workflow and architecture; this file covers what to keep in mind while changing code.

## Commands

```bash
.venv/Scripts/python -m pytest -q                         # full suite (~30 s); keep it green
.venv/Scripts/python tools/loadtest.py local --db racemgr.db   # race-day load test on a copy of the DB
.venv/Scripts/racemgr.exe serve                            # dev server (also .claude/launch.json "racemgr")
```

Windows machine: use `.venv/Scripts/...`. Node is not installed, so check JS changes in the browser pane.

## Data safety

- `racemgr.db` in the repo root is the pack's **real event database**, and another server may have it open. Don't write to it from tests or experiments. Tests use temp databases, and the load test copies it with the SQLite backup API.
- `*Roster*.csv` / `roster2026-27` contain family PII. Never print, copy or commit them.

## Layout

- `core/`: pure scheduling/scoring. No web or DB imports (a test enforces this).
- `services/race.py`: `RaceService`, the **only** way heat state changes (stage → arm → pending → approved / rerun). Each change is audited and published on the bus.
- `services/views.py`, `services/schedule.py`: read-side snapshots, queue planning, standings.
- `routes/`: `pages.py` (pages + HTMX partials), `api.py` (actions + small JSON APIs), `admin.py`.
- `bus.py` → `ws.py`: bus events carry routing `keys`; `static/live.js` re-fetches panels whose `data-live` keys match.
- `cache.py`: shared render cache for public pages (see below).

## Concurrency and load (race day: 6–9 officials, ~100 viewer phones)

These rules come from load-testing a real failure. Re-run `tools/loadtest.py` after changing anything on these paths.

- **One process, one event loop.** `RaceService` methods are `async`, serialized by `self._lock`, and do their (short) SQLite work directly on the event loop. Sync routes run in a 40-thread pool. Never add slow work to `RaceService` or other async routes. Put reads in sync (`def`) routes instead.
- **Connection pool** (`db.py`) must stay larger than the threadpool (each request can hold two sessions). With the old 5+10 default, the event loop blocked waiting for a connection that only the event loop could return, and the server froze for 30 s at a time.
- **Public pages go through the cache.** Decorate anonymous-facing partials/APIs with `@public_cached` (sync route with a `request` parameter). Compute data shared by many viewers once per version with `request.app.state.page_cache.get(key, fn)`, as `shared_standings` and follow status do. Uncached per-viewer recomputation of standings is what saturated the CPU.
- **Cache invalidation is automatic only for changes that go through the app:** a bus publish or any non-GET request. Code that changes data any other way must call `page_cache.bump()` (or rely on the 5 s TTL). Tests that mutate data behind the app's back must bump too.
- **Fan-out:** every event wakes every phone whose `data-live` keys match, so give public panels the narrowest keys that keep them correct, plus `data-jitter`. Admin schedule changes reach viewers only via `track.state` (the `track` key).
- **Lane timing** is stamped on the phone in server-clock ms (`clock.js`), queued in localStorage and retried, and is idempotent by `submission_id`, so a slow server delays results but doesn't change them. Keep `/api/time` async and cheap: clock error is up to half its round trip.

## Conventions

- Match the surrounding style: short docstrings that explain *why*, small helpers, no new dependencies without a reason.
- Error to the user = raise `ServiceError(message, status)`. The app turns it into JSON or a page.
- Native `confirm()`/`prompt()` are intentional. The desktop app's browser pane blocks them, which is not an app bug.
