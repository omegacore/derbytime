"""Lane labels: a track can call its lanes anything (e.g. gutter A has lanes
1, 2 and gutter B lanes 3, 4) while heats still use lane numbers, so the den
runs keep one shared queue across the tracks."""
from sqlmodel import Session, select

from derbytime.models import Heat, HeatStatus, Role, Track
from derbytime.routes.admin import parse_lane_labels
from derbytime.services import views
from derbytime.services.schedule import generate_group_rounds
from derbytime.services.setup import add_track

from test_services import ACTOR, make_event_with_dens, tracks_of
from test_web import admin, app, client, join, track  # noqa: F401  (fixtures)


def split_gutters(session, settings):
    """Two 2-lane tracks, the second labelled 3, 4."""
    ev = make_event_with_dens(session, settings, ["Den 1", "Den 2"], per_den=6, lanes=2)
    add_track(session, settings, ev, "B", ACTOR, lane_count=2)
    a, b = tracks_of(session, ev)
    b.lane_labels = ["3", "4"]
    session.add(b)
    generate_group_rounds(session, ev, ACTOR, passes=1)
    session.commit()
    return ev, a, b


def test_lane_label_defaults_to_the_number():
    t = Track(event_id=1, name="T", lane_count=3, timing_source="manual", min_time=1, max_time=9)
    assert [t.lane_label(n) for n in (1, 2, 3)] == ["1", "2", "3"]
    t.lane_labels = ["Red", "", "Blue"]
    assert [t.lane_label(n) for n in (1, 2, 3)] == ["Red", "2", "Blue"]


def test_parse_lane_labels():
    assert parse_lane_labels("3, 4", 2) == ["3", "4"]
    assert parse_lane_labels("", 2) is None
    assert parse_lane_labels("1,2", 2) is None  # same as the numbers
    assert parse_lane_labels("5", 2) == ["5", "2"]  # blanks keep the number
    assert "different" in parse_lane_labels("3, 3", 2)
    assert "lane labels" in parse_lane_labels("1,2,3", 2)
    assert "short" in parse_lane_labels("Lane-number-one", 1)


def test_labels_keep_the_shared_queue(settings, session):
    ev, a, b = split_gutters(session, settings)
    plan = views.plan_all(session)
    # both tracks list the same den heats in the same order
    assert [h.id for h in plan[a.id]] == [h.id for h in plan[b.id]]
    hv = views.heat_view(session, plan[a.id][0])
    assert hv["shared_queue"]
    assert [(l["lane"], l["lane_label"], l["lane_fixed"]) for l in hv["lanes"]] == [(1, "1/3", False), (2, "2/4", False)]
    # once B stages it, the lanes are B's
    heat = session.get(Heat, plan[a.id][0].id)
    heat.status, heat.track_id = HeatStatus.staged, b.id
    session.add(heat)
    session.commit()
    hv = views.heat_view(session, heat)
    assert [(l["lane_label"], l["lane_fixed"]) for l in hv["lanes"]] == [("3", True), ("4", True)]
    assert views.lane_heads([hv, views.heat_view(session, plan[a.id][1])]) == ["1/3", "2/4"]


def test_same_labels_on_every_track_are_fixed(settings, session):
    ev, a, b = split_gutters(session, settings)
    b.lane_labels = None
    session.add(b)
    session.commit()
    hv = views.heat_view(session, views.plan_all(session)[a.id][0])
    assert [(l["lane_label"], l["lane_fixed"]) for l in hv["lanes"]] == [("1", True), ("2", True)]


def test_ondeck_shows_a_heat_per_track_without_naming_one(settings, session, engine):
    ev, a, b = split_gutters(session, settings)
    board = views.ondeck_board(session)
    assert len(board["on_deck"]) == 2 and len(board["in_hole"]) == 2
    from derbytime.app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, engine, print_admin_link=False)) as c:
        html = c.get("/partials/ondeck").text
    deck = html.split("ON DECK", 1)[1]
    assert "Next free track" not in deck and "od-chip" not in deck
    assert "LANE" not in deck  # 1 on A is 3 on B: the stager picks the lane
    # B's lanes on B's own board
    with TestClient(create_app(settings, engine, print_admin_link=False)) as c:
        own = c.get(f"/partials/ondeck/{b.id}").text.split("ON DECK", 1)[1]
    assert "<small>LANE</small>3" in own and "<small>LANE</small>1<" not in own


def test_admin_sets_lane_labels(app, admin):  # noqa: F811
    t = track(app, "regatta", 1)
    form = {"name": t.name, "lane_count": t.lane_count, "timing_source": t.timing_source, "min_time": t.min_time,
            "max_time": t.max_time, "active": "on", "lane_labels": "3, 4"}
    r = admin.post(f"/admin/track/{t.id}", data=form, follow_redirects=False)
    assert "saved" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert s.get(Track, t.id).lane_labels == ["3", "4"]
        heats = s.exec(select(Heat).where(Heat.track_id == t.id)).all()
    page = admin.get(f"/admin/event/{t.event_id}").text
    assert 'value="3, 4"' in page
    assert "Lane 1/3" in admin.get(f"/partials/event/{t.event_id}").text or not heats
    r = admin.post(f"/admin/track/{t.id}", data={**form, "lane_labels": "3, 3"}, follow_redirects=False)
    assert "different" in r.headers["location"]
    # a lane official's code names the lane by its label
    _, home = join(app, admin, Role.lane_official, t.id, 1, event_id=t.event_id)
    assert "Lane 3" in admin.get(home).text
