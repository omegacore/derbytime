import asyncio
from collections import Counter

import pytest
from sqlmodel import Session, select

from derbytime.bus import EventBus
from derbytime.models import AuditLog, Entry, Event, EventType, Group, Heat, HeatLane, HeatStatus, Racer, Round, RoundKind, Track
from derbytime.seed import seed_demo
from derbytime.services import Actor, ServiceError, views
from derbytime.services.race import RaceService
from derbytime.services.roster import import_csv
from derbytime.services.schedule import (
    add_pass,
    clear_schedule,
    create_final,
    create_runoff,
    event_standings,
    generate_group_rounds,
    group_rounds,
    projected_finish,
    race_order,
    remove_pass,
    set_race_order,
)
from derbytime.services.setup import add_track, create_event

ACTOR = Actor(None, "test")


def run(coro_fn):
    """Run an async test body with a bus bound to the loop."""
    return asyncio.run(coro_fn())


@pytest.fixture
def demo(session, settings):
    derby, regatta = seed_demo(session, settings)
    return derby, regatta


def tracks_of(session, event):
    return list(session.exec(select(Track).where(Track.event_id == event.id).order_by(Track.id)))


async def make_service(settings, engine):
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    events = []

    async def collect(ev):
        events.append(ev)

    bus.subscribe(collect)
    svc = RaceService(settings, engine, bus)
    await svc.sync_timers()
    return svc, events


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- scheduling ---------------------------------------------------------------


def test_seed_builds_balanced_schedules(session, demo):
    derby, regatta = demo
    (main,) = tracks_of(session, derby)
    rounds = group_rounds(session, derby.id)
    # two runs per den, snaking down the (name-sorted) dens and back up
    assert [r.name for r in rounds] == ["Bear Den 5", "Wolf Den 3", "Wolf Den 3 · run 2", "Bear Den 5 · run 2"]
    heats = session.exec(select(Heat).where(Heat.track_id == main.id)).all()
    assert len(heats) == 40  # 10 entries per den, one heat each per run, 2 runs, 2 dens
    lanes = session.exec(select(HeatLane).join(Heat).where(Heat.track_id == main.id)).all()
    per_entry_lane = Counter((hl.entry_id, hl.lane) for hl in lanes)
    assert set(per_entry_lane.values()) == {2}  # every lane exactly twice

    # regatta: every run of every den is raced on both gutters at once
    ga, gb = tracks_of(session, regatta)
    for r in group_rounds(session, regatta.id):
        heats = session.exec(select(Heat).where(Heat.round_id == r.id)).all()
        per_gutter = Counter(h.track_id for h in heats)
        assert set(per_gutter) == {ga.id, gb.id}, r.name
        assert abs(per_gutter[ga.id] - per_gutter[gb.id]) <= 1


def test_generate_twice_and_clear(session, settings, demo):
    derby, _ = demo
    with pytest.raises(ServiceError):
        generate_group_rounds(session, derby, ACTOR)
    clear_schedule(session, derby, ACTOR)
    session.commit()
    assert not session.exec(select(Round).where(Round.event_id == derby.id)).first()
    generate_group_rounds(session, derby, ACTOR)


def make_event_with_dens(session, settings, dens, per_den=4, lanes=4):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    add_track(session, settings, ev, "T1", ACTOR, lane_count=lanes)
    rows = "\n".join(f"Kid{d}{i},X,{d}" for d in dens for i in range(per_den))
    import_csv(session, ev, "first_name,last_name,den\n" + rows, ACTOR, checked_in=True)
    return ev


def test_generate_snakes_through_dens_in_natural_order(session, settings):
    ev = make_event_with_dens(session, settings, ["Den 10", "Den 2", "Den 9"])
    generate_group_rounds(session, ev, ACTOR, passes=3)
    names = [r.name for r in group_rounds(session, ev.id)]
    assert names == [
        "Den 2", "Den 9", "Den 10",
        "Den 10 · run 2", "Den 9 · run 2", "Den 2 · run 2",
        "Den 2 · run 3", "Den 9 · run 3", "Den 10 · run 3",
    ]


