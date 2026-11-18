# Azure IaC & FinOps Drift Auditor

## Project Purpose
Automated governance, security auditing, and live retail SKU cost analysis for Azure Bicep infrastructure—built to run 100% locally without an active Azure subscription.

## Architectural & Security Standards (Strictly Enforced)
- **Storage Accounts:** Must enforce `supportsHttpsTrafficOnly: true`, `minimumTlsVersion: 'TLS1_2'`, and `allowBlobPublicAccess: false`.
- **Virtual Machines:** Never deploy oversized `Standard_E` or `Standard_M` series SKUs in non-prod environments; default to `Standard_B2s` or `Standard_D2s_v5`. Managed disks must use `StandardSSD_LRS` or `Premium_LRS` with encryption at host enabled.
- **Required Resource Tags:** Every deployable resource MUST include `Environment`, `Owner`, and `CostCenter` tags.
- **Validation:** Every `.bicep` change must compile cleanly with `bicep build` and pass `bicep lint`.

## Coding Conventions
- **Python (MCP Server):** Python 3.11+, type hints required, use `mcp.server.fastmcp` (`FastMCP`) and `httpx` for async calls to `https://prices.azure.com/api/retail/prices`.
- **Workflow Preference:** Always use Plan Mode before modifying multiple files or scaffolding new MCP tools.
