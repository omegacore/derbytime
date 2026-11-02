"""Roster import/sync, roster editing and mid-event entry status.

Takes the pack's roster export as CSV or tab-separated text. Columns used
(header row required, case-insensitive):

    Scout First Name   required
    Scout Last Name
    Member ID          stable key: re-imports match on it, so den moves and
                       name fixes update the scout instead of duplicating them
    Den                each den gets its own runs
    Status             if present, only rows marked "Active" race

Every other column (parent contacts, DOB, dues, …) is ignored and never
stored. Optional extras: ``number`` (car/boat number, otherwise assigned) and
``car_name``. Short headers like ``first_name`` also work.

Importing is a sync: ``plan_import`` compares the file with the database and
``apply_import`` carries the plan out, so the admin can preview it first.
Scouts are never deleted by an import; ones no longer Active are marked
inactive (and stop being scheduled) until a later roster lists them again.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, col, func, select

from ..models import Entry, EntryStatus, Event, Group, Heat, HeatLane, HeatStatus, InspectionStatus, Racer, utcnow
from . import Actor, ServiceError
from .audit import audit

ALIASES = {
    "scout first name": "first_name", "first": "first_name", "firstname": "first_name", "first name": "first_name",
    "scout last name": "last_name", "last": "last_name", "lastname": "last_name", "last name": "last_name",
    "member id": "member_id", "memberid": "member_id", "bsa id": "member_id", "bsa member id": "member_id",
    "group": "den", "den name": "den",
    "car": "car_name", "boat": "car_name", "boat_name": "car_name", "boat name": "car_name", "car name": "car_name",
    "car number": "number", "car_number": "number", "boat number": "number", "#": "number",
}


def _dialect(text: str) -> str:
    """Delimiter from the header line: spreadsheet exports may be CSV or tab-separated."""
    header = text.split("\n", 1)[0]
    return max(("\t", ",", ";"), key=header.count)


def _clean(s: str) -> str:
    return " ".join(s.split())


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


# --- import / sync -----------------------------------------------------------


@dataclass
class RosterRow:
    line: int
    first: str
    last: str
    den: str
    member_id: str | None
    active: bool
    number: int | None
    car_name: str | None
    racer_id: int | None = None
    kind: str = "new"  # new | update | reactivate | deactivate | same | skip
    changes: list[str] = field(default_factory=list)
    enter: bool = False  # gets an entry in this event

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass
class ImportPlan:
    rows: list[RosterRow]
    has_status: bool
    # active scouts entered in this event that the file doesn't list
    not_in_file: list[str] = field(default_factory=list)

    def of(self, *kinds: str) -> list[RosterRow]:
        return [r for r in self.rows if r.kind in kinds]

    @property
    def entering(self) -> list[RosterRow]:
        return [r for r in self.rows if r.enter]

    @property
    def changed(self) -> bool:
        return bool(self.entering or self.of("new", "update", "reactivate", "deactivate"))


@dataclass
class ImportResult:
    imported: int
    skipped_inactive: int = 0
    updated: int = 0
    deactivated: int = 0
    reactivated: int = 0

    @property
    def message(self) -> str:
        msg = f"Imported {self.imported} scout{'' if self.imported == 1 else 's'}."
        if self.updated:
            msg += f" Updated {self.updated}."
        if self.reactivated:
            msg += f" {self.reactivated} active again."
        if self.deactivated:
            msg += f" Marked {self.deactivated} inactive."
        if self.skipped_inactive:
            msg += f" Skipped {self.skipped_inactive} not marked Active."
        return msg


def _parse(text: str) -> tuple[list[RosterRow], bool]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text), delimiter=_dialect(text))
    if not reader.fieldnames:
        raise ServiceError("The roster file is empty.")

    def norm_key(k: str) -> str:
        key = _clean(k).lower()
        return ALIASES.get(key, key.replace(" ", "_"))

    keys = {norm_key(k) for k in reader.fieldnames if k}
    if "first_name" not in keys:
        raise ServiceError("Couldn't find a “Scout First Name” column. Check the header row of the roster export.")
    has_status = "status" in keys
    rows = []
    for lineno, raw in enumerate(reader, start=2):
        norm = {norm_key(k): (v or "").strip() for k, v in raw.items() if k is not None and isinstance(v, str)}
        first = _clean(norm.get("first_name", ""))
        if not first:
            continue
        num = norm.get("number")
        try:
            number = int(num) if num else None
        except ValueError:
            raise ServiceError(f"Line {lineno}: number {num!r} is not a whole number.")
        rows.append(
            RosterRow(
                line=lineno,
                first=first,
                last=_clean(norm.get("last_name", "")),
                den=_clean(norm.get("den", "")) or "Unassigned",
                member_id=norm.get("member_id") or None,
                # the roster export lists former scouts too; only Active ones race
                active=not has_status or norm.get("status", "").casefold() == "active",
                number=number,
                car_name=norm.get("car_name") or None,
            )
        )
    return rows, has_status


def plan_import(session: Session, event: Event, text: str) -> ImportPlan:
    """Compare a roster file with the database. Writes nothing."""
    rows, has_status = _parse(text)
    racers = list(session.exec(select(Racer)))
    groups = {g.id: g.name for g in session.exec(select(Group))}
    by_mid = {r.member_id: r for r in racers if r.member_id}
    by_name: dict[tuple[str, str], list[Racer]] = defaultdict(list)
    for r in racers:
        by_name[(r.first_name.casefold(), r.last_name.casefold())].append(r)
    entered = set(session.exec(select(Entry.racer_id).where(Entry.event_id == event.id)))
    claimed: set[int] = set()

    def match(row: RosterRow) -> Racer | None:
        if row.member_id and row.member_id in by_mid:
            return by_mid[row.member_id]
        # without a Member ID match: same name, and not a different Member ID
        same = [
            r for r in by_name[(row.first.casefold(), row.last.casefold())]
            if r.id not in claimed and (not r.member_id or r.member_id == row.member_id)
        ]
        in_den = [r for r in same if groups.get(r.group_id) == row.den]
        if in_den:
            return in_den[0]
        return same[0] if len(same) == 1 else None  # one scout of that name: they moved dens

    for row in rows:
        racer = match(row)
        if racer is None or racer.id in claimed:
            row.kind = "new" if row.active else "skip"
            row.enter = row.active
            continue
        claimed.add(racer.id)
        row.racer_id = racer.id
        if (racer.first_name, racer.last_name) != (row.first, row.last):
            row.changes.append(f"name {racer.display_name} → {row.name}")
        old_den = groups.get(racer.group_id, "")
        if old_den != row.den:
            row.changes.append(f"den {old_den or '–'} → {row.den}")
        if row.member_id and racer.member_id != row.member_id:
            row.changes.append("Member ID added" if not racer.member_id else f"Member ID {racer.member_id} → {row.member_id}")
        if row.active and not racer.active:
            row.kind = "reactivate"
        elif not row.active and racer.active:
            row.kind = "deactivate"
        elif not row.active:
            row.kind = "skip"
        else:
            row.kind = "update" if row.changes else "same"
        row.enter = row.active and racer.id not in entered

    numbers = [r.number for r in rows if r.enter and r.number is not None]
    used = set(session.exec(select(Entry.number).where(Entry.event_id == event.id)))
    for row in rows:
        if row.enter and row.number is not None and (row.number in used or numbers.count(row.number) > 1):
            raise ServiceError(f"Line {row.line}: number {row.number} is already used in this event.")

    listed = {r.racer_id for r in rows if r.racer_id}
    missing = session.exec(
        select(Entry, Racer).join(Racer, Racer.id == Entry.racer_id).where(Entry.event_id == event.id).order_by(Entry.number)
    ).all()
    not_in_file = [f"#{e.number} {r.display_name}" for e, r in missing if r.id not in listed and r.active]
    return ImportPlan(rows, has_status, not_in_file)


def apply_import(session: Session, event: Event, plan: ImportPlan, actor: Actor, checked_in: bool = False) -> ImportResult:
    now = utcnow()
    result = ImportResult(0)
    for row in plan.rows:
        if row.kind == "skip" and not row.changes:
            result.skipped_inactive += row.racer_id is None
            continue
        group = get_or_create_group(session, row.den)
        if row.racer_id is None:
            racer = Racer(first_name=row.first, last_name=row.last, group_id=group.id, member_id=row.member_id)
            session.add(racer)
            session.flush()
        else:
            racer = session.get(Racer, row.racer_id)
            before = racer_snapshot(session, racer)
            racer.first_name, racer.last_name, racer.group_id = row.first, row.last, group.id
            racer.member_id = row.member_id or racer.member_id
            if row.kind == "reactivate":
                racer.inactive_since = None
                result.reactivated += 1
            elif row.kind == "deactivate":
                racer.inactive_since = now
                result.deactivated += 1
            elif row.changes:
                result.updated += 1
            session.add(racer)
            if row.changes or row.kind in ("reactivate", "deactivate"):
                audit(session, actor, "racer.sync", "racer", racer.id, before=before, after=racer_snapshot(session, racer))
        if row.enter:
            add_entry(session, event, racer, row.number, row.car_name, checked_in)
            result.imported += 1
    audit(
        session, actor, "roster.import", "event", event.id,
        after={"rows": result.imported, "updated": result.updated, "deactivated": result.deactivated,
               "reactivated": result.reactivated, "skipped_inactive": result.skipped_inactive},
    )
    return result


def import_csv(session: Session, event: Event, text: str, actor: Actor, checked_in: bool = False) -> ImportResult:
    """Plan and apply in one step (CLI, tests)."""
    return apply_import(session, event, plan_import(session, event, text), actor, checked_in)


# --- editing the roster ------------------------------------------------------


def racer_snapshot(session: Session, racer: Racer) -> dict:
    g = session.get(Group, racer.group_id) if racer.group_id else None
    return {
        "name": racer.display_name,
        "den": g.name if g else None,
        "member_id": racer.member_id,
        "active": racer.active,
    }


def update_racer(
    session: Session,
    racer: Racer,
    actor: Actor,
    first: str,
    last: str,
    den: str,
    member_id: str | None,
    active: bool,
) -> None:
    first, last, den = _clean(first), _clean(last), _clean(den) or "Unassigned"
    if not first:
        raise ServiceError("First name is required.")
    member_id = (member_id or "").strip() or None
    if member_id:
        other = session.exec(select(Racer).where(Racer.member_id == member_id, Racer.id != racer.id)).first()
        if other:
            raise ServiceError(f"Member ID {member_id} already belongs to {other.display_name}. Merge them instead.")
    before = racer_snapshot(session, racer)
    racer.first_name, racer.last_name = first, last
    racer.group_id = get_or_create_group(session, den).id
    racer.member_id = member_id
    if active != racer.active:
        racer.inactive_since = None if active else utcnow()
    session.add(racer)
    audit(session, actor, "racer.update", "racer", racer.id, before=before, after=racer_snapshot(session, racer))


def create_racer(session: Session, actor: Actor, first: str, last: str, den: str, member_id: str | None = None) -> Racer:
    if not _clean(first):
        raise ServiceError("First name is required.")
    member_id = (member_id or "").strip() or None
    if member_id and session.exec(select(Racer).where(Racer.member_id == member_id)).first():
        raise ServiceError(f"Member ID {member_id} is already on the roster.")
    group = get_or_create_group(session, _clean(den) or "Unassigned")
    racer = Racer(first_name=_clean(first), last_name=_clean(last), group_id=group.id, member_id=member_id)
    session.add(racer)
    session.flush()
    audit(session, actor, "racer.create", "racer", racer.id, after=racer_snapshot(session, racer))
    return racer


def _raced_entries(session: Session, entry_ids: list[int]) -> set[int]:
    """Entries with a heat that has started, finished or been re-run."""
    if not entry_ids:
        return set()
    return set(
        session.exec(
            select(HeatLane.entry_id)
            .join(Heat, Heat.id == HeatLane.heat_id)
            .where(col(HeatLane.entry_id).in_(entry_ids), Heat.status != HeatStatus.scheduled)
        )
    )


def merge_racers(session: Session, source: Racer, target: Racer, actor: Actor) -> None:
    """Fold a duplicate scout into another; ``source`` is deleted."""
    if source.id == target.id:
        raise ServiceError("Pick a different scout to merge into.")
    target_events = set(session.exec(select(Entry.event_id).where(Entry.racer_id == target.id)))
    entries = list(session.exec(select(Entry).where(Entry.racer_id == source.id)))
    clash = [e for e in entries if e.event_id in target_events]
    if clash:
        names = ", ".join(sorted({session.get(Event, e.event_id).name for e in clash}))
        raise ServiceError(f"Both scouts have a car in {names}. Remove one of those entries first.")
    for e in entries:
        e.racer_id = target.id
        session.add(e)
    target.member_id = target.member_id or source.member_id
    session.add(target)
    audit(session, actor, "racer.merge", "racer", target.id, before={"merged": racer_snapshot(session, source), "id": source.id})
    session.delete(source)
    session.flush()


def delete_racer(session: Session, racer: Racer, actor: Actor) -> None:
    if session.exec(select(Entry).where(Entry.racer_id == racer.id)).first():
        raise ServiceError("This scout is entered in an event. Remove the entries first, or mark them inactive.")
    audit(session, actor, "racer.delete", "racer", racer.id, before=racer_snapshot(session, racer))
    session.delete(racer)
    session.flush()


def update_entry(session: Session, entry: Entry, actor: Actor, number: int, car_name: str | None) -> None:
    if number != entry.number and session.exec(
        select(Entry).where(Entry.event_id == entry.event_id, Entry.number == number, Entry.id != entry.id)
    ).first():
        raise ServiceError(f"Number {number} is already used in this event.")
    before = {"number": entry.number, "name": entry.name}
    entry.number, entry.name = number, (car_name or "").strip() or None
    session.add(entry)
    audit(session, actor, "entry.update", "entry", entry.id, before=before, after={"number": entry.number, "name": entry.name})


def remove_entry(session: Session, entry: Entry, actor: Actor) -> None:
    """Take a scout out of an event entirely. Only before they have raced:
    after that, withdraw or disqualify instead so the results stay."""
    if _raced_entries(session, [entry.id]):
        raise ServiceError("This car has already raced. Withdraw or disqualify it instead, so its results stay on record.")
    lanes = list(session.exec(select(HeatLane).where(HeatLane.entry_id == entry.id)))
    for hl in lanes:  # unraced heats only; the caller reschedules
        hl.entry_id = None
        session.add(hl)
    audit(session, actor, "entry.remove", "entry", entry.id, before={"event_id": entry.event_id, "number": entry.number, "racer_id": entry.racer_id})
    session.delete(entry)
    session.flush()


def set_entry_status(session: Session, entry: Entry, status: EntryStatus | None, reason: str, actor: Actor) -> None:
    """Withdraw, disqualify or reinstate an entry. The caller reschedules, so
    unraced heats drop (or regain) the car. Raced heats are never touched."""
    reason = reason.strip()
    if status is not None and not reason:
        raise ServiceError("Give a reason (it goes in the audit log and is shown with the result).")
    if status == entry.status:
        return
    before = {"status": entry.status.value if entry.status else None, "reason": entry.status_reason}
    entry.status = status
    entry.status_reason = (reason or None) if status else None
    entry.status_at = utcnow() if status else None
    session.add(entry)
    action = f"entry.{status.value}" if status else "entry.reinstate"
    audit(session, actor, action, "entry", entry.id, before=before,
          after={"status": status.value if status else None}, reason=reason or None)