def test_second_run_meets_different_opponents(session, settings):
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=8)
    generate_group_rounds(session, ev, ACTOR, passes=2)
    r1, r2 = group_rounds(session, ev.id)

    def lineups(rnd):
        heats = session.exec(select(Heat).where(Heat.round_id == rnd.id)).all()
        return {frozenset(hl.entry_id for hl in session.exec(select(HeatLane).where(HeatLane.heat_id == h.id))) for h in heats}

    assert lineups(r1) != lineups(r2)


def test_reorder_add_and_remove_runs(session, settings):
    ev = make_event_with_dens(session, settings, ["Den 1", "Den 2"])
    generate_group_rounds(session, ev, ACTOR, passes=2)
    session.commit()
    rounds = group_rounds(session, ev.id)
    assert [r.name for r in rounds] == ["Den 1", "Den 2", "Den 2 · run 2", "Den 1 · run 2"]

    # move "Den 1 · run 2" to the front: the track's next heat comes from it
    new = [rounds[3].id, rounds[0].id, rounds[1].id, rounds[2].id]
    set_race_order(session, ev, new, ACTOR)
    session.commit()
    assert [r.id for r in group_rounds(session, ev.id)] == new
    track = tracks_of(session, ev)[0]
    assert views.plan_all(session)[track.id][0].round_id == rounds[3].id
    with pytest.raises(ServiceError):
        set_race_order(session, ev, new[:-1], ACTOR)  # stale list

    added = add_pass(session, ev, rounds[1].group_id, ACTOR)
    session.commit()
    assert added.name == "Den 2 · run 3" and group_rounds(session, ev.id)[-1].id == added.id

    remove_pass(session, rounds[2], ACTOR)  # "Den 2 · run 2"
    session.commit()
    assert [r.name for r in group_rounds(session, ev.id)] == ["Den 1 · run 2", "Den 1", "Den 2", "Den 2 · run 2"]
    assert session.get(Round, added.id).name == "Den 2 · run 2"  # renumbered


def test_started_run_cannot_be_removed(settings, engine, session):
    ev = make_event_with_dens(session, settings, ["Den 1"])
    generate_group_rounds(session, ev, ACTOR, passes=2)
    session.commit()

    async def body():
        svc, _ = await make_service(settings, engine)
        await svc.stage(tracks_of(session, ev)[0].id, None, ACTOR)
        await svc.stop()

    run(body)
    first = group_rounds(session, ev.id)[0]
    with pytest.raises(ServiceError, match="already started"):
        remove_pass(session, first, ACTOR)
    assert [o["removable"] for o in race_order(session, ev)] == [False, True]


