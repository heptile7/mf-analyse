"""
Walk-forward backtest of the fund-scoring model.

For each cutoff T (31 Dec of each year) the universe is scored using ONLY data up
to T: NAV histories and benchmark series are truncated, and no enrichment or TER
overrides are used (those are today's values and would leak the future). The
picks are then judged on what happened next (1Y / 3Y forward CAGR):

  hit rate      — share of top picks that beat their category median
  percentile    — where the picks landed inside their category (0.5 = median)
  IC            — Spearman rank correlation of score vs forward return, per category
  basket        — equal-weight top-3-per-category vs a real Nifty 50 index fund

Every metric is also computed for a naive baseline ("pick the best trailing-3Y
return"), which answers: does the 7-component score beat chasing past winners?

Caveats (always shown): live funds only (survivorship), today's SEBI category
labels, enrichment components (valuation / ext_rank / capacity, real TERs) untested.
"""
from datetime import datetime
import html as html_lib
import json

import numpy as np
import pandas as pd
from rich.panel import Panel
from rich.table import Table

from mfa.config import BACKTEST_HTML_FILE, BACKTEST_SUMMARY_FILE, INDEX_FUND_NAME
from mfa.metrics import cagr, nav_on_or_before
from mfa.portfolio import score_universe
from mfa.ui import console
from mfa.util import _save_output_file

CUTOFF_YEARS = tuple(range(2015, 2024))
HORIZONS     = (1, 3)
MIN_CATEGORY = 5          # categories smaller than this are skipped
TOP_K        = 3
CAVEATS = [
    "Survivorship: only funds alive today are in the universe, so the field each "
    "pick is compared against is slightly flattered.",
    "Categories are today's SEBI labels; many funds changed category in the 2017–18 "
    "recategorisation.",
    "Only NAV-derived components (information ratio, capture, consistency) plus a "
    "category-default cost were tested — valuation, external rank, AUM capacity and "
    "real TERs are today's data and were excluded to avoid look-ahead.",
    "Past hit rates are evidence, not a guarantee.",
]


# ── as-of helpers ───────────────────────────────────────────────────────
def truncate_nav_data(nav_data: list, cutoff: pd.Timestamp, min_rows: int = 252) -> list:
    out = []
    for item in nav_data:
        df = item["nav_df"]
        sub = df[df["date"] <= cutoff]
        if len(sub) >= min_rows:
            out.append({"scheme": item["scheme"], "nav_df": sub.reset_index(drop=True)})
    return out


def truncate_benchmarks(benchmarks: dict, cutoff: pd.Timestamp) -> dict:
    # Rows are indexed at the 1st of the month and hold that month's return, so a
    # month-end cutoff includes its own month and nothing after it.
    return {k: s[s.index <= cutoff] for k, s in (benchmarks or {}).items()}


def forward_cagr(df: pd.DataFrame, cutoff: pd.Timestamp, years: int) -> float | None:
    end_target = cutoff + pd.DateOffset(years=years)
    if df is None or df.empty or df["date"].max() < end_target - pd.Timedelta(days=7):
        return None
    start = nav_on_or_before(df, cutoff)
    end   = nav_on_or_before(df, end_target)
    if not start or not end:
        return None
    return cagr(start, end, years)


def spearman(x, y) -> float | None:
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _pick_stats(ranked: list, fwd: dict, k: int) -> list:
    """For the top-k of `ranked` (codes), return [(beat_median, percentile)]."""
    values = np.array(list(fwd.values()), dtype=float)
    median = float(np.median(values))
    out = []
    for code in ranked[:k]:
        r = fwd[code]
        pct = float((values < r).sum() + 0.5 * ((values == r).sum() - 1)) / max(len(values) - 1, 1)
        out.append((r > median, pct))
    return out


