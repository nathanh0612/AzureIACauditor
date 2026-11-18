"""Streamlit GUI for the Azure IaC & FinOps Drift Auditor.

Two modes:

* **Demo Mode** (default, no API key): live Azure Retail Prices lookups against the
  public, unauthenticated API, plus a pre-loaded sample audit of the flawed lab.
* **Live AI Audit**: the viewer supplies an ANTHROPIC_API_KEY in the sidebar and
  audits any uploaded .bicep through the Anthropic API (`llm_audit.py`). When the
  `claude` CLI is on PATH, the original headless Claude Code engine
  (`claude -p --output-format json`, with CLAUDE.md, hooks and the azure-pricing
  MCP server) is offered as a second engine.

Launch:
    mcp-server/.venv/bin/streamlit run ui/app.py
"""

from __future__ import annotations

import difflib
import html
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import llm_audit
from pricing import lookup_price

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BICEP = REPO_ROOT / "infra" / "main.bicep"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEMO_FIXTURE = Path(__file__).resolve().parent / "demo_audit.json"
RUNS_DIR = REPO_ROOT / "ui" / ".runs"
MAX_UPLOAD_BYTES = 200_000
ENGINE_API = "Anthropic API (your key)"
ENGINE_CLI = "Local Claude Code CLI"
PRICE_SERVICES: list[str] = ["Virtual Machines", "Storage", "Azure App Service", "SQL Database"]
QUICK_SKUS: list[str] = ["Standard_B2s", "Standard_D2s_v5", "Standard_D4s_v5", "Standard_E16s_v5", "Standard_M32ms"]
CLAUDE_TIMEOUT_S = 900
BICEP_TIMEOUT_S = 120
REGIONS: list[str] = ["westus2", "eastus", "eastus2", "centralus", "westeurope", "northeurope"]
PRICING_TOOL = "mcp__azure-pricing__get_azure_sku_price"
ALLOWED_TOOLS: list[str] = [
    "Read",
    "Edit",
    "Write",
    "Bash(bicep build:*)",
    "Bash(bicep lint:*)",
    PRICING_TOOL,
]

_PRICE_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resource": {"type": "string"},
        "service_name": {"type": "string"},
        "arm_sku_name": {"type": "string"},
        "unit_of_measure": {"type": "string"},
        "hourly_usd": {"type": ["number", "null"]},
        "monthly_usd": {"type": ["number", "null"]},
    },
    "required": ["resource", "service_name", "arm_sku_name", "unit_of_measure", "hourly_usd", "monthly_usd"],
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string"},
                    "category": {"type": "string", "enum": ["security", "tagging", "sizing", "disk", "other"]},
                    "rule": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["resource", "category", "rule", "before", "after", "severity"],
            },
        },
        "pricing": {
            "type": "object",
            "properties": {
                "region": {"type": "string"},
                "before": {"type": "array", "items": _PRICE_ITEM},
                "after": {"type": "array", "items": _PRICE_ITEM},
            },
            "required": ["region", "before", "after"],
        },
        "validation": {
            "type": "object",
            "properties": {
                "bicep_build_passed": {"type": "boolean"},
                "bicep_lint_passed": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "required": ["bicep_build_passed", "bicep_lint_passed", "notes"],
        },
    },
    "required": ["summary", "violations", "pricing", "validation"],
}


@dataclass
class CheckResult:
    passed: bool
    output: str


@dataclass
class AuditResult:
    run_dir: Path | None
    before: str
    after: str
    region: str
    ok: bool
    error: str | None = None
    report: dict[str, Any] | None = None
    cli_meta: dict[str, Any] = field(default_factory=dict)
    raw_stdout: str = ""
    stderr: str = ""
    build: CheckResult | None = None
    lint: CheckResult | None = None


# --------------------------------------------------------------------------- helpers


