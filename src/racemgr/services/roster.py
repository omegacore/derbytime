"""Roster import and entry helpers.

Takes the pack's roster export as CSV or tab-separated text. Columns used
(header row required, case-insensitive):

    Scout First Name   required
    Scout Last Name
    Den                each den gets its own runs
    Status             if present, only rows marked "Active" are imported

Every other column (Member ID, parent contacts, DOB, dues, …) is ignored and
never stored. Optional extras: ``number`` (car/boat number, otherwise
assigned) and ``car_name``. Short headers like ``first_name`` also work.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlmodel import Session, func, select

from ..models import Entry, Event, Group, InspectionStatus, Racer
from . import Actor, ServiceError
from .audit import audit

ALIASES = {
    "scout first name": "first_name", "first": "first_name", "firstname": "first_name", "first name": "first_name",
    "scout last name": "last_name", "last": "last_name", "lastname": "last_name", "last name": "last_name",
    "group": "den", "den name": "den",
    "car": "car_name", "boat": "car_name", "boat_name": "car_name", "boat name": "car_name", "car name": "car_name",
    "car number": "number", "car_number": "number", "boat number": "number", "#": "number",
}


def _dialect(text: str) -> str:
    """Delimiter from the header line: spreadsheet exports may be CSV or tab-separated."""
    header = text.split("\n", 1)[0]
    return max(("\t", ",", ";"), key=header.count)


def get_or_create_group(session: Session, name: str, rank: str | None = None) -> Group:
    g = session.exec(select(Group).where(Group.name == name)).first()
    if not g:
        order = session.exec(select(func.count()).select_from(Group)).one()
        g = Group(name=name, rank=rank or None, sort_order=order)
        session.add(g)
        session.flush()
    return g


def next_number(session: Session, event_id: int) -> int:
    return (session.exec(select(func.max(Entry.number)).where(Entry.event_id == event_id)).one() or 100) + 1


def add_entry(
    session: Session,
    event: Event,
    racer: Racer,
    number: int | None = None,
    name: str | None = None,
    checked_in: bool = False,
) -> Entry:
    existing = session.exec(select(Entry).where(Entry.event_id == event.id, Entry.racer_id == racer.id)).first()
    if existing:
        return existing
    if number is None:
        number = next_number(session, event.id)
    elif session.exec(select(Entry).where(Entry.event_id == event.id, Entry.number == number)).first():
        raise ServiceError(f"Number {number} is already used in this event.")
    e = Entry(
        event_id=event.id,
        racer_id=racer.id,
        number=number,
        name=name or None,
        checked_in=checked_in,
        inspection_status=InspectionStatus.passed if checked_in else InspectionStatus.pending,
    )
    session.add(e)
    session.flush()
    return e


@dataclass
class ImportResult:
    imported: int
    skipped_inactive: int = 0

    @property
    def message(self) -> str:
        msg = f"Imported {self.imported} scout{'' if self.imported == 1 else 's'}."
        if self.skipped_inactive:
            msg += f" Skipped {self.skipped_inactive} not marked Active."
        return msg


def import_csv(session: Session, event: Event, text: str, actor: Actor, checked_in: bool = False) -> ImportResult:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter=_dialect(text))
    if not reader.fieldnames:
        raise ServiceError("The roster file is empty.")

    def norm_key(k: str) -> str:
        key = " ".join(k.split()).lower()
        return ALIASES.get(key, key.replace(" ", "_"))

    if "first_name" not in {norm_key(k) for k in reader.fieldnames if k}:
        raise ServiceError("Couldn't find a “Scout First Name” column. Check the header row of the roster export.")
    has_status = "status" in {norm_key(k) for k in reader.fieldnames if k}
    count = skipped = 0
    for lineno, row in enumerate(reader, start=2):
        norm = {norm_key(k): (v or "").strip() for k, v in row.items() if k is not None and isinstance(v, str)}
        first = norm.get("first_name", "")
        if not first:
            continue
        # the roster export lists former scouts too; only Active ones race
        if has_status and norm.get("status", "").casefold() != "active":
            skipped += 1
            continue
        last = norm.get("last_name", "")
        den = " ".join(norm.get("den", "").split()) or "Unassigned"
        group = get_or_create_group(session, den)
        racer = session.exec(
            select(Racer).where(Racer.first_name == first, Racer.last_name == last, Racer.group_id == group.id)
        ).first()
        if not racer:
            racer = Racer(first_name=first, last_name=last, group_id=group.id)
            session.add(racer)
            session.flush()
        num = norm.get("number")
        try:
            number = int(num) if num else None
        except ValueError:
            raise ServiceError(f"Line {lineno}: number {num!r} is not a whole number.")
        add_entry(session, event, racer, number, norm.get("car_name"), checked_in)
        count += 1
    audit(session, actor, "roster.import", "event", event.id, after={"rows": count, "skipped_inactive": skipped})
    return ImportResult(count, skipped)
