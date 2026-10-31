"""Regatta shared start: the earliest START press (operator or lane official)
is the heat's start; lane officials press STOP; the server computes the times."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from racemgr.app import create_app
from racemgr.models import Event, Heat, HeatLane, HeatStatus, Role, Track
from racemgr.seed import seed_demo
from racemgr.services import Actor, ServiceError

from test_services import ACTOR, make_service, run, tracks_of


def now_ms():
    return time.time() * 1000


@pytest.fixture
def regatta(session, settings):
    _, regatta = seed_demo(session, settings)
    session.commit()
    return regatta


def lanes_of(svc, heat_id):
    with svc.session() as s:
        return {hl.lane: hl for hl in svc._lanes(s, heat_id)}


def status_of(svc, heat_id):
    with svc.session() as s:
        return s.get(Heat, heat_id)


async def armed_heat(svc, track_id):
    heat = await svc.stage(track_id, None, ACTOR)
    await svc.arm(track_id, ACTOR)
    return heat


def test_operator_starts_lanes_stop(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, events = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        r = await svc.register_start(g.id, heat.id, t0, "Track operator", ACTOR)
        assert r["updated"] and r["start_ms"] == t0
        await asyncio_settle()
        started = [e for e in events if e.type == "heat.started"]
        assert started and started[-1].data["start_ms"] == t0 and "track-%d" % g.id in started[-1].keys

        r = await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 5432)
        assert r["time"] == 5.432 and not r["waiting_for_start"]
        await svc.submit_lane(g.id, 2, heat.id, None, "s2", ACTOR, stop_ms=t0 + 6100)
        h = status_of(svc, heat.id)
        assert h.status == HeatStatus.pending and h.start_source == "Track operator"
        lanes = lanes_of(svc, heat.id)
        assert (lanes[1].final_time, lanes[1].place) == (5.432, 1)
        assert (lanes[2].final_time, lanes[2].place) == (6.1, 2)
        await svc.stop()

    run(body)


async def asyncio_settle():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


def test_stop_before_start_is_known(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        r = await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 4000)
        assert r["waiting_for_start"] and r["time"] is None
        await svc.submit_lane(g.id, 2, heat.id, None, "s2", ACTOR, stop_ms=t0 + 4500)
        assert status_of(svc, heat.id).status == HeatStatus.armed  # can't finish without a start
        with pytest.raises(ServiceError, match="already has a time"):
            await svc.submit_lane(g.id, 1, heat.id, None, "s3", ACTOR, stop_ms=t0 + 9999)

        await svc.register_start(g.id, heat.id, t0, "Track operator", ACTOR)
        assert status_of(svc, heat.id).status == HeatStatus.pending
        lanes = lanes_of(svc, heat.id)
        assert (lanes[1].final_time, lanes[2].final_time) == (4.0, 4.5)
        await svc.stop()

    run(body)


def test_earliest_start_wins_even_after_results(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        await svc.register_start(g.id, heat.id, t0, "Lane 2", ACTOR)
        later = await svc.register_start(g.id, heat.id, t0 + 300, "Track operator", ACTOR)
        assert not later["updated"] and later["start_ms"] == t0 and later["source"] == "Lane 2"

        await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 5000)
        await svc.submit_lane(g.id, 2, heat.id, None, "s2", ACTOR, stop_ms=t0 + 5100)
        assert status_of(svc, heat.id).status == HeatStatus.pending

        # the operator's press was actually 200 ms earlier but arrived late
        r = await svc.register_start(g.id, heat.id, t0 - 200, "Track operator", ACTOR)
        assert r["updated"]
        lanes = lanes_of(svc, heat.id)
        assert (lanes[1].final_time, lanes[2].final_time) == (5.2, 5.3)
        assert status_of(svc, heat.id).start_source == "Track operator"
        await svc.stop()

    run(body)


def test_lane_officials_own_start_counts(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        # offline lane official: their START and STOP arrive together later
        r = await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 3000, start_ms=t0)
        assert r["time"] == 3.0
        assert status_of(svc, heat.id).start_source == "Lane 1"
        await svc.stop()

    run(body)


def test_bad_starts_are_rejected(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await svc.stage(g.id, None, ACTOR)
        with pytest.raises(ServiceError, match="arm it"):
            await svc.register_start(g.id, heat.id, now_ms(), "Track operator", ACTOR)
        await svc.arm(g.id, ACTOR)
        with pytest.raises(ServiceError, match="future"):
            await svc.register_start(g.id, heat.id, now_ms() + 60_000, "Track operator", ACTOR)
        with pytest.raises(ServiceError, match="before the heat was armed"):
            await svc.register_start(g.id, heat.id, now_ms() - 600_000, "Track operator", ACTOR)
        t0 = now_ms()
        await svc.register_start(g.id, heat.id, t0, "Track operator", ACTOR)
        with pytest.raises(ServiceError, match="before the heat's start"):
            await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 - 1000)
        await svc.stop()

    run(body)


def test_disarm_clears_start_and_stops(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        await svc.register_start(g.id, heat.id, t0, "Track operator", ACTOR)
        await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 2000)
        await svc.unstage(g.id, ACTOR)
        h = status_of(svc, heat.id)
        assert h.start_ms is None and h.start_source is None
        assert lanes_of(svc, heat.id)[1].stop_ms is None
        await svc.stop()

    run(body)


# --- disarm / re-run resets ---------------------------------------------------------


def test_disarm_goes_back_to_staged_and_resets_phones(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, events = await make_service(settings, engine)
        heat = await armed_heat(svc, g.id)
        t0 = now_ms()
        await svc.register_start(g.id, heat.id, t0, "Track operator", ACTOR)
        await svc.submit_lane(g.id, 1, heat.id, None, "s1", ACTOR, stop_ms=t0 + 2000)

        await svc.disarm(g.id, ACTOR)
        await asyncio_settle()
        h = status_of(svc, heat.id)
        assert h.status == HeatStatus.staged and h.start_ms is None
        assert lanes_of(svc, heat.id)[1].stop_ms is None and lanes_of(svc, heat.id)[1].submission_id is None
        reset = [e for e in events if e.type == "heat.reset"][-1]
        assert reset.data["heat_id"] == heat.id and reset.data["new_heat_id"] is None

        # same heat can be armed and raced again; the lane can submit again
        await svc.arm(g.id, ACTOR)
        t1 = now_ms()
        await svc.register_start(g.id, heat.id, t1, "Track operator", ACTOR)
        r = await svc.submit_lane(g.id, 1, heat.id, None, "s1-again", ACTOR, stop_ms=t1 + 3000)
        assert r["time"] == 3.0
        with pytest.raises(ServiceError):
            await svc.disarm(g.id + 99, ACTOR)
        await svc.stop()

    run(body)


def test_rerun_only_after_the_start(settings, engine, session, regatta):
    g = tracks_of(session, regatta)[0]

    async def body():
        svc, events = await make_service(settings, engine)
        heat = await svc.stage(g.id, None, ACTOR)
        with pytest.raises(ServiceError, match="hasn't started"):
            await svc.rerun(heat.id, "False start", ACTOR)
        await svc.arm(g.id, ACTOR)
        with pytest.raises(ServiceError, match="hasn't started"):
            await svc.rerun(heat.id, "False start", ACTOR)

        await svc.register_start(g.id, heat.id, now_ms(), "Track operator", ACTOR)
        new = await svc.rerun(heat.id, "Boat tipped over or sank", ACTOR)
        await asyncio_settle()
        assert status_of(svc, heat.id).status == HeatStatus.rerun
        n = status_of(svc, new.id)
        assert n.status == HeatStatus.staged and n.start_ms is None and n.number == heat.number
        reset = [e for e in events if e.type == "heat.reset"][-1]
        assert reset.data["new_heat_id"] == new.id and "Boat tipped" in reset.data["reason"]
        await svc.stop()

    run(body)


def test_derby_rerun_allowed_once_armed(settings, engine, session):
    derby, _ = seed_demo(session, settings)
    session.commit()
    t = tracks_of(session, derby)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await svc.stage(t.id, None, ACTOR)
        await svc.arm(t.id, ACTOR)  # gate timer is running: the race may have started
        new = await svc.rerun(heat.id, "Car jumped into another lane", ACTOR)
        assert status_of(svc, new.id).status == HeatStatus.staged
        await svc.stop()

    run(body)


def test_timekeeper_rerun_when_track_has_moved_on(settings, engine, session):
    derby, _ = seed_demo(session, settings)
    session.commit()
    t = tracks_of(session, derby)[0]

    async def body():
        svc, _ = await make_service(settings, engine)
        first = await svc.stage(t.id, None, ACTOR)
        await svc.manual_entry(t.id, {1: 3.1, 2: 3.2, 3: 3.3, 4: 3.4}, ACTOR)  # pending
        nxt = await svc.stage(t.id, None, ACTOR)  # operator moved on
        new = await svc.rerun(first.id, "Timer didn't record", ACTOR)
        assert status_of(svc, new.id).status == HeatStatus.scheduled  # track busy: front of the queue
        await svc.unstage(t.id, ACTOR)
        from racemgr.services import views

        with svc.session() as s:
            assert views.plan_all(s)[t.id][0].id == new.id
        assert nxt.id != new.id
        await svc.stop()

    run(body)


# --- HTTP ---------------------------------------------------------------------------


@pytest.fixture
def web(settings, engine):
    with Session(engine, expire_on_commit=False) as s:
        seed_demo(s, settings)
        for ev in s.exec(select(Event)):
            ev.status = "active"
        s.commit()
    app = create_app(settings, engine, print_admin_link=False)
    with TestClient(app) as c:
        yield app, c


def join_as(app, role, track_id=None, lane=None):
    from racemgr import auth

    c = TestClient(app)
    with Session(app.state.engine) as s:
        jt = auth.create_join_token(s, role, 10, track_id, lane)
        s.commit()
        c.post(f"/join/{jt.token}")
    return c


def gutters(app):
    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == "regatta")).one()
        return s.exec(select(Track).where(Track.event_id == ev.id).order_by(Track.id)).all()


def test_server_time_endpoint(web):
    _, c = web
    t = c.get("/api/time").json()["now"]
    assert abs(t - now_ms()) < 2000


def test_operator_start_button_and_relay(web):
    app, _ = web
    ga, gb = gutters(app)
    op = join_as(app, Role.track_operator, ga.id)
    lane1 = join_as(app, Role.lane_official, ga.id, 1)
    other_lane = join_as(app, Role.lane_official, gb.id, 1)

    op.post(f"/api/track/{ga.id}/stage")
    assert "data-start-heat" not in op.get(f"/partials/operate/{ga.id}").text  # not armed yet
    op.post(f"/api/track/{ga.id}/arm")
    page = op.get(f"/partials/operate/{ga.id}").text
    assert "data-start-heat" in page and "clock.js" in op.get(f"/track/{ga.id}/operate").text

    with Session(app.state.engine) as s:
        heat = s.exec(select(Heat).where(Heat.track_id == ga.id, Heat.status == HeatStatus.armed)).one()
    t0 = now_ms() - 3000
    assert other_lane.post(f"/api/track/{ga.id}/start", json={"heat_id": heat.id, "start_ms": t0}).status_code == 403
    r = op.post(f"/api/track/{ga.id}/start", json={"heat_id": heat.id, "start_ms": t0})
    assert r.status_code == 200 and r.json()["source"] == "Track operator"

    # relayed to the lane official's page and shown running on the operator's
    info = lane1.get(f"/partials/lane/{ga.id}/1").text
    assert f'data-start="{t0}"' in info and 'data-start-source="Track operator"' in info
    assert "data-running-from" in op.get(f"/partials/operate/{ga.id}").text

    r = lane1.post(f"/api/track/{ga.id}/lane/1/time", json={"heat_id": heat.id, "stop_ms": t0 + 4321, "submission_id": "abcd1"})
    assert r.json()["time"] == 4.321
    assert "Waiting on lane 2" in op.get(f"/partials/operate/{ga.id}").text


@pytest.mark.parametrize("first", ["operator", "lane 1", "lane 2"])
def test_any_of_three_can_start_and_earliest_wins(web, first):
    app, _ = web
    ga, _ = gutters(app)
    phones = {
        "operator": join_as(app, Role.track_operator, ga.id),
        "lane 1": join_as(app, Role.lane_official, ga.id, 1),
        "lane 2": join_as(app, Role.lane_official, ga.id, 2),
    }
    label = {"operator": "Track operator", "lane 1": "Lane 1", "lane 2": "Lane 2"}
    op = phones["operator"]
    op.post(f"/api/track/{ga.id}/stage")
    op.post(f"/api/track/{ga.id}/arm")
    with Session(app.state.engine) as s:
        heat = s.exec(select(Heat).where(Heat.track_id == ga.id, Heat.status == HeatStatus.armed)).one()

    t0 = now_ms()
    r = phones[first].post(f"/api/track/{ga.id}/start", json={"heat_id": heat.id, "start_ms": t0})
    assert r.json()["updated"] and r.json()["source"] == label[first]
    # the operator's page shows who started it, with the clock running and no START button
    page = op.get(f"/partials/operate/{ga.id}").text
    assert f"Started by <strong>{label[first]}</strong>" in page
    assert "data-running-from" in page and "data-start-heat" not in page
    # everyone else's later presses are discarded
    for who, phone in phones.items():
        if who != first:
            r = phone.post(f"/api/track/{ga.id}/start", json={"heat_id": heat.id, "start_ms": t0 + 150})
            assert r.json() == {"ok": True, "start_ms": t0, "source": label[first], "updated": False}
    with Session(app.state.engine) as s:
        h = s.get(Heat, heat.id)
        assert (h.start_ms, h.start_source) == (t0, label[first])


def test_lane_official_can_start_their_track(web):
    app, _ = web
    ga, _ = gutters(app)
    op = join_as(app, Role.track_operator, ga.id)
    lane2 = join_as(app, Role.lane_official, ga.id, 2)
    op.post(f"/api/track/{ga.id}/stage")
    op.post(f"/api/track/{ga.id}/arm")
    with Session(app.state.engine) as s:
        heat = s.exec(select(Heat).where(Heat.track_id == ga.id, Heat.status == HeatStatus.armed)).one()
    r = lane2.post(f"/api/track/{ga.id}/start", json={"heat_id": heat.id, "start_ms": now_ms() - 1000})
    assert r.status_code == 200 and r.json()["source"] == "Lane 2"


def test_derby_operate_page_has_no_start_button(web):
    app, _ = web
    with Session(app.state.engine) as s:
        derby_track = s.exec(select(Track).join(Event).where(Event.type == "derby")).first()
    op = join_as(app, Role.track_operator, derby_track.id)
    op.post(f"/api/track/{derby_track.id}/stage")
    op.post(f"/api/track/{derby_track.id}/arm")
    assert "data-start-heat" not in op.get(f"/partials/operate/{derby_track.id}").text