def build_prompt(rel_path: str, region: str) -> str:
    return f"""You are the Azure IaC & FinOps Drift Auditor running headless.

Target file: {rel_path}
Target pricing region: {region}

Do the following, in order:
1. Read {rel_path} and audit it against every rule in CLAUDE.md (storage security settings,
   VM SKU sizing for non-prod, managed disk type + encryption at host, required tags
   Environment/Owner/CostCenter on every deployable resource).
2. BEFORE editing, call {PRICING_TOOL} for every priced SKU in the ORIGINAL file
   (e.g. service_name "Virtual Machines" with the VM size; "Storage" for storage SKUs)
   using region "{region}".
3. Remediate the file IN PLACE with the Edit tool. Edit only {rel_path}; do not touch any other file.
   Keep the file a self-contained, deployable Bicep template.
4. Run `bicep build {rel_path} --stdout` and `bicep lint {rel_path}` and fix until both are clean.
5. Call {PRICING_TOOL} again for every priced SKU in the REMEDIATED file in region "{region}".
6. Return the structured result. For pricing entries, copy hourly_usd from the tool's
   retail_price_hourly and monthly_usd from its estimated_monthly_cost exactly (null when the
   tool returned null, e.g. per-GB storage meters). Never estimate or invent prices.
   List one violation entry per individual rule you fixed.
"""


def run_bicep(args: list[str]) -> CheckResult:
    if shutil.which("bicep") is None:
        return CheckResult(False, "bicep CLI not found on PATH")
    try:
        proc = subprocess.run(
            ["bicep", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=BICEP_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return CheckResult(False, f"bicep {args[0]} timed out")
    output = (proc.stderr or "").strip()
    return CheckResult(proc.returncode == 0, output or "clean")


def extract_report(envelope: dict[str, Any]) -> dict[str, Any] | None:
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = envelope.get("result")
    if not isinstance(text, str):
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def run_audit(code: str, region: str) -> AuditResult:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = RUNS_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "main.bicep"
    target.write_text(code, encoding="utf-8")
    rel_path = target.relative_to(REPO_ROOT).as_posix()

    result = AuditResult(run_dir=run_dir, before=code, after=code, region=region, ok=False)

    claude = shutil.which("claude")
    if claude is None:
        result.error = "`claude` CLI not found on PATH."
        return result

    cmd = [
        claude,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESULT_SCHEMA),
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=build_prompt(rel_path, region),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result.error = f"Claude Code timed out after {CLAUDE_TIMEOUT_S}s."
        return result

    result.raw_stdout = proc.stdout
    result.stderr = proc.stderr
    result.after = target.read_text(encoding="utf-8")
    result.build = run_bicep(["build", rel_path, "--stdout"])
    result.lint = run_bicep(["lint", rel_path])

    try:
        envelope: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result.error = f"Could not parse Claude Code output as JSON (exit code {proc.returncode})."
        return result

    result.cli_meta = {k: v for k, v in envelope.items() if k not in ("result", "structured_output")}
    if proc.returncode != 0 or envelope.get("is_error"):
        result.error = f"Claude Code reported an error (exit {proc.returncode}, subtype {envelope.get('subtype')})."
        return result

    result.report = extract_report(envelope)
    if result.report is None:
        result.error = "Claude Code finished but returned no structured report."
        return result

    result.ok = True
    return result


def load_git_head_bicep() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "show", "HEAD:infra/main.bicep"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def run_api_audit(api_key: str, code: str, region: str) -> AuditResult:
    """Live AI Audit through the Anthropic API. Nothing (key or code) is written to disk."""
    result = AuditResult(run_dir=None, before=code, after=code, region=region, ok=False)
    started = time.monotonic()
    outcome = llm_audit.run_llm_audit(api_key, code, region, RESULT_SCHEMA)
    result.cli_meta = {**outcome.meta, "duration_ms": int((time.monotonic() - started) * 1000)}
    if outcome.error:
        result.error = outcome.error
        return result
    result.report = outcome.report
    if outcome.remediated:
        result.after = outcome.remediated
    if llm_audit.bicep_available():
        checks = llm_audit.validate_bicep_code(result.after)
        result.build = CheckResult(checks["build"]["passed"], checks["build"]["diagnostics"])
        result.lint = CheckResult(checks["lint"]["passed"], checks["lint"]["diagnostics"])
    result.ok = True
    return result


@st.cache_data
def load_demo_fixture() -> dict[str, Any]:
    return json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))


