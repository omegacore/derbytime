"""Races per scout in a run (1 to N), and the half-empty heat an odd den
leaves behind: its empty lane isn't timed, so one official's time ends it."""
import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from racemgr.app import create_app
from racemgr.models import EventType, Heat, HeatLane, HeatStatus, Role
from racemgr.services import ServiceError
from racemgr.services.roster import import_csv
from racemgr.services.schedule import add_pass, generate_group_rounds, group_rounds, regenerate
from racemgr.services.setup import add_track, create_event

from test_regenerate import check_in_new_scout, runs_per_entry
from test_shared_start import join_as
from test_services import ACTOR, make_service, run, tracks_of


def make_regatta(session, settings, per_den=5, gutters=2):
    ev = create_event(session, settings, "Regatta", EventType.regatta, ACTOR)
    for i in range(gutters):
        add_track(session, settings, ev, f"Gutter {i + 1}", ACTOR)
    rows = "\n".join(f"Kid{i},X,Den 1" for i in range(per_den))
    import_csv(session, ev, "first_name,last_name,den\n" + rows, ACTOR, checked_in=True)
    return ev


def heats_of(session, rnd):
    heats = session.exec(select(Heat).where(Heat.round_id == rnd.id).order_by(Heat.seq)).all()
    return [(h, session.exec(select(HeatLane).where(HeatLane.heat_id == h.id).order_by(HeatLane.lane)).all()) for h in heats]


def test_one_race_each_in_an_odd_den_leaves_one_empty_lane(session, settings):
    ev = make_regatta(session, settings, per_den=5)
    generate_group_rounds(session, ev, ACTOR, passes=2, races=1)
    for rnd in group_rounds(session, ev.id):
        assert rnd.races == 1
        assert set(runs_per_entry(session, rnd).values()) == {1}
        filled = sorted(sum(1 for hl in lanes if hl.entry_id) for _, lanes in heats_of(session, rnd))
        assert filled == [1, 2, 2]


def test_races_per_scout_counts_and_lane_balance(session, settings):
    ev = make_regatta(session, settings, per_den=6)
    generate_group_rounds(session, ev, ACTOR, passes=1, races=3)
    (rnd,) = group_rounds(session, ev.id)
    assert set(runs_per_entry(session, rnd).values()) == {3}
    lanes_used: dict[int, Counter] = {}
    for _, lanes in heats_of(session, rnd):
        for hl in lanes:
            if hl.entry_id:
                lanes_used.setdefault(hl.entry_id, Counter())[hl.lane] += 1
    # 3 races on 2 lanes: each lane at least once, never three times
    assert all(sorted(c.values()) == [1, 2] for c in lanes_used.values())


def test_one_per_lane_is_the_default(session, settings):
    ev = make_regatta(session, settings, per_den=4)
    generate_group_rounds(session, ev, ACTOR, passes=1, races=2)  # = lane count
    (rnd,) = group_rounds(session, ev.id)
    assert rnd.races is None
    assert set(runs_per_entry(session, rnd).values()) == {2}


@pytest.mark.parametrize("races", [0, 13])
def test_races_out_of_range(session, settings, races):
    ev = make_regatta(session, settings)
    with pytest.raises(ServiceError):
        generate_group_rounds(session, ev, ACTOR, passes=1, races=races)


def test_regenerate_and_add_run_keep_races_per_scout(session, settings):
    ev = make_regatta(session, settings, per_den=5)
    generate_group_rounds(session, ev, ACTOR, passes=1, races=1)
    late = check_in_new_scout(session, ev, "Den 1")
    regenerate(session, ev, ACTOR)
    (rnd,) = group_rounds(session, ev.id)
    counts = runs_per_entry(session, rnd)
    assert counts[late.id] == 1 and set(counts.values()) == {1}

    new = add_pass(session, ev, rnd.group_id, ACTOR)
    assert new.races == 1 and set(runs_per_entry(session, new).values()) == {1}


@pytest.mark.parametrize("shared_start", [False, True])
def test_one_official_ends_the_half_empty_heat(settings, engine, session, shared_start):
    ev = make_regatta(session, settings, per_den=5)
    generate_group_rounds(session, ev, ACTOR, passes=1, races=1)
    (rnd,) = group_rounds(session, ev.id)
    solo, lanes = next((h, l) for h, l in heats_of(session, rnd) if sum(1 for hl in l if hl.entry_id) == 1)
    lane = next(hl.lane for hl in lanes if hl.entry_id)
    empty = next(hl.lane for hl in lanes if not hl.entry_id)
    track_id = solo.track_id
    session.commit()

    async def body():
        svc, _ = await make_service(settings, engine)
        await svc.stage(track_id, solo.id, ACTOR)
        await svc.arm(track_id, ACTOR)
        with pytest.raises(ServiceError):  # nothing to time in the empty lane
            await svc.submit_lane(track_id, empty, solo.id, 5.0, "x", ACTOR)
        if shared_start:
            t0 = time.time() * 1000
            await svc.register_start(track_id, solo.id, t0, "Track operator", ACTOR)
            await svc.submit_lane(track_id, lane, solo.id, None, "a", ACTOR, stop_ms=t0 + 8250)
        else:
            await svc.submit_lane(track_id, lane, solo.id, 8.25, "a", ACTOR)
        with svc.session() as s:
            assert s.get(Heat, solo.id).status == HeatStatus.pending
            hl = s.exec(select(HeatLane).where(HeatLane.heat_id == solo.id, HeatLane.lane == lane)).one()
            assert hl.final_time == 8.25 and hl.place == 1
        await svc.approve(solo.id, ACTOR)
        with svc.session() as s:
            assert s.get(Heat, solo.id).status == HeatStatus.approved
        await svc.stop()

    run(body)


def test_generate_form_takes_races_per_scout(settings, engine, session):
    ev = make_regatta(session, settings, per_den=5)
    session.commit()
    app = create_app(settings, engine, print_admin_link=False)
    with TestClient(app):
        c = join_as(app, Role.admin)
        page = c.get(f"/admin/event/{ev.id}")
        assert 'name="races"' in page.text
        c.post(f"/admin/event/{ev.id}/schedule", data={"passes": "1", "races": "1"})
        page = c.get(f"/admin/event/{ev.id}")
        assert "1 race each" in page.text
    session.expire_all()
    (rnd,) = group_rounds(session, ev.id)
    assert rnd.races == 1
