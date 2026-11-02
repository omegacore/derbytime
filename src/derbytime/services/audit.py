from __future__ import annotations

from typing import Any

from sqlmodel import Session

from ..models import AuditLog
from . import Actor


def audit(
    session: Session,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditLog:
    row = AuditLog(
        official_id=actor.official_id,
        actor=actor.label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
    )
    session.add(row)
    return row
