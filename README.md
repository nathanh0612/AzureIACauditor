# Azure IaC & FinOps Drift Auditor

Governance checks, security auditing and live retail SKU cost analysis for Azure Bicep. It runs entirely on your machine and needs no Azure subscription.

**Live demo:** [azure-iac-auditor.streamlit.app](https://azure-iac-auditor.streamlit.app/) (Streamlit Community Cloud)

The sample audit fixes 8 policy violations in a flawed lab template. It cuts the dev VM from **$735.84/mo** (`Standard_E16s_v5`) to **$30.37/mo** (`Standard_B2s`) in `westus2`, using live pay-as-you-go retail prices.

---

## Architecture

```
                ┌──────────────────────────────────────────────┐
                │  CLAUDE.md: governance rules (source of truth)│
                └──────────────┬───────────────────────────────┘
                               │ read by
     ┌─────────────────────────┼──────────────────────────────┐
     ▼                         ▼                              ▼
 Claude Code session    Streamlit GUI (ui/app.py)      Live AI Audit engine
 (interactive/headless) ├─ Demo Mode                   (ui/llm_audit.py, Anthropic API)
     │                  └─ Live AI Audit ──────────────────────┘
     │ Edit/Write *.bicep                │
     ▼                                   ▼
 PostToolUse hook ──► bicep build + bicep lint
     │
     ▼
 azure-pricing MCP server (mcp-server/pricing_server.py, FastMCP)
     └──► https://prices.azure.com/api/retail/prices   (public, no auth)
```

The project has four layers.

### 1. `CLAUDE.md`: governance rules

[`CLAUDE.md`](CLAUDE.md) is the policy document that every audit is checked against. Claude Code loads it automatically, and the API engine injects it into its system prompt.

| Area | Rule |
|---|---|
| Storage Accounts | `supportsHttpsTrafficOnly: true`, `minimumTlsVersion: 'TLS1_2'`, `allowBlobPublicAccess: false` |
| Virtual Machines | No `Standard_E` / `Standard_M` series in non-prod; default to `Standard_B2s` or `Standard_D2s_v5` |
| Managed Disks | `StandardSSD_LRS` or `Premium_LRS`, with encryption at host enabled |
| Tagging | Every deployable resource carries `Environment`, `Owner`, `CostCenter` |
| Validation | Every `.bicep` change must pass `bicep build` and `bicep lint` |

### 2. PostToolUse Bicep validation hook

[`.claude/settings.json`](.claude/settings.json) registers a `PostToolUse` hook on `Edit|Write` that runs [`.claude/hooks/bicep-validate.sh`](.claude/hooks/bicep-validate.sh). The script reads the edited file's path from the hook's JSON input on stdin. If the file is a `*.bicep` file, it runs:

```bash
bicep build <file> --stdout > /dev/null && bicep lint <file>
```

On failure, the script prints the diagnostics to stderr and exits with code 2, which Claude Code feeds back to the agent. The agent sees the compile or lint error immediately and fixes it in the same session.

### 3. `azure-pricing` FastMCP server

[`mcp-server/pricing_server.py`](mcp-server/pricing_server.py) is a stdio MCP server built on `mcp.server.fastmcp.FastMCP` and async `httpx`. It exposes one tool:

```
get_azure_sku_price(service_name: str, arm_sku_name: str, region: str) -> dict
```

- Queries the public [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) with an OData filter on `serviceName`, `armSkuName`, `armRegionName` and `priceType eq 'Consumption'`.
- Follows `NextPageLink` pagination (capped at 10 pages).
- Selects the most representative meter. It skips Spot and Low Priority meters, prefers Linux over Windows and the primary meter region, then takes the lowest non-zero price.
- Returns the hourly price, an estimated monthly cost (730 h), meter and SKU IDs, and up to 10 alternative variants.
- Normalizes region input (`"East US"` → `eastus`) and escapes OData literals.

The server is registered for Claude Code in [`.mcp.json`](.mcp.json). The Streamlit UI imports the same function through [`ui/pricing.py`](ui/pricing.py), so all pricing comes from one implementation.

### 4. Streamlit GUI

[`ui/app.py`](ui/app.py) is the web front end. It has two modes:

- **Demo Mode** (no key needed)
  - *Sample audit*: a pre-recorded before/after audit of the lab template ([`ui/demo_audit.json`](ui/demo_audit.json)).
  - *Live price lookup*: real-time SKU pricing from the Retail Prices API, cached for 1 hour.
  - *Rules*: the active `CLAUDE.md` policy.
- **Live AI Audit**: upload or paste any `.bicep` file, then pick an engine:
  - **Anthropic API (your key)**: a [`ui/llm_audit.py`](ui/llm_audit.py) tool loop with two client-side tools, `get_azure_sku_price` and `validate_bicep` (build + lint, available when the `bicep` CLI is installed). The key is held in session memory only and is never written to disk or logged.
  - **Local Claude Code CLI** (shown when `claude` is on `PATH`): runs `claude -p --output-format json --json-schema …` headless with `CLAUDE.md`, the hooks and the MCP server.

Both engines return the same structured report: violations, before/after pricing and validation status. The UI shows it in three tabs: **Diff**, **Compliance** and **Cost**.

---

## Repository layout

```
.
├── CLAUDE.md                  # Governance rules
├── .claude/
│   ├── settings.json          # Registers the PostToolUse hook
│   └── hooks/bicep-validate.sh  # bicep build + lint on edited .bicep files
├── .mcp.json                  # Registers the azure-pricing MCP server
├── infra/main.bicep           # Lab template (remediated)
├── mcp-server/
│   ├── pricing_server.py      # FastMCP Azure Retail Prices server
│   └── requirements.txt
├── ui/
│   ├── app.py                 # Streamlit GUI
│   ├── llm_audit.py           # Anthropic API audit engine
│   ├── pricing.py             # Sync wrapper over the MCP pricing tool
│   ├── demo_audit.json        # Pre-recorded sample audit
│   └── requirements.txt
└── requirements.txt           # Root deps (used by Streamlit Cloud)
```

---

## Getting started

**Prerequisites:** Python 3.11+. The [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install) is optional for the GUI and required for the hook. [Claude Code](https://claude.com/claude-code) is optional.

```bash
git clone https://github.com/nathanh0612/AzureIACauditor.git
cd AzureIACauditor

python -m venv mcp-server/.venv
source mcp-server/.venv/bin/activate
pip install -r requirements.txt
```

**Run the GUI:**

```bash
streamlit run ui/app.py
```

**Use it from Claude Code:** open the repo in Claude Code. `CLAUDE.md`, the hook and the `azure-pricing` MCP server load automatically. Note that `.mcp.json` uses absolute paths, so update them for your checkout. Then ask:

> Audit `infra/main.bicep` against CLAUDE.md and show the before/after monthly cost in westus2.

**Run the MCP server standalone:**

```bash
python mcp-server/pricing_server.py   # stdio transport
```

### Optional: server-side API key

By default, a public deployment never spends the operator's API credits, and each visitor supplies their own key. To pre-fill a key from `st.secrets["ANTHROPIC_API_KEY"]` or the environment, set `AUDITOR_ALLOW_SERVER_KEY=1`.

---

## Notes

- Prices are public pay-as-you-go retail prices in USD. They do not include reservations, savings plans or negotiated (EA/MCA) discounts. Monthly estimates assume 730 hours.
- Storage is billed per GB, not per hour, so storage meters show a unit price with no monthly estimate.
