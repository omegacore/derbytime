"""Hand-editing a run's unraced heats (e.g. fixing a heat with only one racer)."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from racemgr import auth
from racemgr.app import create_app
from racemgr.models import AuditLog, Heat, HeatLane, HeatStatus, Role
from racemgr.services import ServiceError, views
from racemgr.services.schedule import generate_group_rounds, group_rounds, heat_editor, race_order, save_heat_lineups, solo_heats

from test_regenerate import race
from test_services import ACTOR, make_event_with_dens, tracks_of


def lineups(session, rnd):
    """[(number, status, {lane: entry})] in queue order."""
    heats = session.exec(select(Heat).where(Heat.round_id == rnd.id).order_by(Heat.seq)).all()
    return [
        (h.number, h.status, {hl.lane: hl.entry_id for hl in session.exec(select(HeatLane).where(HeatLane.heat_id == h.id))})
        for h in heats
    ]


@pytest.fixture
def solo_run(session, settings):
    """Five scouts on four lanes, one race each: heats of 4 and 1."""
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=5)
    generate_group_rounds(session, ev, ACTOR, passes=1, races=1)
    session.commit()
    return ev, group_rounds(session, ev.id)[0]


def editor_rows(session, ev, rnd):
    ed = heat_editor(session, ev, rnd)
    return [(str(r["heat"].id), dict(r["lanes"])) for r in ed["rows"] if r["editable"]], ed["version"]


def test_move_a_car_into_the_solo_heat(session, solo_run):
    ev, rnd = solo_run
    assert solo_heats(session, [rnd.id]) == {rnd.id: 1}
    assert race_order(session, ev)[0]["solo"] == 1

    rows, version = editor_rows(session, ev, rnd)
    (h1, l1), (h2, l2) = rows
    moved = l1[4]
    l1[4] = None
    l2[next(l for l, e in l2.items() if e is None)] = moved
    r = save_heat_lineups(session, ev, rnd, version, rows, ACTOR)
    session.commit()

    assert r == {"heats": 2, "removed": 0, "added": 0}
    assert solo_heats(session, [rnd.id]) == {}
    counts = [sum(1 for e in lanes.values() if e) for _, _, lanes in lineups(session, rnd)]
    assert counts == [3, 2]
    log = session.exec(select(AuditLog).where(AuditLog.action == "schedule.edit_heats")).one()
    assert log.entity_id == rnd.id and log.before["heats"] != log.after["heats"]


def test_reorder_empty_and_add_heats(session, solo_run):
    ev, rnd = solo_run
    rows, version = editor_rows(session, ev, rnd)
    (h1, l1), (h2, l2) = rows
    solo_racer = next(e for e in l2.values() if e)
    # empty the solo heat, and split heat 1 into two heats of two (the new one goes first)
    keep = {l: e for l, e in l1.items() if l in (1, 2)}
    new = {1: l1[3], 2: l1[4], 3: solo_racer, 4: None}
    rows = [("new", new), (h1, {**keep, 3: None, 4: None}), (h2, {l: None for l in l2})]
    r = save_heat_lineups(session, ev, rnd, version, rows, ACTOR)
    session.commit()

    assert r == {"heats": 2, "removed": 1, "added": 1}
    out = lineups(session, rnd)
    assert [(n, lanes) for n, _, lanes in out] == [(1, new), (2, {**keep, 3: None, 4: None})]
    # the track's queue follows the new order
    track = tracks_of(session, ev)[0]
    assert [h.number for h in views.plan_all(session)[track.id]] == [1, 2]


def test_bad_edits_are_rejected(session, solo_run):
    ev, rnd = solo_run
    rows, version = editor_rows(session, ev, rnd)
    before = lineups(session, rnd)
    (h1, l1), (h2, l2) = rows

    def attempt(rs, v=version):
        with pytest.raises(ServiceError) as e:
            save_heat_lineups(session, ev, rnd, v, rs, ACTOR)
        session.rollback()
        return e.value.message

    assert "two lanes" in attempt([(h1, {**l1, 2: l1[1]}), (h2, l2)])
    assert "isn't in this run" in attempt([(h1, {**l1, 1: 99999}), (h2, l2)])
    assert "lane 7" in attempt([(h1, {**l1, 7: None}), (h2, l2)])
    assert "missing" in attempt([(h1, l1)])
    assert "changed while you were editing" in attempt(rows, v=h1)
    assert lineups(session, rnd) == before


def test_raced_heats_are_locked(settings, engine, session):
    ev = make_event_with_dens(session, settings, ["Den 1"], per_den=6)
    generate_group_rounds(session, ev, ACTOR, passes=1)
    session.commit()
    rnd = group_rounds(session, ev.id)[0]
    rows, version = editor_rows(session, ev, rnd)

    race(settings, engine, tracks_of(session, ev)[0].id, 1)  # heat 1 races while the editor is open
    session.expire_all()
    with pytest.raises(ServiceError, match="changed while you were editing"):
        save_heat_lineups(session, ev, rnd, version, rows, ACTOR)
    session.rollback()

    ed = heat_editor(session, ev, rnd)
    assert [r["editable"] for r in ed["rows"]][:2] == [False, True]
    raced_before = lineups(session, rnd)[0]
    rows, version = editor_rows(session, ev, rnd)
    rows.reverse()
    save_heat_lineups(session, ev, rnd, version, rows, ACTOR)
    session.commit()
    out = lineups(session, rnd)
    assert out[0] == raced_before
    assert [n for n, _, _ in out] == list(range(1, len(out) + 1))


def test_heat_editor_pages(settings, engine, session, solo_run):
    ev, rnd = solo_run
    app = create_app(settings, engine, print_admin_link=False)
    with TestClient(app) as client:
        jt = auth.create_join_token(session, Role.admin, 10, None, None)
        session.commit()
        client.post(f"/join/{jt.token}", follow_redirects=False)

        assert "1 racer" in client.get(f"/admin/event/{ev.id}").text
        page = client.get(f"/admin/round/{rnd.id}/heats")
        assert page.status_code == 200 and "Races per scout" in page.text

        rows, version = editor_rows(session, ev, rnd)
        (h1, l1), (h2, l2) = rows
        free = next(l for l, e in l2.items() if e is None)
        data = {"version": version, "row": [f"a:{h1}", f"b:{h2}"]}
        data |= {f"lane-a-{l}": "" if l == 4 else str(e or "") for l, e in l1.items()}
        data |= {f"lane-b-{l}": str(e or (l1[4] if l == free else "")) for l, e in l2.items()}
        r = client.post(f"/admin/round/{rnd.id}/heats", data=data, follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
        session.expire_all()
        assert solo_heats(session, [rnd.id]) == {}
