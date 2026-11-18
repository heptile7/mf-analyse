"""
Plain words first: a one-sentence, jargon-free summary for every action, an
"In short" list of what to do this month, and the shared action-style map so
every rating renders properly (no SWITCH/EXIT falling through to "UNKNOWN").
Pure functions — no I/O beyond the console helpers used by print_in_short.
"""
import html as html_lib

from rich.panel import Panel

from mfa.ui import console, fmt_inr

# Every action _holding_action / analyse_holdings can emit → (css, label, chart colour)
ACTION_STYLES = {
    "BUY MORE":   ("background:#dbeafe;color:#1d4ed8;", "💰 BUY MORE", "#2563eb"),
    "HOLD":       ("background:#dcfce7;color:#15803d;", "✅ HOLD",     "#16a34a"),
    "HOLD/WATCH": ("background:#fef9c3;color:#854d0e;", "👀 WATCH",    "#ca8a04"),
    "SWITCH":     ("background:#ffedd5;color:#c2410c;", "🔁 SWITCH",   "#ea580c"),
    "EXIT":       ("background:#fee2e2;color:#b91c1c;", "🔴 EXIT",     "#dc2626"),
    "SELL":       ("background:#fee2e2;color:#b91c1c;", "🔴 SELL",     "#dc2626"),
    "UNKNOWN":    ("background:#f1f5f9;color:#64748b;", "❓ NOT RATED", "#94a3b8"),
}


def action_style(action: str) -> tuple:
    return ACTION_STYLES.get(action or "UNKNOWN", ACTION_STYLES["UNKNOWN"])


def _short(name: str, n: int = 38) -> str:
    name = (name or "?").replace(" - Direct Plan", "").replace(" - Regular Plan", "")
    return name if len(name) <= n else name[: n - 1] + "…"


# ── one-liners ─────────────────────────────────────────────────────────
def plain_switch(sw: dict) -> str:
    h, to, cost = sw["from"], sw.get("to") or {}, sw.get("cost") or {}
    verb = "Exit" if h.get("action") == "EXIT" else "Consider switching out of"
    where = (f" into {_short(to.get('name'))}" if to.get("name") else "")
    rank = (f" — it ranks {h.get('category_rank')} of {h.get('category_size')} in its category"
            if h.get("category_rank") else "")
    if cost.get("locked_value_rs"):
        return f"{verb} {_short(h.get('scheme_name'))}{where} once its ELSS lock-in ends{rank}."
    price = (f"; doing it today costs about {fmt_inr(cost['upfront_rs'])} in exit load and tax"
             if cost.get("upfront_rs") else "; there's no exit load or tax to pay today"
             if cost.get("cost_basis_known") else "")
    return f"{verb} {_short(h.get('scheme_name'))}{where}{rank}{price}."


def plain_direct(ds: dict) -> str:
    h = ds["holding"]
    extra = (f" costing about {fmt_inr(ds['annual_extra_rs'])} a year more than the Direct plan"
             if ds.get("annual_extra_rs") else " that costs more each year than its Direct twin")
    be = ds.get("breakeven_months")
    tail = (f"; switching pays for itself in about {be:.0f} months" if be is not None
            else "")
    return f"Your {_short(h.get('scheme_name'))} is a Regular plan{extra}{tail}. {ds.get('advice', '')}".strip()


def plain_gap(gf: dict) -> str:
    return (f"You have nothing in {gf['slot']} — start {fmt_inr(gf['sip'])}/month in "
            f"{_short((gf.get('fund') or {}).get('name'))}.")


def plain_buy_more(bm: dict) -> str:
    h = bm["holding"]
    return (f"{_short(h.get('scheme_name'))} is one of the best in its category and you're "
            f"underweight there — add {fmt_inr(bm['extra_sip'])}/month to it.")


def plain_overlap(ov: dict) -> str:
    return (f"You hold {len(ov['funds'])} {ov['category']} funds that largely own the same "
            f"stocks — keep the best one and stop SIPs in the others.")


def plain_harvest(harvest: dict) -> str | None:
    if not harvest or not harvest.get("actions"):
        return None
    return (f"Before 31 March, sell and immediately rebuy {fmt_inr(sum(a['redeem_value'] for a in harvest['actions']))} "
            f"of long-held funds to book tax-free gains and save about "
            f"{fmt_inr(harvest['total_tax_saved'])} in future tax.")


