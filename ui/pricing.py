"""Sync wrapper around the azure-pricing MCP server's lookup function.

Imports `get_azure_sku_price` straight from `mcp-server/pricing_server.py` so the
Streamlit demo, the Live AI Audit tool loop, and the MCP server all share exactly
one pricing implementation (public Azure Retail Prices API, no auth required).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_MCP_DIR = Path(__file__).resolve().parent.parent / "mcp-server"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from pricing_server import get_azure_sku_price  # noqa: E402  (FastMCP's decorator returns the original coroutine fn)


def lookup_price(service_name: str, arm_sku_name: str, region: str) -> dict[str, Any]:
    """Return the retail price dict for one SKU. Raises RuntimeError/ValueError on API or input errors."""
    return asyncio.run(get_azure_sku_price(service_name, arm_sku_name, region))