def demo_result() -> AuditResult:
    fx = load_demo_fixture()
    report = fx["report"]
    passed = report["validation"]
    return AuditResult(
        run_dir=None,
        before=fx["before"],
        after=fx["after"],
        region=fx["region"],
        ok=True,
        report=report,
        cli_meta={"engine": "pre-recorded sample", "captured_at": fx["captured_at"]},
        build=CheckResult(passed["bicep_build_passed"], "clean"),
        lint=CheckResult(passed["bicep_lint_passed"], "clean"),
    )


@st.cache_data(ttl=3600, show_spinner=False)
def cached_price(service_name: str, arm_sku_name: str, region: str) -> dict[str, Any]:
    """Hourly-cached retail price lookup (be polite to the public API)."""
    try:
        return lookup_price(service_name, arm_sku_name, region)
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}


def resolve_default_api_key() -> str:
    """Operator-provided key (st.secrets or env), used only when AUDITOR_ALLOW_SERVER_KEY=1.

    Opt-in so a public deployment never silently spends the owner's credits on
    visitors' audits. The key is never echoed to the page.
    """
    if os.environ.get("AUDITOR_ALLOW_SERVER_KEY") != "1":
        return ""
    try:
        secret = st.secrets.get("ANTHROPIC_API_KEY", "")
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        secret = ""
    return str(secret or os.environ.get("ANTHROPIC_API_KEY", ""))


DIFF_CSS = """
<style>
.diffwrap { overflow-x: auto; border: 1px solid rgba(128,128,128,.35); border-radius: 8px; }
table.diff { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px;
             border-collapse: collapse; width: 100%; }
table.diff th { background: rgba(128,128,128,.15); padding: 4px 8px; text-align: left; }
table.diff td { padding: 1px 6px; vertical-align: top; white-space: pre-wrap; }
table.diff td.diff_header { color: rgba(128,128,128,.9); text-align: right; user-select: none; }
table.diff .diff_next { display: none; }
.diff_add { background: rgba(46,160,67,.28); }
.diff_chg { background: rgba(210,153,34,.30); }
.diff_sub { background: rgba(248,81,73,.28); }
</style>
"""


def render_diff(before: str, after: str) -> None:
    if before == after:
        st.info("No changes were made to the file.")
        return
    table = difflib.HtmlDiff(wrapcolumn=70).make_table(
        before.splitlines(), after.splitlines(), "Before", "Remediated", context=False
    )
    st.html(DIFF_CSS + f'<div class="diffwrap">{table}</div>')


def sum_monthly(items: list[dict[str, Any]]) -> float:
    return round(sum(float(i["monthly_usd"]) for i in items if i.get("monthly_usd") is not None), 2)


def money(value: float) -> str:
    return f"${value:,.2f}"


def render_costs(pricing: dict[str, Any]) -> None:
    before_items: list[dict[str, Any]] = pricing.get("before", []) or []
    after_items: list[dict[str, Any]] = pricing.get("after", []) or []
    before_m, after_m = sum_monthly(before_items), sum_monthly(after_items)
    save_m = round(before_m - after_m, 2)

    with st.container(border=True):
        st.markdown(f"**Compute cost — {html.escape(str(pricing.get('region', '')))} (pay-as-you-go, 730 h/month)**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Before / month", money(before_m))
        c2.metric("After / month", money(after_m), delta=money(-save_m) if save_m else None, delta_color="inverse")
        c3.metric("Savings / month", money(save_m))
        c1.metric("Before / year", money(before_m * 12))
        c2.metric("After / year", money(after_m * 12))
        c3.metric("Savings / year", money(save_m * 12))
        if before_m:
            st.caption(f"{save_m / before_m:.1%} reduction. Prices from the azure-pricing MCP server.")

        usage_based = [i for i in before_items + after_items if i.get("monthly_usd") is None]
        if usage_based:
            names = sorted({f"{i.get('resource')} ({i.get('arm_sku_name')}, per {i.get('unit_of_measure')})" for i in usage_based})
            st.caption("Usage-based meters excluded from totals: " + "; ".join(names))

        with st.expander("Per-resource prices"):
            rows = [{"phase": "before", **i} for i in before_items] + [{"phase": "after", **i} for i in after_items]
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            else:
                st.write("No pricing data returned.")


