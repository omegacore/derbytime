"""Scouts' choice voting: the vote official role, the tablet flow and tallies."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from derbytime import auth
from derbytime.app import create_app
from derbytime.models import AuditLog, Entry, EntryStatus, Event, JoinToken, Official, Racer, Role, Track, Vote, VoteCategory
from derbytime.seed import seed_demo
from derbytime.services import Actor, ServiceError, votes


@pytest.fixture
def app(settings, engine):
    with Session(engine, expire_on_commit=False) as s:
        seed_demo(s, settings)
    return create_app(settings, engine, print_admin_link=False)


def derby(s):
    return s.exec(select(Event).where(Event.type == "derby")).one()


def join(app, role, **kw):
    c = TestClient(app)
    with Session(app.state.engine) as s:
        jt = auth.create_join_token(s, role, 10, **kw)
        s.commit()
        token = jt.token
    r = c.post(f"/join/{token}", follow_redirects=False)
    assert r.status_code == 303
    return c, r.headers["location"]


@pytest.fixture
def setup(app):
    """A vote official on the derby, a Best Design category, and two scouts with their entries."""
    with Session(app.state.engine, expire_on_commit=False) as s:
        ev = derby(s)
        cat = votes.add_category(s, ev, "  Best   Design ", Actor.system())
        s.commit()
        entries = s.exec(select(Entry).where(Entry.event_id == ev.id).order_by(Entry.number)).all()
    c, home = join(app, Role.vote_official, event_id=ev.id)
    assert home == "/vote"
    return c, ev, cat, entries


def cast(c, cat, voter, entry, replace=""):
    return c.post("/vote/cast", data={"category_id": cat.id, "voter": voter, "entry": entry, "replace": replace}, headers={"HX-Request": "true"})


def test_vote_official_records_a_vote(app, setup):
    c, ev, cat, entries = setup
    page = c.get("/vote").text
    assert "Best Design" in page and "Who's voting?" in page and 'class="top role-vote_official"' in page
    a, b = entries[0], entries[1]
    r = cast(c, cat, a.racer_id, b.id)
    assert r.status_code == 200, r.text
    assert "Recorded" in r.text and f"#{b.number}" in r.text
    with Session(app.state.engine) as s:
        v = s.exec(select(Vote)).one()
        assert (v.voter_racer_id, v.entry_id, v.category_id) == (a.racer_id, b.id, cat.id)
        assert v.official_id == s.exec(select(Official).where(Official.role == Role.vote_official)).one().id
        assert s.exec(select(AuditLog).where(AuditLog.action == "vote.cast")).one()


def test_one_vote_per_scout_per_category(app, setup):
    c, ev, cat, entries = setup
    a, b, d = entries[0], entries[1], entries[2]
    assert cast(c, cat, a.racer_id, b.id).status_code == 200
    r = cast(c, cat, a.racer_id, d.id)
    assert r.status_code == 409 and "already voted" in r.json()["error"]
    # the tablet sends replace=1 once the official sees the earlier vote
    r = cast(c, cat, a.racer_id, d.id, replace="1")
    assert r.status_code == 200 and "Changed" in r.text
    with Session(app.state.engine) as s:
        assert [v.entry_id for v in s.exec(select(Vote))] == [d.id]
    # a different category is a separate ballot
    with Session(app.state.engine, expire_on_commit=False) as s:
        other = votes.add_category(s, s.get(Event, ev.id), "Most Creative", Actor.system())
        s.commit()
    assert cast(c, other, a.racer_id, b.id).status_code == 200


def test_ballot_rules(app, setup):
    c, ev, cat, entries = setup
    a, b = entries[0], entries[1]
    r = cast(c, cat, a.racer_id, a.id)
    assert r.status_code == 400 and "own vehicle" in r.json()["error"]
    with Session(app.state.engine) as s:
        e = s.get(Entry, b.id)
        e.status = EntryStatus.disqualified
        s.add(e)
        s.commit()
    assert cast(c, cat, a.racer_id, b.id).status_code == 400
    # closed categories take no votes
    assert c.post(f"/vote/category/{cat.id}", data={"action": "close"}, follow_redirects=False).status_code == 303
    r = cast(c, cat, a.racer_id, entries[2].id)
    assert r.status_code == 409 and "closed" in r.json()["error"]
    assert "is closed" in c.get(f"/vote?category={cat.id}").text


def test_undo_vote(app, setup):
    c, ev, cat, entries = setup
    cast(c, cat, entries[0].racer_id, entries[1].id)
    with Session(app.state.engine) as s:
        vid = s.exec(select(Vote)).one().id
    r = c.post(f"/vote/{vid}/delete", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Vote removed" in r.text
    with Session(app.state.engine) as s:
        assert s.exec(select(Vote)).all() == []


def test_tally_places_and_ties(app, setup):
    c, ev, cat, entries = setup
    e1, e2, e3 = entries[0], entries[1], entries[2]
    for voter in entries[3:6]:
        cast(c, cat, voter.racer_id, e1.id)
    cast(c, cat, entries[6].racer_id, e2.id)
    cast(c, cat, entries[7].racer_id, e3.id)
    with Session(app.state.engine) as s:
        rows = votes.tally(s, s.get(VoteCategory, cat.id))
    assert [(r["entry"].id, r["votes"], r["place"], r["tied"]) for r in rows] == [
        (e1.id, 3, 1, False), (e2.id, 1, 2, True), (e3.id, 1, 2, True)
    ]
    page = c.get("/vote/results").text
    assert "Best Design" in page and "tie" in page
    assert c.get(f"/partials/vote-results/{ev.id}").status_code == 200


def test_vote_official_is_bound_to_their_event(app, setup):
    c, ev, cat, entries = setup
    with Session(app.state.engine, expire_on_commit=False) as s:
        regatta = s.exec(select(Event).where(Event.type == "regatta")).one()
        other = votes.add_category(s, regatta, "Best Boat", Actor.system())
        s.commit()
        boat = s.exec(select(Entry).where(Entry.event_id == regatta.id)).first()
    assert cast(c, other, entries[0].racer_id, boat.id).status_code == 403
    r = c.post("/vote/categories", data={"event_id": regatta.id, "name": "Sneaky"})
    assert r.status_code == 403
    # vote officials may add and close categories, but only admins delete them
    r = c.post("/vote/categories", data={"event_id": ev.id, "name": "Funniest"}, follow_redirects=False)
    assert r.status_code == 303 and "Added" in r.headers["location"]
    # added from the empty tablet page: straight to voting in the new category
    r = c.post("/vote/categories", data={"event_id": ev.id, "name": "Fastest Looking", "next": "vote"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/vote?event={ev.id}&category=")
    assert c.post(f"/vote/category/{cat.id}", data={"action": "delete"}).status_code == 403


def test_other_roles_cannot_vote(app, setup):
    _, ev, cat, entries = setup
    c, _ = join(app, Role.checkin, event_id=ev.id)
    assert c.get("/vote").status_code == 403
    assert cast(c, cat, entries[0].racer_id, entries[1].id).status_code == 403
    assert TestClient(app).get("/vote/results").status_code == 403


def test_admin_votes_and_deletes_category(app, setup):
    _, ev, cat, entries = setup
    admin, _ = join(app, Role.admin)
    assert "Votes" in admin.get("/admin").text
    assert cast(admin, cat, entries[0].racer_id, entries[1].id).status_code == 200
    r = admin.post(f"/vote/category/{cat.id}", data={"action": "delete"}, follow_redirects=False)
    assert r.status_code == 303 and "1%20vote" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert s.exec(select(VoteCategory)).all() == [] and s.exec(select(Vote)).all() == []


def test_vote_official_join_code_has_no_track(app):
    admin, _ = join(app, Role.admin)
    with Session(app.state.engine) as s:
        ev = derby(s)
        track = s.exec(select(Track).where(Track.event_id == ev.id)).first()
    # a stale track pick from the form is dropped: votes are event-wide
    r = admin.post("/admin/tokens", data={"role": "vote_official", "event_id": ev.id, "track_id": track.id, "label": "Mr. Diaz"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    with Session(app.state.engine) as s:
        jt = s.exec(select(JoinToken).where(JoinToken.role == Role.vote_official)).one()
        assert (jt.event_id, jt.track_id) == (ev.id, None)
    page = admin.get("/admin/officials").text
    assert "Vote official" in page and "Mr. Diaz" in page


def test_duplicate_category_rejected(session, settings):
    ev, _ = seed_demo(session, settings)
    votes.add_category(session, ev, "Best Design", Actor.system())
    with pytest.raises(ServiceError):
        votes.add_category(session, ev, "best design", Actor.system())
    with pytest.raises(ServiceError):
        votes.add_category(session, ev, "   ", Actor.system())


def test_voters_list_racing_scouts_first(session, settings):
    ev, _ = seed_demo(session, settings)
    session.add(Racer(first_name="Aaron", last_name="Visitor"))
    session.flush()
    cat = votes.add_category(session, ev, "Best Design", Actor.system())
    names = [v["racer"].first_name for v in votes.voters(session, ev.id, cat)]
    assert names[-1] == "Aaron" and len(names) == 21
