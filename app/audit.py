"""Append-only audit trail (spec s51).

Every meaningful change records who, what, when, old value, new value, reason
and source. There is no update or delete path for :class:`AuditLog` anywhere
in this application -- "no silent modifications" is enforced by never writing
code that could perform one.

The ``source`` field distinguishes a human action from an agent action, which
is what lets an auditor answer "did a person decide this, or did the system?".
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from .models import AuditLog, User


class AuditSource:
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    IMPORT = "IMPORT"

    @staticmethod
    def agent(name: str) -> str:
        return f"AGENT:{name}"


def _jsonable(value: Any) -> Any:
    """Make a value safe to store in a JSON column."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def snapshot(obj: Any, fields: Iterable[str]) -> dict:
    """Capture named fields of a model instance for before/after logging."""
    if obj is None:
        return {}
    return {f: _jsonable(getattr(obj, f, None)) for f in fields}


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    entity_uid: Optional[str] = None,
    organization_id: Optional[int] = None,
    actor: Optional[User] = None,
    actor_label: Optional[str] = None,
    old_value: Optional[dict] = None,
    new_value: Optional[dict] = None,
    reason: Optional[str] = None,
    source: str = AuditSource.SYSTEM,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    commit: bool = False,
) -> AuditLog:
    """Append one audit entry.

    ``commit`` defaults to False so the entry participates in the caller's
    transaction: if the financial change rolls back, so does its audit record,
    keeping the log truthful.
    """
    if actor is not None:
        label = actor_label or f"{actor.full_name} <{actor.email}>"
        org_id = organization_id if organization_id is not None else actor.organization_id
        actor_id = actor.id
    else:
        label = actor_label or "system"
        org_id = organization_id
        actor_id = None

    entry = AuditLog(
        organization_id=org_id,
        actor_user_id=actor_id,
        actor_label=label[:200],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_uid=entity_uid,
        old_value_json=_jsonable(old_value) if old_value is not None else None,
        new_value_json=_jsonable(new_value) if new_value is not None else None,
        reason=reason,
        source=source,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry


def diff(before: dict, after: dict) -> dict:
    """Return only the fields that actually changed.

    Logging unchanged fields buries the real change in noise.
    """
    changed = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[key] = {"from": old, "to": new}
    return changed


def history(db: Session, entity_type: str, entity_id: int, limit: int = 100) -> list[AuditLog]:
    """Audit history for one record, newest first."""
    return (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )
