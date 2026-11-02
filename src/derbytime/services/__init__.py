"""Application services: glue between the pure ``core`` package, the database,
the event bus and timing sources. Web routes call into these; nothing here
knows about HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass


class ServiceError(Exception):
    """A rule was violated; ``status`` maps to an HTTP status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Actor:
    official_id: int | None
    label: str

    @classmethod
    def system(cls, label: str = "system") -> "Actor":
        return cls(None, label)
