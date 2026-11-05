"""Scouts' choice voting: the vote official's tablet page, the tallies and the
category controls. Votes are official-only (never public pages), so nothing
here goes through the public cache."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from .. import auth
from ..models import Event, EventStatus, Official, Vote, VoteCategory
from ..services import ServiceError, votes
from .deps import get_official, get_session, render, require

router = APIRouter()


def vote_events(s: Session, me: Official) -> list[Event]:
    """Vote officials work their own event; admins pick from the unfinished ones."""
    if me.event_id:
        return [e for e in [s.get(Event, me.event_id)] if e]
    return list(s.exec(select(Event).where(Event.status != EventStatus.done).order_by(Event.id)))


def pick_event(s: Session, me: Official, event_id: int | None) -> tuple[list[Event], Event | None]:
    events = vote_events(s, me)
    ev = next((e for e in events if e.id == event_id), None) or (events[0] if events else None)
    return events, ev


def my_category(s: Session, me: Official | None, category_id: int) -> VoteCategory:
    require(auth.can_vote(me), "voting")
    cat = votes.get_category(s, category_id)
    if me.event_id and me.event_id != cat.event_id:
        raise ServiceError("That category belongs to a different event.", 403)
    return cat


def panel_ctx(s: Session, ev: Event, cat: VoteCategory, **extra) -> dict:
    voter_rows = votes.voters(s, ev.id, cat)
    return {
        "event": ev,
        "cat": cat,
        "voters": voter_rows,
        "voted": sum(1 for v in voter_rows if v["vote"]),
        "ballot": votes.ballot(s, ev.id),
        "recent": votes.recent_votes(s, cat),
        **extra,
    }


def publish(request: Request, event_id: int, what: str) -> None:
    # only vote tablets and the results page listen; no viewer phone wakes up
    request.app.state.bus.publish("vote.updated", {"what": what}, keys=[f"votes-{event_id}"])


@router.get("/vote")
def vote_page(request: Request, event: int | None = None, category: int | None = None, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_vote(me), "voting")
    events, ev = pick_event(s, me, event)
    cats = votes.categories(s, ev.id) if ev else []
    cat = next((c for c in cats if c.id == category), None) or next((c for c in cats if c.open), None) or (cats[0] if cats else None)
    ctx = {"events": events, "event": ev, "cats": cats, "cat": cat}
    if cat:
        ctx.update(panel_ctx(s, ev, cat))
    return render(request, "vote.html", ctx)


@router.get("/partials/vote/{category_id}")
def vote_panel(category_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    cat = my_category(s, me, category_id)
    return render(request, "partials/vote_panel.html", panel_ctx(s, s.get(Event, cat.event_id), cat))


@router.post("/vote/cast")
def vote_cast(
    request: Request,
    category_id: int = Form(...),
    voter: int | None = Form(None),
    entry: int | None = Form(None),
    replace: str = Form(""),
    s: Session = Depends(get_session),
    me=Depends(get_official),
):
    cat = my_category(s, me, category_id)
    if voter is None:
        raise ServiceError("Pick the scout who is voting.")
    if entry is None:
        raise ServiceError("Pick the vehicle they voted for.")
    vote, changed = votes.cast_vote(s, cat, voter, entry, auth.actor_for(me), replace=replace == "1")
    s.commit()
    publish(request, cat.event_id, "cast")
    ev = s.get(Event, cat.event_id)
    rec = next(r for r in votes.recent_votes(s, cat, limit=50) if r["vote"].id == vote.id)
    return render(request, "partials/vote_panel.html", panel_ctx(s, ev, cat, just=rec, changed=changed))


@router.post("/vote/{vote_id}/delete")
def vote_delete(vote_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    v = s.get(Vote, vote_id)
    if v is None:
        raise ServiceError("That vote was already removed.", 404)
    cat = my_category(s, me, v.category_id)
    votes.delete_vote(s, vote_id, auth.actor_for(me))
    s.commit()
    publish(request, cat.event_id, "delete")
    return render(request, "partials/vote_panel.html", panel_ctx(s, s.get(Event, cat.event_id), cat, undone=True))


# --- results and categories ----------------------------------------------------------


def results_ctx(s: Session, ev: Event) -> dict:
    cats = votes.categories(s, ev.id)
    return {"event": ev, "results": [{"cat": c, "rows": votes.tally(s, c)} for c in cats]}


@router.get("/vote/results")
def vote_results(request: Request, event: int | None = None, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_vote(me), "vote results")
    events, ev = pick_event(s, me, event)
    ctx = {"events": events, "event": ev, "results": []}
    if ev:
        ctx.update(results_ctx(s, ev))
    return render(request, "vote_results.html", ctx)


@router.get("/partials/vote-results/{event_id}")
def vote_results_partial(event_id: int, request: Request, s: Session = Depends(get_session), me=Depends(get_official)):
    require(auth.can_vote(me), "vote results")
    _, ev = pick_event(s, me, event_id)
    if ev is None or ev.id != event_id:
        raise ServiceError("Event not found.", 404)
    return render(request, "partials/vote_results.html", results_ctx(s, ev))


def back(event_id: int, msg: str = "", error: bool = False) -> RedirectResponse:
    from .admin import back as admin_back

    return admin_back(f"/vote/results?event={event_id}", msg, error)


@router.post("/vote/categories")
def category_add(
    request: Request,
    event_id: int = Form(...),
    name: str = Form(""),
    next: str = Form(""),
    s: Session = Depends(get_session),
    me=Depends(get_official),
):
    require(auth.can_vote(me), "voting")
    _, ev = pick_event(s, me, event_id)
    if ev is None or ev.id != event_id:
        raise ServiceError("You can't add categories to that event.", 403)
    try:
        cat = votes.add_category(s, ev, name, auth.actor_for(me))
    except ServiceError as e:
        return back(event_id, e.message, True)
    s.commit()
    publish(request, ev.id, "category")
    if next == "vote":  # added from the empty tablet page: start voting right away
        return RedirectResponse(f"/vote?event={ev.id}&category={cat.id}", status_code=303)
    return back(event_id, f"Added {cat.name}.")


@router.post("/vote/category/{category_id}")
def category_update(category_id: int, request: Request, action: str = Form(...), s: Session = Depends(get_session), me=Depends(get_official)):
    cat = my_category(s, me, category_id)
    event_id, name = cat.event_id, cat.name
    if action in ("open", "close"):
        votes.set_category_open(s, cat, action == "open", auth.actor_for(me))
        msg = f"{name} is {'open' if action == 'open' else 'closed'} for voting."
    elif action == "delete":
        require(auth.can_admin(me), "deleting vote categories")
        n = votes.delete_category(s, cat, auth.actor_for(me))
        msg = f"Deleted {name}" + (f" and its {n} vote{'' if n == 1 else 's'}." if n else ".")
    else:
        raise ServiceError("Unknown action.")
    s.commit()
    publish(request, event_id, "category")
    return back(event_id, msg)
