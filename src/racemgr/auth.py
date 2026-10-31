"""Device auth for officials.

The admin page shows QR codes for short-lived JoinTokens bound to a role, an
event (except for admins) and optionally a track/lane. Redeeming one creates an Official and sets a random
device cookie; only its SHA-256 is stored. Viewers never log in.
"""
from __future__ import annotations

import hashlib
import secrets
import socket
from datetime import timedelta

from fastapi import Request
from sqlmodel import Session, select

from .config import Settings
from .models import Event, JoinToken, Official, Role, Track, utcnow
from .services import Actor, ServiceError

COOKIE = "rm_device"

ROLE_LABELS = {
    Role.admin: "Admin",
    Role.head_timekeeper: "Head timekeeper",
    Role.track_operator: "Track operator",
    Role.lane_official: "Lane official",
    Role.checkin: "Check-in",
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_join_token(
    session: Session,
    role: Role,
    minutes: int,
    track_id: int | None = None,
    lane: int | None = None,
    label: str = "",
    max_uses: int = 1,
    event_id: int | None = None,
) -> JoinToken:
    if role == Role.lane_official and (track_id is None or lane is None):
        raise ServiceError("Lane officials need a track and lane.")
    if track_id is not None:
        track = session.get(Track, track_id)
        if track is None:
            raise ServiceError("Track not found.")
        if event_id is not None and event_id != track.event_id:
            raise ServiceError("That track belongs to a different event.")
        event_id = track.event_id
    jt = JoinToken(
        token=secrets.token_urlsafe(16),
        role=role,
        event_id=event_id,
        track_id=track_id,
        lane=lane,
        label=label,
        max_uses=max(1, max_uses),
        expires_at=utcnow() + timedelta(minutes=minutes),
    )
    session.add(jt)
    session.flush()
    return jt


def token_problem(jt: JoinToken | None) -> str | None:
    if jt is None:
        return "This join code is not valid."
    if jt.revoked:
        return "This join code was revoked."
    if jt.uses >= jt.max_uses:
        return "This join code has already been used."
    if jt.expires_at < utcnow():
        return "This join code has expired. Ask the admin for a new one."
    return None


def redeem(session: Session, token: str) -> tuple[Official, str]:
    jt = session.exec(select(JoinToken).where(JoinToken.token == token)).first()
    problem = token_problem(jt)
    if problem:
        raise ServiceError(problem, 410)
    device = secrets.token_urlsafe(32)
    official = Official(
        role=jt.role,
        label=jt.label or ROLE_LABELS[jt.role],
        event_id=jt.event_id,
        track_id=jt.track_id,
        lane=jt.lane,
        device_token_hash=hash_token(device),
    )
    jt.uses += 1
    session.add(jt)
    session.add(official)
    session.flush()
    return official, device


def official_from_request(session: Session, request: Request) -> Official | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    o = session.exec(select(Official).where(Official.device_token_hash == hash_token(token))).first()
    if not o or o.revoked:
        return None
    now = utcnow()
    if not o.last_seen or (now - o.last_seen).total_seconds() > 60:
        o.last_seen = now
        session.add(o)
        session.commit()
    return o


def actor_for(o: Official | None) -> Actor:
    if o is None:
        return Actor(None, "anonymous")
    return Actor(o.id, f"{ROLE_LABELS[o.role]}: {o.label}" if o.label else ROLE_LABELS[o.role])


# --- permissions -------------------------------------------------------------


def can_admin(o: Official | None) -> bool:
    return bool(o and o.role == Role.admin)


def can_timekeep(o: Official | None) -> bool:
    return bool(o and o.role in (Role.admin, Role.head_timekeeper))


def can_operate(o: Official | None, track_id: int) -> bool:
    if not o:
        return False
    if o.role in (Role.admin, Role.head_timekeeper):
        return True
    return o.role == Role.track_operator and o.track_id in (None, track_id)


def can_time_lane(o: Official | None, track_id: int, lane: int) -> bool:
    if not o:
        return False
    if o.role == Role.lane_official:
        return o.track_id == track_id and o.lane == lane
    return can_operate(o, track_id)


def can_start(o: Official | None, track_id: int) -> bool:
    """Operators and any lane official on the track may press START."""
    if o and o.role == Role.lane_official:
        return o.track_id == track_id
    return can_operate(o, track_id)


def can_checkin(o: Official | None) -> bool:
    return bool(o and o.role in (Role.admin, Role.checkin))


def home_for(o: Official) -> str:
    if o.role == Role.admin:
        return "/admin"
    if o.role == Role.head_timekeeper:
        return "/timekeeper"
    if o.role == Role.track_operator:
        return f"/track/{o.track_id}/operate" if o.track_id else "/operate"
    if o.role == Role.lane_official:
        return f"/track/{o.track_id}/lane/{o.lane}"
    if o.role == Role.checkin:
        return "/checkin"
    return "/"


ROLE_HINTS = {
    Role.admin: "Set up events, tracks and officials.",
    Role.head_timekeeper: "Approve, edit or re-run results.",
    Role.track_operator: "Stage and arm heats.",
    Role.lane_official: "Time your lane with the stopwatch.",
    Role.checkin: "Check in, weigh and inspect entries.",
}


def nav_for(o: Official | None) -> list[tuple[str, str]]:
    """(href, label) links for this device, the role's own page first."""
    if o is None:
        return [("/display/standings", "Standings")]
    links = [(home_for(o), {Role.admin: "Admin", Role.head_timekeeper: "Timekeeper", Role.track_operator: "Operate",
                             Role.lane_official: "My lane", Role.checkin: "Check-in"}[o.role])]
    if o.role == Role.admin:
        links += [("/admin/officials", "Officials"), ("/operate", "Operate"), ("/timekeeper", "Timekeeper"), ("/checkin", "Check-in")]
    elif o.role == Role.head_timekeeper:
        links += [("/operate", "Operate")]
    links += [("/", "Schedule"), ("/display/standings", "Standings")]
    return links


def scope_label(o: Official, track_name: str | None, event_name: str | None = None) -> str:
    """'Fall Derby · Main Track · lane 2', 'Fall Derby · all tracks' for operators,
    just the event for other roles; '' for admins and officials without an event."""
    parts = [event_name] if event_name else []
    if o.role == Role.lane_official:
        parts.append(f"{track_name or 'track ' + str(o.track_id)} · lane {o.lane}")
    elif o.role == Role.track_operator:
        parts.append(track_name or "all tracks")
    return " · ".join(parts)


def bound_names(session: Session, o: Official | JoinToken) -> tuple[str | None, str | None]:
    """(event name, track name) an official or join code is bound to."""
    ev = session.get(Event, o.event_id) if o.event_id else None
    track = session.get(Track, o.track_id) if o.track_id else None
    return (ev.name if ev else None), (track.name if track else None)


# --- URLs ----------------------------------------------------------------------


def lan_ip() -> str:
    """Best-guess LAN address. Works offline too (UDP connect sends nothing)."""
    for probe in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 1))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    return ip
        except OSError:
            continue
    return "127.0.0.1"


def base_url(settings: Settings, request: Request | None = None) -> str:
    """URL phones should use: config > request host (if not localhost) > LAN IP."""
    if settings.public_base_url:
        return settings.public_base_url
    scheme = "https" if settings.use_https else "http"
    if request is not None:
        host = request.url.hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return str(request.base_url).rstrip("/")
        port = request.url.port or settings.port
        return f"{request.url.scheme}://{lan_ip()}:{port}"
    return f"{scheme}://{lan_ip()}:{settings.port}"


def ensure_admin_link(session: Session, settings: Settings, force: bool = False) -> str | None:
    """Return a one-time admin URL if no admin device exists yet (or ``force``)."""
    has_admin = session.exec(select(Official).where(Official.role == Role.admin, Official.revoked == False)).first()  # noqa: E712
    if has_admin and not force:
        return None
    jt = create_join_token(session, Role.admin, settings.admin_link_minutes, label="Admin")
    session.commit()
    return f"{base_url(settings)}/join/{jt.token}"
