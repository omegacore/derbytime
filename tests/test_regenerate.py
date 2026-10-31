"""Regenerating the schedule mid-event: late check-ins, scratches, broken
lanes, extra tracks and new dens. Raced heats must never change."""
import asyncio
import sqlite3
from collections import Counter

import pytest
from sqlmodel import Session, select

from racemgr.db import init_db, make_engine
from racemgr.models import AuditLog, Entry, Heat, HeatLane, HeatStatus, Racer, Round, Track
from racemgr.seed import seed_demo
from racemgr.services import ServiceError
from racemgr.services.roster import add_entry, get_or_create_group, import_csv
from racemgr.services.schedule import generate_group_rounds, group_rounds, regenerate, schedule_problems
from racemgr.services.setup import add_track

from test_services import ACTOR, make_event_with_dens, make_service, run, tracks_of


async def race_heats(svc, track_id, count):
    """Stage, enter and approve the next ``count`` heats on a track."""
    for _ in range(count):
        heat = await svc.stage(track_id, None, ACTOR)
        with svc.session() as s:
            lanes = svc._lanes(s, heat.id)
        await svc.manual_entry(track_id, {l.lane: 3.0 + l.lane / 100 for l in lanes if l.entry_id}, ACTOR)
        await svc.approve(heat.id, ACTOR)


def race(settings, engine, track_id, count):
    async def body():
        svc, _ = await make_service(settings, engine)
        await race_heats(svc, track_id, count)
        await svc.stop()

    run(body)


def raced_snapshot(session, event):
    """{heat id: (status, track, ((lane, entry, time), ...))} for every heat that has raced."""
    out = {}
    for r in session.exec(select(Round).where(Round.event_id == event.id)):
        for h in session.exec(select(Heat).where(Heat.round_id == r.id, Heat.status != HeatStatus.scheduled)):
            lanes = session.exec(select(HeatLane).where(HeatLane.heat_id == h.id).order_by(HeatLane.lane)).all()
            out[h.id] = (h.status, h.track_id, tuple((l.lane, l.entry_id, l.final_time) for l in lanes))
    return out


def runs_per_entry(session, rnd):
    c = Counter()
    for h in session.exec(select(Heat).where(Heat.round_id == rnd.id, Heat.status != HeatStatus.rerun)):
        for hl in session.exec(select(HeatLane).where(HeatLane.heat_id == h.id)):
            if hl.entry_id:
                c[hl.entry_id] += 1
    return c


def unraced_heats(session, rnd):
    return session.exec(select(Heat).where(Heat.round_id == rnd.id, Heat.status == HeatStatus.scheduled)).all()


def check_in_new_scout(session, ev, den):
    racer = Racer(first_name="Late", last_name="Arrival", group_id=get_or_create_group(session, den).id)
    session.add(racer)
    session.flush()
    return add_entry(session, ev, racer, checked_in=True)


def test_late_scout_joins_mid_event_and_results_stay(settings, engine, session):
    ev = make_event_with_dens(session, settings, ["Den 1", "Den 2"], per_den=6)
    generate_group_rounds(session, ev, ACTOR, passes=2)
    session.commit()
    race(settings, engine, tracks_of(session, ev)[0].id, 3)
    before = raced_snapshot(session, ev)
    assert len(before) == 3

    late = check_in_new_scout(session, ev, "Den 1")
    session.commit()
    assert schedule_problems(session, ev) == {"missing": [f"#{late.number} Late Arrival"]}

    result = regenerate(session, ev, ACTOR)
    session.commit()
    assert raced_snapshot(session, ev) == before
    assert result["kept"] == 3
    assert schedule_problems(session, ev) == {}
    for rnd in group_rounds(session, ev.id):
        counts = runs_per_entry(session, rnd)
        assert set(counts.values()) == {4}, rnd.name  # everyone runs every lane once
        assert (late.id in counts) == rnd.name.startswith("Den 1")


def test_scratched_scout_loses_unraced_heats(settings, session):
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=5)
    generate_group_rounds(session, ev, ACTOR, passes=1)
    entry = session.exec(select(Entry).where(Entry.event_id == ev.id).order_by(Entry.number)).first()
    entry.checked_in = False
    session.add(entry)
    session.commit()
    assert "scratched" in schedule_problems(session, ev)
    regenerate(session, ev, ACTOR)
    session.commit()
    (rnd,) = group_rounds(session, ev.id)
    assert entry.id not in runs_per_entry(session, rnd)
    assert schedule_problems(session, ev) == {}