# ── "In short" ─────────────────────────────────────────────────────────
def in_short(plan: dict | None, harvest: dict | None = None, limit: int = 5) -> list:
    """Top actions for this month, most important first."""
    if not plan:
        return []
    lines = []
    lines += [plain_switch(s) for s in plan.get("switches", []) if s["from"].get("action") == "EXIT"]
    lines += [plain_direct(d) for d in plan.get("direct_switches", [])
              if (d.get("breakeven_months") or 1e9) <= 24]
    lines += [plain_switch(s) for s in plan.get("switches", []) if s["from"].get("action") != "EXIT"]
    lines += [plain_overlap(o) for o in plan.get("overlaps", [])]
    lines += [plain_gap(g) for g in plan.get("gap_fills", [])]
    h = plain_harvest(harvest)
    if h:
        lines.append(h)
    lines += [plain_direct(d) for d in plan.get("direct_switches", [])
              if (d.get("breakeven_months") or 1e9) > 24]
    lines += [plain_buy_more(b) for b in plan.get("buy_mores", [])]
    if not lines:
        lines.append("Nothing urgent — keep your SIPs running and check again next month.")
    return lines[:limit]


def print_in_short(lines: list) -> None:
    if lines:
        console.print(Panel("\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1)),
                            title="[bold]In short — this month[/bold]", border_style="green"))


# ── HTML ───────────────────────────────────────────────────────────────
def action_plan_html(plan: dict | None, harvest: dict | None) -> str:
    """'In short' box + every action with its plain line first, numbers below."""
    if not plan:
        return ""
    esc = html_lib.escape
    items = []

    def item(tag, colour, plain, numbers=""):
        items.append(
            f"<li style='margin:10px 0'><span style='background:{colour};color:#fff;border-radius:6px;"
            f"padding:1px 7px;font-size:11px;font-weight:600'>{esc(tag)}</span> "
            f"<b>{esc(plain)}</b>"
            + (f"<div style='font-size:12px;color:#64748b;margin-top:3px'>{esc(numbers)}</div>" if numbers else "")
            + "</li>")

    for sw in plan.get("switches", []):
        c = sw.get("cost") or {}
        nums = []
        if c:
            nums.append(f"Value {fmt_inr(c.get('value_rs', 0))} · exit load {fmt_inr(c.get('exit_load_rs', 0))}"
                        f" · STCG tax {fmt_inr(c.get('stcg_tax_rs', 0))} · LTCG tax {fmt_inr(c.get('ltcg_tax_rs', 0))}"
                        + (" · approx (no transaction history)" if c.get("approx") else ""))
        if sw.get("required_outperf_pa") is not None:
            nums.append(f"needs {sw['required_outperf_pa']:.2f}% p.a. outperformance for 3 years to pay back")
        item("EXIT" if sw["from"].get("action") == "EXIT" else "SWITCH",
             "#dc2626" if sw["from"].get("action") == "EXIT" else "#ea580c", plain_switch(sw), " · ".join(nums))
    for ds in plan.get("direct_switches", []):
        nums = (f"Measured gap {ds['gap_pa']:.2f}% p.a." if ds.get("gap_pa") else "Gap not measurable") + \
               f" · upfront cost {fmt_inr((ds.get('cost') or {}).get('upfront_rs', 0))}"
        item("DIRECT PLAN", "#9333ea", plain_direct(ds), nums)
    for ov in plan.get("overlaps", []):
        item("OVERLAP", "#b91c1c", plain_overlap(ov), ", ".join(ov["funds"][:4]))
    for gf in plan.get("gap_fills", []):
        f = gf.get("fund") or {}
        item("ADD NEW", "#0891b2", plain_gap(gf),
             f"Rank {f.get('category_rank', '?')}/{f.get('category_size', '?')} · score {f.get('score') or '?'}")
    for bm in plan.get("buy_mores", []):
        item("INCREASE", "#16a34a", plain_buy_more(bm), f"Slot {bm['slot']} is {bm['gap_pct']:.1f}% below target")
    h = plain_harvest(harvest)
    if h:
        rows = "; ".join(f"{a['scheme_name'][:30]}: redeem {a['units_to_redeem']} units ({fmt_inr(a['redeem_value'])})"
                         for a in harvest["actions"])
        item("TAX HARVEST", "#0d9488", h, rows)

    short = "".join(f"<li>{esc(l)}</li>" for l in in_short(plan, harvest))
    return ("<div class='section'><h2>🧭 What To Do</h2>"
            "<div style='background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:12px 16px;'>"
            f"<b>In short — this month</b><ol style='margin:6px 0 0 18px'>{short}</ol></div>"
            f"<ul style='list-style:none;padding-left:0'>{''.join(items)}</ul>"
            "<p style='font-size:11px;color:#94a3b8'>Ranks are a tie-breaker, not a forecast — see the "
            "backtest in the Track Record section.</p></div>")
