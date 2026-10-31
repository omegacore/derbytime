import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from racemgr import auth
from racemgr.app import create_app
from racemgr.models import Entry, Event, Heat, HeatLane, HeatStatus, JoinToken, Official, Role, Track, utcnow
from racemgr.seed import seed_demo


@pytest.fixture
def app(settings, engine):
    with Session(engine, expire_on_commit=False) as s:
        seed_demo(s, settings)
    return create_app(settings, engine, print_admin_link=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def join(app, client, role, track_id=None, lane=None, **kw):
    with Session(app.state.engine) as s:
        jt = auth.create_join_token(s, role, 10, track_id, lane, **kw)
        s.commit()
        token = jt.token
    r = client.post(f"/join/{token}", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert auth.COOKIE in r.cookies or client.cookies.get(auth.COOKIE)
    return token, r.headers["location"]


def track(app, event_type, idx=0):
    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == event_type)).first()
        return s.exec(select(Track).where(Track.event_id == ev.id).order_by(Track.id)).all()[idx]


@pytest.fixture
def admin(app, client):
    join(app, client, Role.admin)
    return client


def other_client(app):
    return TestClient(app)


# --- public pages ----------------------------------------------------------------


def test_public_pages_render(client, app):
    t = track(app, "derby")
    for url in ["/", "/partials/home", "/display/standings", "/partials/standings", f"/display/ondeck/{t.id}",
                f"/partials/ondeck/{t.id}", "/event/1", "/partials/event/1", "/racer/1", "/partials/racer/1",
                "/partials/search?q=liam", "/partials/search?q=101", "/me", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])
    assert "Liam" in client.get("/partials/search?q=liam").text
    assert "Main Track" in client.get("/partials/home").text
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_rank_theme_matches_den_name():
    from racemgr.routes.pages import rank_theme

    assert rank_theme("Wolf Den 3") == "wolf"
    assert rank_theme("0 - LION") == "lion"
    assert rank_theme("5 - AOL") == "aol"
    assert rank_theme("Arrow of Light Den 1") == "aol"
    assert rank_theme("Webelos 2") == "webelos"
    assert rank_theme("Open") == "pack"


def test_standings_board_slides(client):
    html = client.get("/partials/standings?rows=3").text
    assert 'theme-wolf den' in html and 'theme-bear den' in html
    assert "Pack Overall" in html and 'data-slide="1-pack-1"' in html
    # dens come before the pack overall
    assert html.index("theme-wolf") < html.index("Pack Overall")
    # long lists are split into evenly sized pages
    assert "1 of " in html


def test_standings_board_uses_official_icons(client, tmp_path, monkeypatch):
    from racemgr.routes import pages

    monkeypatch.setattr(pages, "RANK_ICON_DIR", tmp_path)
    (tmp_path / "Wolf.png").write_bytes(b"")
    html = client.get("/partials/standings").text
    assert 'src="/static/ranks/Wolf.png"' in html
    assert html.count('<img class="rank-icon official"') == html.count("theme-wolf")
    assert '<svg class="rank-icon"' in html  # other ranks keep the drawn icon


def test_standings_board_shows_vehicles(app, client, monkeypatch):
    from racemgr.core.scoring import Standing
    from racemgr.routes import pages

    def row(entry_id, rank, score, photo):
        return {"s": Standing(entry_id, rank, score, 1, score, score, 0), "entry_id": entry_id, "number": 100 + entry_id,
                "car_name": "", "racer_id": entry_id, "racer_name": f"Racer {entry_id}", "group": "Wolf Den 3",
                "photo_url": photo}

    rows = [row(7, 1, 2.5, "/api/entry/7/photo/entry?v=a.jpg"), row(8, 2, 3.0, None), row(9, 3, None, "/x.jpg")]
    pack = {"round": None, "name": "Pack Overall", "approved": 1, "total": 2, "complete": False, "rows": rows}
    monkeypatch.setattr(pages, "event_standings", lambda s, ev: [])
    monkeypatch.setattr(pages, "pack_standings", lambda s, ev: pack)
    html = client.get("/partials/standings?event=1").text
    assert 'data-lead="7"' in html and "Leader" in html  # the scored leader's vehicle is featured
    assert '<img src="/api/entry/7/photo/entry?v=a.jpg"' in html and 'src="/x.jpg"' in html
    assert html.count('class="vehicle-glyph"') == 1  # entry 8 has no photo yet

    rows[0]["photo_url"] = None  # leader without a photo: no spotlight
    app.state.page_cache.bump()  # data changed behind the app's back
    assert "spotlight" not in client.get("/partials/standings?event=1").text


