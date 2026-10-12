"""PromQL building with an enforced tenant selector.

ARC never runs user-supplied PromQL. KPI templates contain a `{sel}` placeholder and
ARC fills it with the label matchers of the tenant/products the user may see.
"""
from __future__ import annotations

import re

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _quote(value: str) -> str:
    if not _SAFE_VALUE.match(value):
        raise ValueError(f"unsafe label value: {value!r}")
    return value


def selector(tenants: list[str] | None = None, products: list[str] | None = None, **labels: str) -> str:
    """Label matchers, e.g. arc_tenant_id="acme",arc_product_id=~"claims|billing".

    tenants=None means "all tenants" (platform admins / executives only).
    """
    parts = []
    for label, values in (("arc_tenant_id", tenants), ("arc_product_id", products)):
        if values is None:
            continue
        if not values:
            parts.append(f'{label}="__none__"')  # empty scope must match nothing
        elif len(values) == 1:
            parts.append(f'{label}="{_quote(values[0])}"')
        else:
            parts.append(f'{label}=~"{"|".join(_quote(v) for v in values)}"')
    for label, value in labels.items():
        parts.append(f'{label}="{_quote(value)}"')
    return ",".join(parts)


def render(template: str, sel: str, window: str = "1h") -> str:
    return template.replace("{sel}", sel).replace("{window}", window)
