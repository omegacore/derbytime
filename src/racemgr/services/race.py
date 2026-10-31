"""Heat lifecycle: stage -> arm -> pending -> approved (or rerun).

All state changes go through ``RaceService`` so every change is audited and
published on the bus.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from ..bus import EventBus
from ..config import Settings
from ..core.scoring import place_heat
from ..models import ON_TRACK, Heat, HeatLane, HeatStatus, Track, utcnow
from ..timers import REGISTRY, PendingResult, PhoneStopwatch, TimerSource, TimerStatus
from . import Actor, ServiceError, views
from .alerts import AlertService
from .audit import audit

log = logging.getLogger(__name__)

# tolerated clock disagreement between a phone and the server
START_SKEW_MS = 5000


def _lane_dict(hl: HeatLane) -> dict[str, Any]:
    return {"lane": hl.lane, "final_time": hl.final_time, "dnf": hl.dnf, "place": hl.place}


def _apply_places(lanes: list[HeatLane]) -> None:
    occupied = [hl for hl in lanes if hl.entry_id is not None]
    places = place_heat({hl.lane: (None if hl.dnf else hl.final_time) for hl in occupied})
    for hl in occupied:
        hl.place = places[hl.lane]


class RaceService:
    def __init__(self, settings: Settings, engine: Engine, bus: EventBus):
        self.settings = settings
        self.engine = engine
        self.bus = bus
        self.alerts = AlertService(bus)
        self.timers: dict[int, TimerSource] = {}
        self._timer_sig: dict[int, tuple] = {}
        self._lock = asyncio.Lock()

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # --- timers ------------------------------------------------------------

    def timer_status(self, track_id: int) -> dict[str, Any]:
        src = self.timers.get(track_id)
        if not src:
            return {"connected": False, "detail": "timer not running", "kind": None}
        return {**src.status.to_json(), "kind": src.kind, "label": src.label, "human": src.human_timed}

    def _on_timer_status(self, track_id: int, status: TimerStatus) -> None:
        self.bus.publish("timer.status", {"track_id": track_id, **status.to_json()}, keys=["timer", f"track-{track_id}"])

    async def _on_timer_result(self, result: PendingResult) -> None:
        try:
            await self.record_pending(result.heat_id, result.times, result.source, Actor.system(result.source), raw=result.raw)
        except ServiceError as exc:
            log.warning("timer result for heat %s rejected: %s", result.heat_id, exc.message)

    async def sync_timers(self) -> None:
        """Start/replace/stop timing sources to match the tracks in the database."""
        with self.session() as s:
            tracks = views.active_tracks(s)
            armed = {h.track_id: h for h in s.exec(select(Heat).where(Heat.status == HeatStatus.armed))}
            armed_lanes = views.lanes_for(s, [h.id for h in armed.values()])
        wanted = {t.id: t for t in tracks}
        for tid in list(self.timers):
            if tid not in wanted:
                await self.timers.pop(tid).stop()
                self._timer_sig.pop(tid, None)
        for t in tracks:
            settings = {**self.settings.timer_settings(t.timing_source), **(t.timer_settings or {}), "data_dir": str(self.settings.data_dir)}
            sig = (t.timing_source, t.lane_count, tuple(sorted((k, str(v)) for k, v in settings.items())))
            if self._timer_sig.get(t.id) == sig:
                continue
            if t.id in self.timers:
                await self.timers.pop(t.id).stop()
            cls = REGISTRY.get(t.timing_source)
            if cls is None:
                log.error("track %s: unknown timing source %r", t.id, t.timing_source)
                continue
            src = cls(t.id, t.lane_count, settings, self._on_timer_result, self._on_timer_status)
            self.timers[t.id] = src
            self._timer_sig[t.id] = sig
            await src.start()
            if t.id in armed:  # resume after restart
                lanes = armed_lanes[armed[t.id].id]
                existing = {hl.lane: (None if hl.dnf else hl.raw_time) for hl in lanes if hl.raw_time is not None or hl.dnf}
                await src.arm(armed[t.id].id, [hl.lane for hl in lanes if hl.entry_id], existing)
        self.publish_tracks()

    async def stop(self) -> None:
        for src in self.timers.values():
            await src.stop()
        self.timers.clear()
        self._timer_sig.clear()

    # --- publishing ----------------------------------------------------------

    def publish_tracks(self, track_ids: list[int] | None = None) -> None:
        with self.session() as s:
            tracks = views.active_tracks(s)
            for t in tracks:
                if track_ids is None or t.id in track_ids:
                    state = views.track_state(s, t, self.timer_status(t.id))
                    self.bus.publish("track.state", state, keys=["track", f"track-{t.id}", f"event-{t.event_id}"])
            self.alerts.refresh(s)

    def _publish_heat(self, s: Session, heat: Heat, type: str) -> dict[str, Any]:
        hv = views.heat_view(s, heat)
        keys = ["heat", f"heat-{heat.id}", f"track-{heat.track_id}", f"event-{hv['event_id']}"]
        keys += [f"racer-{r}" for r in hv["racer_ids"]]
        if type in ("heat.approved", "heat.updated"):
            keys.append("standings")
        self.bus.publish(type, hv, keys=keys)
        return hv

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _get_track(s: Session, track_id: int) -> Track:
        t = s.get(Track, track_id)
        if not t:
            raise ServiceError("Track not found", 404)
        return t

    @staticmethod
    def _get_heat(s: Session, heat_id: int) -> Heat:
        h = s.get(Heat, heat_id)
        if not h:
            raise ServiceError("Heat not found", 404)
        return h

    @staticmethod
    def current_heat(s: Session, track_id: int) -> Heat | None:
        return s.exec(select(Heat).where(Heat.track_id == track_id, col(Heat.status).in_(ON_TRACK))).first()

    @staticmethod
    def _lanes(s: Session, heat_id: int) -> list[HeatLane]:
        return list(s.exec(select(HeatLane).where(HeatLane.heat_id == heat_id).order_by(HeatLane.lane)))

    @classmethod
    def _move_heat(cls, s: Session, heat: Heat, track: Track) -> None:
        """Hand a heat from the shared queue to the track that races it. Cars
        keep their lane numbers; empty lanes follow the new track's lanes."""
        usable = set(track.usable_lanes)
        have = set()
        for hl in cls._lanes(s, heat.id):
            if hl.entry_id is None and hl.lane not in usable:
                s.delete(hl)
            else:
                have.add(hl.lane)
        for lane in sorted(usable - have):
            s.add(HeatLane(heat_id=heat.id, lane=lane))
        heat.track_id = track.id

    # --- lifecycle -------------------------------------------------------------

    async def stage(self, track_id: int, heat_id: int | None, actor: Actor) -> Heat:
        async with self._lock:
            with self.session() as s:
                track = self._get_track(s, track_id)
                if self.current_heat(s, track_id):
                    raise ServiceError("A heat is already staged on this track.", 409)
                if heat_id is None:
                    plan = views.plan_all(s).get(track_id, [])
                    if not plan:
                        raise ServiceError("No heats left in the queue for this track.", 409)
                    heat = plan[0]
                else:
                    heat = self._get_heat(s, heat_id)
                if heat.status != HeatStatus.scheduled or track.id not in {t.id for t in views.heat_tracks(s, heat)}:
                    raise ServiceError("That heat can't be staged on this track.", 409)
                mine = views.racers_by_heat(s, [heat.id])[heat.id]
                clash = set()
                for other, racers in views.on_track_racers(s).items():
                    if other != track_id:
                        clash |= mine & racers
                if clash:
                    names = ", ".join(sorted(l["racer_name"] for l in views.heat_view(s, heat)["lanes"] if l.get("racer_id") in clash))
                    raise ServiceError(f"{names} is racing on another track right now.", 409)
                moved_from = heat.track_id if heat.track_id != track.id else None
                if moved_from is not None:
                    self._move_heat(s, heat, track)
                heat.status = HeatStatus.staged
                heat.staged_at = utcnow()
                s.add(heat)
                audit(s, actor, "heat.stage", "heat", heat.id, after={"track": track.id, "from_track": moved_from} if moved_from else None)
                s.commit()
                self._publish_heat(s, heat, "heat.staged")
        self.publish_tracks()
        return heat

    def _clear_attempt(self, s: Session, heat: Heat) -> None:
        """Forget this attempt's start, STOP presses and partial lane times."""
        for hl in self._lanes(s, heat.id):
            hl.raw_time = hl.final_time = hl.place = hl.source = hl.submission_id = hl.stop_ms = None
            hl.dnf = False
            s.add(hl)
        heat.start_ms = heat.start_source = None
        heat.armed_at = None

    async def disarm(self, track_id: int, actor: Actor, reason: str = "disarmed") -> Heat:
        """Armed -> staged: e.g. a bad start before anything counted. Lane
        phones reset and wait for the heat to be armed again."""
        async with self._lock:
            with self.session() as s:
                heat = self.current_heat(s, track_id)
                if not heat or heat.status != HeatStatus.armed:
                    raise ServiceError("No armed heat on this track.", 409)
                if track_id in self.timers:
                    await self.timers[track_id].disarm()
                self._clear_attempt(s, heat)
                heat.status = HeatStatus.staged
                s.add(heat)
                audit(s, actor, "heat.disarm", "heat", heat.id, reason=reason)
                s.commit()
                self._publish_reset(heat, reason)
                self._publish_heat(s, heat, "heat.disarmed")
        self.publish_tracks([track_id])
        return heat

    async def unstage(self, track_id: int, actor: Actor) -> None:
        """Take the heat off the track and back into the queue."""
        async with self._lock:
            with self.session() as s:
                heat = self.current_heat(s, track_id)
                if not heat:
                    raise ServiceError("Nothing is staged.", 409)
                was_armed = heat.status == HeatStatus.armed
                if was_armed and track_id in self.timers:
                    await self.timers[track_id].disarm()
                self._clear_attempt(s, heat)
                before = heat.status.value
                heat.status = HeatStatus.scheduled
                heat.staged_at = None
                s.add(heat)
                audit(s, actor, "heat.unstage", "heat", heat.id, before={"status": before})
                s.commit()
                if was_armed:
                    self._publish_reset(heat, "taken off the track")
                self._publish_heat(s, heat, "heat.unstaged")
        self.publish_tracks()

    async def arm(self, track_id: int, actor: Actor) -> Heat:
        async with self._lock:
            with self.session() as s:
                heat = self.current_heat(s, track_id)
                if not heat or heat.status != HeatStatus.staged:
                    raise ServiceError("Stage a heat before arming.", 409)
                heat.status = HeatStatus.armed
                heat.armed_at = utcnow()
                s.add(heat)
                audit(s, actor, "heat.arm", "heat", heat.id)
                s.commit()
                lanes = [hl.lane for hl in self._lanes(s, heat.id) if hl.entry_id]
                self._publish_heat(s, heat, "heat.armed")
        src = self.timers.get(track_id)
        if src:
            await src.arm(heat.id, lanes)
        self.publish_tracks([track_id])
        return heat

    async def record_pending(
        self,
        heat_id: int,
        times: dict[int, float | None],
        source: str,
        actor: Actor,
        raw: str | None = None,
        allow_staged: bool = False,
    ) -> Heat:
        """Store timer (or manual) results as pending for the head timekeeper."""
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                ok = {HeatStatus.armed} | ({HeatStatus.staged} if allow_staged else set())
                if heat.status not in ok:
                    raise ServiceError(f"Heat is {heat.status.value}, not armed.", 409)
                lanes = self._lanes(s, heat.id)
                for hl in lanes:
                    if hl.entry_id is None:
                        continue
                    if hl.lane not in times:
                        raise ServiceError(f"Missing time for lane {hl.lane}.")
                    t = times[hl.lane]
                    if t is not None and t <= 0:
                        raise ServiceError(f"Lane {hl.lane}: time must be positive.")
                    hl.raw_time = t
                    hl.final_time = t
                    hl.dnf = t is None
                    if not (source == "phone" and hl.source == "phone"):
                        hl.source = source
                    s.add(hl)
                _apply_places(lanes)
                heat.status = HeatStatus.pending
                heat.results_at = utcnow()
                s.add(heat)
                audit(s, actor, "heat.results", "heat", heat.id, after={"source": source, "raw": raw, "lanes": [_lane_dict(h) for h in lanes]})
                s.commit()
                self._publish_heat(s, heat, "heat.pending")
            src = self.timers.get(heat.track_id)
            if src and src.armed_heat == heat.id:
                await src.disarm()
        self.publish_tracks()
        return heat

    async def manual_entry(self, track_id: int, times: dict[int, float | None], actor: Actor) -> Heat:
        with self.session() as s:
            heat = self.current_heat(s, track_id)
            if not heat:
                raise ServiceError("No staged or armed heat on this track.", 409)
        src = self.timers.get(track_id)
        if src and src.armed_heat == heat.id:
            await src.disarm()
        return await self.record_pending(heat.id, times, "manual", actor, allow_staged=True)

    async def register_start(self, track_id: int, heat_id: int, start_ms: float, source: str, actor: Actor) -> dict[str, Any]:
        """Record a start press for a human-timed heat; the earliest one wins.

        ``start_ms`` is in server-clock epoch milliseconds (phones sync their
        clocks to the server). Lanes that already pressed STOP get their times
        (re)computed, and the start is relayed to every screen.
        """
        if start_ms > time.time() * 1000 + START_SKEW_MS:
            raise ServiceError("That start time is in the future. Reload the page to re-sync the phone's clock.")
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                if heat.track_id != track_id:
                    raise ServiceError("Heat is not on this track.", 409)
                if heat.status not in (HeatStatus.armed, HeatStatus.pending):
                    raise ServiceError(f"Heat {heat.number} is {heat.status.value}; arm it before starting.", 409)
                if heat.armed_at and start_ms < heat.armed_at.timestamp() * 1000 - START_SKEW_MS:
                    raise ServiceError("That start was pressed before the heat was armed.", 409)
                if heat.start_ms is not None and start_ms >= heat.start_ms:
                    return {"ok": True, "start_ms": heat.start_ms, "source": heat.start_source, "updated": False}
                before = heat.start_ms
                heat.start_ms, heat.start_source = start_ms, source
                s.add(heat)
                lanes = self._lanes(s, heat.id)
                feed = []
                for hl in lanes:
                    if hl.stop_ms is None or hl.dnf or hl.edit_reason:
                        continue
                    t = round((hl.stop_ms - start_ms) / 1000, 3)
                    if t > 0:
                        hl.raw_time = hl.final_time = t
                        s.add(hl)
                        feed.append((hl.lane, t))
                if heat.status == HeatStatus.pending:
                    _apply_places(lanes)
                audit(s, actor, "heat.start", "heat", heat.id, before={"start_ms": before}, after={"start_ms": start_ms, "source": source})
                s.commit()
                self.bus.publish(
                    "heat.started",
                    {"heat_id": heat.id, "track_id": track_id, "start_ms": start_ms, "source": source, "corrected": before is not None},
                    keys=["heat", f"heat-{heat.id}", f"track-{track_id}"],
                )
                if heat.status == HeatStatus.pending:
                    self._publish_heat(s, heat, "heat.updated")
                armed = heat.status == HeatStatus.armed
        src = self.timers.get(track_id)
        if armed and isinstance(src, PhoneStopwatch):
            for lane, t in feed:
                await src.submit_lane(heat_id, lane, t)  # may complete the heat
        self.publish_tracks([track_id])
        return {"ok": True, "start_ms": start_ms, "source": source, "updated": True}

    async def submit_lane(
        self,
        track_id: int,
        lane: int,
        heat_id: int,
        seconds: float | None,
        submission_id: str,
        actor: Actor,
        *,
        stop_ms: float | None = None,
        start_ms: float | None = None,
    ) -> dict[str, Any]:
        """One lane official's result. Idempotent per ``submission_id``.

        Either ``stop_ms`` (STOP press on the server clock; the time is stop
        minus the heat's shared start) or ``seconds`` (a locally timed elapsed
        time). Neither means DNF. ``start_ms`` is the official's own START
        press, if they made one; it counts toward the earliest start.
        """
        if start_ms is not None:
            try:
                await self.register_start(track_id, heat_id, start_ms, f"Lane {lane}", actor)
            except ServiceError:
                pass  # the checks below give the real answer
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                if heat.track_id != track_id:
                    raise ServiceError("Heat is not on this track.", 409)
                hl = s.exec(select(HeatLane).where(HeatLane.heat_id == heat_id, HeatLane.lane == lane)).first()
                if not hl or hl.entry_id is None:
                    raise ServiceError("No racer in this lane for that heat.", 409)
                if hl.submission_id and hl.submission_id == submission_id:
                    return {"ok": True, "duplicate": True, "time": hl.raw_time}
                if heat.status != HeatStatus.armed:
                    raise ServiceError(f"Heat {heat.number} is {heat.status.value}; result not accepted.", 409)
                if hl.raw_time is not None or hl.dnf or hl.stop_ms is not None:
                    raise ServiceError("This lane already has a time for the heat.", 409)
                dnf = stop_ms is None and seconds is None
                if stop_ms is not None:
                    seconds = None
                    if heat.start_ms is not None:
                        seconds = round((stop_ms - heat.start_ms) / 1000, 3)
                        if seconds <= 0:
                            raise ServiceError("STOP was pressed before the heat's start.")
                    hl.stop_ms = stop_ms
                elif seconds is not None and seconds <= 0:
                    raise ServiceError("Time must be positive.")
                hl.raw_time = seconds
                hl.final_time = seconds
                hl.dnf = dnf
                hl.source = "phone"
                hl.submission_id = submission_id
                s.add(hl)
                audit(s, actor, "lane.submit", "heat", heat.id, after={"lane": lane, "time": seconds, "stop_ms": stop_ms})
                s.commit()
                self.bus.publish(
                    "lane.submitted",
                    {"heat_id": heat.id, "track_id": track_id, "lane": lane, "time": seconds},
                    keys=["heat", f"heat-{heat.id}", f"track-{track_id}"],
                )
        src = self.timers.get(track_id)
        if isinstance(src, PhoneStopwatch) and (seconds is not None or dnf):
            await src.submit_lane(heat_id, lane, seconds)  # may emit -> record_pending
        self.publish_tracks([track_id])
        return {"ok": True, "time": seconds, "waiting_for_start": stop_ms is not None and seconds is None}

    async def approve(self, heat_id: int, actor: Actor) -> Heat:
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                if heat.status != HeatStatus.pending:
                    raise ServiceError(f"Heat is {heat.status.value}; only pending heats can be approved.", 409)
                heat.status = HeatStatus.approved
                heat.approved_at = utcnow()
                s.add(heat)
                lanes = self._lanes(s, heat.id)
                audit(s, actor, "heat.approve", "heat", heat.id, after={"lanes": [_lane_dict(h) for h in lanes]})
                s.commit()
                self._publish_heat(s, heat, "heat.approved")
                self.alerts.results(s, heat)
        self.publish_tracks()
        return heat

    async def edit_lane(self, heat_id: int, lane: int, seconds: float | None, reason: str, actor: Actor) -> Heat:
        reason = (reason or "").strip()
        if not reason:
            raise ServiceError("A reason is required to edit a time.")
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                if heat.status not in (HeatStatus.pending, HeatStatus.approved):
                    raise ServiceError("Only pending or approved heats can be edited.", 409)
                lanes = self._lanes(s, heat.id)
                hl = next((h for h in lanes if h.lane == lane and h.entry_id), None)
                if not hl:
                    raise ServiceError("No racer in that lane.")
                if seconds is not None and seconds <= 0:
                    raise ServiceError("Time must be positive.")
                before = _lane_dict(hl)
                hl.final_time = seconds
                hl.dnf = seconds is None
                hl.edit_reason = reason
                _apply_places(lanes)
                for h in lanes:
                    s.add(h)
                audit(s, actor, "lane.edit", "heat", heat.id, before=before, after=_lane_dict(hl), reason=reason)
                s.commit()
                self._publish_heat(s, heat, "heat.updated")
                if heat.status == HeatStatus.approved:
                    self.alerts.results(s, heat)
        return heat

    async def rerun(self, heat_id: int, reason: str, actor: Actor) -> Heat:
        """Void a heat that has raced and put the same lineup back on the
        track, staged and disarmed (or first in the queue if the track has
        already moved on to another heat)."""
        reason = (reason or "").strip() or "re-run"
        async with self._lock:
            with self.session() as s:
                heat = self._get_heat(s, heat_id)
                if heat.status in (HeatStatus.scheduled, HeatStatus.rerun):
                    raise ServiceError(f"Heat is {heat.status.value}; nothing to re-run.", 409)
                if not views.heat_started(heat, s.get(Track, heat.track_id)):
                    raise ServiceError("This heat hasn't started yet, so there's nothing to re-run. Use Disarm or Unstage instead.", 409)
                src = self.timers.get(heat.track_id)
                if src and src.armed_heat == heat.id:
                    await src.disarm()
                was_on_track = heat.status in (HeatStatus.staged, HeatStatus.armed)
                before = heat.status.value
                heat.status = HeatStatus.rerun
                s.add(heat)
                s.flush()
                # slot the replacement right after the original in the queue
                later = s.exec(
                    select(Heat.seq).where(Heat.round_id == heat.round_id, Heat.seq > heat.seq).order_by(Heat.seq)
                ).first()
                new_seq = (heat.seq + later) / 2 if later is not None else heat.seq + 0.5
                new = Heat(
                    round_id=heat.round_id, track_id=heat.track_id, seq=new_seq, number=heat.number, rerun_of_id=heat.id
                )
                s.add(new)
                s.flush()
                for hl in self._lanes(s, heat.id):
                    s.add(HeatLane(heat_id=new.id, lane=hl.lane, entry_id=hl.entry_id))
                s.flush()
                # straight back on the track if it's free and nobody is racing elsewhere
                busy = set().union(*(r for t, r in views.on_track_racers(s).items() if t != heat.track_id)) if not was_on_track else set()
                mine = {hl.entry_id for hl in self._lanes(s, new.id) if hl.entry_id}
                racers = views.racers_by_heat(s, [new.id])[new.id]
                if was_on_track or (self.current_heat(s, heat.track_id) is None and not (racers & busy) and mine):
                    new.status = HeatStatus.staged
                    new.staged_at = utcnow()
                    s.add(new)
                audit(
                    s, actor, "heat.rerun", "heat", heat.id,
                    before={"status": before}, after={"new_heat": new.id, "new_status": new.status.value}, reason=reason,
                )
                s.commit()
                self._publish_heat(s, heat, "heat.rerun")
                self._publish_reset(heat, "re-run: " + reason, new_heat_id=new.id)
                if new.status == HeatStatus.staged:
                    self._publish_heat(s, new, "heat.staged")
        self.publish_tracks()
        return new

    def _publish_reset(self, heat: Heat, reason: str, new_heat_id: int | None = None) -> None:
        """Tell lane phones to clear their stopwatch for this heat."""
        self.bus.publish(
            "heat.reset",
            {"heat_id": heat.id, "track_id": heat.track_id, "reason": reason, "new_heat_id": new_heat_id},
            keys=["heat", f"heat-{heat.id}", f"track-{heat.track_id}"],
        )

    # --- queries used by pages -------------------------------------------------

    def pending_heats(self) -> list[dict[str, Any]]:
        with self.session() as s:
            heats = list(
                s.exec(
                    select(Heat).where(Heat.status == HeatStatus.pending).order_by(Heat.results_at)
                )
            )
            return views.heat_views(s, heats)
