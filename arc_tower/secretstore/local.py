"""Secrets encrypted with ARC_SECRET_KEY (Fernet) and stored in the ARC database."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from arc_tower.core.security import decrypt, encrypt
from arc_tower.db.models import StoredSecret
from arc_tower.db.session import session_scope

from .base import SecretStore


class LocalSecretStore(SecretStore):
    def get(self, name: str) -> dict | None:
        with session_scope() as session:
            row = session.get(StoredSecret, name)
            return json.loads(decrypt(row.ciphertext)) if row else None

    def put(self, name: str, value: dict, session: Session | None = None) -> None:
        ciphertext = encrypt(json.dumps(value))
        if session is not None:          # part of the caller's transaction
            self._upsert(session, name, ciphertext)
            return
        with session_scope() as own:
            self._upsert(own, name, ciphertext)

    @staticmethod
    def _upsert(session: Session, name: str, ciphertext: str) -> None:
        row = session.get(StoredSecret, name)
        if row:
            row.ciphertext = ciphertext
        else:
            session.add(StoredSecret(name=name, ciphertext=ciphertext))

    def delete(self, name: str) -> None:
        with session_scope() as session:
            row = session.get(StoredSecret, name)
            if row:
                session.delete(row)
