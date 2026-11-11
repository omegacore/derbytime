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
    split_den,
    set_entry_status,
    update_entry,
    update_racer,
)
from derbytime.routes import roster as roster_routes
from derbytime.services.schedule import (
    create_final,
    eligible_entries,
    event_standings,
    generate_group_rounds,
    group_key,
    group_rounds,
    pack_standings,
    regenerate,
)
from derbytime.services.setup import add_track, create_event

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
    assert plan.rows[0].changes == ["den Lion → Tiger"]
    assert "name Amy Jones → Amie Jones" in plan.rows[1].changes
    assert [r.name for r in plan.entering] == ["Bo Lee"]
    # nothing was written by planning
    assert den_of(session, racers(session)["111"]) == "Lion"

    result = apply_import(session, ev, plan, ACTOR)
    session.commit()
    assert (result.imported, result.updated, result.deactivated, result.skipped_inactive) == (1, 1, 1, 1)
    rs = racers(session)
    assert len(rs) == 3  # no duplicates
    assert den_of(session, rs["111"]) == "Tiger"
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
    sam = session.exec(select(Entry).join(Racer).where(Racer.first_name == "Sam")).one()
    assert plan.not_in_file == [f"#{sam.number} Sam Fox"]
    apply_import(session, ev, plan, ACTOR)
    assert racers(session)["111"].first_name == "Kyle"
    assert len(racers(session)) == 2


def test_different_member_id_is_a_different_scout(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, HEADER + "Sam,Lee,111,Active,Den 1\n", ACTOR)
    plan = plan_import(session, ev, HEADER + "Sam,Lee,999,Active,Den 1\n")
    assert plan.rows[0].kind == "new"


def test_pack_numbers_random_from_10_and_kept_across_events(session, settings, monkeypatch):
    monkeypatch.setattr("random.shuffle", lambda xs: xs.reverse())  # stand-in for a random order
    derby = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, derby, "first_name,den\nAl,Den 1\nBo,Den 1\nCy,Den 2\n", ACTOR)
    nums = {r.first_name: r.number for r in session.exec(select(Racer))}
    assert nums == {"Cy": 10, "Bo": 11, "Al": 12}
    entries = {e.racer_id: e.number for e in session.exec(select(Entry).where(Entry.event_id == derby.id))}
    assert sorted(entries.values()) == [10, 11, 12]

    regatta = create_event(session, settings, "Regatta", EventType.regatta, ACTOR)
    import_csv(session, regatta, "first_name,den\nAl,Den 1\nBo,Den 1\nCy,Den 2\nDee,Den 2\n", ACTOR)
    got = {r.first_name: e.number for e, r in session.exec(select(Entry, Racer).join(Racer).where(Entry.event_id == regatta.id))}
    assert got == {**nums, "Dee": 13}

    al = session.exec(select(Entry).join(Racer).where(Entry.event_id == regatta.id, Racer.first_name == "Al")).one()
    update_entry(session, al, ACTOR, 50, None)
    assert session.get(Racer, al.racer_id).number == 50  # an edited number follows the racer
    bo = session.exec(select(Entry).join(Racer).where(Entry.event_id == regatta.id, Racer.first_name == "Bo")).one()
    with pytest.raises(ServiceError, match="already used"):
        update_entry(session, bo, ACTOR, 10, None)  # Cy's number in this event


def test_reimport_without_member_ids_sets_boat_names(session, settings):
    ev = create_event(session, settings, "Regatta", EventType.regatta, ACTOR)
    import_csv(session, ev, HEADER + "Kyle,Smith,111,Active,Lions\nAmy,Jones,222,Active,Tigers\nBo,Lee,333,Active,Tigers\n", ACTOR)
    session.commit()
    nums = {e.racer_id: e.number for e in session.exec(select(Entry).where(Entry.event_id == ev.id))}
    kyle, amy, bo = (racers(session)[m] for m in ("111", "222", "333"))
    update_entry(session, session.exec(select(Entry).where(Entry.racer_id == bo.id)).one(), ACTOR, nums[bo.id], "Old Name")
    session.commit()

    # a later sheet with no Member ID column, but boat numbers and names; Amy's number is a typo
    text = ("Scout First Name,Scout Last Name,Den,Boat Number,Boat Name\n"
            f"kyle,smith,Lions,{nums[kyle.id]},  Sea   Dog \n"
            f"Amy,Jones,Tigers,{nums[amy.id] + 100},Tugboat\n"
            f"Bo,Lee,Tigers,{nums[bo.id]},\n")
    plan = plan_import(session, ev, text)
    assert plan.columns["car_name"] == "Boat Name" and plan.columns["member_id"] is None
    assert [r.racer_id for r in plan.rows] == [kyle.id, amy.id, bo.id]
    assert not plan.entering
    assert [(r.name, r.rename) for r in plan.renaming] == [("kyle smith", "Sea Dog"), ("Amy Jones", "Tugboat")]
    assert plan.changed

    result = apply_import(session, ev, plan, ACTOR)
    session.commit()
    names = {e.racer_id: e.name for e in session.exec(select(Entry).where(Entry.event_id == ev.id))}
    # a blank cell keeps Bo's name; numbers never change on import
    assert names == {kyle.id: "Sea Dog", amy.id: "Tugboat", bo.id: "Old Name"}
    assert {e.racer_id: e.number for e in session.exec(select(Entry).where(Entry.event_id == ev.id))} == nums
    assert result.renamed == 2 and "Set 2 boat names." in result.message
    # the same file again changes nothing
    assert not plan_import(session, ev, text).changed


