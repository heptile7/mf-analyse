"""Chart pack, self-contained HTML report and terminal summary."""

from datetime import datetime
import base64
import html as html_lib
import io

import matplotlib
matplotlib.use("Agg")
from rich.panel import Panel
from rich.table import Table
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from mfa.config import CHART_FILE, HTML_REPORT_FILE, MC_N_SIMS_STANDARD, MIDCAP_TICKERS, PASSIVE_DEBT_RETURN, PASSIVE_TER_DEBT, PASSIVE_TER_EQUITY, RISK_FREE_MONTHLY, SCORE_WEIGHTS
from mfa.portfolio import portfolio_risk_metrics
from mfa.ui import console, fmt_inr, pct_str, val_str
from mfa.util import _safe_float, _save_output_file


# ══════════════════════════════════════════════════════════════════════
# VISUALIZATION
# ══════════════════════════════════════════════════════════════════════

def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

def generate_charts(
    portfolio: list, cohort: dict,
    monthly_sip: int, timeline_years: float, optimise: str,
    benchmarks: dict = None, market_context: str = "",
    passive_mc: dict = None,
    holdings_analysis: list = None,
) -> dict:
    """
    4-panel 2×2 chart — each panel earns its space:
      TL: Allocation donut + IR/capture bar per fund
      TR: Peer scatter — every category peer shown, portfolio funds highlighted
      BL: Monte Carlo projection fan (P10/P50/P90) vs passive baseline
      BR: Existing portfolio HOLD/SELL/BUY bar chart  OR  score breakdown if no holdings
    """
    from matplotlib.patches import Patch
    benchmarks      = benchmarks or {}
    passive_mc      = passive_mc or {}
    holdings_analysis = holdings_analysis or []
    nifty_b         = benchmarks.get("^NSEI", pd.Series(dtype=float))
    n               = len(portfolio)
    pal             = matplotlib.colormaps["tab10"]
    clr             = [pal(i / max(n - 1, 1)) for i in range(n)]
    snm             = [f["name"][:28] for f in portfolio]
    months_arr      = np.arange(0, int(timeline_years * 12) + 1)

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(2, 2, figsize=(18, 12), constrained_layout=True)
    fig.suptitle(
        f"MF Portfolio — {optimise.title()}  ·  ₹{monthly_sip:,}/mo  ·  {timeline_years:.0f}Y"
        + (f"  ·  {market_context[:40]}" if market_context else ""),
        fontsize=13, fontweight="bold", y=1.01,
    )
    ax_tl, ax_tr, ax_bl, ax_br = axes[0,0], axes[0,1], axes[1,0], axes[1,1]

    # ── TL: Allocation donut + IR·capture bar ────────────────────────────
    ax_tl.axis("off")
    # Donut (inner axes)
    ax_donut = ax_tl.inset_axes([0.0, 0.35, 0.45, 0.62])
    sizes = [f["weight_pct"] for f in portfolio]
    wedges, _ = ax_donut.pie(sizes, colors=clr, startangle=90,
                              wedgeprops=dict(width=0.5, edgecolor="white"))
    ax_donut.set_title("Allocation", fontsize=9, fontweight="bold", pad=2)

    # IR + capture ratio mini-bars (right side of TL)
    ax_bar = ax_tl.inset_axes([0.50, 0.05, 0.48, 0.92])
    yp = np.arange(n)
    ir_vals  = [f.get("info_ratio")   or 0 for f in portfolio]
    cap_vals = [f.get("capture_ratio") or 0 for f in portfolio]
    ax_bar.barh(yp - 0.18, ir_vals,  height=0.32, color=clr, alpha=0.85, label="Info Ratio")
    ax_bar.barh(yp + 0.18, cap_vals, height=0.32, color=clr, alpha=0.45, label="Capture Ratio", hatch="///")
    ax_bar.axvline(0, color="black", linewidth=0.7)
    ax_bar.axvline(1, color="green", linewidth=0.7, linestyle="--", alpha=0.5)
    ax_bar.set_yticks(yp)
    ax_bar.set_yticklabels([f"{nm}\n[bold]{f['score']:.0f}pts[/bold]".replace("[bold]","").replace("[/bold]","")
                             for nm, f in zip(snm, portfolio)], fontsize=7)
    ax_bar.set_xlabel("IR  /  Capture ratio", fontsize=8)
    ax_bar.set_title("Info Ratio & Capture", fontsize=9, fontweight="bold")
    ax_bar.legend(fontsize=7, loc="lower right")
    # Legend for donut
    ax_tl.legend(wedges, [f"{nm} {w}%" for nm, w in zip(snm, sizes)],
                 loc="lower left", fontsize=7, frameon=True)

    # ── TR: Peer scatter (all category peers + portfolio highlighted) ─────
    plotted_cats: set = set()
    for cat, cs in cohort.items():
        if cs.get("median_vol") and cs.get("median_r3"):
            lbl = cat.title() if cat not in plotted_cats else None
            ax_tr.scatter(cs["median_vol"], cs["median_r3"],
                          s=30, alpha=0.18, color="gray", zorder=1)
            if lbl:
                ax_tr.annotate(cat[:12], (cs["median_vol"], cs["median_r3"]),
                               fontsize=6, color="#94a3b8", alpha=0.7)
            plotted_cats.add(cat)
    for f, c in zip(portfolio, clr):
        v, r = f.get("volatility"), f.get("return_3y")
        if v and r:
            ax_tr.scatter(v, r, s=200, color=c, edgecolor="black",
                          linewidth=1.5, zorder=5)
            ax_tr.annotate(f["name"][:20], (v, r),
                           fontsize=7, xytext=(5, 5), textcoords="offset points",
                           fontweight="bold")
    ax_tr.set_xlabel("Volatility (ann. %)", fontsize=9)
    ax_tr.set_ylabel("3Y Return (%)", fontsize=9)
    ax_tr.set_title("Risk vs Return — portfolio vs category peers", fontsize=10, fontweight="bold")

    # ── BL: Monte Carlo fan + passive baseline ───────────────────────────
    p_mc   = [f.get("mc_projection") or {} for f in portfolio]
    p10tot = [sum(p.get("p10", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]
    p50tot = [sum(p.get("p50", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]
    p90tot = [sum(p.get("p90", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]

    # Per-fund deterministic growth lines
    det_total = np.zeros(len(months_arr))
    for f, c in zip(portfolio, clr):
        r  = f["estimated_rate"] / 12
        al = f["monthly_alloc"]
        dt = al * ((np.power(1 + r, months_arr) - 1) / r) * (1 + r) if r else al * months_arr
        ax_bl.plot(months_arr / 12, dt / 1e5, color=c, linewidth=1.0, alpha=0.35)
        det_total += dt

    # Portfolio MC fan
    ax_bl.fill_between([0, timeline_years], [0, p10tot[0] / 1e5], [0, p90tot[0] / 1e5],
                       alpha=0.12, color="steelblue", label="Active P10–P90")
    ax_bl.plot([0, timeline_years], [0, p50tot[0] / 1e5], color="steelblue",
               linewidth=2.5, label=f"Active P50 {fmt_inr(p50tot[0])}")

    # Passive baseline
    if passive_mc:
        pv50 = passive_mc.get("p50", 0)
        pv10 = passive_mc.get("p10", 0)
        pv90 = passive_mc.get("p90", 0)
        ax_bl.fill_between([0, timeline_years], [0, pv10 / 1e5], [0, pv90 / 1e5],
                           alpha=0.08, color="gray")
        ax_bl.plot([0, timeline_years], [0, pv50 / 1e5], color="gray",
                   linewidth=1.5, linestyle="--", label=f"Passive P50 {fmt_inr(pv50)}")

    # Invested amount
    invested = monthly_sip * timeline_years * 12
    ax_bl.plot(months_arr / 12, monthly_sip * months_arr / 1e5, color="red",
               linewidth=1.0, linestyle=":", label=f"Invested {fmt_inr(invested)}")
    ax_bl.set_xlabel("Years", fontsize=9)
    ax_bl.set_ylabel("₹ Lakhs", fontsize=9)
    ax_bl.set_title("Monte Carlo Projection — Active vs Passive", fontsize=10, fontweight="bold")
    ax_bl.legend(fontsize=7, loc="upper left")

    # ── BR: Existing portfolio (if available) OR score breakdown ─────────
    valid_h = [h for h in holdings_analysis if (h.get("current_value") or 0) > 0]
    if valid_h:
        ACTION_COLORS = {"HOLD": "#16a34a", "BUY MORE": "#2563eb",
                         "SELL": "#dc2626", "UNKNOWN": "#94a3b8"}
        names_h  = [h.get("scheme_name", "?")[:30] for h in valid_h]
        vals_h   = [h.get("current_value", 0) / 1e5 for h in valid_h]
        bar_cols = [ACTION_COLORS.get(h.get("action", "UNKNOWN"), "#94a3b8") for h in valid_h]
        bars     = ax_br.barh(names_h, vals_h, color=bar_cols, edgecolor="white", height=0.6)
        for bar, v in zip(bars, vals_h):
            ax_br.text(bar.get_width() + max(vals_h) * 0.01,
                       bar.get_y() + bar.get_height() / 2,
                       f"₹{v:.1f}L", va="center", fontsize=7.5)
        total_v = sum(vals_h)
        ax_br.set_title(f"Your Portfolio — {fmt_inr(total_v * 1e5)} current value",
                         fontsize=10, fontweight="bold")
        ax_br.set_xlabel("₹ Lakhs", fontsize=9)
        ax_br.tick_params(axis="y", labelsize=7.5)
        ax_br.legend(handles=[Patch(color=c, label=a) for a, c in ACTION_COLORS.items()
                               if a != "UNKNOWN"], fontsize=7, loc="lower right")
    else:
        # Score component breakdown as stacked bar
        comps = list(SCORE_WEIGHTS["balanced"].keys())
        cc = matplotlib.colormaps["Set2"](np.linspace(0, 1, len(comps)))
        bots = np.zeros(n)
        yp2  = np.arange(n)
        for i, comp in enumerate(comps):
            vals = np.array([f.get("score_breakdown", {}).get(comp, 0) for f in portfolio])
            ax_br.barh(yp2, vals, left=bots, color=cc[i], label=comp, edgecolor="white")
            bots += vals
        ax_br.set_yticks(yp2)
        ax_br.set_yticklabels(snm, fontsize=8)
        ax_br.invert_yaxis()
        ax_br.set_xlabel("Score contribution", fontsize=9)
        ax_br.set_title(f"Score Breakdown ({len(comps)} components)", fontsize=10, fontweight="bold")
        ax_br.legend(fontsize=7, loc="lower right", ncol=2)

    buf = _b64(fig)

    def _write_png(fh):
        fig.savefig(fh, format="png", dpi=110, bbox_inches="tight")

    chart_path = _save_output_file(_write_png, CHART_FILE)
    if chart_path != CHART_FILE:
        console.print(f"  [yellow]Chart saved to: {chart_path}[/yellow]")

    plt.close(fig)
    return {"main": buf, "_path": chart_path}


# ══════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════

def generate_html_report(
    portfolio: list, params: dict,
    tax: dict, passive_mc: dict, advantage: dict,
    drift: dict, req_sip: dict,
    charts_b64: dict, market_context: str,
    holdings_analysis: list = None,
    cohort: dict = None,
    portfolio_holdings: dict = None,
) -> str:

    def money(v): return fmt_inr(v) if v else "-"
    def pct(v, dp=1): return f"{v:.{dp}f}%" if v is not None else "-"
    def val(v, dp=2): return f"{v:.{dp}f}" if v is not None else "-"
    def esc(v): return html_lib.escape(str(v if v is not None else ""))

    date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    total_p50 = sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"])
                    for f in portfolio)
    invested  = params["monthly_sip"] * params["timeline_years"] * 12
    net_p50   = tax.get("p50", {}).get("net", total_p50)
    risk      = portfolio_risk_metrics(portfolio)
    months    = int(params["timeline_years"] * 12)

    category_totals: dict[str, dict] = {}
    for f in portfolio:
        cat = f.get("category", "other").title()
        bucket = category_totals.setdefault(cat, {"weight": 0.0, "sip": 0.0})
        bucket["weight"] += float(f.get("weight_pct", 0))
        bucket["sip"] += float(f.get("monthly_alloc", 0))

    allocation_rows = "".join(
        f"<tr><td>{i}</td><td><b>{esc(f.get('name'))}</b><br>"
        f"<span style='color:#64748b;font-size:11px;'>{esc(f.get('amc_name','Unknown AMC'))}</span></td>"
        f"<td>{esc(str(f.get('category','other')).title())}</td>"
        f"<td>{money(f.get('monthly_alloc',0))}</td><td>{pct(f.get('weight_pct'))}</td>"
        f"<td>{pct(f.get('return_3y'))}</td><td>{pct(f.get('volatility'))}</td>"
        f"<td>{pct(f.get('max_drawdown'))}</td><td>{val(f.get('sharpe'))}</td>"
        f"<td>{pct(f.get('ter'))}</td><td>{f.get('score',0):.0f}/100</td></tr>"
        for i, f in enumerate(portfolio, 1)
    )
    category_rows = "".join(
        f"<tr><td>{esc(cat)}</td><td>{pct(data['weight'])}</td><td>{money(data['sip'])}</td></tr>"
        for cat, data in sorted(category_totals.items())
    )

    risk_tiles = "".join([
        f"<div class='metric'><div class='mlbl'>Portfolio Volatility</div><b>{pct(risk.get('volatility'))}</b><span>Annualised from weighted monthly NAV returns</span></div>",
        f"<div class='metric'><div class='mlbl'>Max Drawdown</div><b class='bad'>{pct(risk.get('max_drawdown'))}</b><span>Worst historical peak-to-trough fall in blended series</span></div>",
        f"<div class='metric'><div class='mlbl'>Sharpe</div><b>{val(risk.get('sharpe'))}</b><span>Excess return over {RISK_FREE_MONTHLY:.1f}% monthly risk-free proxy</span></div>",
        f"<div class='metric'><div class='mlbl'>Sortino</div><b>{val(risk.get('sortino'))}</b><span>Downside-risk adjusted return</span></div>",
    ])

    active_range = (tax.get('p90') or {}).get('gross', 0) - (tax.get('p10') or {}).get('gross', 0)
    passive_range = passive_mc.get('p90', 0) - passive_mc.get('p10', 0)
    mc_assumptions = f"""
      <div class="note-grid">
        <div><b>Active simulation</b><span>Each fund bootstraps its own historical monthly NAV returns with replacement. Fund P10/P50/P90 values are summed into the portfolio range.</span></div>
        <div><b>Passive simulation</b><span>{passive_mc.get('equity_split',75)}% Nifty 50 monthly returns less {PASSIVE_TER_EQUITY:.2f}% TER, plus {passive_mc.get('debt_split',25)}% debt at {PASSIVE_DEBT_RETURN:.1f}% p.a. less {PASSIVE_TER_DEBT:.2f}% TER.</span></div>
        <div><b>Simulation count</b><span>{MC_N_SIMS_STANDARD} paths over {months} months. Random seeds are fixed for reproducible reports.</span></div>
        <div><b>Model risk</b><span>Bootstrap sampling assumes historical monthly return shapes are useful proxies. It does not forecast valuation, rates, manager changes, fund closure, or liquidity stress.</span></div>
      </div>
    """

    # ── Existing portfolio section (only when holdings were fetched) ─────
    _existing_portfolio_html = ""
    if holdings_analysis:
        _ACTION_STYLE = {
            "HOLD":    ("background:#dcfce7;color:#15803d;", "✅ HOLD"),
            "SELL":    ("background:#fee2e2;color:#b91c1c;", "🔴 SELL"),
            "BUY MORE":("background:#dbeafe;color:#1d4ed8;", "💰 BUY MORE"),
            "UNKNOWN": ("background:#f1f5f9;color:#64748b;", "❓ UNKNOWN"),
        }
        total_cur_val = sum(h.get("current_value") or 0 for h in holdings_analysis)
        action_counts = {}
        for h in holdings_analysis:
            action_counts[h.get("action","?")] = action_counts.get(h.get("action","?"), 0) + 1

        _unk = _ACTION_STYLE["UNKNOWN"]
        summary_chips = " &nbsp; ".join(
            "<span style='{s}padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600;'>"
            "{lbl} &times; {n}</span>".format(
                s=_ACTION_STYLE.get(a, _unk)[0],
                lbl=_ACTION_STYLE.get(a, _unk)[1],
                n=n,
            )
            for a, n in sorted(action_counts.items())
        )

        holding_rows = ""
        for h in holdings_analysis:
            action     = h.get("action", "UNKNOWN")
            astyle, alabel = _ACTION_STYLE.get(action, _ACTION_STYLE["UNKNOWN"])
            cr, cs     = h.get("category_rank"), h.get("category_size")
            rank_s     = f"{cr}/{cs}" if cr and cs else "—"
            top_pct    = f"{(cs-cr)/cs*100:.0f}%" if (cr and cs) else "—"
            cagr_s     = f"{h['holding_cagr']:.1f}%" if h.get("holding_cagr") is not None else "—"
            gain_s     = (f"<span style='color:{'#15803d' if (h.get('gain_pct') or 0)>=0 else '#b91c1c'}'>"
                          f"{h['gain_pct']:+.1f}%</span>" if h.get("gain_pct") is not None else "—")
            score_s    = f"{h['score']:.0f}/100" if h.get("score") else "—"
            alpha_s    = f"{h['alpha']:.2f}%" if h.get("alpha") is not None else "—"
            val_s      = money(h.get("current_value") or 0)
            sip_s      = money(h.get("sip_amount") or 0)
            holding_rows += (
                f"<tr>"
                f"<td><b>{esc(h.get('scheme_name','?')[:40])}</b><br>"
                f"<span style='color:#64748b;font-size:11px;'>{esc(h.get('category','?'))}</span></td>"
                f"<td>{sip_s}<br><span style='color:#94a3b8;font-size:11px;'>SIP/mo</span></td>"
                f"<td><b>{val_s}</b></td>"
                f"<td>{cagr_s} CAGR<br>{gain_s} total</td>"
                f"<td>{rank_s}<br><span style='color:#64748b;font-size:11px;'>Top {top_pct}</span></td>"
                f"<td>{score_s}</td>"
                f"<td>{alpha_s}</td>"
                f"<td><span style='{astyle}padding:3px 8px;border-radius:6px;"
                f"font-size:12px;font-weight:600;'>{alabel}</span><br>"
                f"<span style='color:#64748b;font-size:11px;'>{esc(h.get('action_reason',''))}</span></td>"
                f"</tr>"
            )

        _existing_portfolio_html = f"""
<div class="section" id="existing-portfolio">
  <h2>📂 Your Existing Portfolio</h2>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px;">
    <div>
      <span style="font-size:22px;font-weight:700;">{money(total_cur_val)}</span>
      <span style="color:#64748b;font-size:13px;margin-left:8px;">current portfolio value</span>
    </div>
    <div>{summary_chips}</div>
  </div>
  <table>
    <thead><tr>
      <th>Fund</th><th>SIP / Value</th><th>Current Value</th>
      <th>Returns</th><th>Rank in Category</th><th>Score</th><th>Alpha</th><th>Recommendation</th>
    </tr></thead>
    <tbody>{holding_rows}</tbody>
  </table>
  <p style="margin-top:14px;font-size:12px;color:#64748b;">
    <b>HOLD</b> = top-50% of category, age ≥ 5Y or rank strong enough. &nbsp;
    <b>SELL</b> = bottom quartile of category for 2+ years (consider switching). &nbsp;
    <b>BUY MORE</b> = top-25% and above average alpha — strong conviction candidate.
    Rank is within the fund's SEBI category. Returns are simple CAGR from first purchase date.
  </p>
</div>"""

    # ── Category Scorecard — shows why each fund was chosen ─────────────
    leaders = (cohort or {}).get("_category_leaders", {})
    _scorecard_html = ""
    if leaders:
        cat_blocks = ""
        selected_codes = {f["scheme_code"] for f in portfolio}
        for cat, funds in sorted(leaders.items()):
            top = sorted(funds, key=lambda x: x["score"], reverse=True)[:8]
            rows = ""
            for rank, fd in enumerate(top, 1):
                is_sel = fd["scheme_code"] in selected_codes
                bg     = "background:#f0fdf4;" if is_sel else ""
                badge  = (" <span style='background:#16a34a;color:#fff;padding:1px 6px;"
                          "border-radius:4px;font-size:10px;'>SELECTED</span>") if is_sel else ""
                aum_tag = (" <span style='color:#b91c1c;font-size:10px;'>⚠AUM</span>"
                           if fd.get("aum_warn") else "")
                ret_s  = f"{fd['return_3y']:.1f}%" if fd.get("return_3y") is not None else "—"
                alp_s  = f"{fd['alpha']:.2f}%" if fd.get("alpha") is not None else "—"
                aum_s  = f"{fd['aum_cr']:,.0f} Cr" if fd.get("aum_cr") else "—"
                rows += (
                    f"<tr style='{bg}'>"
                    f"<td style='font-weight:{'700' if is_sel else '400'};'>"
                    f"#{rank} {esc(fd['name'][:48])}{badge}{aum_tag}</td>"
                    f"<td style='text-align:right;font-weight:{'700' if is_sel else '400'};'>"
                    f"{fd['score']:.1f}</td>"
                    f"<td style='text-align:right;'>{ret_s}</td>"
                    f"<td style='text-align:right;'>{alp_s}</td>"
                    f"<td style='text-align:right;color:#64748b;font-size:11px;'>{aum_s}</td>"
                    f"</tr>"
                )
            cat_blocks += f"""
<div style="margin-bottom:20px;">
  <h3 style="font-size:14px;font-weight:600;color:#1e3a5f;margin-bottom:6px;">
    {esc(cat.title())}
    <span style="font-weight:400;color:#64748b;font-size:12px;">
      — {len(funds)} funds scored, top 8 shown
    </span>
  </h3>
  <table>
    <thead><tr><th>Fund</th><th>Score</th><th>3Y Return</th><th>Alpha</th><th>AUM</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""
        _scorecard_html = f"""
<div class="section" id="scorecard">
  <h2>🏆 Category Scorecard — Why These Funds Were Chosen</h2>
  <p style="font-size:12px;color:#64748b;margin-bottom:16px;">
    Every fund in the NAV universe is scored (0–100) within its SEBI category.
    The highest scorer per slot is selected. Green rows = chosen for your portfolio.
    ⚠AUM = fund's AUM exceeds category capacity threshold, score penalised.
  </p>
  {cat_blocks}
</div>"""

    # market banner colour
    if "BULL" in market_context:  mcol = "#744210"; mbg = "#fefce8"
    elif "BEAR" in market_context: mcol = "#7f1d1d"; mbg = "#fff1f2"
    else:                          mcol = "#1e3a5f"; mbg = "#eff6ff"

    fund_cards = ""
    for i, f in enumerate(portfolio, 1):
        mc  = f.get("mc_projection") or {}
        cat = esc(f["category"].title())
        fund_name = esc(f["name"])
        amc_name = esc(f.get("amc_name", "N/A"))
        scheme_code = esc(f.get("scheme_code", ""))
        isin = esc(f.get("isin", "N/A"))
        bench = esc(f.get("category_benchmark", "^NSEI"))
        is_debt = f["category"] == "debt"
        bdg_col = "#0369a1" if not is_debt else "#15803d"
        aum_warn = (f"<span style='background:#fef2f2;color:#b91c1c;padding:2px 6px;"
                    f"border-radius:4px;font-size:11px;margin-left:8px;'>⚠ AUM high</span>"
                    if f.get("aum_warn") else "")

        # score bar — 7 actual components matching SCORE_WEIGHTS keys
        score_comp_colors = {
            "info_ratio": "#3b82f6", "capture": "#8b5cf6",
            "consistency": "#06b6d4", "cost": "#f97316",
            "capacity": "#84cc16", "valuation": "#10b981",
            "ext_rank": "#f59e0b",
        }
        score_bar = ""
        bd = f.get("score_breakdown", {})
        for comp, color in score_comp_colors.items():
            w_px = int(bd.get(comp, 0) / 100 * 280)
            if w_px > 0:
                score_bar += (
                    f"<div style='width:{w_px}px;height:12px;background:{color};"
                    f"display:inline-block;' title='{comp}: {bd.get(comp,0):.1f}'></div>"
                )

        up_dn = (f"↑{f.get('up_capture','?'):.0f}% / ↓{f.get('down_capture','?'):.0f}%"
                 if f.get("up_capture") and f.get("down_capture") else "N/A")

        # PE vs category from mfdata.in
        fund_pe = f.get("pe_ratio")
        cat_pe  = f.get("category_pe")
        pe_html = ""
        if fund_pe is not None:
            pe_color = "#16a34a" if (cat_pe and fund_pe < cat_pe * 0.95) else (
                       "#dc2626" if (cat_pe and fund_pe > cat_pe * 1.15) else "#1e293b")
            cat_pe_str = f" (cat avg {cat_pe:.1f})" if cat_pe else ""
            pe_html = (f"<div><div style='color:#64748b;'>PE Ratio</div>"
                       f"<b style='color:{pe_color};'>{fund_pe:.1f}{cat_pe_str}</b></div>")

        # Fund manager from enrichment / portfolio holdings
        ph = (portfolio_holdings or {}).get(str(f.get("scheme_code", "")), {})
        raw_mgr = f.get("fund_manager") or ph.get("fund_manager")
        mgr_html = ""
        if raw_mgr:
            if isinstance(raw_mgr, list):
                mgr_names = ", ".join(
                    (m.get("name") or m.get("manager_name") or str(m))[:30]
                    if isinstance(m, dict) else str(m)[:30]
                    for m in raw_mgr[:3]
                )
            elif isinstance(raw_mgr, dict):
                mgr_names = raw_mgr.get("name") or raw_mgr.get("manager_name") or str(raw_mgr)[:30]
            else:
                mgr_names = str(raw_mgr)[:60]
            mgr_html = (
                f"<div style='margin-bottom:10px;font-size:12px;'>"
                f"<span style='color:#64748b;'>Fund Manager: </span>"
                f"<b>{esc(mgr_names)}</b></div>"
            )

        # Top holdings chips
        raw_holdings = ph.get("top_holdings") or []
        holdings_html = ""
        if raw_holdings:
            chips = ""
            for h in raw_holdings[:8]:
                if isinstance(h, dict):
                    hname = h.get("name") or h.get("stock_name") or h.get("issuer") or ""
                    hpct  = h.get("weight") or h.get("percentage") or h.get("pct") or ""
                    hpct_str = f" {float(hpct):.1f}%" if hpct else ""
                else:
                    hname, hpct_str = str(h), ""
                if hname:
                    chips += (
                        f"<span style='background:#f1f5f9;border:1px solid #e2e8f0;"
                        f"border-radius:4px;padding:2px 7px;font-size:11px;margin:2px;'>"
                        f"{esc(str(hname)[:22])}{hpct_str}</span>"
                    )
            if chips:
                holdings_html = (
                    f"<div style='margin-bottom:10px;'>"
                    f"<div style='color:#64748b;font-size:11px;margin-bottom:4px;'>Top Holdings</div>"
                    f"<div style='display:flex;flex-wrap:wrap;gap:2px;'>{chips}</div></div>"
                )

        # Sector allocation mini bar
        raw_sectors = ph.get("sector_allocation") or []
        sector_html = ""
        if raw_sectors:
            bars = ""
            for s in raw_sectors[:6]:
                if isinstance(s, dict):
                    sname = s.get("sector") or s.get("name") or ""
                    spct  = _safe_float(s.get("weight") or s.get("percentage") or s.get("pct") or 0) or 0
                else:
                    sname, spct = str(s), 0
                if sname and spct:
                    bar_w = min(100, int(spct))
                    bars += (
                        f"<div style='display:flex;align-items:center;gap:6px;margin-bottom:3px;font-size:11px;'>"
                        f"<div style='width:90px;color:#475569;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;'>{esc(str(sname)[:14])}</div>"
                        f"<div style='flex:1;background:#f1f5f9;border-radius:3px;height:8px;'>"
                        f"<div style='width:{bar_w}%;background:#6366f1;height:100%;border-radius:3px;'></div></div>"
                        f"<div style='width:32px;text-align:right;color:#64748b;'>{spct:.0f}%</div></div>"
                    )
            if bars:
                sector_html = (
                    f"<div style='margin-bottom:10px;'>"
                    f"<div style='color:#64748b;font-size:11px;margin-bottom:4px;'>Sector Allocation</div>"
                    f"{bars}</div>"
                )

        fund_cards += f"""
        <div style="border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
            <div>
              <h3 style="font-size:16px;font-weight:600;margin-bottom:4px;">#{i} {fund_name}</h3>
              <div style="color:#64748b;font-size:12px;">{amc_name} &nbsp;|&nbsp;
                Code: {scheme_code} &nbsp;|&nbsp; ISIN: {isin}</div>
            </div>
            <div style="text-align:right;">
              <span style="background:{'#dbeafe' if not is_debt else '#dcfce7'};color:{bdg_col};
                           padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600;">{cat}</span>
              {aum_warn}
              <div style="color:#94a3b8;font-size:11px;margin-top:4px;">vs {bench}</div>
            </div>
          </div>
          {mgr_html}
          <div style="background:#e0f2fe;border-radius:4px;height:6px;margin-bottom:16px;">
            <div style="background:#0284c7;width:{f['weight_pct']}%;height:100%;border-radius:4px;"></div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;font-size:12px;">
            <div><div style="color:#64748b;">Monthly SIP</div><b>{money(f['monthly_alloc'])}</b> ({f['weight_pct']}%)</div>
            <div><div style="color:#64748b;">3Y Return</div><b>{pct(f.get('return_3y'))}</b></div>
            <div><div style="color:#64748b;">5Y Return</div><b>{pct(f.get('return_5y'))}</b></div>
            <div><div style="color:#64748b;">Alpha (vs idx)</div><b>{val(f.get('alpha'))}%</b></div>
            <div><div style="color:#64748b;">Sharpe</div><b>{val(f.get('sharpe'))}</b></div>
            <div><div style="color:#64748b;">Max Drawdown</div><b style="color:#dc2626;">{pct(f.get('max_drawdown'))}</b></div>
            <div><div style="color:#64748b;">Volatility</div><b>{pct(f.get('volatility'))}</b></div>
            <div><div style="color:#64748b;">TER</div><b>{pct(f.get('ter'))}</b></div>
            <div><div style="color:#64748b;">AUM</div><b>{val(f.get('aum_cr'))} Cr</b></div>
            <div><div style="color:#64748b;">Rating</div><b>{val(f.get('rating'))}</b></div>
            <div><div style="color:#64748b;">Bear Alpha</div><b>{val(f.get('bear_alpha'))}%/mo</b></div>
            <div><div style="color:#64748b;">Up/Down Cap</div><b>{up_dn}</b></div>
            <div><div style="color:#64748b;">Rank Consistency</div><b>{val(f.get('rank_consistency'))}</b></div>
            <div><div style="color:#64748b;">Score</div><b>{f['score']}/100</b></div>
            {pe_html}
          </div>
          <div style="margin-bottom:10px;">
            <div style="color:#64748b;font-size:11px;margin-bottom:4px;">
              Score breakdown — <span style='color:#3b82f6;'>IR</span>
              <span style='color:#8b5cf6;'> Capture</span>
              <span style='color:#06b6d4;'> Consistency</span>
              <span style='color:#f97316;'> Cost</span>
              <span style='color:#84cc16;'> Capacity</span>
              <span style='color:#10b981;'> Valuation</span>
              <span style='color:#f59e0b;'> Ext Rank</span>
            </div>
            <div style="border-radius:4px;overflow:hidden;height:12px;">{score_bar}</div>
          </div>
          {holdings_html}{sector_html}
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:12px;">
            {"".join(f"<div style='background:{'#f0fdf4' if k=='p50' else '#f8fafc'};border-radius:6px;padding:8px;text-align:center;'><div style='color:#64748b;font-size:10px;'>{k.upper()}</div><b>{money(v)}</b></div>" for k,v in mc.items() if k in ('p10','p50','p90'))}
          </div>
        </div>"""

    # Rebalancing table
    reb_rows = ""
    for yr, data in sorted(drift.items()):
        trig_col = "#dc2626" if data["trigger"] else "#16a34a"
        trig_txt = "⚠ Rebalance" if data["trigger"] else "✓ OK"
        reb_rows += f"<tr><td style='font-weight:600;'>Year {yr}</td>"
        for fd in data["funds"]:
            dev_col = "#dc2626" if fd["flag"] else ("#d97706" if abs(fd["deviation"])>3 else "#16a34a")
            reb_rows += f"<td>{fd['target']:.0f}% → <span style='color:{dev_col};font-weight:600;'>{fd['actual']:.1f}%</span></td>"
        reb_rows += f"<td style='color:{trig_col};font-weight:600;'>{trig_txt} (max {data['max_deviation']:.1f}pp)</td></tr>"

    reb_headers = "".join(f"<th>{fd['name'][:16]}</th>" for fd in (drift.get(1,{}).get("funds") or []))

    # TER drag
    ter_drag = tax.get("ter_drag_rs", 0)

    # passive comparison block
    adv_col = "#16a34a" if advantage.get("advantage_rs",0) > 0 else "#dc2626"

    # required SIP block
    req_sip_html = ""
    if req_sip:
        req_sip_html = f"""
        <div class="section">
          <h2>🎯 Goal-Based Planning</h2>
          <p style="color:#64748b;margin-bottom:16px;">
            Target corpus: <b>{money(params.get('target_corpus',0))}</b> in
            <b>{params['timeline_years']:.0f} years</b>
          </p>
          <table><thead><tr><th>Confidence Level</th><th>Required Monthly SIP</th><th>Interpretation</th></tr></thead>
          <tbody>
            <tr><td>P90 (90% likely)</td><td><b>{money(req_sip.get('p90_sip',0))}</b></td><td>Conservative — invest this to be 90% sure of reaching target</td></tr>
            <tr><td>P50 (50% likely)</td><td><b>{money(req_sip.get('p50_sip',0))}</b></td><td>Median scenario</td></tr>
            <tr><td>P10 (10% likely)</td><td><b>{money(req_sip.get('p10_sip',0))}</b></td><td>Optimistic — only if markets do very well</td></tr>
          </tbody></table>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MF Portfolio Report — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b}}
.container{{max-width:1200px;margin:0 auto;padding:24px}}
.hero{{background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);color:white;border-radius:16px;padding:32px;margin-bottom:20px}}
.hero h1{{font-size:24px;font-weight:700;margin-bottom:6px}}
.hero .sub{{font-size:13px;opacity:.75;margin-bottom:20px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.kpi{{background:rgba(255,255,255,.15);border-radius:10px;padding:14px}}
.kpi .lbl{{font-size:10px;text-transform:uppercase;letter-spacing:1px;opacity:.8}}
.kpi .val{{font-size:20px;font-weight:700;margin-top:4px}}
.kpi .sub2{{font-size:11px;opacity:.65;margin-top:2px}}
.section{{background:white;border-radius:12px;padding:24px;margin-bottom:16px;
           box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.section h2{{font-size:17px;font-weight:600;color:#1e40af;margin-bottom:16px;
             border-bottom:2px solid #dbeafe;padding-bottom:8px}}
.metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.metric{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px}}
.metric .mlbl{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#64748b;margin-bottom:6px}}
.metric b{{display:block;font-size:20px;margin-bottom:4px}}
.metric span{{display:block;font-size:11px;color:#64748b;line-height:1.4}}
.note-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;font-size:12px;color:#475569}}
.note-grid div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px}}
.note-grid b{{display:block;color:#1e293b;margin-bottom:4px}}
.note-grid span{{display:block;line-height:1.5}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f1f5f9;padding:10px 12px;text-align:left;font-weight:600;color:#475569}}
td{{padding:10px 12px;border-bottom:1px solid #f1f5f9}}
tr:hover td{{background:#f8fafc}}
.good{{color:#16a34a}} .warn{{color:#d97706}} .bad{{color:#dc2626}}
.footer{{text-align:center;color:#94a3b8;font-size:11px;padding:24px;line-height:1.8}}
</style></head>
<body><div class="container">

<div class="hero">
  <h1>MF SIP Portfolio Report</h1>
  <div class="sub">Generated {date_str} &nbsp;|&nbsp; Strategy: {params['optimise'].title()}
   &nbsp;|&nbsp; {params['timeline_years']:.0f}Y · ₹{params['monthly_sip']:,}/mo</div>
  <div class="kpi-grid">
    <div class="kpi"><div class="lbl">P50 Gross Corpus</div>
      <div class="val">{money(total_p50)}</div>
      <div class="sub2">P10: {money((tax.get('p10') or {}).get('gross',0))} &nbsp; P90: {money((tax.get('p90') or {}).get('gross',0))}</div></div>
    <div class="kpi"><div class="lbl">Net After Tax (P50)</div>
      <div class="val">{money(net_p50)}</div>
      <div class="sub2">Tax bracket: {params.get('tax_bracket_pct',0)}% slab</div></div>
    <div class="kpi"><div class="lbl">TER Drag vs Passive</div>
      <div class="val" style="color:#fca5a5;">{money(ter_drag)}</div>
      <div class="sub2">Cost of active TER over {params['timeline_years']:.0f}Y</div></div>
    <div class="kpi"><div class="lbl">Active vs Passive</div>
      <div class="val" style="color:{'#86efac' if advantage.get('advantage_rs',0)>0 else '#fca5a5'};">
        {money(abs(advantage.get('advantage_rs',0)))}</div>
      <div class="sub2">{advantage.get('verdict','—')}</div></div>
  </div>
</div>

{"<div style='background:" + mbg + ";color:" + mcol + ";border-radius:10px;padding:14px;margin-bottom:16px;font-size:13px;font-weight:500;'>📊 Market Signal: " + market_context + "</div>" if market_context else ""}

{req_sip_html}

<div class="section"><h2>Portfolio Breakdown</h2>
<table><thead><tr><th>#</th><th>Fund</th><th>Category</th><th>Monthly SIP</th><th>Allocation</th>
  <th>3Y Return</th><th>Volatility</th><th>Max Drawdown</th><th>Sharpe</th><th>TER</th><th>Score</th></tr></thead>
<tbody>{allocation_rows}</tbody></table>
</div>

<div class="section"><h2>Category Allocation</h2>
<table><thead><tr><th>Category</th><th>Allocation</th><th>Monthly SIP</th></tr></thead>
<tbody>{category_rows}</tbody></table>
</div>

<div class="section"><h2>Risk Metrics</h2>
  <div class="metric-grid">{risk_tiles}</div>
  <p style="margin-top:12px;font-size:12px;color:#64748b;">
    Risk metrics use the weighted historical monthly return series for the selected funds
    ({risk.get('months','-')} overlapping monthly observations). They are context for the
    median corpus comparison, not a guarantee of future risk.
  </p>
</div>

<div class="section"><h2>Monte Carlo Assumptions</h2>
  {mc_assumptions}
</div>

<div class="section"><h2>📊 Main Chart Pack</h2>
  <p style="color:#64748b;font-size:12px;margin-bottom:12px;">
    Chart panels cover allocation, risk-return, score components, fund Monte Carlo ranges,
    rolling behaviour, correlation, regime behaviour, and active/passive corpus fan.
  </p>
  <img src="data:image/png;base64,{charts_b64.get('main','')}" style="width:100%;border-radius:8px;">
</div>

<div class="section"><h2>📁 Fund Details</h2>{fund_cards}</div>

<div class="section"><h2>💰 Tax Impact</h2>
<table><thead><tr><th>Scenario</th><th>Gross Corpus</th><th>LTCG Tax</th>
  <th>Debt Tax</th><th>Total Tax</th><th>Net Corpus</th></tr></thead>
<tbody>{"".join(f"<tr><td><b>{s.upper()}</b></td><td>{money((tax.get(s) or {}).get('gross',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('eq_tax',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('db_tax',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('total_tax',0))}</td><td class='good'><b>{money((tax.get(s) or {}).get('net',0))}</b></td></tr>" for s in ('p10','p50','p90'))}</tbody>
</table>
<p style="margin-top:12px;font-size:12px;color:#64748b;">
  LTCG exemption used: {money(tax.get('ltcg_exemption',0))} total (₹1.25L/yr × {params['timeline_years']:.0f}Y).
  TER drag estimate vs {PASSIVE_TER_EQUITY:.2f}% passive TER: <b class="bad">{money(tax.get('ter_drag_rs',0))}</b>
  (avg active TER {tax.get('avg_ter',0):.2f}%). This is a fee-only approximation, not a performance forecast.
</p></div>

<div class="section"><h2>⚖️ Active vs Passive Portfolio</h2>
<table><thead><tr><th>Metric</th><th>Active Portfolio</th><th>Passive Basket</th><th>Difference</th></tr></thead>
<tbody>
  <tr><td>P10 Corpus</td><td>{money((tax.get('p10') or {}).get('gross',0))}</td><td>{money(passive_mc.get('p10',0))}</td>
      <td style="color:{adv_col};">{money((tax.get('p10') or {}).get('gross',0) - passive_mc.get('p10',0))}</td></tr>
  <tr><td><b>P50 Corpus</b></td><td><b>{money(total_p50)}</b></td><td><b>{money(passive_mc.get('p50',0))}</b></td>
      <td style="color:{adv_col};font-weight:700;">{money(advantage.get('advantage_rs',0))}</td></tr>
  <tr><td>P90 Corpus</td><td>{money((tax.get('p90') or {}).get('gross',0))}</td><td>{money(passive_mc.get('p90',0))}</td>
      <td style="color:{adv_col};">{money((tax.get('p90') or {}).get('gross',0) - passive_mc.get('p90',0))}</td></tr>
  <tr><td>P10-P90 Range</td><td>{money(active_range)}</td><td>{money(passive_range)}</td>
      <td>{money(active_range - passive_range)}</td></tr>
</tbody></table>
<p style="margin-top:12px;font-size:12px;color:#64748b;">
  Passive basket: {passive_mc.get('equity_split',75)}% Nifty 50 index (TER {PASSIVE_TER_EQUITY:.2f}%) +
  {passive_mc.get('debt_split',25)}% debt (TER {PASSIVE_TER_DEBT:.2f}%, {PASSIVE_DEBT_RETURN:.1f}% p.a.).
  Verdict is based on median simulated corpus only and must be read with the risk metrics above:
  <b>{advantage.get('verdict','-')}</b>.
</p></div>

<div class="section"><h2>🔄 Rebalancing Schedule</h2>
<table><thead><tr><th>Checkpoint</th>{reb_headers}<th>Status</th></tr></thead>
<tbody>{reb_rows}</tbody></table>
<p style="margin-top:10px;font-size:12px;color:#64748b;">
  Rebalance when any fund deviates &gt;5pp from target. Growth-heavy (small/mid cap) funds typically
  drift upward in bull markets.
</p></div>

{_existing_portfolio_html}

{_scorecard_html}

<div class="section"><h2>📋 Methodology & Disclosures</h2>
<ul style="font-size:12px;color:#475569;line-height:2;padding-left:20px;">
  <li><b>Data sources:</b> mfapi.in (NAV history), AMFI NAVAll.txt (official categories + AMC names), mfdata.in (AUM, TER, ratings, return ranks where available), yfinance (Nifty 50 / Midcap benchmarks).</li>
  <li><b>Category benchmarks:</b> Mid cap → {' / '.join(MIDCAP_TICKERS)} (first available); Small cap → ^NSEI (no reliable 10Y smallcap ticker in yfinance).</li>
  <li><b>Survivorship bias:</b> mfapi.in lists active schemes only. Closed/merged funds (usually worst performers) excluded → historical returns overstated by ~0.5%. Addressed via extra 0.5% haircut on all projections.</li>
  <li><b>TER:</b> Uses mf_ter_overrides.json first, then mfdata.in expense ratio, then category defaults.</li>
  <li><b>Scoring:</b> {len(SCORE_WEIGHTS[params['optimise']])} components ({params['optimise']} weights) — {', '.join(f'{k} {v}%' for k, v in SCORE_WEIGHTS[params['optimise']].items())}. Most components are category-relative; AUM is treated as a liquidity/capacity guardrail, not pure size worship.</li>
  <li><b>Monte Carlo:</b> Bootstrap resampling of fund's own historical monthly returns ({MC_N_SIMS_STANDARD} simulations). Not a forecast — past return distribution may not repeat.</li>
  <li><b>Tax:</b> LTCG 12.5% on equity gains above ₹1.25L/year (FY25-26). Debt: slab rate on full gains (no indexation post Apr 2023). Simplified — actual tax depends on redemption timing.</li>
  <li><b>Exit load:</b> Most equity funds charge 1% if redeemed within 12M of each SIP instalment. SIP investors holding &gt;12M typically pay 0%.</li>
  <li><b>Not SEBI-registered financial advice.</b> Consult a qualified financial planner before investing.</li>
</ul></div>

<div class="footer">
  MF Portfolio Builder v3 &nbsp;|&nbsp; mfapi.in · AMFI · mfdata.in · yfinance
  &nbsp;|&nbsp; Generated {date_str}
</div>
</div></body></html>"""

    html_bytes = html.encode("utf-8")

    def _write_html(fh):
        fh.write(html_bytes)

    html_path = _save_output_file(_write_html, HTML_REPORT_FILE, auto_open=True)
    if html_path != HTML_REPORT_FILE:
        console.print(f"  [yellow]HTML report saved to: {html_path}[/yellow]")
    return html_path


# ══════════════════════════════════════════════════════════════════════
# TERMINAL OUTPUT
# ══════════════════════════════════════════════════════════════════════

def print_portfolio(portfolio: list, params: dict, tax: dict,
                    passive_mc: dict, advantage: dict, req_sip: dict):
    console.print(Panel("[bold white]YOUR PERSONALISED SIP PORTFOLIO[/bold white]",
                        border_style="cyan"))

    monthly_sip    = params["monthly_sip"]
    timeline_years = params["timeline_years"]
    optimise       = params["optimise"]
    total_invested = monthly_sip * timeline_years * 12
    total_p50      = sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"])
                         for f in portfolio)
    net_p50        = (tax.get("p50") or {}).get("net", total_p50)
    gain           = (total_p50 / total_invested - 1) * 100 if total_invested else 0

    # Summary KPIs
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=22); t.add_column("v", style="bold white")
    rows_kpi = [
        ("Monthly SIP",       fmt_inr(monthly_sip)),
        ("Timeline",          f"{timeline_years:.1f} years ({int(timeline_years*12)} months)"),
        ("Strategy",          optimise.title()),
        ("Total invested",    fmt_inr(total_invested)),
        ("P50 gross corpus",  fmt_inr(total_p50)),
        ("P50 net (post-tax)",fmt_inr(net_p50)),
        ("P50 gain",          f"{gain:.1f}%"),
        ("TER drag",          f"{fmt_inr(tax.get('ter_drag_rs',0))} vs passive 0.10% TER"),
        ("vs Passive P50",    f"{fmt_inr(abs(advantage.get('advantage_rs',0)))} "
                              f"— {advantage.get('verdict','—')}"),
    ]
    for k, v in rows_kpi: t.add_row(k, v)
    console.print(t)
    console.print()

    if req_sip:
        console.print(Panel(
            f"  Target corpus: [bold]{fmt_inr(params.get('target_corpus',0))}[/bold]\n"
            f"  Required SIP — P90 (90% confidence): [bold green]{fmt_inr(req_sip.get('p90_sip',0))}[/bold green]/mo\n"
            f"  Required SIP — P50 (median):         [bold]{fmt_inr(req_sip.get('p50_sip',0))}[/bold]/mo\n"
            f"  Required SIP — P10 (optimistic):     [bold cyan]{fmt_inr(req_sip.get('p10_sip',0))}[/bold cyan]/mo",
            title="[bold yellow]GOAL-BASED SIP REQUIREMENT[/bold yellow]",
            border_style="yellow",
        ))

    # Main fund table
    ft = Table(title="Fund Details", border_style="dim",
               show_lines=True, padding=(0, 1))
    for col in ["#","Fund","Cat","SIP","Wt","3Y%","10Y%","α","MaxDD","Up/Dn","TER","AUM","Score","P50"]:
        ft.add_column(col, style="white", no_wrap=True)
    for i, f in enumerate(portfolio, 1):
        mc = f.get("mc_projection") or {}
        ft.add_row(
            str(i), f["name"][:30], f["category"].title(),
            fmt_inr(f["monthly_alloc"]), f"{f['weight_pct']}%",
            pct_str(f.get("return_3y")),
            pct_str(f.get("return_10y")) if f.get("return_10y") is not None else "[dim]N/A[/dim]",
            val_str(f.get("alpha")),
            pct_str(f.get("max_drawdown")),
            f"{f.get('up_capture','?'):.0f}/{f.get('down_capture','?'):.0f}" if f.get("up_capture") else "N/A",
            pct_str(f.get("ter")),
            f"{f.get('aum_cr'):.0f}Cr" if f.get("aum_cr") is not None else "N/A",
            f"{f['score']:.0f}",
            fmt_inr(mc.get("p50", f["projected_corpus"])),
        )
    console.print(ft)
    console.print()

    # MC range
    rt = Table(title="Monte Carlo Range", border_style="dim", padding=(0,1))
    for col in ["Fund","P10","P25","P50","P75","P90"]:
        rt.add_column(col)
    for f in portfolio:
        mc = f.get("mc_projection") or {}
        rt.add_row(f["name"][:28],
                   *[fmt_inr(mc.get(k, 0)) for k in ("p10","p25","p50","p75","p90")])
    console.print(rt)
    console.print()

    # ── Auto-validation panel ─────────────────────────────────────────
    vt = Table(title="Portfolio Confidence (how funds rank in their universe)",
               border_style="dim", padding=(0, 1))
    vt.add_column("Fund", style="white")
    vt.add_column("Category", style="dim", width=12)
    vt.add_column("Rank", justify="center", width=10)
    vt.add_column("Top %", justify="center", width=8)
    vt.add_column("Alpha vs idx", justify="center", width=12)
    vt.add_column("Bear α/mo", justify="center", width=10)
    vt.add_column("Confidence", width=14)

    for f in portfolio:
        cr = f.get("category_rank"); cs = f.get("category_size")
        if cr and cs:
            top_pct = (cs - cr) / cs * 100
            rank_str = f"{cr}/{cs}"
            pct_s = f"{top_pct:.0f}%"
            if top_pct >= 85:  conf = "[bold green]VERY HIGH[/]"
            elif top_pct >= 70: conf = "[green]HIGH[/]"
            elif top_pct >= 50: conf = "[yellow]MODERATE[/]"
            else:               conf = "[red]LOW[/]"
        else:
            rank_str = pct_s = "—"; conf = "—"

        alpha_s = val_str(f.get("alpha")) + "%" if f.get("alpha") is not None else "—"
        bear_s  = val_str(f.get("bear_alpha")) + "%" if f.get("bear_alpha") is not None else "—"
        vt.add_row(f["name"][:32], f["category"].title(), rank_str, pct_s,
                   alpha_s, bear_s, conf)
    console.print(vt)
    console.print()

    console.print("[yellow]NOTES[/yellow]")
    console.print("  • MC bootstraps each fund's own historical monthly returns — not a forecast.")
    console.print("  • LTCG 12.5% above ₹1.25L/yr applied to equity; slab rate to debt.")
    console.print("  • TER drag = cost of active mgmt vs 0.10% index fund over the timeline.")
    console.print("  • ELSS: 3-yr lock-in per instalment. Not SEBI-registered advice.\n")


def print_stepup_analysis(stepup: dict) -> None:
    console.print("\n[bold cyan]═══ STEP-UP SIP ANALYSIS ═══[/bold cyan]")
    pct   = stepup["stepup_pct"]
    extra = stepup["extra_corpus"]
    flat  = stepup["flat_p50"]
    adv   = (extra / flat * 100) if flat else 0

    console.print(Panel(
        f"  Flat SIP P50 corpus   : [white]{fmt_inr(flat)}[/white]\n"
        f"  Step-up {pct:.0f}%/yr P50   : [bold green]{fmt_inr(stepup['stepup_p50'])}[/bold green]\n"
        f"  Extra corpus from step-up: [bold yellow]+{fmt_inr(extra)} (+{adv:.0f}%)[/bold yellow]\n"
        f"  Total invested (step-up) : {fmt_inr(stepup['total_invested'])}",
        title=f"[bold]Annual {pct:.0f}% SIP Step-Up vs Flat SIP[/bold]",
        border_style="green",
    ))

    st = Table(title="Step-Up Schedule", border_style="dim", padding=(0, 1))
    st.add_column("Year", justify="right", width=6)
    st.add_column("Monthly SIP", justify="right", width=14)
    for s in stepup["stepup_schedule"][:10]:
        st.add_row(str(s["year"]), fmt_inr(s["monthly_sip"]))
    if len(stepup["stepup_schedule"]) > 10:
        st.add_row("...", "...")
    console.print(st)
