"""`derbytime seed-demo`: a demo derby and regatta for two dens of ten scouts."""
from __future__ import annotations

from sqlmodel import Session

from .config import Settings
from .models import Event, EventType, Racer
from .services import Actor
from .services.roster import add_entry, get_or_create_group
from .services.schedule import generate_group_rounds
from .services.setup import add_track, create_event

WOLVES = ["Liam", "Noah", "Oliver", "Elijah", "James", "Aiden", "Lucas", "Mason", "Ethan", "Logan"]
BEARS = ["Ava", "Mia", "Leo", "Owen", "Caleb", "Ezra", "Nora", "Hudson", "Isla", "Theo"]
LAST = ["Garcia", "Smith", "Nguyen", "Patel", "Brown", "Lee", "Johnson", "Kim", "Lopez", "Walker"]
CARS = ["Blue Lightning", "Turbo", "Rocket", "Comet", "Flash", "Thunder", "Zoom", "Bolt", "Viper", "Nitro"]
BOATS = ["Sea Biscuit", "Wave Rider", "Splash", "Driftwood", "Minnow", "Skipper", "Tidal", "Gull", "Puffin", "Anchor"]


def seed_demo(session: Session, settings: Settings) -> tuple[Event, Event]:
    actor = Actor.system("seed-demo")
    wolf = get_or_create_group(session, "Wolf Den 3", "Wolf")
    bear = get_or_create_group(session, "Bear Den 5", "Bear")
    racers = []
    for names, group in ((WOLVES, wolf), (BEARS, bear)):
        for i, first in enumerate(names):
            r = Racer(first_name=first, last_name=LAST[i], group_id=group.id)
            session.add(r)
            racers.append(r)
    session.flush()

    derby = create_event(session, settings, "Demo Pinewood Derby", EventType.derby, actor)
    add_track(session, settings, derby, "Main Track", actor)
    regatta = create_event(session, settings, "Demo Rain Gutter Regatta", EventType.regatta, actor)
    add_track(session, settings, regatta, "Gutter A", actor)
    add_track(session, settings, regatta, "Gutter B", actor)

    for i, r in enumerate(racers):
        add_entry(session, derby, r, 101 + i, CARS[i % 10], checked_in=True)
        add_entry(session, regatta, r, 201 + i, BOATS[i % 10], checked_in=True)

    generate_group_rounds(session, derby, actor, settings.type_defaults("derby").passes_per_den)
    generate_group_rounds(session, regatta, actor, settings.type_defaults("regatta").passes_per_den)
    session.commit()
    return derby, regatta