def test_lane_breaks_mid_run(settings, engine, session):
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=8)
    generate_group_rounds(session, ev, ACTOR, passes=1)
    session.commit()
    track = tracks_of(session, ev)[0]
    race(settings, engine, track.id, 3)
    before = raced_snapshot(session, ev)

    track = session.get(Track, track.id)
    track.disabled_lanes = [2]
    session.add(track)
    session.commit()
    regenerate(session, ev, ACTOR, reason="lane 2 broke")
    session.commit()

    assert raced_snapshot(session, ev) == before
    (rnd,) = group_rounds(session, ev.id)
    lanes_used = {hl.lane for h in unraced_heats(session, rnd) for hl in session.exec(select(HeatLane).where(HeatLane.heat_id == h.id))}
    assert 2 not in lanes_used
    # the run started at 4 runs per car, so every car still gets exactly 4
    assert set(runs_per_entry(session, rnd).values()) == {4}
    log = session.exec(select(AuditLog).where(AuditLog.action == "schedule.regenerate")).one()
    assert log.reason == "lane 2 broke"


def test_runs_not_yet_started_shrink_to_working_lanes(settings, session):
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=6)
    generate_group_rounds(session, ev, ACTOR, passes=1)
    track = tracks_of(session, ev)[0]
    track.disabled_lanes = [4]
    session.add(track)
    regenerate(session, ev, ACTOR)
    (rnd,) = group_rounds(session, ev.id)
    assert set(runs_per_entry(session, rnd).values()) == {3}  # each working lane once


def test_inflating_another_gutter_spreads_remaining_heats(settings, engine, session):
    _, regatta = seed_demo(session, settings)
    gutter_a = tracks_of(session, regatta)[0]
    race(settings, engine, gutter_a.id, 2)
    before = raced_snapshot(session, regatta)

    gutter_c = add_track(session, settings, regatta, "Gutter C", ACTOR)
    session.commit()
    regenerate(session, regatta, ACTOR)
    session.commit()

    assert raced_snapshot(session, regatta) == before
    for rnd in group_rounds(session, regatta.id):
        assert gutter_c.id in {h.track_id for h in unraced_heats(session, rnd)}, rnd.name
        assert set(runs_per_entry(session, rnd).values()) == {2}, rnd.name


def test_new_den_gets_runs_at_the_end(settings, session):
    ev = make_event_with_dens(session, settings, ["Den 1", "Den 2"], per_den=4)
    generate_group_rounds(session, ev, ACTOR, passes=2)
    import_csv(session, ev, "first_name,last_name,den\nNew,Kid,Den 3\nOther,Kid,Den 3\n", ACTOR, checked_in=True)
    assert regenerate(session, ev, ACTOR)["new_dens"] == 1
    assert [r.name for r in group_rounds(session, ev.id)][-2:] == ["Den 3", "Den 3 · run 2"]


def test_regenerate_needs_a_schedule(settings, session):
    ev = make_event_with_dens(session, settings, ["Den 1"])
    with pytest.raises(ServiceError, match="Generate"):
        regenerate(session, ev, ACTOR)


def test_all_lanes_out_is_an_error(settings, session):
    ev = make_event_with_dens(session, settings, ["Den 1"])
    generate_group_rounds(session, ev, ACTOR, passes=1)
    track = tracks_of(session, ev)[0]
    track.disabled_lanes = [1, 2, 3, 4]
    session.add(track)
    with pytest.raises(ServiceError, match="working lane"):
        regenerate(session, ev, ACTOR)


def test_older_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE track (id INTEGER PRIMARY KEY, event_id INTEGER, name VARCHAR, lane_count INTEGER,"
        " timing_source VARCHAR, timer_settings JSON, min_time FLOAT, max_time FLOAT, active BOOLEAN)"
    )
    con.execute("INSERT INTO track VALUES (1, 1, 'Main', 4, 'simulator', '{}', 2, 6, 1)")
    con.commit()
    con.close()
    engine = make_engine(db)
    init_db(engine)
    with Session(engine) as s:
        t = s.get(Track, 1)
        assert t.disabled_lanes is None and t.usable_lanes == [1, 2, 3, 4]
