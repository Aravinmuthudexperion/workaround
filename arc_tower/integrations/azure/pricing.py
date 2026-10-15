"""Azure Retail Prices API: Azure's public list prices, in any billing currency, no sign-in needed.

https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
List prices: enterprise agreements, reservations and savings plans can make the real bill lower.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx

from arc_tower.utils.http import client as http_client

from .client import AzureError

PRICES = "https://prices.azure.com"
_CACHE_SECONDS = 24 * 3600           # list prices change rarely
_MAX_PAGES = 60                      # 100 items a page


def unit_size(unit: str) -> float:
    """How many usage units one priced unit covers: "10K" -> 10000, "1M" -> 1e6, "10" -> 10, "1 GB Second" -> 1."""
    match = re.match(r"^\s*([\d.]+)\s*([KM])?\b", unit or "")
    if not match:
        return 1.0
    size = float(match.group(1))
    return size * {"K": 1e3, "M": 1e6}.get(match.group(2) or "", 1)


@dataclass(frozen=True)
class Meter:
    """One priced meter, with its tiers: [(first unit of the tier, price per unit)], counted in `unit` blocks.
    A first tier at price 0 is a free grant (e.g. 5 GB of Log Analytics ingestion a month)."""
    service: str
    product: str
    sku: str
    name: str
    region: str
    unit: str
    tiers: tuple[tuple[float, float], ...]

    def cost(self, quantity: float) -> float:
        """Cost of `quantity` usage units (e.g. tokens, GB-seconds), across the tiers."""
        blocks = max(quantity, 0.0) / unit_size(self.unit)
        total = 0.0
        for i, (start, price) in enumerate(self.tiers):
            end = self.tiers[i + 1][0] if i + 1 < len(self.tiers) else float("inf")
            if blocks > start:
                total += (min(blocks, end) - start) * price
        return total

    @property
    def free_units(self) -> float:
        """Usage units in a free first tier (0 if none)."""
        if len(self.tiers) > 1 and self.tiers[0][1] == 0:
            return self.tiers[1][0] * unit_size(self.unit)
        return 0.0


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class RetailPrices:
    """Meters by filter, cached for a day per (currency, filter)."""

    def __init__(self, http: httpx.Client | None = None):
        self._http = http or http_client(PRICES, timeout=httpx.Timeout(60.0, connect=10.0))
        self._cache: dict[tuple[str, str], tuple[float, list[Meter]]] = {}

    def meters(self, currency: str, *, service: str = "", product: str = "", region: str = "",
               sku: str = "") -> list[Meter]:
        """Consumption meters matching the given exact fields (region = ARM region name, e.g. centralindia)."""
        clauses = ["type eq 'Consumption'"]
        for field, value in (("serviceName", service), ("productName", product), ("armRegionName", region),
                             ("skuName", sku)):
            if value:
                clauses.append(f"{field} eq {_quote(value)}")
        flt = " and ".join(clauses)
        key = (currency.upper(), flt)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        items: list[dict] = []
        url, params = "/api/retail/prices", {"currencyCode": currency.upper(), "$filter": flt}
        for _ in range(_MAX_PAGES):
            resp = self._http.get(url, params=params)
            if resp.status_code >= 400:
                raise AzureError(f"Azure Retail Prices: HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
            body = resp.json()
            items.extend(body.get("Items") or [])
            url, params = body.get("NextPageLink"), None
            if not url:
                break
        meters = _group(items)
        self._cache[key] = (time.time(), meters)
        return meters

    def close(self) -> None:
        self._http.close()


def _group(items: list[dict]) -> list[Meter]:
    """One Meter per (product, sku, meter, region), its tier rows merged."""
    grouped: dict[tuple, dict] = {}
    for item in items:
        key = (item.get("serviceName", ""), item.get("productName", ""), item.get("skuName", ""),
               item.get("meterName", ""), item.get("armRegionName", ""), item.get("unitOfMeasure", ""))
        tiers = grouped.setdefault(key, {})
        tiers[float(item.get("tierMinimumUnits") or 0)] = float(item.get("retailPrice") or 0)
    return [Meter(*key, tiers=tuple(sorted(tiers.items()))) for key, tiers in grouped.items()]