def render_violations(violations: list[dict[str, Any]]) -> None:
    if not violations:
        st.success("No violations found.")
        return
    df = pd.DataFrame(violations)
    counts = df["category"].value_counts()
    cols = st.columns(len(counts) + 1)
    cols[0].metric("Fixed", len(df))
    for col, (cat, n) in zip(cols[1:], counts.items()):
        col.metric(str(cat).title(), int(n))
    order = ["severity", "category", "resource", "rule", "before", "after"]
    st.dataframe(df[[c for c in order if c in df.columns]], hide_index=True, width="stretch")


def badge(label: str, passed: bool | None) -> str:
    if passed is None:
        return f"⚪ {label}: n/a"
    return f"{'✅' if passed else '❌'} {label}"


def render_validation(res: AuditResult) -> None:
    reported = (res.report or {}).get("validation", {})
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Claude reported**")
        st.markdown(badge("bicep build", reported.get("bicep_build_passed")))
        st.markdown(badge("bicep lint", reported.get("bicep_lint_passed")))
        if reported.get("notes"):
            st.caption(reported["notes"])
    with c2:
        st.markdown("**Verified by this app**")
        st.markdown(badge("bicep build", res.build.passed if res.build else None))
        st.markdown(badge("bicep lint", res.lint.passed if res.lint else None))
        for name, check in (("build", res.build), ("lint", res.lint)):
            if check and not check.passed:
                st.code(check.output, language="text")


# --------------------------------------------------------------------------- UI


def init_state() -> None:
    if "code" not in st.session_state:
        head = load_git_head_bicep()
        if head is None and DEMO_FIXTURE.exists():
            head = load_demo_fixture()["before"]
        st.session_state.code = head or ""
    st.session_state.setdefault("upload_id", None)
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("api_key", resolve_default_api_key())


def render_results(res: AuditResult) -> None:
    if res.error:
        st.error(res.error)

    report = res.report or {}
    if report.get("summary"):
        st.markdown(f"**Summary.** {report['summary']}")

    tab_diff, tab_comp, tab_cost = st.tabs(["Diff", "Compliance", "Cost"])
    with tab_diff:
        render_diff(res.before, res.after)
        st.download_button(
            "Download remediated .bicep",
            data=res.after,
            file_name="main.remediated.bicep",
            mime="text/plain",
            key=f"dl-{id(res)}",
        )
    with tab_comp:
        render_violations(report.get("violations", []) or [])
        st.divider()
        render_validation(res)
    with tab_cost:
        if report.get("pricing"):
            render_costs(report["pricing"])
        else:
            st.info("No pricing data returned.")

    with st.expander("Run details"):
        if res.run_dir is not None:
            st.write(f"Run directory: `{res.run_dir.relative_to(REPO_ROOT)}`")
        meta = res.cli_meta
        if meta.get("engine") == "pre-recorded sample":
            st.caption(f"Pre-recorded sample captured {meta.get('captured_at')}; no Claude call was made.")
            st.json(report, expanded=False)
            return
        m1, m2, m3 = st.columns(3)
        m1.metric("Claude cost (USD)", f"${meta.get('total_cost_usd', 0) or 0:.4f}")
        m2.metric("Duration", f"{(meta.get('duration_ms') or 0) / 1000:.0f}s")
        m3.metric("Turns", meta.get("num_turns", "—"))
        st.json(meta, expanded=False)
        if report:
            st.json(report, expanded=False)
        if res.stderr.strip():
            st.code(res.stderr, language="text")
        if not res.ok and res.raw_stdout:
            st.code(res.raw_stdout[:20000], language="json")