def test_board_vehicle_matches_event_name():
    from racemgr.models import EventType
    from racemgr.routes.pages import board_vehicle

    assert board_vehicle(Event(name="321 Regatta 2026!", type=EventType.regatta)) == "boat"
    assert board_vehicle(Event(name="Pack 9 Pinewood DERBY", type=EventType.regatta)) == "car"  # name wins
    assert board_vehicle(Event(name="Spring races", type=EventType.regatta)) == "boat"  # else the type
    assert board_vehicle(Event(name="Spring races", type=EventType.derby)) == "car"


def test_event_standings_table_has_no_vehicle_column(client):
    assert "col-car" not in client.get("/partials/event/1").text


def test_racers_api(client):
    r = client.get("/api/racers?ids=1,2").json()
    assert {x["id"] for x in r["racers"]} == {1, 2}
    assert client.get("/api/racers?ids=abc").status_code == 400


@pytest.mark.parametrize("url", ["/admin", "/admin/officials", "/timekeeper", "/checkin", "/track/1/operate", "/track/2/lane/1"])
def test_officials_pages_need_auth(client, url):
    r = client.get(url)
    assert r.status_code == 403
    assert "QR code" in r.text


def test_actions_need_auth(client, app):
    t = track(app, "derby")
    assert client.post(f"/api/track/{t.id}/stage").status_code == 403
    assert client.post("/api/heat/1/approve").status_code == 403


# --- joining --------------------------------------------------------------------------


def test_admin_link_and_join(app, client):
    with Session(app.state.engine) as s:
        link = auth.ensure_admin_link(s, app.state.settings)
    assert "/join/" in link
    token = link.rsplit("/", 1)[1]
    assert "Join as Admin" in client.get(f"/join/{token}").text
    r = client.post(f"/join/{token}", follow_redirects=False)
    assert r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    # one-time: second use fails
    c2 = other_client(app)
    assert c2.post(f"/join/{token}").status_code == 410
    # an admin exists now, so no new link is printed on startup
    with Session(app.state.engine) as s:
        assert auth.ensure_admin_link(s, app.state.settings) is None


def test_expired_and_revoked_tokens(app, client):
    with Session(app.state.engine) as s:
        jt = auth.create_join_token(s, Role.checkin, 10)
        jt.expires_at = utcnow() - timedelta(minutes=1)
        jt2 = auth.create_join_token(s, Role.checkin, 10)
        jt2.revoked = True
        s.commit()
        a, b = jt.token, jt2.token
    assert client.get(f"/join/{a}").status_code == 410
    assert "expired" in client.post(f"/join/{a}").text
    assert "revoked" in client.post(f"/join/{b}").text
    assert client.post("/join/nope").status_code == 410


