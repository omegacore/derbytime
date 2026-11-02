"""Race-day load test: officials drive heats on every track while simulated
phones watch, then report latencies and PASS/FAIL.

It never touches your real database: ``prepare`` takes a consistent copy and
mints join tokens for the simulated officials in the copy only. The heats it
races are the copy's scheduled heats of the active event(s), so set up the
event (roster, tracks, schedule) before testing.

    # Everything on this machine (copy, start a server, test, clean up):
    python tools/loadtest.py local --db racemgr.db

    # Test another machine as the server:
    #   on the server:   python tools/loadtest.py prepare --db racemgr.db --out lt
    #                    derbytime --config lt/config.toml serve --port 8765
    #   from any laptop: python tools/loadtest.py run --url http://SERVER-IP:8765 --plan lt/plan.json

Simulated phones behave like static/live.js and static/follow.js: one
WebSocket each, re-fetching their page's partial (after the same jitter) when
a matching bus event arrives. Needs ``httpx`` and ``websockets`` (both come
with ``pip install -e ".[dev]"``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# --- prepare -------------------------------------------------------------------


def prepare(db: Path, out: Path, port: int) -> Path:
    """Copy ``db`` into ``out`` and write config.toml + plan.json there."""
    from sqlmodel import Session, select

    from derbytime import auth
    from derbytime.db import init_db, make_engine
    from derbytime.models import Entry, Event, EventStatus, Role, Track

    out.mkdir(parents=True, exist_ok=True)
    copy = out / "loadtest.db"
    for p in out.glob("loadtest.db*"):
        p.unlink()
    src = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)  # consistent even while the live server is writing
    dst.close()
    src.close()
    (out / "config.toml").write_text(f'[server]\nport = {port}\ndb_path = "loadtest.db"\ndata_dir = "data"\n')

    engine = make_engine(copy)
    init_db(engine)
    plan: dict = {"tokens": {}, "tracks": [], "racers": [], "events": []}
    with Session(engine) as s:
        events = list(s.exec(select(Event).where(Event.status == EventStatus.active)))
        if not events:
            raise SystemExit("No active event in that database: activate one with a schedule first.")

        def token(name: str, role: Role, **kw) -> None:
            plan["tokens"][name] = auth.create_join_token(s, role, 24 * 60, max_uses=5, **kw).token

        token("timekeeper", Role.head_timekeeper)
        for ev in events:
            plan["events"].append(ev.id)
            for t in s.exec(select(Track).where(Track.event_id == ev.id, Track.active == True)):  # noqa: E712
                plan["tracks"].append({"id": t.id, "name": t.name, "lanes": t.lane_count, "phone": t.timing_source == "phone"})
                token(f"op{t.id}", Role.track_operator, track_id=t.id)
                if t.timing_source == "phone":
                    for lane in range(1, t.lane_count + 1):
                        token(f"lane{t.id}_{lane}", Role.lane_official, track_id=t.id, lane=lane)
            plan["racers"] += [e.racer_id for e in s.exec(select(Entry).where(Entry.event_id == ev.id))]
        s.commit()
    plan["racers"] = sorted(set(plan["racers"]))
    path = out / "plan.json"
    path.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    print(f"Prepared {copy} ({len(plan['tracks'])} tracks, {len(plan['racers'])} racers, {len(plan['tokens'])} officials)")
    return path


# --- run -----------------------------------------------------------------------

OFFICIAL_ACTIONS = ("POST stage", "POST arm", "POST start", "POST lane stop", "POST manual", "POST approve")


class LoadTest:
    def __init__(self, url: str, plan: dict, viewers: int, heats: int, gap: float):
        import httpx

        self.httpx = httpx
        self.base = url.rstrip("/")
        self.ws_url = self.base.replace("http", "ws", 1) + "/ws"
        self.plan, self.viewers, self.heats, self.gap = plan, viewers, heats, gap
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.retries = 0  # GETs retried after a keep-alive race (see get())
        self.ws_lag: list[float] = []
        self.heats_done = 0
        self.stop = asyncio.Event()
        self.ssl = ssl.create_default_context()  # once: building one per client stalls the generator
        self._joined: dict = {}

    def client(self):
        return self.httpx.AsyncClient(base_url=self.base, timeout=30, verify=self.ssl)

    async def timed(self, name: str, coro, ok=(200, 204)):
        t0 = time.perf_counter()
        try:
            r = await coro
            if r.status_code not in ok:
                self.errors[f"{name} {r.status_code}: {r.text[:80]}"] += 1
            return r
        except Exception as e:
            self.errors[f"{name} {type(e).__name__}"] += 1
        finally:
            self.lat[name].append((time.perf_counter() - t0) * 1000)

    async def get(self, c, url: str):
        """GET that, like a browser, retries once when the server closed the
        idle keep-alive connection just as we reused it (the uvicorn keep-alive
        race). Counted in `retries` so it stays visible in the report."""
        try:
            return await c.get(url)
        except (self.httpx.ReadError, self.httpx.RemoteProtocolError):
            self.retries += 1
            return await c.get(url)

    async def joined(self, name: str):
        if name not in self._joined:
            c = self.client()
            r = await c.post(f"/join/{self.plan['tokens'][name]}")
            if r.status_code != 303:
                raise SystemExit(f"Couldn't join as {name} ({r.status_code}). Re-run prepare: tokens are single-database.")
            self._joined[name] = c
        return self._joined[name]

    async def phone(self, c, keys: list[str], partial: str, tag: str, jitter: float = 0, follow: int | None = None):
        """One open page: initial load, then live refreshes like live.js/follow.js."""
        pending: dict[str, asyncio.Task] = {}

        def later(slot: str, delay: float, name: str, url: str, reset: bool) -> None:
            task = pending.get(slot)
            if task and not task.done():
                if not reset:
                    return
                task.cancel()

            async def go():
                await asyncio.sleep(delay)
                await self.timed(name, self.get(c, url))

            pending[slot] = asyncio.create_task(go())

        follow_url = f"/api/racers?ids={follow}"
        await self.timed(f"{tag} partial", self.get(c, partial))
        if follow:
            await self.timed("follow panel", self.get(c, follow_url))
        cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        try:
            import websockets

            async with websockets.connect(
                self.ws_url, additional_headers={"Cookie": cookie} if cookie else {}, open_timeout=20, ping_interval=None
            ) as ws:
                while not self.stop.is_set():
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), 1))
                    except asyncio.TimeoutError:
                        continue
                    if m.get("ts"):
                        self.ws_lag.append(time.time() * 1000 - datetime.fromisoformat(m["ts"]).timestamp() * 1000)
                    mkeys = m.get("keys", [])
                    if any(k in mkeys for k in keys):
                        later("panel", 0.12 + random.random() * jitter, f"{tag} partial", partial, reset=False)
                    if follow and f"racer-{follow}" in mkeys:
                        later("follow", 0.3, "follow panel", follow_url, reset=True)
                    elif follow and m["type"] == "track.state":
                        later("follow", 5 + random.random() * 10, "follow panel", follow_url, reset=False)
        except Exception as e:
            self.errors[f"websocket ({tag}) {type(e).__name__}"] += 1
        for task in pending.values():
            task.cancel()

    async def viewer(self):
        await asyncio.sleep(random.random() * 3)  # families don't all arrive in the same instant
        c = self.client()
        roll = random.random()
        if roll < 0.75 and self.plan["racers"]:  # a parent on their scout's page, following them
            rid = random.choice(self.plan["racers"])
            await self.timed("racer page", self.get(c, f"/racer/{rid}"))
            await self.phone(c, [f"racer-{rid}", "track"], f"/partials/racer/{rid}", "racer", 1.5, follow=rid)
        elif roll < 0.95:
            await self.timed("home page", self.get(c, "/"))
            await self.phone(c, ["track", "heat"], "/partials/home", "home", 1.0)
        else:
            ev = random.choice(self.plan["events"])
            await self.phone(c, [f"event-{ev}"], f"/partials/event/{ev}", "event", 1.5)

    async def official_screens(self) -> list:
        screens = []
        for t in self.plan["tracks"]:
            tid = t["id"]
            screens.append(self.phone(await self.joined(f"op{tid}"), [f"track-{tid}", "timer"], f"/partials/operate/{tid}", "operate"))
            if t["phone"]:
                for lane in range(1, t["lanes"] + 1):
                    screens.append(self.phone(await self.joined(f"lane{tid}_{lane}"), [f"track-{tid}"], f"/partials/lane/{tid}/{lane}", "lane"))
            screens.append(self.phone(self.client(), [f"track-{tid}"], f"/partials/ondeck/{tid}", "ondeck"))
        screens.append(self.phone(await self.joined("timekeeper"), ["heat", "track"], "/partials/timekeeper", "timekeeper"))
        screens.append(self.phone(self.client(), ["standings"], "/partials/standings", "standings board"))
        return screens

    async def drive_track(self, t: dict) -> None:
        """Operator, lane officials and timekeeper race heats at ``gap`` seconds per step."""
        tid = t["id"]
        op, tk = await self.joined(f"op{tid}"), await self.joined("timekeeper")
        lanes = {l: await self.joined(f"lane{tid}_{l}") for l in range(1, t["lanes"] + 1)} if t["phone"] else {}
        done = tries = 0
        while done < self.heats and tries < self.heats * 3:
            tries += 1
            st = await self.timed("GET track state", self.get(op, f"/api/track/{tid}/state"))
            if st is None or st.status_code != 200:
                await asyncio.sleep(self.gap)
                continue
            if st.json()["current"]:  # left over from a failed step
                await op.post(f"/api/track/{tid}/unstage")
            r = await self.timed("POST stage", op.post(f"/api/track/{tid}/stage", data={}), ok=(204,))
            if r is None or r.status_code != 204:
                await asyncio.sleep(self.gap)
                continue
            heat = (await op.get(f"/api/track/{tid}/state")).json()["current"]
            occupied = [l["lane"] for l in heat["lanes"] if not l["bye"]]
            await asyncio.sleep(self.gap)
            if t["phone"]:
                await self.timed("POST arm", op.post(f"/api/track/{tid}/arm"))
                await asyncio.sleep(self.gap)
                start = time.time() * 1000
                await self.timed("POST start", op.post(f"/api/track/{tid}/start", json={"heat_id": heat["id"], "start_ms": start}))
                await asyncio.sleep(self.gap)
                # every lane official hits STOP within milliseconds of the others
                await asyncio.gather(*(
                    self.timed("POST lane stop", lanes[l].post(f"/api/track/{tid}/lane/{l}/time", json={
                        "heat_id": heat["id"], "stop_ms": start + random.uniform(4000, 9000), "submission_id": uuid.uuid4().hex,
                    }))
                    for l in occupied
                ))
            else:
                times = {f"lane_{l}": f"{random.uniform(2.5, 4.5):.3f}" for l in occupied}
                await self.timed("POST manual", op.post(f"/api/track/{tid}/manual", data=times))
            await asyncio.sleep(self.gap)
            r = await self.timed("POST approve", tk.post(f"/api/heat/{heat['id']}/approve"))
            if r is not None and r.status_code == 204:
                done += 1
                self.heats_done += 1
            await asyncio.sleep(self.gap)

    async def main(self) -> None:
        bg = [asyncio.create_task(s) for s in await self.official_screens()]
        bg += [asyncio.create_task(self.viewer()) for _ in range(self.viewers)]
        await asyncio.sleep(5)
        t0 = time.time()
        budget = self.heats * self.gap * 10 + 120
        try:
            await asyncio.wait_for(asyncio.gather(*(self.drive_track(t) for t in self.plan["tracks"])), budget)
        except asyncio.TimeoutError:
            self.errors[f"officials didn't finish within {budget:.0f}s"] += 1
        self.duration = time.time() - t0
        await asyncio.sleep(2)
        self.stop.set()
        await asyncio.gather(*bg, return_exceptions=True)

    def report(self, max_official_p95: float, max_viewer_p95: float) -> bool:
        def pct(v: list[float], p: float) -> float:
            v = sorted(v)
            return v[min(len(v) - 1, int(p * len(v)))]

        print(f"\n{self.viewers} viewers, {len(self.plan['tracks'])} tracks x {self.heats} heats, "
              f"{self.gap}s per step: {self.heats_done} heats approved in {self.duration:.0f}s\n")
        print(f"{'':24s} {'n':>6s} {'p50':>7s} {'p95':>7s} {'max':>7s}   ms")
        for name in sorted(self.lat):
            v = self.lat[name]
            print(f"{name:24s} {len(v):6d} {pct(v, .5):7.0f} {pct(v, .95):7.0f} {max(v):7.0f}")
        if self.ws_lag:
            print(f"{'live update delay':24s} {len(self.ws_lag):6d} {pct(self.ws_lag, .5):7.0f} {pct(self.ws_lag, .95):7.0f} {max(self.ws_lag):7.0f}")
        if self.retries:
            print(f"\n{self.retries} GET(s) retried after the server closed an idle keep-alive connection (browsers do the same)")

        problems = [f"{n}x {e}" for e, n in self.errors.items()]
        if self.heats_done < self.heats * len(self.plan["tracks"]):
            problems.append(f"only {self.heats_done} of {self.heats * len(self.plan['tracks'])} heats approved (not enough scheduled heats?)")
        for name, v in self.lat.items():
            limit = max_official_p95 if name in OFFICIAL_ACTIONS else max_viewer_p95
            if pct(v, .95) > limit:
                problems.append(f"{name} p95 {pct(v, .95):.0f} ms > {limit:.0f} ms")
        print()
        for p in problems:
            print("  -", p)
        print("FAIL" if problems else "PASS", flush=True)
        return not problems


def run(url: str, plan_path: Path, args) -> bool:
    lt = LoadTest(url, json.loads(plan_path.read_text(encoding="utf-8")), args.viewers, args.heats, args.gap)
    asyncio.run(lt.main())
    return lt.report(args.max_official_p95, args.max_viewer_p95)


def local(args) -> bool:
    """The temp copy is deleted afterwards unless something went wrong (then
    its server.log is worth reading)."""
    import httpx

    out = Path(tempfile.mkdtemp(prefix="derbytime-loadtest-"))
    plan = prepare(Path(args.db), out, args.port)
    url = f"http://127.0.0.1:{args.port}"
    with open(out / "server.log", "w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "derbytime", "--config", str(out / "config.toml"), "serve",
             "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(40):
                try:
                    if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.5)
            else:
                print(f"Server didn't start; see {out / 'server.log'}")
                return False
            ok = run(url, plan, args)
        finally:
            server.terminate()
            server.wait(10)
    crashes = (out / "server.log").read_text(encoding="utf-8", errors="replace").count("Exception in ASGI application")
    if crashes:
        print(f"  - server logged {crashes} unhandled exceptions; see {out / 'server.log'}")
    if ok and not crashes:
        shutil.rmtree(out, ignore_errors=True)
        return True
    print(f"Kept {out} for inspection.")
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def load_opts(sp):
        sp.add_argument("--viewers", type=int, default=100, help="simulated family phones (default 100)")
        sp.add_argument("--heats", type=int, default=3, help="heats raced per track (default 3)")
        sp.add_argument("--gap", type=float, default=8.0, help="seconds between official steps; 8 is about a real heat (default 8)")
        sp.add_argument("--max-official-p95", type=float, default=1000, help="fail if an official action's p95 exceeds this (ms)")
        sp.add_argument("--max-viewer-p95", type=float, default=3000, help="fail if a viewer page's p95 exceeds this (ms)")

    sp = sub.add_parser("prepare", help="copy a database and mint official tokens for a load test")
    sp.add_argument("--db", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--port", type=int, default=8765)
    sp = sub.add_parser("run", help="load-test a running server prepared with `prepare`")
    sp.add_argument("--url", required=True)
    sp.add_argument("--plan", required=True)
    load_opts(sp)
    sp = sub.add_parser("local", help="prepare, start a server on this machine, run, clean up")
    sp.add_argument("--db", required=True)
    sp.add_argument("--port", type=int, default=8765)
    load_opts(sp)

    args = p.parse_args(argv)
    if args.cmd == "prepare":
        prepare(Path(args.db), Path(args.out), args.port)
        return 0
    ok = run(args.url, Path(args.plan), args) if args.cmd == "run" else local(args)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