def render_sidebar() -> tuple[str, str, str]:
    """Returns (mode, region, engine)."""
    with st.sidebar:
        st.header("Settings")
        mode = st.radio(
            "Mode",
            ["Demo Mode", "Live AI Audit"],
            captions=["No key needed: live prices + sample audit", "Audit any .bicep with your Anthropic key"],
        )
        region = st.selectbox("Azure pricing region", REGIONS, index=0)

        engine = ENGINE_API
        if mode == "Live AI Audit":
            st.divider()
            st.subheader("Live AI Audit")
            if shutil.which("claude"):
                engine = st.radio("Engine", [ENGINE_API, ENGINE_CLI])
            if engine == ENGINE_API:
                st.text_input(
                    "ANTHROPIC_API_KEY",
                    key="api_key",
                    type="password",
                    placeholder="sk-ant-...",
                    help="Get one at https://console.anthropic.com/settings/keys",
                )
                st.caption(
                    f"Model `{llm_audit.MODEL}`. Your key stays in this browser session's memory only: "
                    "it is not written to disk or logged. A typical audit costs a few cents."
                )
            else:
                st.caption("Runs headless Claude Code with CLAUDE.md, hooks, and the azure-pricing MCP server.")

        st.divider()
        st.caption(
            "Prices: public [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) "
            "(pay-as-you-go, USD, 730 h/month)."
        )
    return mode, region, engine


def render_price_lookup(region: str) -> None:
    st.markdown("Query the public Azure Retail Prices API live. It needs no Azure subscription or API key.")
    c1, c2 = st.columns([2, 3])
    service = c1.selectbox("Service", PRICE_SERVICES, index=0)
    sku = c2.selectbox("ARM SKU", QUICK_SKUS, index=3, accept_new_options=True)
    if st.button("Look up price", type="primary"):
        with st.spinner("Querying prices.azure.com…"):
            data = cached_price(service, str(sku), region)
        if "error" in data:
            st.warning(data["error"])
            if data.get("hint"):
                st.caption(data["hint"])
        else:
            m1, m2, m3 = st.columns(3)
            hourly = data.get("retail_price_hourly")
            monthly = data.get("estimated_monthly_cost")
            m1.metric("Hourly", f"${hourly:,.4f}" if hourly is not None else "n/a")
            m2.metric("Monthly (730 h)", money(monthly) if monthly is not None else "n/a")
            m3.metric("Yearly", money(monthly * 12) if monthly is not None else "n/a")
            st.caption(
                f"{data.get('product_name')} · meter `{data.get('meter_name')}` · "
                f"{data.get('unit_of_measure')} · effective {str(data.get('effective_start_date', ''))[:10]}"
            )
            if data.get("note"):
                st.caption(data["note"])
            variants = data.get("other_variants") or []
            if variants:
                with st.expander(f"Other meter variants ({data.get('total_matching_meters')} total)"):
                    st.dataframe(pd.DataFrame(variants), hide_index=True, width="stretch")

    st.divider()
    st.markdown("**Compare VM SKUs.** See what right-sizing a non-prod VM saves.")
    picks = st.multiselect("VM sizes", QUICK_SKUS, default=["Standard_E16s_v5", "Standard_D2s_v5", "Standard_B2s"])
    if picks and st.button("Compare"):
        rows: list[dict[str, Any]] = []
        with st.spinner(f"Pricing {len(picks)} SKUs in {region}…"):
            for name in picks:
                data = cached_price("Virtual Machines", name, region)
                rows.append(
                    {
                        "sku": name,
                        "hourly_usd": data.get("retail_price_hourly"),
                        "monthly_usd": data.get("estimated_monthly_cost"),
                        "yearly_usd": round(data["estimated_monthly_cost"] * 12, 2)
                        if data.get("estimated_monthly_cost") is not None
                        else None,
                        "error": data.get("error"),
                    }
                )
        df = pd.DataFrame(rows).sort_values("monthly_usd", ascending=False, na_position="last")
        if df["error"].isna().all():
            df = df.drop(columns="error")
        st.dataframe(df, hide_index=True, width="stretch")
        priced = df.dropna(subset=["monthly_usd"])
        if len(priced) >= 2:
            hi, lo = priced.iloc[0], priced.iloc[-1]
            st.success(
                f"Moving {hi['sku']} → {lo['sku']} saves {money(hi['monthly_usd'] - lo['monthly_usd'])}/month "
                f"({money((hi['monthly_usd'] - lo['monthly_usd']) * 12)}/year) in {region}."
            )


