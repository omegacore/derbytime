"""What check-in measures for each kind of event.

A derby car is weighed and a regatta boat is measured for length. Neither is
required unless config.toml says so (`require_weight` / `require_length`
under `[defaults.<type>]`).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..models import Entry, Event
from . import ServiceError


@dataclass(frozen=True)
class Measure:
    field: str  # Entry column
    label: str  # "Weight"
    unit: str  # "oz"
    unit_name: str  # "ounces"
    verb: str  # "Weigh"
    example: str
    required: bool = False

    def value(self, entry: Entry) -> float | None:
        return getattr(entry, self.field)

    def shown(self, entry: Entry) -> str:
        v = self.value(entry)
        return "—" if v is None else f"{v:g} {self.unit}"


WEIGHT = Measure("weight_oz", "Weight", "oz", "ounces", "Weigh", "4.9")
LENGTH = Measure("length_in", "Length", "in", "inches", "Measure", "12")


def measure_for(event: Event, settings: Settings) -> Measure:
    kind = event.type.value
    d = settings.defaults.get(kind)
    if kind == "regatta":
        return Measure(**{**LENGTH.__dict__, "required": bool(d and d.require_length)})
    return Measure(**{**WEIGHT.__dict__, "required": bool(d and d.require_weight)})


def apply_measure(entry: Entry, m: Measure, form: dict[str, str]) -> None:
    """Set the entry's measurement from the form; a form without the field
    (an older page still open) leaves it alone."""
    if m.field not in form:
        return
    raw = form[m.field].strip()
    try:
        value = float(raw) if raw else None
    except ValueError:
        raise ServiceError(f"{raw!r} is not a {m.label.lower()}.")
    if value is not None and value <= 0:
        raise ServiceError(f"The {m.label.lower()} must be more than 0.")
    setattr(entry, m.field, value)


def check_required(entry: Entry, m: Measure, noun: str) -> None:
    if entry.checked_in and m.required and m.value(entry) is None:
        raise ServiceError(f"{m.verb} the {noun} before checking in.")
