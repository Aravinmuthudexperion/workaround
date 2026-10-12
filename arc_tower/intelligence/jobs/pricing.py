"""Export model prices per tenant so dashboards can compute cost = tokens x price in PromQL."""
from __future__ import annotations

from sqlalchemy import select

from arc_tower.core.config import get_settings
from arc_tower.db.models import Tenant
from arc_tower.db.session import session_scope

from .. import metrics

INTERVAL = 60


def run() -> None:
    prices = get_settings().model_prices          # {"model": [input_per_1M, output_per_1M]}
    with session_scope() as session:
        tenants = list(session.scalars(select(Tenant.slug)))
    rows = []
    for tenant in tenants:
        for model, (price_in, price_out) in prices.items():
            rows.append(((tenant, model, "input"), float(price_in) / 1e6))
            rows.append(((tenant, model, "output"), float(price_out) / 1e6))
    metrics.replace(metrics.model_price, rows)