def test_import_preview_lists_boat_names(app, admin):  # noqa: F811
    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == EventType.regatta)).first()
        e, r = s.exec(select(Entry, Racer).join(Racer, Racer.id == Entry.racer_id).where(Entry.event_id == ev.id)).first()
        g = s.get(Group, r.group_id)
    text = f"First Name,Last Name,Den,Boat Number,Boat Name\n{r.first_name},{r.last_name},{g.name},{e.number + 500},Paddle Pop\n"
    page = admin.post(f"/admin/event/{ev.id}/import", data={"csv_text": text}).text
    assert "Boat names" in page and "Paddle Pop" in page and f"file #{e.number + 500}" in page


def test_import_rejects_used_numbers(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, "first_name,number\nAl,7\n", ACTOR)
    with pytest.raises(ServiceError, match="number 7"):
        plan_import(session, ev, "first_name,number\nBo,7\n")



def test_import_with_picked_columns(session, settings):
    """A roster with unfamiliar headers imports once the admin picks the columns."""
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    text = "Given,Family,Patrol,Enrolled,ID\nKyle,Smith,Den 1,Active,111\nOld,Kid,Den 2,Dropped,222\n"
    with pytest.raises(ServiceError, match="first name"):
        plan_import(session, ev, text)
    cols = {"first_name": "Given", "last_name": "Family", "den": "Patrol", "status": "Enrolled", "member_id": "ID"}
    plan = plan_import(session, ev, text, cols)
    assert plan.headers == ["Given", "Family", "Patrol", "Enrolled", "ID"]
    assert [(r.name, r.den, r.member_id, r.kind) for r in plan.rows] == [("Kyle Smith", "Den 1", "111", "new"), ("Old Kid", "Den 2", "222", "skip")]
    # unpicking Status makes every row count as Active; a header not in the file is ignored
    plan = plan_import(session, ev, text, cols | {"status": None, "car_name": "Nope"})
    assert not plan.has_status and [r.kind for r in plan.rows] == ["new", "new"]
    assert plan.columns["car_name"] is None


