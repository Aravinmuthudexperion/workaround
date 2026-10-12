"""Encrypted secret values for the "local" secrets backend."""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from arc_tower.db.base import Base, TimestampMixin


class StoredSecret(TimestampMixin, Base):
    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