# ── one cutoff ──────────────────────────────────────────────────────────
def evaluate_cutoff(nav_data: list, benchmarks: dict, index_df, cutoff: pd.Timestamp,
                    optimise: str = "balanced", horizons=HORIZONS) -> list:
    """Returns one row per horizon with per-category detail and summary numbers."""
    trunc = truncate_nav_data(nav_data, cutoff)
    if not trunc:
        return []
    scored, _ = score_universe(trunc, optimise, False,
                               benchmarks=truncate_benchmarks(benchmarks, cutoff),
                               enrichment={}, ter_overrides=None, verbose=False)
    full_df = {str(i["scheme"].get("schemeCode")): i["nav_df"] for i in nav_data}

    rows = []
    for h in horizons:
        by_cat: dict = {}
        for f in scored:
            if f["category"] in ("other", "debt"):
                continue
            r = forward_cagr(full_df.get(str(f["scheme_code"])), cutoff, h)
            if r is not None:
                by_cat.setdefault(f["category"], []).append((f, r))
        cats = {c: v for c, v in by_cat.items() if len(v) >= MIN_CATEGORY}
        if not cats:
            continue

        model_hits, naive_hits, model_pct, naive_pct = [], [], [], []
        model_top1, naive_top1, model_ic, naive_ic = [], [], [], []
        model_basket, naive_basket, median_all = [], [], []
        detail = []
        for cat, pairs in sorted(cats.items()):
            fwd = {str(f["scheme_code"]): r for f, r in pairs}
            by_model = [str(f["scheme_code"]) for f, _ in sorted(pairs, key=lambda p: -p[0]["score"])]
            by_naive = [str(f["scheme_code"]) for f, _ in sorted(
                pairs, key=lambda p: -(p[0].get("return_3y") if p[0].get("return_3y") is not None else -1e9))]
            ms, ns = _pick_stats(by_model, fwd, TOP_K), _pick_stats(by_naive, fwd, TOP_K)
            model_hits += [b for b, _ in ms];  model_pct += [p for _, p in ms]
            naive_hits += [b for b, _ in ns];  naive_pct += [p for _, p in ns]
            model_top1.append(ms[0][0]);       naive_top1.append(ns[0][0])
            ic_m = spearman([f["score"] for f, _ in pairs], [r for _, r in pairs])
            r3 = [(f.get("return_3y"), r) for f, r in pairs if f.get("return_3y") is not None]
            ic_n = spearman([a for a, _ in r3], [b for _, b in r3])
            if ic_m is not None: model_ic.append(ic_m)
            if ic_n is not None: naive_ic.append(ic_n)
            model_basket += [fwd[c] for c in by_model[:TOP_K]]
            naive_basket += [fwd[c] for c in by_naive[:TOP_K]]
            median_all.append(float(np.median(list(fwd.values()))))
            top = next(f for f, _ in pairs if str(f["scheme_code"]) == by_model[0])
            detail.append({"category": cat, "n": len(pairs), "top_pick": top["name"],
                           "top_pick_fwd": round(fwd[by_model[0]], 2),
                           "category_median": round(float(np.median(list(fwd.values()))), 2),
                           "ic": None if ic_m is None else round(ic_m, 3)})

        idx = forward_cagr(index_df, cutoff, h) if index_df is not None else None
        rows.append({
            "cutoff": cutoff.strftime("%Y-%m-%d"), "horizon": h,
            "n_funds": sum(len(v) for v in cats.values()), "n_categories": len(cats),
            "model": {"hit_rate": float(np.mean(model_hits)), "top1_hit_rate": float(np.mean(model_top1)),
                      "mean_percentile": float(np.mean(model_pct)),
                      "ic": float(np.mean(model_ic)) if model_ic else None,
                      "basket_cagr": float(np.mean(model_basket))},
            "naive": {"hit_rate": float(np.mean(naive_hits)), "top1_hit_rate": float(np.mean(naive_top1)),
                      "mean_percentile": float(np.mean(naive_pct)),
                      "ic": float(np.mean(naive_ic)) if naive_ic else None,
                      "basket_cagr": float(np.mean(naive_basket))},
            "avg_fund_cagr": float(np.mean(median_all)),
            "index_cagr": idx,
            "categories": detail,
        })
    return rows


# ── full run ────────────────────────────────────────────────────────────
def _mean(rows, path):
    vals = []
    for r in rows:
        v = r
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if v is not None:
            vals.append(v)
    return float(np.mean(vals)) if vals else None