def test_import_guesses_columns(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    plan = plan_import(session, ev, HEADER + "Kyle,Smith,111,Active,Den 1\n")
    assert plan.columns == {"first_name": "Scout First Name", "last_name": "Scout Last Name", "member_id": "Member ID",
                            "den": "Den", "status": "Status", "number": None, "car_name": None}


def test_split_den_strips_only_an_ordering_prefix():
    assert split_den("0 - Lion") == ("Lion", 0)
    assert split_den(" 5 -  AOL ") == ("AOL", 5)
    assert split_den("3-Bear") == ("Bear", 3)
    for bare in ("Lion", "Den 3", "Wolf Den 3", "2026 Graduates", "0 - "):
        assert split_den(bare) == (" ".join(bare.split()), None)


PACK_DENS = ["Lion", "Tiger", "Wolf", "Bear", "Webelos", "AOL", "Graduated"]


def test_prefixed_den_matches_existing_bare_den(session, settings):
    """The live dens were renamed to bare names; re-importing the roster
    export ("0 - Lion") must reuse them, not bring the prefixed dens back."""
    for i, name in enumerate(PACK_DENS):
        session.add(Group(name=name, sort_order=i))
    session.flush()
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    text = HEADER + "Kyle,Smith,111,Active,0 - Lion\nAmy,Jones,222,Active,1 - Tiger\n"
    plan = plan_import(session, ev, text)
    assert [(r.den, r.den_order) for r in plan.rows] == [("Lion", 0), ("Tiger", 1)]
    apply_import(session, ev, plan, ACTOR)
    session.commit()
    assert [g.name for g in session.exec(select(Group).order_by(Group.sort_order))] == PACK_DENS
    rs = racers(session)
    assert (den_of(session, rs["111"]), den_of(session, rs["222"])) == ("Lion", "Tiger")

    # re-importing the same file changes nothing
    assert [r.kind for r in plan_import(session, ev, text).rows] == ["same", "same"]


def test_new_dens_take_their_place_from_the_prefix(session, settings):
    ev = create_event(session, settings, "Derby", EventType.derby, ACTOR)
    import_csv(session, ev, HEADER + "A,A,1,Active,3 - Bear\nB,B,2,Active,0 - Lion\nC,C,3,Active,Graduated\nD,D,4,Active,2 - Wolf\n", ACTOR)
    session.commit()
    order = {g.name: g.sort_order for g in session.exec(select(Group))}
    # no prefix: shares the last place and sorts by name there
    assert order == {"Bear": 3, "Lion": 0, "Graduated": 3, "Wolf": 2}
    assert [g.name for g in sorted(session.exec(select(Group)), key=group_key)] == ["Lion", "Wolf", "Bear", "Graduated"]


def add_pack_dens(s, first_order=0):
    """The live dens: bare names, sort_order in pack order. Racers are added
    in name order (AOL first) so a name sort would show."""
    ev = create_event(s, s.info["settings"], "Derby", EventType.derby, ACTOR)
    for i, name in enumerate(PACK_DENS):
        s.add(Group(name=name, sort_order=first_order + i))
    rows = "".join(f"{d}kid,Scout,{100 + i},Active,{d}\n" for i, d in enumerate(sorted(PACK_DENS)))
    import_csv(s, ev, HEADER + rows, ACTOR, checked_in=True)
    s.commit()
    return ev


def test_dens_run_and_rank_in_sort_order(session, settings):
    session.info["settings"] = settings
    ev = add_pack_dens(session)
    assert [g.name for g in sorted(session.exec(select(Group)), key=group_key)] == PACK_DENS
    add_track(session, settings, ev, "Main", ACTOR, lane_count=4)
    generate_group_rounds(session, ev, ACTOR, passes=2)
    session.commit()
    assert [r.name for r in group_rounds(session, ev.id)][:7] == PACK_DENS
    assert [b["name"] for b in event_standings(session, ev)][:7] == PACK_DENS


def test_admin_pages_list_dens_in_sort_order(app, admin, settings):  # noqa: F811
    with Session(app.state.engine) as s:
        s.info["settings"] = settings
        ev = add_pack_dens(s, first_order=2)  # after the seeded Bear Den 5, Wolf Den 3 (both 0)
        assert roster_routes.den_names(s) == ["Bear Den 5", "Wolf Den 3", *PACK_DENS]
        add_track(s, settings, ev, "Main", ACTOR, lane_count=4)
        generate_group_rounds(s, ev, ACTOR, passes=1)
        s.commit()
        ev_id = ev.id
    app.state.page_cache.bump()
    page = admin.get("/admin/roster").text
    at = [page.index(f"{d}kid Scout") for d in PACK_DENS]
    assert at == sorted(at)
    page = admin.get(f"/admin/event/{ev_id}").text
    at = [page.index(f">{d}</option>") for d in PACK_DENS]
    assert at == sorted(at)


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
    assert "Weight (oz),Length (in)," in admin.get(f"/admin/event/{ev.id}/export/roster.csv").text
    assert "555" in admin.get("/admin/roster.csv").text
    r = admin.get("/admin/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")


def test_roster_admin_needs_admin(app, client):  # noqa: F811
    join(app, client, Role.checkin)
    for url in ("/admin/roster", "/admin/racer/1", "/admin/backup", "/admin/roster.csv", "/admin/event/1/export/heats.csv"):
        assert client.get(url).status_code == 403


def test_import_preview_asks_for_columns(app, admin):  # noqa: F811
    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == EventType.derby)).first()
        before = len(s.exec(select(Racer)).all())
    url = f"/admin/event/{ev.id}/import"
    text = "Given,Family,Patrol\nZed,Quinn,Den 9\n"
    # unrecognised headers: the preview shows the pickers (with sample values) instead of failing
    page = admin.post(url, data={"csv_text": text}).text
    assert "Pick which column holds the first name" in page and "e.g. Zed" in page and "Apply import" not in page
    cols = {"columns": "1", "col_first_name": "Given", "col_last_name": "Family", "col_den": "Patrol"}
    page = admin.post(url, data={"csv_text": text} | cols).text
    assert "Zed Quinn" in page and "Apply import" in page and 'name="col_den" value="Patrol"' in page
    r = admin.post(url, data={"csv_text": text, "apply": "1"} | cols, follow_redirects=False)
    assert "Imported+1" in r.headers["location"] or "Imported%201" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert len(s.exec(select(Racer)).all()) == before + 1
        assert s.exec(select(Racer).where(Racer.first_name == "Zed")).one().last_name == "Quinn"
