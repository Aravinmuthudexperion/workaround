"""Audit trail of administrative actions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.db.models import AuditLog


def record(session: Session, actor: str, action: str, target_type: str = "", target_id: object = "",
           tenant_id: int | None = None, **details) -> None:
    session.add(AuditLog(actor=actor, action=action, target_type=target_type, target_id=str(target_id),
                         tenant_id=tenant_id, details=details))


def recent(session: Session, tenant_ids: list[int] | None = None, limit: int = 200) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
    if tenant_ids is not None:
        stmt = stmt.where(AuditLog.tenant_id.in_(tenant_ids))
    return list(session.scalars(stmt))