def summarise(rows: list) -> dict:
    out = {}
    for h in HORIZONS:
        hr = [r for r in rows if r["horizon"] == h]
        if not hr:
            continue
        s = {"n_cutoffs": len(hr)}
        for who in ("model", "naive"):
            s[who] = {k: _mean(hr, (who, k)) for k in
                      ("hit_rate", "top1_hit_rate", "mean_percentile", "ic", "basket_cagr")}
        s["avg_fund_cagr"] = _mean(hr, ("avg_fund_cagr",))
        s["index_cagr"]    = _mean(hr, ("index_cagr",))
        with_idx = [r for r in hr if r["index_cagr"] is not None]
        s["model_beat_index"] = (sum(r["model"]["basket_cagr"] > r["index_cagr"] for r in with_idx)
                                 if with_idx else None)
        s["n_with_index"] = len(with_idx)
        out[str(h)] = s
    return out


def verdict(summary: dict) -> str:
    """One plain-words sentence on whether the score has shown an edge."""
    s = summary.get("3") or summary.get("1")
    if not s:
        return "Not enough history to judge the scoring model."
    m, n = s["model"], s["naive"]
    edge_vs_naive = (m["hit_rate"] or 0) - (n["hit_rate"] or 0)
    h = "3-year" if "3" in summary else "1-year"
    base = (f"Over {s['n_cutoffs']} past start dates, the model's top picks beat their category "
            f"median {m['hit_rate']:.0%} of the time on a {h} view "
            f"(chasing last 3Y's winners: {n['hit_rate']:.0%}). ")
    if m["hit_rate"] >= 0.6 and edge_vs_naive >= 0.05:
        return base + "That is a real but modest edge — use the score, don't worship it."
    if m["hit_rate"] >= 0.5:
        return base + "That is close to a coin flip — treat rankings as a tie-breaker, not a signal."
    return base + "The score has NOT shown an edge — prefer a low-cost index fund as the core."


def run_backtest(nav_data: list, benchmarks: dict, index_df, optimise: str = "balanced",
                 years=CUTOFF_YEARS) -> dict:
    rows = []
    for y in years:
        cutoff = pd.Timestamp(f"{y}-12-31")
        console.print(f"  [dim]Backtest cutoff {cutoff.date()}...[/dim]")
        rows += evaluate_cutoff(nav_data, benchmarks, index_df, cutoff, optimise)
    summary = summarise(rows)
    return {"generated_on": datetime.now().strftime("%Y-%m-%d"), "strategy": optimise,
            "index_fund": INDEX_FUND_NAME, "rows": rows, "summary": summary,
            "verdict": verdict(summary), "caveats": CAVEATS}


# ── output ──────────────────────────────────────────────────────────────
def _ic(v) -> str:
    return "—" if v is None else f"{v:+.2f}"


def _p(v, pct=True):
    if v is None:
        return "—"
    return f"{v:.0%}" if pct else f"{v:.1f}%"


def print_backtest(result: dict) -> None:
    t = Table(title=f"Scoring model backtest ({result['strategy']})", border_style="cyan")
    t.add_column("Horizon"); t.add_column("Cutoffs", justify="right")
    t.add_column("Hit rate\nmodel / naive", justify="right")
    t.add_column("Top-1 hit\nmodel / naive", justify="right")
    t.add_column("Avg percentile\nmodel / naive", justify="right")
    t.add_column("Rank IC\nmodel / naive", justify="right")
    t.add_column("Top-3 basket CAGR\nmodel / naive / avg fund / index", justify="right")
    for h, s in result["summary"].items():
        m, n = s["model"], s["naive"]
        t.add_row(f"{h}Y", str(s["n_cutoffs"]),
                  f"{_p(m['hit_rate'])} / {_p(n['hit_rate'])}",
                  f"{_p(m['top1_hit_rate'])} / {_p(n['top1_hit_rate'])}",
                  f"{_p(m['mean_percentile'])} / {_p(n['mean_percentile'])}",
                  f"{_ic(m['ic'])} / {_ic(n['ic'])}",
                  f"{_p(m['basket_cagr'], False)} / {_p(n['basket_cagr'], False)} / "
                  f"{_p(s['avg_fund_cagr'], False)} / {_p(s['index_cagr'], False)}")
    console.print(t)
    console.print(Panel(f"[bold]{result['verdict']}[/bold]", title="In plain words",
                        border_style="green"))
    console.print("[dim]" + "\n".join(f"• {c}" for c in result["caveats"]) + "[/dim]")


