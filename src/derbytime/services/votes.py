"""Scouts' choice voting: a vote official holds a tablet, each scout says which
car or boat they like best in a category (e.g. "Best Design"), and the
official records it under the scout's name.

Plain sync session work, called from sync routes: none of this touches heat
state, so it stays out of RaceService and off the event loop.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, func, select

from ..models import Entry, EntryStatus, Event, Group, InspectionStatus, Racer, Vote, VoteCategory, utcnow
from . import Actor, ServiceError
from .audit import audit

MAX_NAME = 40


def categories(s: Session, event_id: int) -> list[VoteCategory]:
    return list(s.exec(select(VoteCategory).where(VoteCategory.event_id == event_id).order_by(VoteCategory.sort_order, VoteCategory.id)))


def get_category(s: Session, category_id: int) -> VoteCategory:
    cat = s.get(VoteCategory, category_id)
    if cat is None:
        raise ServiceError("Vote category not found.", 404)
    return cat


def add_category(s: Session, ev: Event, name: str, actor: Actor) -> VoteCategory:
    name = " ".join(name.split())[:MAX_NAME]
    if not name:
        raise ServiceError("Name the category, e.g. Best Design.")
    if any(c.name.lower() == name.lower() for c in categories(s, ev.id)):
        raise ServiceError(f"{ev.name} already has a {name} category.")
    last = s.exec(select(func.max(VoteCategory.sort_order)).where(VoteCategory.event_id == ev.id)).one()
    cat = VoteCategory(event_id=ev.id, name=name, sort_order=(last or 0) + 1)
    s.add(cat)
    s.flush()
    audit(s, actor, "vote.category.add", "vote_category", cat.id, after={"name": name, "event": ev.id})
    return cat


def set_category_open(s: Session, cat: VoteCategory, is_open: bool, actor: Actor) -> None:
    if cat.open != is_open:
        audit(s, actor, "vote.category.open" if is_open else "vote.category.close", "vote_category", cat.id)
        cat.open = is_open
        s.add(cat)


def delete_category(s: Session, cat: VoteCategory, actor: Actor) -> int:
    """Remove a category and its votes; returns how many votes went with it."""
    votes = list(s.exec(select(Vote).where(Vote.category_id == cat.id)))
    for v in votes:
        s.delete(v)
    audit(s, actor, "vote.category.delete", "vote_category", cat.id, before={"name": cat.name, "votes": len(votes)})
    s.delete(cat)
    return len(votes)


def eligible_entry(entry: Entry | None, event_id: int) -> bool:
    """Vehicles on the ballot: checked in, passed or awaiting inspection, not disqualified."""
    return bool(
        entry
        and entry.event_id == event_id
        and entry.checked_in
        and entry.inspection_status != InspectionStatus.failed
        and entry.status != EntryStatus.disqualified
    )


def ballot(s: Session, event_id: int) -> list[dict[str, Any]]:
    """Vehicles a scout can vote for, by number."""
    rows = s.exec(
        select(Entry, Racer).join(Racer, Racer.id == Entry.racer_id).where(Entry.event_id == event_id).order_by(Entry.number)
    ).all()
    return [{"entry": e, "racer": r} for e, r in rows if eligible_entry(e, event_id)]


def voters(s: Session, event_id: int, cat: VoteCategory | None) -> list[dict[str, Any]]:
    """Every active scout in the pack, by name, with this category's vote if any.
    Scouts racing in the event come first: they're the ones at the table."""
    racing = set(s.exec(select(Entry.racer_id).where(Entry.event_id == event_id)))
    votes = {v.voter_racer_id: v for v in s.exec(select(Vote).where(Vote.category_id == cat.id))} if cat else {}
    numbers = dict(s.exec(select(Entry.id, Entry.number).where(Entry.event_id == event_id)).all())
    rows = s.exec(
        select(Racer, Group).join(Group, Group.id == Racer.group_id, isouter=True).where(Racer.inactive_since == None)  # noqa: E711
    ).all()
    out = []
    for r, g in rows:
        v = votes.get(r.id)
        out.append({"racer": r, "group": g, "racing": r.id in racing, "vote": v, "voted_number": numbers.get(v.entry_id) if v else None})
    out.sort(key=lambda x: (not x["racing"], x["racer"].first_name.lower(), x["racer"].last_name.lower()))
    return out


