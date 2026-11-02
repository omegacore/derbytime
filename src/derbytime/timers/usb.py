"""USB timer drivers. Phase 1 ships the interface only; ``parse_line`` for
each device lands in a later phase. Until then they connect, log raw lines and
report status, but never emit results (use manual entry on the operate page).
"""
from __future__ import annotations

from .serial_base import SerialTimerSource


class MicroWizardTimer(SerialTimerSource):
    kind = "microwizard"
    label = "MicroWizard Fast Track"
    default_baudrate = 9600


class ChampTimer(SerialTimerSource):
    kind = "champ"
    label = "BestTrack Champ"
    default_baudrate = 9600


class ArduinoGateTimer(SerialTimerSource):
    kind = "arduino_gate"
    label = "Arduino gate timer"
    default_baudrate = 115200