def save_backtest(result: dict) -> str:
    """Write the summary JSON (read by the main report) and the HTML report."""
    slim = {k: result[k] for k in ("generated_on", "strategy", "index_fund", "summary", "verdict")}
    _js = json.dumps(slim, indent=2).encode()
    _save_output_file(lambda fh: fh.write(_js), BACKTEST_SUMMARY_FILE)

    esc = html_lib.escape
    srows = []
    for h, s in result["summary"].items():
        m, n = s["model"], s["naive"]
        srows.append(
            f"<tr><td>{h}Y</td><td>{s['n_cutoffs']}</td>"
            f"<td>{_p(m['hit_rate'])}</td><td>{_p(n['hit_rate'])}</td>"
            f"<td>{_p(m['mean_percentile'])}</td><td>{_p(n['mean_percentile'])}</td>"
            f"<td>{_ic(m['ic'])}</td><td>{_ic(n['ic'])}</td>"
            f"<td>{_p(m['basket_cagr'], False)}</td><td>{_p(s['avg_fund_cagr'], False)}</td>"
            f"<td>{_p(s['index_cagr'], False)}</td></tr>")
    crows = []
    for r in result["rows"]:
        for c in r["categories"]:
            beat = c["top_pick_fwd"] > c["category_median"]
            crows.append(
                f"<tr><td>{r['cutoff'][:4]}</td><td>{r['horizon']}Y</td><td>{esc(c['category'])}</td>"
                f"<td>{c['n']}</td><td>{esc(c['top_pick'])}</td>"
                f"<td class='{'good' if beat else 'bad'}'>{c['top_pick_fwd']:.1f}%</td>"
                f"<td>{c['category_median']:.1f}%</td>"
                f"<td>{_ic(c['ic'])}</td></tr>")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scoring Model Backtest</title>
<style>
:root{{--bg:#f8fafc;--fg:#0f172a;--muted:#64748b;--card:#fff;--line:#e2e8f0;--good:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b1220;--fg:#e2e8f0;--muted:#94a3b8;--card:#111a2e;--line:#1e293b;--good:#4ade80;--bad:#f87171}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child,td:nth-child(3),td:nth-child(5){{text-align:left}}
.good{{color:var(--good)}}.bad{{color:var(--bad)}}.muted{{color:var(--muted)}}
.verdict{{font-size:17px;font-weight:600}}
</style></head><body><main>
<h1>Does the fund score pick winners?</h1>
<p class="muted">Walk-forward backtest · strategy weights: {esc(result['strategy'])} · generated {result['generated_on']} · yardstick: {esc(result['index_fund'])}</p>
<div class="card"><div class="verdict">{esc(result['verdict'])}</div></div>
<div class="card"><h2>Summary</h2><table>
<tr><th>Horizon</th><th>Cutoffs</th><th>Hit rate (model)</th><th>Hit rate (naive)</th>
<th>Percentile (model)</th><th>Percentile (naive)</th><th>IC (model)</th><th>IC (naive)</th>
<th>Top-3 basket</th><th>Avg fund</th><th>Index fund</th></tr>
{''.join(srows)}</table>
<p class="muted">Hit rate: share of top-3 picks per category that beat their category median over the next horizon. Percentile 50% = median fund. IC: rank correlation of score with forward return (0 = no information). Naive = rank by trailing 3Y return. Returns are annualised.</p></div>
<div class="card"><h2>Every top pick</h2><table>
<tr><th>Year</th><th>Horizon</th><th>Category</th><th>Funds</th><th>Model's #1 pick</th><th>Its return</th><th>Category median</th><th>IC</th></tr>
{''.join(crows)}</table></div>
<div class="card"><h2>Read this before trusting the numbers</h2><ul>{''.join(f'<li>{esc(c)}</li>' for c in result['caveats'])}</ul></div>
</main></body></html>"""
    _pg = page.encode("utf-8")
    return _save_output_file(lambda fh: fh.write(_pg), BACKTEST_HTML_FILE)


def load_backtest_summary() -> dict | None:
    try:
        with open(BACKTEST_SUMMARY_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