def cast_vote(
    s: Session, cat: VoteCategory, voter_racer_id: int, entry_id: int, actor: Actor, replace: bool = False
) -> tuple[Vote, bool]:
    """Record a scout's vote. A second vote in the same category needs
    ``replace`` (the tablet asks first). Returns (vote, changed_existing)."""
    if not cat.open:
        raise ServiceError(f"Voting for {cat.name} is closed.", 409)
    voter = s.get(Racer, voter_racer_id)
    if voter is None or not voter.active:
        raise ServiceError("Pick the scout who is voting.")
    entry = s.get(Entry, entry_id)
    if not eligible_entry(entry, cat.event_id):
        raise ServiceError("That vehicle isn't on the ballot (not checked in, or out of the event).")
    if entry.racer_id == voter.id:
        raise ServiceError(f"{voter.display_name} can't vote for their own vehicle.")
    existing = s.exec(select(Vote).where(Vote.category_id == cat.id, Vote.voter_racer_id == voter.id)).first()
    if existing and not replace:
        old = s.get(Entry, existing.entry_id)
        raise ServiceError(f"{voter.display_name} already voted for #{old.number if old else '?'} in {cat.name}.", 409)
    before = {"entry": existing.entry_id} if existing else None
    vote = existing or Vote(category_id=cat.id, voter_racer_id=voter.id, entry_id=entry.id)
    vote.entry_id = entry.id
    vote.official_id = actor.official_id
    vote.created_at = utcnow()
    s.add(vote)
    s.flush()
    audit(s, actor, "vote.change" if existing else "vote.cast", "vote", vote.id, before=before,
          after={"category": cat.id, "voter": voter.id, "entry": entry.id})
    return vote, existing is not None


def delete_vote(s: Session, vote_id: int, actor: Actor) -> Vote:
    vote = s.get(Vote, vote_id)
    if vote is None:
        raise ServiceError("That vote was already removed.", 404)
    audit(s, actor, "vote.delete", "vote", vote.id, before={"category": vote.category_id, "voter": vote.voter_racer_id, "entry": vote.entry_id})
    s.delete(vote)
    return vote


def recent_votes(s: Session, cat: VoteCategory, limit: int = 8) -> list[dict[str, Any]]:
    rows = s.exec(
        select(Vote, Racer, Entry)
        .join(Racer, Racer.id == Vote.voter_racer_id)
        .join(Entry, Entry.id == Vote.entry_id)
        .where(Vote.category_id == cat.id)
        .order_by(col(Vote.created_at).desc(), col(Vote.id).desc())
        .limit(limit)
    ).all()
    return [{"vote": v, "voter": r, "entry": e} for v, r, e in rows]


def tally(s: Session, cat: VoteCategory) -> list[dict[str, Any]]:
    """Vehicles with at least one vote, most votes first, with a shared place on ties."""
    counts = s.exec(select(Vote.entry_id, func.count()).where(Vote.category_id == cat.id).group_by(Vote.entry_id)).all()
    if not counts:
        return []
    info = {
        e.id: (e, r, g)
        for e, r, g in s.exec(
            select(Entry, Racer, Group)
            .join(Racer, Racer.id == Entry.racer_id)
            .join(Group, Group.id == Racer.group_id, isouter=True)
            .where(col(Entry.id).in_([eid for eid, _ in counts]))
        )
    }
    rows = []
    for eid, n in counts:
        e, r, g = info[eid]
        rows.append({"entry": e, "racer": r, "group": g, "votes": n, "out": not eligible_entry(e, cat.event_id)})
    rows.sort(key=lambda x: (-x["votes"], x["entry"].number))
    place = 0
    for i, row in enumerate(rows):
        if i == 0 or row["votes"] != rows[i - 1]["votes"]:
            place = i + 1
        row["place"] = place
        row["tied"] = sum(1 for o in rows if o["votes"] == row["votes"]) > 1
    return rows
