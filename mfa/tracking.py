"""
Track record: how did past recommendations do versus simply holding the
Nifty 50 index fund over the same period?

Every build-mode run appends its recommended funds (with their NAV on that day)
to RECOMMENDATIONS_FILE; later runs compare the weighted point-to-point return
since then with the index fund's. Action-plan switches are tracked the same way
(kept fund vs suggested fund) in mfa/holdings/db.py via `since_return`.
"""
from datetime import datetime
import html as html_lib
import json
import os

from rich.table import Table

from mfa.config import INDEX_FUND_NAME, RECOMMENDATIONS_FILE
from mfa.data.nav import fetch_scheme_nav
from mfa.ui import console
from mfa.util import _save_output_file

MAX_ENTRIES   = 120
MIN_DAYS_SHOW = 30        # younger recommendations are listed as "too early"


# ── pure helpers ────────────────────────────────────────────────────────
def pct_change(start: float | None, end: float | None) -> float | None:
    if not start or not end or start <= 0:
        return None
    return (end / start - 1) * 100


def annualise(total_pct: float | None, days: int) -> float | None:
    if total_pct is None or days < 365:
        return None
    return ((1 + total_pct / 100) ** (365.25 / days) - 1) * 100


def evaluate_entry(entry: dict, current_nav, index_now: float | None,
                   today: datetime | None = None) -> dict:
    """
    entry: {date, strategy, funds:[{code,name,weight_pct,nav}], index_nav}
    current_nav: callable code -> latest NAV (or None)
    """
    today = today or datetime.now()
    days = (today - datetime.fromisoformat(entry["date"])).days
    num = den = 0.0
    missing = []
    for f in entry.get("funds", []):
        r = pct_change(f.get("nav"), current_nav(str(f.get("code"))))
        if r is None:
            missing.append(f.get("name", f.get("code")))
            continue
        w = f.get("weight_pct") or 0
        num += w * r
        den += w
    port = num / den if den else None
    idx = pct_change(entry.get("index_nav"), index_now)
    return {
        "date": entry["date"], "strategy": entry.get("strategy", "?"), "days": days,
        "funds": [f.get("name") for f in entry.get("funds", [])],
        "portfolio_pct": port, "index_pct": idx,
        "portfolio_pa": annualise(port, days), "index_pa": annualise(idx, days),
        "diff_pct": (port - idx) if (port is not None and idx is not None) else None,
        "missing": missing, "too_early": days < MIN_DAYS_SHOW,
    }


# ── storage ─────────────────────────────────────────────────────────────
def load_recommendations(path: str = RECOMMENDATIONS_FILE) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("entries", [])
    except Exception:
        return []


