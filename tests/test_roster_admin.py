"""Roster sync, roster editing, withdraw/disqualify, and exports."""
import csv
import io
import sqlite3

import pytest
from sqlmodel import Session, select

from derbytime.models import Entry, EntryStatus, Event, EventType, Group, Heat, HeatLane, HeatStatus, Racer, Role
from derbytime.services import ServiceError, export
from derbytime.services.roster import (
    apply_import,
    import_csv,
    merge_racers,
    plan_import,
    remove_entry,
    set_entry_status,
    update_racer,
)
from derbytime.services.schedule import create_final, eligible_entries, event_standings, pack_standings, regenerate
from derbytime.services.setup import create_event

from test_services import ACTOR, demo, make_service, race_all, run, tracks_of  # noqa: F401
from test_web import admin, app, client, join  # noqa: F401

HEADER = "Scout First Name,Scout Last Name,Member ID,Status,Den\n"


def racers(session):
    return {r.member_id or r.display_name: r for r in session.exec(select(Racer))}


def den_of(session, racer):
    return session.get(Group, racer.group_id).name


# --- roster sync -------------------------------------------------------------


def test_reimport_matches_member_id_and_syncs_changes(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, HEADER + "Kyle,Smith,111,Active,0 - Lion\nAmy,Jones,222,Active,0 - Lion\nOld,Kid,333,Inactive,1 - Tiger\n", ACTOR)
    session.commit()
    assert len(racers(session)) == 2

    # a year later: Kyle moved up a rank, Amy's name was fixed, Bo joined,
    # Old Kid is still inactive, and Amy left the pack
    text = HEADER + "Kyle,Smith,111,Active,1 - Tiger\nAmie,Jones,222,Inactive,1 - Tiger\nBo,Lee,444,Active,1 - Tiger\nOld,Kid,333,Inactive,1 - Tiger\n"
    plan = plan_import(session, ev, text)
    assert [r.kind for r in plan.rows] == ["update", "deactivate", "new", "skip"]
    assert plan.rows[0].changes == ["den 0 - Lion → 1 - Tiger"]
    assert "name Amy Jones → Amie Jones" in plan.rows[1].changes
    assert [r.name for r in plan.entering] == ["Bo Lee"]
    # nothing was written by planning
    assert den_of(session, racers(session)["111"]) == "0 - Lion"

    result = apply_import(session, ev, plan, ACTOR)
    session.commit()
    assert (result.imported, result.updated, result.deactivated, result.skipped_inactive) == (1, 1, 1, 1)
    rs = racers(session)
    assert len(rs) == 3  # no duplicates
    assert den_of(session, rs["111"]) == "1 - Tiger"
    assert rs["222"].first_name == "Amie" and not rs["222"].active
    # the same file again changes nothing
    assert not plan_import(session, ev, text).changed

    # Amy comes back
    plan = plan_import(session, ev, HEADER + "Amie,Jones,222,Active,1 - Tiger\n")
    assert plan.rows[0].kind == "reactivate"
    apply_import(session, ev, plan, ACTOR)
    assert racers(session)["222"].active


def test_legacy_racers_without_member_id_get_one(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, "first_name,last_name,den\nKyle,Smith,Den 1\nSam,Fox,Den 1\n", ACTOR)
    plan = plan_import(session, ev, HEADER + "Kyle,Smith,111,Active,Den 2\n")
    assert plan.rows[0].kind == "update"
    assert plan.rows[0].changes == ["den Den 1 → Den 2", "Member ID added"]
    assert plan.not_in_file == ["#102 Sam Fox"]
    apply_import(session, ev, plan, ACTOR)
    assert racers(session)["111"].first_name == "Kyle"
    assert len(racers(session)) == 2


def test_different_member_id_is_a_different_scout(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, HEADER + "Sam,Lee,111,Active,Den 1\n", ACTOR)
    plan = plan_import(session, ev, HEADER + "Sam,Lee,999,Active,Den 1\n")
    assert plan.rows[0].kind == "new"


def test_import_rejects_used_numbers(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, "first_name,number\nAl,7\n", ACTOR)
    with pytest.raises(ServiceError, match="number 7"):
        plan_import(session, ev, "first_name,number\nBo,7\n")


