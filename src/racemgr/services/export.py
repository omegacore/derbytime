"""Exports: a full database backup, and CSV files for people.

The backup is the exact serialization (every table, audit log included):
restore it by stopping the server and putting the file in place of the
database. The CSVs are for spreadsheets; the roster CSV uses the same column
names the importer reads, so it can be edited and imported back.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from ..models import Entry, Event, Group, Heat, HeatLane, HeatStatus, Racer, Round, Track
from .schedule import event_standings, pack_standings


def backup_db(engine: Engine, dest: Path) -> Path:
    """Consistent copy of the live database (SQLite online backup: safe while
    the server is writing, and includes changes still in the WAL file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = engine.raw_connection()
    try:
        target = sqlite3.connect(dest)
        try:
            raw.driver_connection.backup(target)
        finally:
            target.close()
    finally:
        raw.close()
    return dest


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _t(v: float | None) -> str:
    return "" if v is None else f"{v:.3f}"


def roster_csv(session: Session, event: Event | None = None) -> str:
    """The pack roster, or one event's entries. Importable as-is."""
    header = ["Scout First Name", "Scout Last Name", "Member ID", "Den", "Status"]
    if event is None:
        rows = session.exec(
            select(Racer, Group).join(Group, Group.id == Racer.group_id, isouter=True).order_by(Racer.last_name, Racer.first_name)
        ).all()
        return _csv(header, ([r.first_name, r.last_name, r.member_id or "", g.name if g else "", "Active" if r.active else "Inactive"] for r, g in rows))
    header += ["Car Number", "Car Name", "Checked In", "Weight (oz)", "Inspection", "Inspection Notes", "Race Status", "Race Status Reason"]
    rows = session.exec(
        select(Entry, Racer, Group)
        .join(Racer, Racer.id == Entry.racer_id)
        .join(Group, Group.id == Racer.group_id, isouter=True)
        .where(Entry.event_id == event.id)
        .order_by(Entry.number)
    ).all()
    return _csv(header, (
        [r.first_name, r.last_name, r.member_id or "", g.name if g else "", "Active" if r.active else "Inactive",
         e.number, e.name or "", "yes" if e.checked_in else "no", "" if e.weight_oz is None else e.weight_oz,
         e.inspection_status.value, e.inspection_notes or "", e.status.value if e.status else "racing", e.status_reason or ""]
        for e, r, g in rows
    ))


def standings_csv(session: Session, event: Event) -> str:
    header = ["Standings", "Rank", "Tied", "Car Number", "Scout", "Den", "Car Name", "Score", "Average", "Best", "Points", "Runs", "Race Status", "Race Status Reason"]
    out = []
    blocks = event_standings(session, event)
    for b in [*blocks, pack_standings(session, event)] if blocks else []:
        for row in b["rows"]:
            s = row["s"]
            out.append([b["name"], s.rank if s.score is not None else "", "yes" if s.tied else "", row["number"], row["racer_name"],
                        row["group"], row["car_name"], "" if s.score is None else round(s.score, 3), _t(s.average), _t(s.best),
                        s.points, s.runs, row["status"] or "racing", row["status_reason"]])
        for d in b.get("disqualified", []):
            out.append([b["name"], "DQ", "", d["number"], d["racer_name"], d["group"], d["car_name"], "", "", "", "", "", "disqualified", d["status_reason"]])
    return _csv(header, out)


def heats_csv(session: Session, event: Event) -> str:
    """Every lane of every heat, re-runs included, in race order."""
    header = ["Round", "Heat", "Track", "Heat Status", "Lane", "Car Number", "Scout", "Den", "Raw Time", "Final Time",
              "DNF", "Place", "Source", "Edit Reason", "Results At", "Approved At", "Re-run Of Heat"]
    rows = session.exec(
        select(Heat, Round, Track)
        .join(Round, Round.id == Heat.round_id)
        .join(Track, Track.id == Heat.track_id)
        .where(Round.event_id == event.id)
        .order_by(Round.seq, Round.id, Heat.seq)
    ).all()
    lanes = session.exec(select(HeatLane).where(col(HeatLane.heat_id).in_([h.id for h, _, _ in rows])).order_by(HeatLane.lane)).all()
    by_heat: dict[int, list[HeatLane]] = {}
    for hl in lanes:
        by_heat.setdefault(hl.heat_id, []).append(hl)
    who = {
        e.id: (e.number, r.display_name, g.name if g else "")
        for e, r, g in session.exec(
            select(Entry, Racer, Group)
            .join(Racer, Racer.id == Entry.racer_id)
            .join(Group, Group.id == Racer.group_id, isouter=True)
            .where(Entry.event_id == event.id)
        )
    }
    numbers = {h.id: (rnd.name, h.number) for h, rnd, _ in rows}
    out = []
    for h, rnd, t in rows:
        for hl in by_heat.get(h.id, []):
            if hl.entry_id is None:
                continue
            num, name, den = who.get(hl.entry_id, ("", "", ""))
            raced = h.status in (HeatStatus.pending, HeatStatus.approved, HeatStatus.rerun)
            orig = numbers.get(h.rerun_of_id)
            out.append([
                rnd.name, h.number, t.name, h.status.value, hl.lane, num, name, den,
                _t(hl.raw_time) if raced else "", _t(hl.final_time) if raced else "", "yes" if hl.dnf else "",
                hl.place or "", hl.source or "", hl.edit_reason or "",
                h.results_at.isoformat(timespec="seconds") if h.results_at else "",
                h.approved_at.isoformat(timespec="seconds") if h.approved_at else "",
                f"{orig[0]} heat {orig[1]}" if orig else "",
            ])
    return _csv(header, out)