def record_recommendation(portfolio: list, strategy: str, index_nav: float | None,
                          path: str = RECOMMENDATIONS_FILE, today: str | None = None) -> bool:
    """Append today's recommendation; one entry per (date, strategy, fund set)."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    funds = [{"code": str(f["scheme_code"]), "name": f["name"],
              "weight_pct": f.get("weight_pct"), "nav": f.get("latest_nav")}
             for f in portfolio if f.get("latest_nav")]
    if not funds:
        return False
    entries = load_recommendations(path)
    key = (today, strategy, tuple(sorted(f["code"] for f in funds)))
    if any((e["date"], e.get("strategy"), tuple(sorted(f["code"] for f in e["funds"]))) == key
           for e in entries):
        return False
    entries.append({"date": today, "strategy": strategy, "funds": funds, "index_nav": index_nav})
    entries = entries[-MAX_ENTRIES:]
    _p = json.dumps({"entries": entries}, indent=1).encode()
    _save_output_file(lambda fh: fh.write(_p), path)
    return True


def nav_lookup_from(nav_data: list) -> dict:
    out = {}
    for item in nav_data or []:
        try:
            out[str(item["scheme"].get("schemeCode"))] = float(item["nav_df"]["nav"].iloc[-1])
        except Exception:
            pass
    return out


def current_nav_fn(nav_lookup: dict):
    """code -> latest NAV: universe first, then a single-scheme fetch (cached 24h)."""
    def _get(code: str):
        if code in nav_lookup:
            return nav_lookup[code]
        df = fetch_scheme_nav(code)
        nav = float(df["nav"].iloc[-1]) if df is not None and not df.empty else None
        nav_lookup[code] = nav
        return nav
    return _get


def since_return(code, nav_then, nav_lookup: dict) -> float | None:
    if not code or not nav_then:
        return None
    return pct_change(nav_then, current_nav_fn(nav_lookup)(str(code)))


def evaluate_recommendations(nav_lookup: dict, index_now: float | None,
                             entries: list | None = None) -> list:
    entries = load_recommendations() if entries is None else entries
    get = current_nav_fn(dict(nav_lookup))
    return [evaluate_entry(e, get, index_now) for e in reversed(entries)]


# ── output ──────────────────────────────────────────────────────────────
def _fmt(v, signed=True):
    return "—" if v is None else (f"{v:+.1f}%" if signed else f"{v:.1f}%")


def print_track_record(rows: list, limit: int = 8) -> None:
    rows = [r for r in rows if not r["too_early"]][:limit]
    if not rows:
        return
    t = Table(title=f"Track record — past recommendations vs {INDEX_FUND_NAME}",
              border_style="cyan")
    for c in ("Recommended", "Strategy", "Held", "Picks", "Index", "Difference", "Picks p.a.", "Index p.a."):
        t.add_column(c, justify="left" if c in ("Recommended", "Strategy") else "right")
    for r in rows:
        diff_col = "green" if (r["diff_pct"] or 0) >= 0 else "red"
        t.add_row(r["date"], r["strategy"], f"{r['days']}d",
                  _fmt(r["portfolio_pct"]), _fmt(r["index_pct"]),
                  f"[{diff_col}]{_fmt(r['diff_pct'])}[/{diff_col}]",
                  _fmt(r["portfolio_pa"]), _fmt(r["index_pa"]))
    console.print(t)


def track_record_html(rows: list, backtest: dict | None) -> str:
    """HTML section: live track record + the backtest verdict."""
    esc = html_lib.escape
    parts = ['<div class="section"><h2>📈 Track Record &amp; Model Check</h2>']
    if backtest:
        parts.append(
            f"<p><b>Backtest ({esc(backtest.get('generated_on', ''))}):</b> "
            f"{esc(backtest.get('verdict', ''))} <span style='color:#64748b'>"
            f"(see mf_backtest_report.html)</span></p>")
    else:
        parts.append("<p style='color:#64748b'>No backtest yet — run menu option 4 to test "
                     "whether the scoring model has picked winners in the past.</p>")
    shown = [r for r in rows if not r["too_early"]][:10]
    if shown:
        trs = "".join(
            f"<tr><td>{esc(r['date'])}</td><td>{esc(r['strategy'])}</td><td>{r['days']}d</td>"
            f"<td>{_fmt(r['portfolio_pct'])}</td><td>{_fmt(r['index_pct'])}</td>"
            f"<td style='color:{'#15803d' if (r['diff_pct'] or 0) >= 0 else '#b91c1c'}'>"
            f"{_fmt(r['diff_pct'])}</td></tr>" for r in shown)
        parts.append(
            "<table><tr><th>Recommended</th><th>Strategy</th><th>Held</th><th>Picks</th>"
            f"<th>{esc(INDEX_FUND_NAME)}</th><th>Difference</th></tr>{trs}</table>"
            "<p style='font-size:12px;color:#64748b'>Point-to-point NAV return of the recommended "
            "funds (by weight) since the day they were recommended, vs the index fund over the "
            "same days. Lump-sum basis; ignores SIP timing.</p>")
    else:
        n_early = sum(r["too_early"] for r in rows)
        parts.append(f"<p style='color:#64748b'>{'No past recommendations yet.' if not rows else f'{n_early} recommendation(s) too recent to judge (under {MIN_DAYS_SHOW} days).'}</p>")
    parts.append("</div>")
    return "".join(parts)
