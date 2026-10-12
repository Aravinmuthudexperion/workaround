"""Response models for /api/v1."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TenantOut(BaseModel):
    id: int
    slug: str
    name: str


class ProductOut(BaseModel):
    id: int
    tenant: str
    slug: str
    name: str
    connector_type: str
    ingest_mode: str
    status: str
    environment: str
    first_seen_at: datetime | None
    last_seen_at: datetime | None


class AgentOut(BaseModel):
    id: int
    tenant: str
    product: str
    service_name: str
    agent_name: str
    display_name: str
    owner: str
    risk_class: str
    platform: str
    models: list[str]
    status: str
    last_seen_at: datetime | None


class KpiOut(BaseModel):
    key: str
    name: str
    category: str
    unit: str
    value: float | None
    target: float | None
    breached: bool


class TraceOut(BaseModel):
    id: str
    name: str
    timestamp: str
    user_id: str | None
    session_id: str | None
    latency_s: float | None
    cost_usd: float | None
    level: str
    url: str
