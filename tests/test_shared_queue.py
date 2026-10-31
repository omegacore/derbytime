"""One shared queue of den heats for all of an event's tracks: whichever
track is free races the next heat, so a slow track never holds the others up."""
from sqlmodel import select

from racemgr.models import AuditLog, Heat, HeatLane, HeatStatus, RoundKind
from racemgr.services import views
from racemgr.services.schedule import create_final, generate_group_rounds
from racemgr.services.setup import add_track

from test_regenerate import race_heats
from test_services import ACTOR, make_event_with_dens, make_service, run, tracks_of


def two_track_event(session, settings, per_den=8, lanes_b=4):
    ev = make_event_with_dens(session, settings, ["Den 1", "Den 2"], per_den=per_den)
    add_track(session, settings, ev, "T2", ACTOR, lane_count=lanes_b)
    tracks = tracks_of(session, ev)
    assert len(tracks) == 2
    generate_group_rounds(session, ev, ACTOR, passes=1)
    session.commit()
    return ev, tracks


def test_free_track_keeps_racing_while_other_is_stuck(settings, engine, session):
    ev, (a, b) = two_track_event(session, settings)

    async def body():
        svc, _ = await make_service(settings, engine)
        stuck = await svc.stage(a.id, None, ACTOR)  # track A never finishes
        with svc.session() as s:
            queue = [h.id for h, _ in views.scheduled_heats(s, [a, b])]
            rbh = views.racers_by_heat(s, [stuck.id, *queue])
            first_planned_for = {h.id: h.track_id for h, _ in views.scheduled_heats(s, [a, b])}
        # B races 5 heats in a row, in queue order, skipping only heats with
        # a scout who is on track A right now
        await race_heats(svc, b.id, 5)
        expected = [h for h in queue if not rbh[h] & rbh[stuck.id]][:5]
        with svc.session() as s:
            raced = s.exec(select(Heat).where(Heat.track_id == b.id, Heat.status == HeatStatus.approved).order_by(Heat.approved_at)).all()
            assert [h.id for h in raced] == expected
            assert any(first_planned_for[h] == a.id for h in expected)  # took over A's share
        await svc.stop()

    run(body)


def test_shared_queue_runs_in_race_order(settings, engine, session):
    ev, (a, b) = two_track_event(session, settings)

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            queue = [h.id for h, _ in views.scheduled_heats(s, [a, b])]
        order = []
        for track in (a, b, b, a, b):
            heat = await svc.stage(track.id, None, ACTOR)
            order.append(heat.id)
            with svc.session() as s:
                lanes = svc._lanes(s, heat.id)
            await svc.manual_entry(track.id, {l.lane: 3.0 for l in lanes if l.entry_id}, ACTOR)
            await svc.approve(heat.id, ACTOR)
        # no scout races twice at once here, so each stage takes the front of the queue
        assert order == queue[:5]
        await svc.stop()

    run(body)


def test_heat_moves_to_the_track_that_races_it(settings, engine, session):
    ev, (a, b) = two_track_event(session, settings)
    first_for_a = views.plan_all(session)[a.id][0]
    assert first_for_a.track_id == a.id

    async def body():
        svc, _ = await make_service(settings, engine)
        heat = await svc.stage(b.id, None, ACTOR)  # B asks first and gets the front heat
        assert heat.id == first_for_a.id
        with svc.session() as s:
            assert s.get(Heat, heat.id).track_id == b.id
            entry = s.exec(select(AuditLog).where(AuditLog.action == "heat.stage", AuditLog.entity_id == heat.id)).one()
            assert entry.after == {"track": b.id, "from_track": a.id}
        # unstaged, it goes back to the shared queue and A can take it
        await svc.unstage(b.id, ACTOR)
        again = await svc.stage(a.id, None, ACTOR)
        assert again.id == heat.id
        await svc.stop()

    run(body)


def test_heat_fits_track_lanes(settings, engine, session):
    ev, (a, b) = two_track_event(session, settings, lanes_b=3)
    sched = views.scheduled_heats(session, [a, b])
    four_lane = {h.id for h, fit in sched if [t.id for t in fit] == [a.id]}
    three_lane = [h.id for h, fit in sched if len(fit) == 2]
    assert four_lane and three_lane  # heats using lane 4 can only go to A

    async def body():
        svc, _ = await make_service(settings, engine)
        for _ in range(3):
            heat = await svc.stage(b.id, None, ACTOR)
            assert heat.id not in four_lane
            with svc.session() as s:
                lanes = svc._lanes(s, heat.id)
                assert [l.lane for l in lanes] == [1, 2, 3]
            await svc.manual_entry(b.id, {l.lane: 3.0 for l in lanes if l.entry_id}, ACTOR)
            await svc.approve(heat.id, ACTOR)
        # A can race a 3-lane heat too; lane 4 is left empty
        with svc.session() as s:
            nxt = next(h for h, fit in views.scheduled_heats(s, [a, b]) if len(fit) == 2)
        heat = await svc.stage(a.id, nxt.id, ACTOR)
        with svc.session() as s:
            lanes = svc._lanes(s, heat.id)
            assert [l.lane for l in lanes] == [1, 2, 3, 4]
            assert lanes[3].entry_id is None
        await svc.stop()

    run(body)


def test_finals_stay_on_their_track(settings, engine, session):
    ev, (a, b) = two_track_event(session, settings, per_den=4)
    ev.finalists_per_group = 4  # every scout races: no cutoff ties to settle
    session.add(ev)
    session.commit()

    async def body():
        svc, _ = await make_service(settings, engine)
        while True:
            with svc.session() as s:
                plan = views.plan_all(s)
            track = next((t for t in (a, b) if plan[t.id]), None)
            if not track:
                break
            await race_heats(svc, track.id, 1)
        await svc.stop()

    run(body)
    session.expire_all()
    final = create_final(session, ev, a.id, ACTOR)
    session.commit()
    plan = views.plan_all(session)
    assert plan[b.id] == []
    assert {h.round_id for h in plan[a.id]} == {final.id}
    assert final.kind == RoundKind.final


def test_queued_den_heats_show_next_free_track(settings, session):
    ev, (a, b) = two_track_event(session, settings)
    heat = views.plan_all(session)[b.id][0]
    assert views.heat_view(session, heat)["track_name"] == "Next free track"
    # the forecast for a track names that track
    (hv,) = views.heat_views(session, [heat], track_id=b.id)
    assert hv["track_name"] == b.name
    # with one track there's nothing to share
    b.active = False
    session.add(b)
    session.commit()
    assert views.heat_view(session, heat)["track_name"] != "Next free track"
