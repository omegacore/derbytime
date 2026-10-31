"""Follow alerts for racers.

Alerts are computed server-side and published on the bus as ``alert`` events
keyed ``racer-<id>``. Browsers filter for the racers they follow; a future Web
Push sender would subscribe to the same events and look up push subscriptions
by racer id.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session

from ..bus import EventBus
from ..models import Heat
from . import views

KIND_TEXT = {
    "two_away": "is 2 heats away",
    "on_deck": "is on deck",
    "racing": "is racing now",
    "results": "results posted",
}


class AlertService:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._sent: set[tuple[int, str, int]] = set()

    def _send(self, kind: str, heat: dict[str, Any], lane: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
        key = (lane["racer_id"], kind, heat["id"])
        if key in self._sent:
            return
        self._sent.add(key)
        msg = f"{lane['racer_name']} (#{lane['number']}) {KIND_TEXT[kind]} — {heat['track_name']}, heat {heat['number']}, lane {lane['lane']}"
        self.bus.publish(
            "alert",
            {
                "kind": kind,
                "racer_id": lane["racer_id"],
                "racer_name": lane["racer_name"],
                "number": lane["number"],
                "heat_id": heat["id"],
                "heat_number": heat["number"],
                "track_id": heat["track_id"],
                "track_name": heat["track_name"],
                "lane": lane["lane"],
                "message": msg,
                **(extra or {}),
            },
            keys=["alert", f"racer-{lane['racer_id']}"],
        )

    def refresh(self, session: Session) -> None:
        """Send any new racing / on-deck / two-away alerts."""
        for heat in views.heat_views(session, views.on_track_heats(session)):
            for lane in heat["lanes"]:
                if not lane["bye"]:
                    self._send("racing", heat, lane)
        for track_id, heats in views.plan_all(session, limit=2).items():
            for kind, hv in zip(("on_deck", "two_away"), views.heat_views(session, heats, track_id=track_id)):
                for lane in hv["lanes"]:
                    if not lane["bye"]:
                        self._send(kind, hv, lane)

    def results(self, session: Session, heat: Heat) -> None:
        hv = views.heat_view(session, heat)
        for lane in hv["lanes"]:
            if lane["bye"]:
                continue
            t = "DNF" if lane["dnf"] else f"{lane['final_time']:.3f}s"
            self._sent.discard((lane["racer_id"], "results", heat.id))  # re-send after edits
            self._send("results", hv, lane, {"time": t, "place": lane["place"]})

    def status_for(self, session: Session, racer_ids: set[int] | None = None) -> dict[int, dict[str, Any]]:
        """Current state for followed racers (used when a page first loads).
        ``None`` means every racer with a status."""
        out: dict[int, dict[str, Any]] = {}
        for heat in views.heat_views(session, views.on_track_heats(session)):
            for lane in heat["lanes"]:
                if not lane["bye"] and (racer_ids is None or lane["racer_id"] in racer_ids):
                    out[lane["racer_id"]] = {"kind": "racing", "track_name": heat["track_name"], "heat_number": heat["number"], "lane": lane["lane"]}
        for track_id, heats in views.plan_all(session, limit=2).items():
            for kind, hv in zip(("on_deck", "two_away"), views.heat_views(session, heats, track_id=track_id)):
                for lane in hv["lanes"]:
                    if not lane["bye"] and (racer_ids is None or lane["racer_id"] in racer_ids) and lane["racer_id"] not in out:
                        out[lane["racer_id"]] = {"kind": kind, "track_name": hv["track_name"], "heat_number": hv["number"], "lane": lane["lane"]}
        return out
