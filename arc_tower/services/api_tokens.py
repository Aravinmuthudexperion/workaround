"""Personal API tokens for the outward REST API (/api/v1)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_tower.core.security import API_TOKEN_PREFIX, hash_key, key_hint, new_opaque_key
from arc_tower.db.models import ApiToken, User
from arc_tower.utils.timeutil import utcnow

from . import audit


def create(session: Session, user: User, name: str) -> str:
    plain = new_opaque_key(API_TOKEN_PREFIX)
    session.add(ApiToken(user_id=user.id, name=name.strip() or "token", token_hash=hash_key(plain),
                         hint=key_hint(plain)))
    audit.record(session, user.email, "api_token.create", "user", user.id, name=name)
    return plain


def revoke(session: Session, user: User, token_id: int) -> None:
    token = session.get(ApiToken, token_id)
    if token and token.user_id == user.id:
        token.revoked_at = utcnow()
        audit.record(session, user.email, "api_token.revoke", "user", user.id, name=token.name)


def user_for_token(session: Session, plain: str) -> User | None:
    token = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_key(plain)))
    if not token or token.revoked_at or not token.user.active:
        return None
    token.last_used_at = utcnow()
    return token.user
