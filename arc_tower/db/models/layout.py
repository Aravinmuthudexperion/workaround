"""Saved page layouts (which KPI tiles and charts a page shows).

user_id NULL = the default for everyone (set by a platform admin); otherwise a personal layout.
"""
from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arc_tower.db.base import Base, TimestampMixin


class DashboardLayout(TimestampMixin, Base):
    __tablename__ = "dashboard_layouts"
    __table_args__ = (UniqueConstraint("user_id", "page"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    page: Mapped[str] = mapped_column(String(32))            # "overview" | "agent"
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # {"tiles": [...], "charts": [...]}