def test_inactive_scouts_are_not_scheduled(session, settings, demo):
    derby, _ = demo
    before = len(eligible_entries(session, derby.id))
    liam = session.exec(select(Racer).where(Racer.first_name == "Liam")).one()
    update_racer(session, liam, ACTOR, "Liam", "Garcia", "Wolf Den 3", None, False)
    assert len(eligible_entries(session, derby.id)) == before - 1
    regenerate(session, derby, ACTOR)
    entry = session.exec(select(Entry).where(Entry.event_id == derby.id, Entry.racer_id == liam.id)).one()
    scheduled = session.exec(
        select(HeatLane).join(Heat).where(HeatLane.entry_id == entry.id, Heat.status == HeatStatus.scheduled)
    ).all()
    assert scheduled == []


def test_merge_and_member_id_clash(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    other = create_event(session, settings, "Regatta", EventType.regatta, ACTOR)
    import_csv(session, ev, HEADER + "Kyle,Smith,111,Active,Den 1\n", ACTOR)
    import_csv(session, other, "first_name,last_name,den\nKyle,Smyth,Den 1\n", ACTOR)
    rs = racers(session)
    dup, keep = rs["Kyle Smyth"], rs["111"]
    with pytest.raises(ServiceError, match="already belongs"):
        update_racer(session, dup, ACTOR, "Kyle", "Smyth", "Den 1", "111", True)
    merge_racers(session, dup, keep, ACTOR)
    assert len(racers(session)) == 1
    assert {e.event_id for e in session.exec(select(Entry).where(Entry.racer_id == keep.id))} == {ev.id, other.id}
    with pytest.raises(ServiceError, match="different scout"):
        merge_racers(session, keep, keep, ACTOR)


# --- withdraw / disqualify ------------------------------------------------------


def test_withdraw_requires_reason_and_drops_unraced_heats(session, settings, demo):
    derby, _ = demo
    entry = session.exec(select(Entry).where(Entry.event_id == derby.id, Entry.number == 105)).one()
    with pytest.raises(ServiceError, match="reason"):
        set_entry_status(session, entry, EntryStatus.withdrawn, " ", ACTOR)
    set_entry_status(session, entry, EntryStatus.withdrawn, "car broke", ACTOR)
    regenerate(session, derby, ACTOR)
    assert not session.exec(select(HeatLane).where(HeatLane.entry_id == entry.id)).first()
    # reinstated: back in the schedule
    set_entry_status(session, entry, None, "", ACTOR)
    regenerate(session, derby, ACTOR)
    assert session.exec(select(HeatLane).where(HeatLane.entry_id == entry.id)).first()
    assert entry.status is None and entry.status_reason is None


def test_remove_entry_only_before_racing(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
        heat = await svc.stage(track.id, None, ACTOR)
        with svc.session() as s:
            lane = next(l for l in svc._lanes(s, heat.id) if l.entry_id)
            on_track = s.get(Entry, lane.entry_id)
            with pytest.raises(ServiceError, match="already raced"):
                remove_entry(s, on_track, ACTOR)
            idle = s.exec(select(Entry).where(Entry.event_id == derby.id, Entry.number == 120)).one()
            remove_entry(s, idle, ACTOR)
            s.commit()
            assert s.get(Entry, idle.id) is None
        await svc.stop()

    run(body)


def test_disqualified_and_withdrawn_in_standings_and_final(settings, engine, demo):
    derby, _ = demo

    async def body():
        svc, _ = await make_service(settings, engine)
        with svc.session() as s:
            track = tracks_of(s, derby)[0]
            entries = s.exec(select(Entry).where(Entry.event_id == derby.id).order_by(Entry.number)).all()
        speed = {e.id: 3.0 + i * 0.01 for i, e in enumerate(entries)}
        await race_all(svc, track.id, speed.get)

        with svc.session() as s:
            ev = s.get(Event, derby.id)
            set_entry_status(s, s.get(Entry, entries[0].id), EntryStatus.disqualified, "illegal weights", ACTOR)  # 101, fastest
            set_entry_status(s, s.get(Entry, entries[1].id), EntryStatus.withdrawn, "went home", ACTOR)  # 102
            s.commit()
            wolf = next(b for b in event_standings(s, ev) if b["name"] == "Wolf Den 3")
            assert [r["number"] for r in wolf["rows"][:3]] == [102, 103, 104]  # DQ out, withdrawn still ranked
            assert wolf["rows"][0]["s"].rank == 1 and wolf["rows"][0]["status"] == "withdrawn"
            assert [d["number"] for d in wolf["disqualified"]] == [101]
            assert 101 not in [r["number"] for r in pack_standings(s, ev)["rows"]]

            final = create_final(s, ev, None, ACTOR)
            s.commit()
            fin = {hl.entry_id for hl in s.exec(select(HeatLane).join(Heat).where(Heat.round_id == final.id)) if hl.entry_id}
            assert sorted(e.number for e in entries if e.id in fin) == [103, 104, 105, 111, 112, 113]

            # the exports carry the statuses
            rows = list(csv.DictReader(io.StringIO(export.standings_csv(s, ev))))
            assert any(r["Car Number"] == "101" and r["Rank"] == "DQ" and r["Race Status Reason"] == "illegal weights" for r in rows)
            heats = list(csv.DictReader(io.StringIO(export.heats_csv(s, ev))))
            assert heats and all(h["Final Time"] for h in heats if h["Heat Status"] == "approved")
        await svc.stop()

    run(body)


# --- exports -------------------------------------------------------------------


def test_backup_and_roster_round_trip(session, engine, settings, demo, tmp_path):
    derby, _ = demo
    dest = export.backup_db(engine, tmp_path / "b" / "copy.db")
    with sqlite3.connect(dest) as con:
        assert con.execute("select count(*) from racer").fetchone()[0] == 20
        assert con.execute("select count(*) from heat").fetchone()[0] > 0
    # the event roster export imports back without changes
    text = export.roster_csv(session, derby)
    assert "Car Number" in text and "Blue Lightning" in text
    assert not plan_import(session, derby, text).changed
    assert len(export.roster_csv(session).splitlines()) == 21


# --- web -----------------------------------------------------------------------


def test_admin_roster_pages_and_dq_flow(app, admin):  # noqa: F811
    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == EventType.derby)).first()
        entry = s.exec(select(Entry).where(Entry.event_id == ev.id, Entry.number == 104)).one()
        racer = s.get(Racer, entry.racer_id)
    assert racer.display_name in admin.get("/admin/roster").text
    page = admin.get(f"/admin/racer/{racer.id}").text
    assert "Withdraw" in page and "Disqualify" in page

    nxt = f"/admin/event/{ev.id}"
    r = admin.post(f"/admin/entry/{entry.id}/status", data={"status": "disqualified", "reason": "", "next": nxt}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = admin.post(f"/admin/entry/{entry.id}/status", data={"status": "disqualified", "reason": "Lost wheel", "next": nxt}, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith(nxt) and "disqualified" in loc and "rescheduled" in loc
    assert "disqualified" in admin.get(nxt).text
    with Session(app.state.engine) as s:
        assert not s.exec(
            select(HeatLane).join(Heat).where(HeatLane.entry_id == entry.id, Heat.status == HeatStatus.scheduled)
        ).first()

    r = admin.post(f"/admin/racer/{racer.id}", data={"first_name": "Jim", "last_name": racer.last_name, "den": "Wolf Den 3", "member_id": "555", "active": "on"})
    assert "Saved Jim" in r.text

    for kind in ("roster", "standings", "heats"):
        r = admin.get(f"/admin/event/{ev.id}/export/{kind}.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "555" in admin.get("/admin/roster.csv").text
    r = admin.get("/admin/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")


def test_roster_admin_needs_admin(app, client):  # noqa: F811
    join(app, client, Role.checkin)
    for url in ("/admin/roster", "/admin/racer/1", "/admin/backup", "/admin/roster.csv", "/admin/event/1/export/heats.csv"):
        assert client.get(url).status_code == 403