def test_admin_creates_lane_codes_with_qr(admin, app):
    g = track(app, "regatta")
    r = admin.post("/admin/tokens", data={"role": "lane_official", "track_id": g.id, "all_lanes": "on"}, follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/admin/officials").text
    assert page.count("<svg") >= 2 and "lane 2" in page
    with Session(app.state.engine) as s:
        toks = s.exec(select(JoinToken).where(JoinToken.role == Role.lane_official)).all()
        assert sorted(t.lane for t in toks) == [1, 2]
    # lane official without lane is rejected
    r = admin.post("/admin/tokens", data={"role": "lane_official", "track_id": g.id})
    assert "need a track and lane" in r.text


def test_lane_official_scope_and_revoke(app, admin):
    g = track(app, "regatta")
    lane_client = other_client(app)
    _, home = join(app, lane_client, Role.lane_official, g.id, 1)
    assert home == f"/track/{g.id}/lane/1"
    assert lane_client.get(home).status_code == 200
    assert lane_client.get(f"/track/{g.id}/lane/2").status_code == 403
    assert lane_client.get(f"/track/{g.id}/operate").status_code == 403
    assert lane_client.get("/timekeeper").status_code == 403

    with Session(app.state.engine) as s:
        o = s.exec(select(Official).where(Official.role == Role.lane_official)).one()
    assert admin.post(f"/admin/officials/{o.id}/revoke", follow_redirects=False).status_code == 303
    assert lane_client.get(home).status_code == 403


def test_operator_bound_to_track(app):
    a, b = track(app, "regatta", 0), track(app, "regatta", 1)
    c = other_client(app)
    _, home = join(app, c, Role.track_operator, a.id)
    assert home == f"/track/{a.id}/operate"
    assert c.get(home).status_code == 200
    assert c.get(f"/track/{b.id}/operate").status_code == 403
    assert c.post(f"/api/track/{b.id}/stage").status_code == 403


def test_unbound_operator_gets_track_picker(app):
    c = other_client(app)
    _, home = join(app, c, Role.track_operator)
    assert home == "/operate"
    page = c.get(home).text
    for t in (track(app, "derby"), track(app, "regatta", 0), track(app, "regatta", 1)):
        assert f"/track/{t.id}/operate" in page
    assert 'href="/operate" class="mine">Operate</a>' in c.get("/").text  # own page first in nav
    t = track(app, "derby")
    assert c.post(f"/api/track/{t.id}/stage").status_code == 204


def test_operate_picker_access(app, client):
    assert client.get("/operate").status_code == 403  # anonymous
    lane = other_client(app)
    join(app, lane, Role.lane_official, track(app, "regatta").id, 1)
    assert lane.get("/operate").status_code == 403
    bound = other_client(app)
    t = track(app, "derby")
    join(app, bound, Role.track_operator, t.id)
    r = bound.get("/operate", follow_redirects=False)  # only one track: go straight there
    assert r.status_code == 303 and r.headers["location"] == f"/track/{t.id}/operate"
    assert f'href="/track/{t.id}/operate" class="mine">Operate</a>' in bound.get("/").text


def role_bar(html):
    start = html.index('class="role-bar"')
    return html[start : html.index("</header>", start)]


@pytest.mark.parametrize(
    "role, lane, label, scope, home",
    [
        (Role.admin, None, "Admin", "", "/admin"),
        (Role.head_timekeeper, None, "Head timekeeper", "", "/timekeeper"),
        (Role.track_operator, None, "Track operator", "all tracks", "/operate"),
        (Role.track_operator, "bound", "Track operator", "Gutter A", "/track/{t}/operate"),
        (Role.lane_official, 2, "Lane official", "Gutter A · lane 2", "/track/{t}/lane/2"),
        (Role.checkin, None, "Check-in", "", "/checkin"),
    ],
)
def test_every_role_sees_who_they_are_and_way_home(app, role, lane, label, scope, home):
    g = track(app, "regatta")
    home = home.format(t=g.id)
    c = other_client(app)
    track_id = g.id if lane is not None else None
    _, landed = join(app, c, role, track_id, lane if isinstance(lane, int) else None)
    assert landed == home

    # on their own page: role, scope and a hint; own page highlighted first in the nav
    page = c.get(home, follow_redirects=True).text
    bar = role_bar(page)
    assert label in bar and scope in bar and "Back to my page" not in bar
    assert f'header class="top role-{role.value}"' in page
    assert f'<a href="{home}" aria-current="page" class="mine">' in page or role == Role.track_operator and lane is None

    # anywhere else (public pages too): a one-tap way back
    bar = role_bar(c.get("/display/standings").text)
    assert label in bar and f'href="{home}">Back to my page' in bar

    # a page their role can't open explains why and links home instead of "scan a QR code"
    if role != Role.admin:
        r = c.get("/admin")
        assert r.status_code == 403
        assert "Go to my page" in r.text and f'href="{home}"' in r.text and label in r.text


def test_viewer_has_no_role_bar(client):
    html = client.get("/").text
    assert 'class="role-bar"' not in html and 'class="mine"' not in html


def test_switching_roles_retires_old_device(app):
    c = other_client(app)
    join(app, c, Role.checkin)
    with Session(app.state.engine) as s:
        jt = auth.create_join_token(s, Role.head_timekeeper, 10)
        s.commit()
        token = jt.token
    page = c.get(f"/join/{token}").text
    assert "already" in page and "Switch to this role" in page
    c.post(f"/join/{token}")
    assert c.get("/timekeeper").status_code == 200
    assert c.get("/checkin").status_code == 403
    with Session(app.state.engine) as s:
        active = s.exec(select(Official).where(Official.revoked == False)).all()  # noqa: E712
        assert [o.role for o in active] == [Role.head_timekeeper]


# --- heat flow over HTTP ---------------------------------------------------------------


def heat_status(app, heat_id):
    with Session(app.state.engine) as s:
        return s.get(Heat, heat_id).status


def current_heat(app, track_id):
    with Session(app.state.engine) as s:
        return s.exec(select(Heat).where(Heat.track_id == track_id, Heat.status.in_([HeatStatus.staged, HeatStatus.armed]))).first()


def test_regatta_flow_with_phones(app, admin):
    g = track(app, "regatta")
    lane1 = other_client(app)
    join(app, lane1, Role.lane_official, g.id, 1)

    assert admin.post(f"/api/track/{g.id}/stage").status_code == 204
    heat = current_heat(app, g.id)
    assert "#2" in admin.get(f"/partials/operate/{g.id}").text  # lane cards render
    info = lane1.get(f"/partials/lane/{g.id}/1").text
    assert 'data-armed="0"' in info and f'data-heat="{heat.id}"' in info

    assert admin.post(f"/api/track/{g.id}/arm").status_code == 204
    assert 'data-armed="1"' in lane1.get(f"/partials/lane/{g.id}/1").text

    body = {"heat_id": heat.id, "elapsed_ms": 5432.1, "submission_id": "abcd-1"}
    r = lane1.post(f"/api/track/{g.id}/lane/1/time", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["time"] == 5.432
    assert lane1.post(f"/api/track/{g.id}/lane/1/time", json=body).json()["duplicate"]
    assert lane1.post(f"/api/track/{g.id}/lane/2/time", json={**body, "submission_id": "x123"}).status_code == 403
    # the operator can time lane 2 (e.g. when short a volunteer)
    r = admin.post(f"/api/track/{g.id}/lane/2/time", json={"heat_id": heat.id, "elapsed_ms": None, "submission_id": "abcd-2"})
    assert r.status_code == 200
    assert heat_status(app, heat.id) == HeatStatus.pending

    tk = admin.get("/partials/timekeeper").text
    assert "5.432" in tk and "DNF" in tk

    r = admin.post(f"/api/heat/{heat.id}/edit", data={"lane": 2, "time": "9.5", "reason": ""})
    assert r.status_code == 400 and "reason" in r.json()["error"]
    r = admin.post(f"/api/heat/{heat.id}/edit", data={"lane": 2, "time": "9.5", "reason": "saw it finish"})
    assert r.status_code == 204
    assert admin.post(f"/api/heat/{heat.id}/approve").status_code == 204
    assert heat_status(app, heat.id) == HeatStatus.approved
    assert "9.500" in admin.get(f"/partials/event/{g.event_id}").text
    assert "saw it finish" in admin.get("/admin/audit").text


def test_derby_flow_with_simulator_and_rerun(app, admin):
    t = track(app, "derby")
    admin.post(f"/api/track/{t.id}/stage")
    heat = current_heat(app, t.id)
    admin.post(f"/api/track/{t.id}/arm")
    for _ in range(200):
        if heat_status(app, heat.id) == HeatStatus.pending:
            break
        time.sleep(0.01)
    assert heat_status(app, heat.id) == HeatStatus.pending
    page = admin.get("/partials/timekeeper").text  # pending heats belong to the head timekeeper
    assert "Re-run because" in page and "Car jumped into another lane" in page  # derby reasons
    r = admin.post(f"/api/heat/{heat.id}/rerun", data={"reason": ""}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "reason" in r.json()["error"]
    r = admin.post(f"/api/heat/{heat.id}/rerun", data={"reason": "__other", "reason_other": "lane 3 jumped"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert heat_status(app, heat.id) == HeatStatus.rerun
    assert "lane 3 jumped" in admin.get("/admin/audit").text
    # the replacement is next in the queue
    admin.post(f"/api/track/{t.id}/stage")
    new = current_heat(app, t.id)
    assert new.rerun_of_id == heat.id


def test_manual_entry_validation(app, admin):
    t = track(app, "derby")
    assert admin.post(f"/api/track/{t.id}/manual", data={}).status_code == 409  # nothing staged
    admin.post(f"/api/track/{t.id}/stage")
    r = admin.post(f"/api/track/{t.id}/manual", data={"lane_1": "3.1", "lane_2": "abc", "lane_3": "3.2", "lane_4": "3.3"})
    assert r.status_code == 400 and "not a time" in r.json()["error"]
    r = admin.post(f"/api/track/{t.id}/manual", data={"lane_1": "3.1", "lane_2": "dnf", "lane_3": "3.2", "lane_4": "1.0"})
    assert r.status_code == 204
    tk = admin.get("/partials/timekeeper").text
    assert "outside expected range" in tk  # 1.0 < min_time


def test_websocket_receives_events(app, admin):
    t = track(app, "derby")
    with admin.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        admin.post(f"/api/track/{t.id}/stage")
        seen = set()
        for _ in range(30):
            msg = ws.receive_json()
            seen.add(msg["type"])
            if {"heat.staged", "track.state", "alert"} <= seen:
                break
        assert {"heat.staged", "track.state", "alert"} <= seen


# --- admin setup ------------------------------------------------------------------------


def test_admin_event_setup_end_to_end(app, admin):
    r = admin.post("/admin/events", data={"name": "Pack Derby", "type": "derby"}, follow_redirects=False)
    assert r.status_code == 303
    ev_url = r.headers["location"].split("?")[0]
    ev_id = int(ev_url.rsplit("/", 1)[1])
    csv = "first_name,last_name,den\n" + "\n".join(f"Kid{i},X,Lion Den" for i in range(6))
    r = admin.post(f"/admin/event/{ev_id}/import", data={"csv_text": csv, "checked_in": "on"})
    assert "Roster import preview" in r.text and "6</strong> new entries" in r.text  # nothing written yet
    with Session(app.state.engine) as sess:
        assert not sess.exec(select(Entry).where(Entry.event_id == ev_id)).first()
    r = admin.post(f"/admin/event/{ev_id}/import", data={"csv_text": csv, "checked_in": "on", "apply": "1"}, follow_redirects=False)
    assert "Imported" in r.headers["location"]
    r = admin.post(f"/admin/event/{ev_id}/schedule", follow_redirects=False)
    assert "generated" in r.headers["location"]
    page = admin.get(ev_url).text
    assert "Lion Den" in page and "Create final" in page
    r = admin.post(f"/admin/event/{ev_id}/final", follow_redirects=True)
    assert "not approved yet" in r.text
    # new event's track is live on the public page
    assert "Track 1" in admin.get("/partials/home").text


def race_order_ids(app, event_type):
    from racemgr.services.schedule import group_rounds

    with Session(app.state.engine) as s:
        ev = s.exec(select(Event).where(Event.type == event_type)).first()
        return ev.id, [(r.id, r.name) for r in group_rounds(s, ev.id)]


def test_admin_race_order_controls(app, admin):
    ev_id, order = race_order_ids(app, "derby")
    page = admin.get(f"/admin/event/{ev_id}").text
    assert 'id="race-order"' in page and "Sortable.min.js" in page
    assert page.index("Bear Den 5 · run 2") > page.index("Wolf Den 3 · run 2")

    # drag-and-drop save (JSON)
    new = [i for i, _ in reversed(order)]
    r = admin.post(f"/admin/event/{ev_id}/order", json={"round_ids": new})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [i for i, _ in race_order_ids(app, "derby")[1]] == new
    # the operator's queue follows the new order immediately
    t = track(app, "derby")
    assert "Bear Den 5 · run 2 · heat 1" in admin.get(f"/partials/operate/{t.id}").text
    # stale list is rejected as JSON, not an HTML page
    r = admin.post(f"/admin/event/{ev_id}/order", json={"round_ids": new[:2]})
    assert r.status_code == 409 and "Reload" in r.json()["error"]

    admin.post(f"/admin/event/{ev_id}/reverse")
    assert [i for i, _ in race_order_ids(app, "derby")[1]] == [i for i, _ in order]

    with Session(app.state.engine) as s:
        from racemgr.models import Group
        wolf = s.exec(select(Group).where(Group.name == "Wolf Den 3")).one()
    r = admin.post(f"/admin/event/{ev_id}/passes", data={"group_id": wolf.id}, follow_redirects=False)
    assert "Run%20added" in r.headers["location"]
    _, after = race_order_ids(app, "derby")
    assert after[-1][1] == "Wolf Den 3 · run 3"
    r = admin.post(f"/admin/round/{after[-1][0]}/remove", follow_redirects=False)
    assert "Run%20removed" in r.headers["location"]
    assert len(race_order_ids(app, "derby")[1]) == 4


def test_admin_subpages_count_as_own_page(app, admin):
    bar = role_bar(admin.get("/admin/event/1").text)
    assert "Back to my page" not in bar and "Set up events" in bar


def test_race_order_is_admin_only(app):
    ev_id, order = race_order_ids(app, "derby")
    c = other_client(app)
    join(app, c, Role.head_timekeeper)
    assert c.post(f"/admin/event/{ev_id}/order", json={"round_ids": [i for i, _ in order]}).status_code == 403


def test_checkin(app):
    c = other_client(app)
    join(app, c, Role.checkin)
    page = c.get("/checkin").text
    assert "Liam" in page
    r = c.post("/api/entry/1/checkin", data={"weight_oz": "4.9", "inspection_status": "passed"})
    assert r.status_code == 200 and "saved" in r.text and "4.9" in r.text
    assert c.post("/api/entry/1/checkin", data={"weight_oz": "heavy"}).status_code == 400


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64


def test_checkin_photos(app, client):
    c = other_client(app)
    join(app, c, Role.checkin)
    photos = app.state.settings.data_dir / "photos"
    assert 'capture="environment"' in c.get("/checkin").text

    r = c.post("/api/entry/1/photo/entry", files={"photo": ("car.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 200, r.text
    first = r.json()["url"]
    assert c.get(first).content == JPEG
    assert len(list(photos.iterdir())) == 1
    assert "/api/entry/1/photo/entry?v=" in c.get("/checkin").text
    # scouts themselves aren't photographed
    assert c.post("/api/entry/1/photo/scout", files={"photo": ("s.jpg", JPEG, "image/jpeg")}).status_code == 404
    # the car shows on the public racer page
    with Session(app.state.engine) as s_:
        racer_id = s_.get(Entry, 1).racer_id
    public = client.get(f"/partials/racer/{racer_id}").text
    assert "/api/entry/1/photo/entry?v=" in public
    assert client.get("/api/entry/1/photo/entry").content == JPEG

    # retake replaces the old file; delete removes it
    c.post("/api/entry/1/photo/entry", files={"photo": ("car.jpg", JPEG, "image/jpeg")})
    assert len(list(photos.iterdir())) == 1
    assert c.post("/api/entry/1/photo/entry/delete").json() == {"url": None}
    assert c.get("/api/entry/1/photo/entry").status_code == 404
    assert len(list(photos.iterdir())) == 0

    # only images; only check-in devices upload
    assert c.post("/api/entry/1/photo/entry", files={"photo": ("x.jpg", b"<html>", "image/jpeg")}).status_code == 400
    assert c.post("/api/entry/1/photo/other", files={"photo": ("x.jpg", JPEG, "image/jpeg")}).status_code == 404
    assert client.post("/api/entry/1/photo/entry", files={"photo": ("x.jpg", JPEG, "image/jpeg")}).status_code == 403
    assert client.get("/api/entry/1/photo/entry").status_code == 404  # deleted above


def test_rerun_dropdown_uses_event_reasons(app, admin):
    import time as _time

    g = track(app, "regatta")
    admin.post(f"/api/track/{g.id}/stage")
    heat = current_heat(app, g.id)
    assert "Re-run because" not in admin.get(f"/partials/operate/{g.id}").text  # staged: not started
    admin.post(f"/api/track/{g.id}/arm")
    assert "Re-run because" not in admin.get(f"/partials/operate/{g.id}").text  # armed, no START yet
    r = admin.post(f"/api/heat/{heat.id}/rerun", data={"reason": "False start"}, headers={"HX-Request": "true"})
    assert r.status_code == 409 and "hasn't started" in r.json()["error"]

    admin.post(f"/api/track/{g.id}/start", json={"heat_id": heat.id, "start_ms": _time.time() * 1000})
    page = admin.get(f"/partials/operate/{g.id}").text
    assert "Boat tipped over or sank" in page and "Car left the track" not in page
    r = admin.post(f"/api/heat/{heat.id}/rerun", data={"reason": "Boat tipped over or sank"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert "Boat tipped over or sank" in admin.get("/admin/audit").text
    # same boats back on the track, staged and disarmed
    new = current_heat(app, g.id)
    assert new.rerun_of_id == heat.id and new.status == HeatStatus.staged and new.start_ms is None


def test_disarm_button_goes_back_to_staged(app, admin):
    g = track(app, "regatta")
    admin.post(f"/api/track/{g.id}/stage")
    admin.post(f"/api/track/{g.id}/arm")
    page = admin.get(f"/partials/operate/{g.id}").text
    assert f'hx-post="/api/track/{g.id}/disarm"' in page
    assert admin.post(f"/api/track/{g.id}/disarm").status_code == 204
    assert current_heat(app, g.id).status == HeatStatus.staged
    assert admin.post(f"/api/track/{g.id}/disarm").status_code == 409  # not armed any more


def test_admin_regenerate_and_lane_switches(app, admin):
    t = track(app, "derby")
    ev_id = t.event_id
    page = admin.get(f"/admin/event/{ev_id}").text
    assert "Regenerate remaining heats" in page and 'name="lane_on"' in page

    r = admin.post(f"/admin/event/{ev_id}/regenerate", data={"reason": "late arrivals"}, follow_redirects=False)
    assert "Schedule%20rebuilt" in r.headers["location"]

    # lane 2 breaks: untick it -> unraced heats are rebuilt without lane 2
    form = {"name": t.name, "lane_count": 4, "timing_source": "simulator", "min_time": 2, "max_time": 6,
            "active": "on", "lanes_shown": "1", "lane_on": ["1", "3", "4"]}
    r = admin.post(f"/admin/track/{t.id}", data=form, follow_redirects=False)
    assert "rescheduled" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert s.get(Track, t.id).disabled_lanes == [2]
        lanes = {hl.lane for hl in s.exec(select(HeatLane).join(Heat).where(Heat.track_id == t.id, Heat.status == HeatStatus.scheduled))}
    assert 2 not in lanes and lanes == {1, 3, 4}
    assert "Out of service: lane 2" in admin.get(f"/admin/event/{ev_id}").text

    # saving without a capacity change doesn't touch the schedule
    r = admin.post(f"/admin/track/{t.id}", data={**form, "name": "Main Track!"}, follow_redirects=False)
    assert "rescheduled" not in r.headers["location"]


def test_out_of_date_warning_after_late_checkin(app, admin):
    t = track(app, "derby")
    r = admin.post(f"/admin/event/{t.event_id}/import", data={"csv_text": "first_name,last_name,den\nLate,Kid,Wolf Den 3", "checked_in": "on", "apply": "1"})
    page = admin.get(f"/admin/event/{t.event_id}").text
    assert "The schedule is out of date" in page and "Late Kid" in page
    admin.post(f"/admin/event/{t.event_id}/regenerate")
    assert "The schedule is out of date" not in admin.get(f"/admin/event/{t.event_id}").text


# --- event assignment ------------------------------------------------------------------


def test_join_codes_carry_their_event(admin, app):
    g = track(app, "regatta")
    with Session(app.state.engine) as s:
        derby = s.exec(select(Event).where(Event.type == "derby")).one()
        regatta = s.get(Event, g.event_id)
    # non-admin roles must pick an event; a track implies its own event
    r = admin.post("/admin/tokens", data={"role": "checkin"})
    assert "Choose the event" in r.text
    r = admin.post("/admin/tokens", data={"role": "track_operator", "event_id": derby.id, "track_id": g.id})
    assert "different event" in r.text
    admin.post("/admin/tokens", data={"role": "head_timekeeper", "event_id": derby.id})
    with Session(app.state.engine) as s:
        jt = s.exec(select(JoinToken).where(JoinToken.role == Role.head_timekeeper)).one()
        assert jt.event_id == derby.id
        token = jt.token
    c = other_client(app)
    assert f"Event: <strong>{derby.name}</strong>" in c.get(f"/join/{token}").text
    c.post(f"/join/{token}")
    assert derby.name in role_bar(c.get("/timekeeper").text)
    assert derby.name in c.get("/me").text
    page = admin.get("/admin/officials").text
    assert derby.name in page.split("Joined devices")[1]
    # a lane official's code is tied to the track's event
    _, _ = join(app, other_client(app), Role.lane_official, g.id, 1)
    with Session(app.state.engine) as s:
        o = s.exec(select(Official).where(Official.role == Role.lane_official)).one()
        assert o.event_id == regatta.id


def test_finished_events_leave_the_track_picker(admin, app):
    g = track(app, "regatta")
    with Session(app.state.engine) as s:
        ev = s.get(Event, g.event_id)
        ev.status = "done"
        s.add(ev)
        s.commit()
        name, ev_id = ev.name, ev.id
    form = admin.get("/admin/officials").text.split("Active codes")[0].split("Joined devices")[0]
    assert f'value="{g.id}"' not in form and f'value="{ev_id}"' not in form and name not in form
    r = admin.post("/admin/tokens", data={"role": "checkin", "event_id": ev_id})
    assert "is finished" in r.text


def test_event_bound_officials_only_see_their_event(app):
    d, g = track(app, "derby"), track(app, "regatta")
    op = other_client(app)
    join(app, op, Role.track_operator, event_id=g.event_id)
    page = op.get("/operate").text
    assert f"/track/{g.id}/operate" in page and f"/track/{d.id}/operate" not in page
    ci = other_client(app)
    join(app, ci, Role.checkin, event_id=d.event_id)
    html = ci.get(f"/checkin?event={g.event_id}").text
    assert '<select name="event"' not in html


def test_page_cache_renders_once_for_concurrent_viewers():
    import threading
    from racemgr.cache import PageCache

    cache, calls = PageCache(), []

    def render():
        calls.append(1)
        time.sleep(0.05)
        return "page"

    threads = [threading.Thread(target=cache.get, args=("k", render)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    cache.bump()  # data changed: next viewer re-renders
    assert cache.get("k", render) == "page" and len(calls) == 2


def test_public_partials_cached_until_data_changes(app, client):
    cache = app.state.page_cache
    client.get("/partials/home")
    v = cache.version
    assert "/partials/home?" in cache._entries
    app.state.bus.publish("heat.approved")
    assert cache.version > v
    v = cache.version
    client.post("/logout")  # any write request
    assert cache.version > v


def test_officials_bypass_public_cache(app, client):
    join(app, client, Role.admin)
    client.get("/partials/home")
    assert "/partials/home?" not in app.state.page_cache._entries