def render_rules() -> None:
    if CLAUDE_MD.exists():
        text = CLAUDE_MD.read_text(encoding="utf-8")
        match = re.search(r"## Architectural & Security Standards.*?(?=\n## |\Z)", text, re.DOTALL)
        st.markdown(match.group(0) if match else text)
    else:
        st.markdown("## Architectural & Security Standards\n" + llm_audit.FALLBACK_RULES)
    st.caption("These rules come from the repo's CLAUDE.md and are fed to Claude verbatim during a live audit.")


def render_demo(region: str) -> None:
    fx = load_demo_fixture()
    st.info(
        "**Demo Mode: no API key needed.** Browse a pre-recorded audit of the intentionally flawed lab "
        f"(prices captured from the Azure Retail Prices API in `{fx['region']}` on {fx['captured_at']}), "
        "or run live price lookups. Switch to **Live AI Audit** in the sidebar to audit your own .bicep file.",
        icon="🧪",
    )
    tab_sample, tab_price, tab_rules = st.tabs(["Sample audit", "Live price lookup", "Rules"])
    with tab_sample:
        render_results(demo_result())
    with tab_price:
        render_price_lookup(region)
    with tab_rules:
        render_rules()


def render_live(region: str, engine: str) -> None:
    left, right = st.columns([5, 7], gap="large")

    with left:
        st.subheader("Input")
        uploaded = st.file_uploader("Upload a .bicep file", type=["bicep"])
        if uploaded is not None and uploaded.file_id != st.session_state.upload_id:
            st.session_state.upload_id = uploaded.file_id
            if uploaded.size > MAX_UPLOAD_BYTES:
                st.error(f"File is {uploaded.size:,} bytes; the limit is {MAX_UPLOAD_BYTES:,}.")
            else:
                try:
                    st.session_state.code = uploaded.getvalue().decode("utf-8")
                except UnicodeDecodeError:
                    st.error("File is not valid UTF-8 text.")

        b1, b2 = st.columns(2)
        if b1.button("Load flawed lab", width="stretch"):
            head = load_git_head_bicep() or load_demo_fixture()["before"]
            st.session_state.code = head
        if b2.button("Load infra/main.bicep", width="stretch") and DEFAULT_BICEP.exists():
            st.session_state.code = DEFAULT_BICEP.read_text(encoding="utf-8")

        st.text_area("Bicep", key="code", height=480, label_visibility="collapsed")

        api_key = (st.session_state.get("api_key") or "").strip()
        needs_key = engine == ENGINE_API and not api_key
        too_big = len(st.session_state.code.encode("utf-8")) > MAX_UPLOAD_BYTES
        if needs_key:
            st.warning("Enter an ANTHROPIC_API_KEY in the sidebar to run a live audit.", icon="🔑")
        if too_big:
            st.error(f"Input exceeds {MAX_UPLOAD_BYTES:,} bytes.")

        run = st.button(
            "Audit & Remediate with Claude",
            type="primary",
            width="stretch",
            disabled=needs_key or too_big or not st.session_state.code.strip(),
        )
        if run:
            label = "Claude is auditing via the Anthropic API…" if engine == ENGINE_API else "Running Claude Code headless…"
            with st.status(label, expanded=False) as status:
                st.write(f"Region: `{region}` · engine: {engine}")
                if engine == ENGINE_API:
                    res = run_api_audit(api_key, st.session_state.code, region)
                else:
                    res = run_audit(st.session_state.code, region)
                st.session_state.result = res
                status.update(
                    label="Audit complete" if res.ok else "Audit failed",
                    state="complete" if res.ok else "error",
                )

    with right:
        res: AuditResult | None = st.session_state.result
        st.subheader("Results")
        if res is None:
            st.info("Run an audit to see the diff, the violations fixed, and the cost comparison.")
            return
        render_results(res)


def main() -> None:
    st.set_page_config(page_title="IaC FinOps Drift Auditor", page_icon="🛡️", layout="wide")
    init_state()

    st.title("Azure IaC & FinOps Drift Auditor")
    st.caption(
        "Audits Azure Bicep against governance rules, remediates it with Claude, "
        "and prices the before/after with live Azure retail SKU prices."
    )

    mode, region, engine = render_sidebar()
    if mode == "Demo Mode":
        render_demo(region)
    else:
        render_live(region, engine)


if __name__ == "__main__":
    main()