def test_roster_import(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    csv = "First Name,Last Name,Den,Car Number,Car\nSam,Lee,Tiger Den 1,7,Zippy\nJo,Ray,Tiger Den 1,,\n,,,,\n"
    assert import_csv(session, ev, csv, ACTOR, checked_in=True).imported == 2
    entries = session.exec(select(Entry).where(Entry.event_id == ev.id).order_by(Entry.number)).all()
    assert [e.number for e in entries] == [7, 8]
    assert entries[0].name == "Zippy" and entries[0].checked_in
    with pytest.raises(ServiceError):
        import_csv(session, ev, "first_name,number\nAl,x\n", ACTOR)


PACK_HEADER = [
    "Scout First Name", "Scout Last Name", "Member ID", "Status", "School", "Class Year", "Grade", "Gender",
    "Parent 1 Name", "Parent 1 Email", "Parent 1 Phone", "Parent 1 Address", "Parent 2 Name", "Parent 2 Email",
    "Parent 2 Phone", "Scout DOB", "Den", "Notes", "Pack Dues", "National Dues", "T-Shirt Sep 2026",
]


def pack_row(first, last, status, den):
    values = dict.fromkeys(PACK_HEADER, "x")
    values.update({"Scout First Name": first, "Scout Last Name": last, "Status": status, "Den": den,
                   "Parent 1 Email": "parent@example.com", "Parent 1 Address": "1 Main St, Town", "Notes": "allergy, peanuts"})
    return [values[h] for h in PACK_HEADER]


@pytest.mark.parametrize("delimiter", ["\t", ","])
def test_pack_roster_export(session, settings, delimiter):
    import csv as csvmod
    import io

    rows = [
        pack_row("Liam", "Garcia", "Active", "Den 3"),
        pack_row("Ava", "Brown", "active ", "Den  5"),  # case, stray spaces
        pack_row("Old", "Timer", "Inactive", "Den 3"),
        pack_row("Moved", "Away", "", "Den 5"),
    ]
    buf = io.StringIO()
    w = csvmod.writer(buf, delimiter=delimiter)
    w.writerow(PACK_HEADER)
    w.writerows(rows)

    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    result = import_csv(session, ev, "﻿" + buf.getvalue(), ACTOR, checked_in=True)
    assert (result.imported, result.skipped_inactive) == (2, 2)
    assert result.message == "Imported 2 scouts. Skipped 2 not marked Active."
    racers = session.exec(select(Racer).order_by(Racer.first_name)).all()
    assert [(r.first_name, r.last_name) for r in racers] == [("Ava", "Brown"), ("Liam", "Garcia")]
    groups = {g.name for g in session.exec(select(Group))}
    assert groups == {"Den 3", "Den 5"}  # "Den  5" normalised
    # nothing from the ignored columns ends up in the database
    dump = repr([r.model_dump() for r in racers]) + repr([e.model_dump() for e in session.exec(select(Entry))])
    assert "example.com" not in dump and "Main St" not in dump and "peanuts" not in dump


def test_roster_without_status_column_imports_everyone(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    result = import_csv(session, ev, "Scout First Name\tScout Last Name\tDen\nA\tB\tDen 1\nC\tD\tDen 1\n", ACTOR)
    assert (result.imported, result.skipped_inactive) == (2, 0)
    assert result.message == "Imported 2 scouts."


def test_roster_missing_first_name_column(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    with pytest.raises(ServiceError, match="Scout First Name"):
        import_csv(session, ev, "Name,Den\nSam,Den 1\n", ACTOR)


# --- heat lifecycle -----------------------------------------------------------


def test_simulator_heat_lifecycle(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, events = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
        heat = await svc.stage(track.id, None, ACTOR)
        with pytest.raises(ServiceError):
            await svc.stage(track.id, None, ACTOR)  # one heat at a time
        await svc.arm(track.id, ACTOR)
        for _ in range(100):
            await asyncio.sleep(0.01)
            with svc.session() as s:
                if s.get(Heat, heat.id).status == HeatStatus.pending:
                    break
        with svc.session() as s:
            h = s.get(Heat, heat.id)
            assert h.status == HeatStatus.pending
            lanes = s.exec(select(HeatLane).where(HeatLane.heat_id == h.id)).all()
            assert all(l.final_time and l.source == "simulator" and l.place for l in lanes)
        await svc.approve(heat.id, ACTOR)
        with pytest.raises(ServiceError):
            await svc.approve(heat.id, ACTOR)
        await settle()
        types = [e.type for e in events]
        for t in ("heat.staged", "heat.armed", "heat.pending", "heat.approved", "track.state", "alert"):
            assert t in types
        kinds = {e.data["kind"] for e in events if e.type == "alert"}
        assert {"racing", "on_deck", "two_away", "results"} <= kinds
        with svc.session() as s:
            actions = [a.action for a in s.exec(select(AuditLog).where(AuditLog.entity_type == "heat", AuditLog.entity_id == heat.id))]
            assert actions == ["heat.stage", "heat.arm", "heat.results", "heat.approve"]
        await svc.stop()

    run(body)


def test_phone_stopwatch_submissions(settings, engine, demo):
    _, regatta = demo

    async def body():
        svc, events = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, regatta)[0]
        heat = await svc.stage(track.id, None, ACTOR)
        with pytest.raises(ServiceError):  # not armed yet
            await svc.submit_lane(track.id, 1, heat.id, 5.0, "a", ACTOR)
        await svc.arm(track.id, ACTOR)
        assert (await svc.submit_lane(track.id, 1, heat.id, 5.123, "a", ACTOR))["ok"] is True
        # retry of the same submission is a no-op
        assert (await svc.submit_lane(track.id, 1, heat.id, 5.123, "a", ACTOR))["duplicate"]
        with pytest.raises(ServiceError):  # different submission for a filled lane
            await svc.submit_lane(track.id, 1, heat.id, 6.0, "b", ACTOR)
        with svc.session() as s:
            assert s.get(Heat, heat.id).status == HeatStatus.armed
        await svc.submit_lane(track.id, 2, heat.id, None, "c", ACTOR)  # DNF
        with svc.session() as s:
            h = s.get(Heat, heat.id)
            assert h.status == HeatStatus.pending
            lanes = {l.lane: l for l in s.exec(select(HeatLane).where(HeatLane.heat_id == h.id))}
            assert lanes[1].final_time == 5.123 and lanes[1].place == 1
            assert lanes[2].dnf and lanes[2].place == 2
        with pytest.raises(ServiceError):  # late queued submission for a closed heat
            await svc.submit_lane(track.id, 2, heat.id, 7.0, "d", ACTOR)
        await svc.stop()

    run(body)


def test_phone_resumes_after_restart(settings, engine, demo):
    _, regatta = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, regatta)[0]
        heat = await svc.stage(track.id, None, ACTOR)
        await svc.arm(track.id, ACTOR)
        await svc.submit_lane(track.id, 1, heat.id, 5.0, "a", ACTOR)
        await svc.stop()
        svc2, _ = await make_service(settings, engine)  # "restart"
        await svc2.submit_lane(track.id, 2, heat.id, 6.0, "b", ACTOR)
        with svc2.session() as s:
            assert s.get(Heat, heat.id).status == HeatStatus.pending
        await svc2.stop()

    run(body)


def test_manual_entry_edit_and_rerun(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
        heat = await svc.stage(track.id, None, ACTOR)
        await svc.manual_entry(track.id, {1: 3.1, 2: 3.2, 3: 3.3, 4: 3.4}, ACTOR)
        with pytest.raises(ServiceError):
            await svc.edit_lane(heat.id, 4, 3.0, "  ", ACTOR)
        await svc.edit_lane(heat.id, 4, 3.0, "timer glitch", ACTOR)
        with svc.session() as s:
            lanes = {l.lane: l for l in s.exec(select(HeatLane).where(HeatLane.heat_id == heat.id))}
            assert lanes[4].place == 1 and lanes[4].raw_time == 3.4 and lanes[4].edit_reason == "timer glitch"
            assert lanes[1].place == 2
            log = s.exec(select(AuditLog).where(AuditLog.action == "lane.edit")).one()
            assert log.before["final_time"] == 3.4 and log.reason == "timer glitch"

        new = await svc.rerun(heat.id, "car jumped the track", ACTOR)
        with svc.session() as s:
            assert s.get(Heat, heat.id).status == HeatStatus.rerun
            assert new.rerun_of_id == heat.id and new.number == heat.number
            # the track was free, so the same lineup goes straight back on it, staged
            assert s.get(Heat, new.id).status == HeatStatus.staged
            assert svc.current_heat(s, track.id).id == new.id
            assert [hl.entry_id for hl in svc._lanes(s, new.id)] == [hl.entry_id for hl in svc._lanes(s, heat.id)]
        await svc.stop()

    run(body)


def test_outlier_flagged(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
        await svc.stage(track.id, None, ACTOR)
        await svc.manual_entry(track.id, {1: 3.1, 2: 0.5, 3: 3.3, 4: None}, ACTOR)
        (pending,) = svc.pending_heats()
        assert pending["flagged"]
        assert [l["outlier"] for l in pending["lanes"]] == [False, True, False, False]
        await svc.stop()

    run(body)


def test_racer_cannot_be_on_two_tracks(settings, engine, demo):
    derby, regatta = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            main = tracks_of(s, derby)[0]
            gutters = tracks_of(s, regatta)
        heat = await svc.stage(main.id, None, ACTOR)
        with svc.session() as s:
            busy = views.racers_by_heat(s, [heat.id])[heat.id]
            # every regatta track plans a heat without those racers next
            for g in gutters:
                nxt = views.plan_all(s)[g.id][0]
                assert not (views.racers_by_heat(s, [nxt.id])[nxt.id] & busy)
            # and explicitly staging a clashing heat is refused
            clash, fit = next(
                (h, fit)
                for h, fit in views.scheduled_heats(s, gutters)
                if views.racers_by_heat(s, [h.id])[h.id] & busy
            )
        with pytest.raises(ServiceError, match="another track"):
            await svc.stage(fit[0].id, clash.id, ACTOR)
        await svc.stop()

    run(body)


# --- standings, runoffs and finals --------------------------------------------


async def race_all(svc, track_id, time_for):
    """Stage/enter/approve every heat on a track. time_for(entry_id) -> seconds."""
    while True:
        with svc.session() as s:
            if not views.plan_all(s).get(track_id):
                return
        heat = await svc.stage(track_id, None, ACTOR)
        with svc.session() as s:
            lanes = svc._lanes(s, heat.id)
        await svc.manual_entry(track_id, {l.lane: time_for(l.entry_id) for l in lanes if l.entry_id}, ACTOR)
        await svc.approve(heat.id, ACTOR)


def test_standings_final_and_runoff(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
            entries = s.exec(select(Entry).where(Entry.event_id == derby.id).order_by(Entry.number)).all()
        speed = {e.id: 3.0 + i * 0.01 for i, e in enumerate(entries)}
        # make the 3rd and 4th Wolf cars tie, straddling the 3-finalist cutoff
        speed[entries[3].id] = speed[entries[2].id]

        with svc.session() as s:
            with pytest.raises(ServiceError, match="not approved"):
                create_final(s, s.get(Event, derby.id), None, ACTOR)

        await race_all(svc, track.id, speed.get)

        with svc.session() as s:
            ev = s.get(Event, derby.id)
            st = event_standings(s, ev)
            wolf = next(b for b in st if b["name"] == "Wolf Den 3")
            assert {r["s"].runs for r in wolf["rows"]} == {8}  # both runs x 4 lanes combined
            assert wolf["complete"] and wolf["rows"][0]["number"] == 101
            assert [r["number"] for r in wolf["ties"][0]] == [103, 104]
            with pytest.raises(ServiceError, match="runoff"):
                create_final(s, ev, None, ACTOR)
            create_runoff(s, ev, wolf["round"], [entries[2].id, entries[3].id], None, ACTOR)
            s.commit()

        speed[entries[3].id] = 2.9  # wins the runoff
        await race_all(svc, track.id, speed.get)

        with svc.session() as s:
            ev = s.get(Event, derby.id)
            wolf = next(b for b in event_standings(s, ev) if b["name"] == "Wolf Den 3")
            assert wolf["ties"] == []
            assert [r["number"] for r in wolf["rows"][:4]] == [101, 102, 104, 103]
            final = create_final(s, ev, None, ACTOR)
            s.commit()
            fin_entries = {
                hl.entry_id for hl in s.exec(select(HeatLane).join(Heat).where(Heat.round_id == final.id)) if hl.entry_id
            }
            nums = sorted(e.number for e in entries if e.id in fin_entries)
            assert nums == [101, 102, 104, 111, 112, 113]
            assert final.kind == RoundKind.final
        await svc.stop()

    run(body)


def test_projected_finish_uses_estimate_then_measured_pace(session, demo):
    from datetime import timedelta

    from derbytime.models import utcnow

    derby, _ = demo
    track = tracks_of(session, derby)[0]
    heats = list(session.exec(select(Heat).where(Heat.track_id == track.id).order_by(Heat.seq)))
    now = utcnow()

    p = projected_finish(session, derby, 45.0, now=now)
    row = p["tracks"][0]
    assert row["remaining"] == len(heats) and not row["measured"]
    assert p["seconds"] == len(heats) * 45.0

    # four heats raced 30 s apart, plus a 20-minute break that is ignored
    base = now - timedelta(hours=1)
    for i, h in enumerate(heats[:4]):
        h.status = HeatStatus.approved
        h.results_at = base + timedelta(seconds=30 * i + (1200 if i == 3 else 0))
        session.add(h)
    h = heats[4]
    h.status = HeatStatus.approved
    h.results_at = heats[3].results_at + timedelta(seconds=30)
    session.add(h)
    session.commit()

    row = projected_finish(session, derby, 45.0, now=now)["tracks"][0]
    assert row["measured"] and row["pace"] == 30
    assert row["remaining"] == len(heats) - 5

    for h in heats:
        h.status = HeatStatus.approved
        session.add(h)
    session.commit()
    assert projected_finish(session, derby, 45.0, now=now) is None
