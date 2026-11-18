"""
Data status: what loaded, how fresh it is, and how much of the score it supports.

The friend's-guide rule: stale or missing data is shown loudly, never papered
over. Fetchers (or the CLI, from what they returned) call `record`; reports read
`snapshot` and `score_completeness` to show a banner and a per-source table.
"""
from datetime import datetime
import html as html_lib

import numpy as np
import pandas as pd
from rich.table import Table

from mfa.config import DEFAULT_TER
from mfa.ui import console

NAV_REFUSE_COVERAGE = 0.80     # below this, build mode will not recommend
NAV_STALE_BDAYS     = 5

_REGISTRY: dict = {}

COMPONENT_INPUTS = {
    "info_ratio":  lambda f: f.get("info_ratio") is not None,
    "capture":     lambda f: f.get("capture_ratio") is not None,
    "consistency": lambda f: f.get("rank_consistency") is not None,
    "cost":        lambda f: f.get("ter") is not None
                             and f.get("ter") != DEFAULT_TER.get(f.get("category"), 0.80),
    "capacity":    lambda f: f.get("aum_cr") is not None,
    "valuation":   lambda f: f.get("pe_ratio") is not None and f.get("category_pe") is not None,
    "ext_rank":    lambda f: bool(f.get("mfdata_returns")) or f.get("rating") is not None,
}


def reset() -> None:
    _REGISTRY.clear()


def record(source: str, ok: bool, detail: str = "", as_of: str | None = None,
           coverage: float | None = None) -> None:
    _REGISTRY[source] = {"ok": bool(ok), "detail": detail, "as_of": as_of, "coverage": coverage}


def snapshot() -> dict:
    return {k: dict(v) for k, v in _REGISTRY.items()}


# ── assessors ───────────────────────────────────────────────────────────
def nav_latest_date(nav_data: list):
    dates = [item["nav_df"]["date"].max() for item in nav_data or [] if not item["nav_df"].empty]
    return max(dates) if dates else None


def business_days_old(ts, today=None) -> int | None:
    if ts is None:
        return None
    today = pd.Timestamp(today or datetime.now()).normalize()
    return max(int(np.busday_count(pd.Timestamp(ts).date(), today.date())), 0)


def assess_sources(nav_data: list, amfi_master: dict, benchmarks: dict, enrichment: dict,
                   portfolio_holdings: dict, today=None) -> None:
    """Record status for everything the CLI fetched (NAV coverage is recorded by nav.py)."""
    latest = nav_latest_date(nav_data)
    stale = business_days_old(latest, today)
    nav = _REGISTRY.get("NAV history (mfapi.in)", {"ok": bool(nav_data), "coverage": None, "detail": ""})
    nav["as_of"] = latest.strftime("%Y-%m-%d") if latest is not None else None
    if stale is not None and stale > NAV_STALE_BDAYS:
        nav["ok"] = False
        nav["detail"] = (nav.get("detail", "") + f" Latest NAV is {stale} business days old.").strip()
    _REGISTRY["NAV history (mfapi.in)"] = nav

    parsed = pd.to_datetime(pd.Series([v.get("nav_date") for v in (amfi_master or {}).values()],
                                      dtype=object), errors="coerce", format="%d-%b-%Y").dropna()
    record("AMFI scheme master", bool(amfi_master),
           f"{len(amfi_master or {}):,} schemes" if amfi_master else "unavailable — categories fall back to names",
           as_of=f"{parsed.max():%Y-%m-%d}" if not parsed.empty else None)
    if benchmarks:
        last = max(s.index.max() for s in benchmarks.values())
        first = min(s.index.min() for s in benchmarks.values())
        record("Benchmarks (yfinance)", "^NSEI" in benchmarks,
               f"{', '.join(sorted(benchmarks))} since {first:%Y}", as_of=f"{last:%Y-%m}")
    else:
        record("Benchmarks (yfinance)", False, "unavailable — no alpha, information ratio or capture")
    record("Enrichment (mfdata.in)", bool(enrichment),
           f"{len(enrichment):,} funds" if enrichment
           else "unavailable — valuation, external rank, AUM capacity and real TERs fall back to neutral")
    record("Portfolio disclosures", bool(portfolio_holdings),
           f"{len(portfolio_holdings):,} funds" if portfolio_holdings else "unavailable — no holdings overlap view")


