from __future__ import annotations

from .base import PendingResult, TimerSource, TimerStatus
from .phone import PhoneStopwatch
from .simulator import SimulatorTimer
from .usb import ArduinoGateTimer, ChampTimer, MicroWizardTimer

REGISTRY: dict[str, type[TimerSource]] = {
    cls.kind: cls
    for cls in (SimulatorTimer, PhoneStopwatch, MicroWizardTimer, ChampTimer, ArduinoGateTimer)
}

__all__ = ["REGISTRY", "PendingResult", "TimerSource", "TimerStatus", "PhoneStopwatch", "SimulatorTimer"]
