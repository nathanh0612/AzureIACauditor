"""Azure Retail Pricing MCP server.

Exposes a FastMCP tool that queries the public, unauthenticated Azure Retail
Prices API (https://prices.azure.com/api/retail/prices) and returns hourly and
estimated monthly (730h) USD pricing for a given service / ARM SKU / region.

Run (stdio transport):
    python pricing_server.py
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
CURRENCY = "USD"
HOURS_PER_MONTH = 730
REQUEST_TIMEOUT = 30.0
MAX_PAGES = 10
MAX_VARIANTS = 10
EXCLUDED_MARKERS = ("spot", "low priority")

mcp = FastMCP("azure-pricing")


def _odata_escape(value: str) -> str:
    """Escape a value for embedding in an OData single-quoted string literal."""
    return value.replace("'", "''")


def _normalize_region(region: str) -> str:
    """Normalize 'East US' / 'East-US' style input to ARM form ('eastus')."""
    return "".join(ch for ch in region.strip().lower() if ch.isalnum())


def _build_filter(service_name: str, arm_sku_name: str, region: str) -> str:
    return (
        f"serviceName eq '{_odata_escape(service_name)}' "
        f"and armSkuName eq '{_odata_escape(arm_sku_name)}' "
        f"and armRegionName eq '{_odata_escape(region)}' "
        "and priceType eq 'Consumption'"
    )


async def _fetch_items(client: httpx.AsyncClient, odata_filter: str) -> list[dict[str, Any]]:
    """Fetch all matching price items, following NextPageLink up to MAX_PAGES."""
    items: list[dict[str, Any]] = []
    response = await client.get(
        RETAIL_PRICES_URL,
        params={
            "api-version": API_VERSION,
            "currencyCode": CURRENCY,
            "$filter": odata_filter,
        },
    )
    for _ in range(MAX_PAGES):
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        items.extend(payload.get("Items", []))
        next_link: str | None = payload.get("NextPageLink")
        if not next_link:
            break
        response = await client.get(next_link)
    return items


def _is_excluded(item: dict[str, Any]) -> bool:
    text = f"{item.get('skuName', '')} {item.get('meterName', '')}".lower()
    return any(marker in text for marker in EXCLUDED_MARKERS)


def _select_primary(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most representative on-demand meter for the SKU.

    Preference order: non-Spot/Low-Priority, non-Windows (Linux/base compute),
    primary meter region, then lowest non-zero retail price.
    """
    if not items:
        return None
    candidates = [i for i in items if not _is_excluded(i)] or items

    def rank(item: dict[str, Any]) -> tuple[int, int, int, float]:
        price = float(item.get("retailPrice") or 0.0)
        return (
            1 if "windows" in str(item.get("productName", "")).lower() else 0,
            0 if item.get("isPrimaryMeterRegion") else 1,
            1 if price <= 0 else 0,
            price,
        )

    return min(candidates, key=rank)


def _variant_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku_name": item.get("skuName"),
        "product_name": item.get("productName"),
        "meter_name": item.get("meterName"),
        "retail_price": item.get("retailPrice"),
        "unit_of_measure": item.get("unitOfMeasure"),
    }


@mcp.tool()
async def get_azure_sku_price(service_name: str, arm_sku_name: str, region: str) -> dict[str, Any]:
    """Look up the Azure retail (pay-as-you-go) USD price for a SKU in a region.

    Args:
        service_name: Azure service name exactly as the Retail Prices API spells it,
            e.g. "Virtual Machines", "Storage", "Azure App Service".
        arm_sku_name: ARM SKU name, e.g. "Standard_D2s_v5", "Standard_B2s".
        region: ARM region name, e.g. "eastus", "westeurope" ("East US" is accepted).

    Returns:
        Hourly retail price, estimated monthly cost (730 hours) for hourly meters,
        meter/product/SKU details, and other meter variants (Windows, Spot, etc.).
    """
    if not service_name.strip() or not arm_sku_name.strip() or not region.strip():
        raise ValueError("service_name, arm_sku_name, and region are all required.")

    service_name = service_name.strip()
    arm_sku_name = arm_sku_name.strip()
    arm_region = _normalize_region(region)
    odata_filter = _build_filter(service_name, arm_sku_name, arm_region)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            items = await _fetch_items(client, odata_filter)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Azure Retail Prices API returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Failed to reach Azure Retail Prices API: {exc!r}") from exc

    primary = _select_primary(items)
    if primary is None:
        return {
            "error": "No pricing found for the given service, SKU, and region.",
            "filter": odata_filter,
            "hint": (
                "serviceName is case-sensitive (e.g. 'Virtual Machines'); armSkuName must "
                "match exactly (e.g. 'Standard_D2s_v5'); region must be an ARM name like 'eastus'."
            ),
        }

    price = float(primary.get("retailPrice") or 0.0)
    unit = str(primary.get("unitOfMeasure", ""))
    is_hourly = unit.strip().lower() == "1 hour"

    result: dict[str, Any] = {
        "service_name": service_name,
        "arm_sku_name": arm_sku_name,
        "region": arm_region,
        "currency": primary.get("currencyCode", CURRENCY),
        "retail_price_hourly": price if is_hourly else None,
        "retail_price": price,
        "unit_of_measure": unit,
        "estimated_monthly_cost": round(price * HOURS_PER_MONTH, 2) if is_hourly else None,
        "hours_per_month": HOURS_PER_MONTH,
        "meter_name": primary.get("meterName"),
        "product_name": primary.get("productName"),
        "sku_name": primary.get("skuName"),
        "sku_id": primary.get("skuId"),
        "meter_id": primary.get("meterId"),
        "effective_start_date": primary.get("effectiveStartDate"),
        "is_primary_meter_region": primary.get("isPrimaryMeterRegion"),
        "other_variants": [
            _variant_summary(i) for i in items if i is not primary
        ][:MAX_VARIANTS],
        "total_matching_meters": len(items),
    }
    if not is_hourly:
        result["note"] = (
            f"Meter is billed per '{unit}', not hourly; monthly estimate not computed. "
            "See retail_price."
        )
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