def score_completeness(scored: list) -> dict:
    """Which of the 7 components had real inputs for most of the universe."""
    n = len(scored) or 1
    shares = {c: sum(1 for f in scored if test(f)) / n for c, test in COMPONENT_INPUTS.items()}
    real = [c for c, s in shares.items() if s >= 0.5]
    return {"shares": shares, "real": real, "missing": [c for c in shares if c not in real],
            "n_real": len(real), "n_total": len(shares)}


def nav_refusal_reason() -> str | None:
    nav = _REGISTRY.get("NAV history (mfapi.in)") or {}
    cov = nav.get("coverage")
    if cov is not None and cov < NAV_REFUSE_COVERAGE:
        return (f"Only {cov:.0%} of the fund universe loaded (need {NAV_REFUSE_COVERAGE:.0%}). "
                "Recommendations from a partial universe would be misleading — re-run later "
                "or use menu option 3 to refresh the cache.")
    return None


def is_degraded(completeness: dict | None = None) -> bool:
    return (any(not v["ok"] for v in _REGISTRY.values())
            or bool(completeness and completeness["missing"]))


# ── output ──────────────────────────────────────────────────────────────
def _cov(v):
    return "" if v is None else f" · {v:.0%} coverage"


def print_status(completeness: dict | None = None) -> None:
    t = Table(title="Data status", border_style="dim", show_lines=False)
    t.add_column("Source"); t.add_column(""); t.add_column("As of"); t.add_column("Detail")
    for src, v in _REGISTRY.items():
        t.add_row(src, "[green]OK[/green]" if v["ok"] else "[yellow]DEGRADED[/yellow]",
                  v.get("as_of") or "—", (v.get("detail") or "") + _cov(v.get("coverage")))
    console.print(t)
    if completeness:
        col = "green" if not completeness["missing"] else "yellow"
        console.print(f"  [{col}]Scores use {completeness['n_real']} of {completeness['n_total']} "
                      f"components with real data[/{col}]"
                      + (f"[dim] — neutral fallback for: {', '.join(completeness['missing'])}[/dim]"
                         if completeness["missing"] else ""))


def status_banner_html(completeness: dict | None = None) -> str:
    if not is_degraded(completeness):
        return ""
    bad = [k for k, v in _REGISTRY.items() if not v["ok"]]
    parts = []
    if bad:
        parts.append("Degraded: " + ", ".join(html_lib.escape(b) for b in bad) + ".")
    if completeness and completeness["missing"]:
        parts.append(f"Scores use only {completeness['n_real']} of {completeness['n_total']} "
                     f"components with real data (neutral for {', '.join(completeness['missing'])}).")
    return ("<div style='background:#fff7ed;color:#9a3412;border:1px solid #fed7aa;border-radius:10px;"
            "padding:12px 14px;margin-bottom:16px;font-size:13px;'>⚠️ <b>Data quality:</b> "
            + " ".join(parts) + " See <i>Data status</i> below.</div>")


def status_section_html(completeness: dict | None = None) -> str:
    esc = html_lib.escape
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td style='color:{'#15803d' if v['ok'] else '#b45309'}'>"
        f"{'OK' if v['ok'] else 'DEGRADED'}</td><td>{esc(v.get('as_of') or '—')}</td>"
        f"<td>{esc((v.get('detail') or '') + _cov(v.get('coverage')))}</td></tr>"
        for k, v in _REGISTRY.items())
    comp = ""
    if completeness:
        comp = "<p style='font-size:12px;color:#475569'>Share of funds with real inputs per score component: " + \
            ", ".join(f"{esc(c)} {s:.0%}" for c, s in completeness["shares"].items()) + ".</p>"
    return ("<div class='section'><h2>🩺 Data Status</h2><table><tr><th>Source</th><th>Status</th>"
            f"<th>As of</th><th>Detail</th></tr>{rows}</table>{comp}</div>")
