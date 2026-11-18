"""Action plan (switches, gap fills, buy-more) and LTCG tax-harvest plan."""

from rich.panel import Panel
from rich.table import Table

from mfa.portfolio import ALLOCATION_WEIGHTS
from mfa.ui import console, fmt_inr


# ── Broad category normaliser ─────────────────────────────────────────────
# Maps fine-grained SEBI categories → slot-level buckets for gap analysis.
_SLOT_KEYWORDS = {
    "aggressive": [
        ("small cap",                                         ["small cap"]),
        ("mid cap",                                           ["mid cap"]),
        ("flexi / multi / focused",                          ["flexi cap","multi cap","focused"]),
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("value / elss",                                     ["value","elss","contra"]),
    ],
    "balanced": [
        ("flexi / multi",                                     ["flexi cap","multi cap"]),
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("mid cap",                                           ["mid cap"]),
        ("small cap",                                         ["small cap"]),
        ("debt",                                              ["debt","short duration","low duration",
                                                               "ultra short","money market",
                                                               "corporate bond","banking and psu"]),
    ],
    "conservative": [
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("flexi / multi",                                     ["flexi cap","multi cap"]),
        ("debt",                                              ["debt","short duration","low duration",
                                                               "ultra short","money market",
                                                               "corporate bond","banking and psu"]),
        ("mid / value",                                       ["mid cap","value"]),
    ],
}


def _holding_slot(category: str, strategy: str) -> str:
    """Map a holding's SEBI category to the slot label for the given strategy."""
    cat_l = (category or "").lower()
    for slot_label, kws in _SLOT_KEYWORDS.get(strategy, _SLOT_KEYWORDS["balanced"]):
        if any(k in cat_l for k in kws):
            return slot_label
    return "other"


def find_replacement_fund(category: str, all_scored: list,
                           held_codes: set, held_amcs: set) -> dict | None:
    """
    Return the top-scoring fund in `category` that the user doesn't already hold.
    Prefers a different AMC from those already held to maintain diversification.
    """
    cat_l = category.lower()
    candidates = [
        f for f in all_scored
        if (f.get("category") or "").lower() == cat_l
        and str(f.get("scheme_code", "")) not in held_codes
        and f.get("category_rank", 99) <= 10
    ]
    # Prefer different AMC
    diff_amc = [f for f in candidates
                if (f.get("amc_name","").split()[0].upper() not in held_amcs)]
    return (diff_amc or candidates or [None])[0]


def generate_action_plan(
    holdings_analysis: list,
    all_scored: list,
    strategy: str,
    monthly_sip: int,
    nav_data: list,
    new_portfolio: list = None,
) -> dict:
    """
    Unified action plan bridging existing portfolio management and new value addition.

    Produces five prioritised action groups:
      SWITCH  — underperforming funds with a specific replacement named
      GAP FILL — categories with no coverage; exact fund + SIP amount
      BUY MORE — existing top funds that are underweight; exact ₹ top-up
      HARVEST  — LTCG tax harvest (reuse compute_tax_harvest_plan output)
      HOLD     — funds requiring no action
      OVERLAP  — duplicate category warnings
    """
    held_codes = {str(h.get("scheme_code","")) for h in holdings_analysis}
    held_amcs  = {(h.get("amc","") or "").split()[0].upper()
                  for h in holdings_analysis if h.get("amc")}

    # ── Current allocation by slot ────────────────────────────────────────
    slots = _SLOT_KEYWORDS.get(strategy, _SLOT_KEYWORDS["balanced"])
    weights = ALLOCATION_WEIGHTS.get(strategy, [25,20,20,15,20])
    target = {label: weights[i] for i, (label, _) in enumerate(slots)
              if i < len(weights)}

    total_value = sum(h.get("current_value") or 0 for h in holdings_analysis)
    total_sip   = sum(h.get("sip_amount")    or 0 for h in holdings_analysis) or monthly_sip

    slot_value: dict = {}
    slot_holdings: dict = {}
    for h in holdings_analysis:
        slot = _holding_slot(h.get("category",""), strategy)
        slot_value[slot]    = slot_value.get(slot, 0) + (h.get("current_value") or 0)
        slot_holdings.setdefault(slot, []).append(h)

    # ── Gap analysis per slot ─────────────────────────────────────────────
    gaps: dict = {}
    for label, tgt_pct in target.items():
        cur_val  = slot_value.get(label, 0)
        cur_pct  = (cur_val / total_value * 100) if total_value else 0
        gap_pct  = tgt_pct - cur_pct
        gaps[label] = {
            "target_pct":  tgt_pct,
            "current_pct": round(cur_pct, 1),
            "gap_pct":     round(gap_pct, 1),
            "sip_gap":     max(0, round(monthly_sip * gap_pct / 100)),
            "status":      ("UNCOVERED" if cur_pct < 1
                            else ("UNDER" if gap_pct > 7
                                  else ("OVER" if gap_pct < -7 else "OK"))),
        }

    # ── Overlap detection (two holdings in same fine-grained category) ────
    cat_counts: dict = {}
    for h in holdings_analysis:
        c = h.get("category","other")
        cat_counts.setdefault(c, []).append(h.get("scheme_name","?"))
    overlaps = [{"category": c, "funds": names}
                for c, names in cat_counts.items() if len(names) > 1]

    # ── Cross-reference with new portfolio recommendation ─────────────────
    new_portfolio = new_portfolio or []
    new_codes = {str(f.get("scheme_code","")) for f in new_portfolio}
    already_recommended = [h for h in holdings_analysis
                           if str(h.get("scheme_code","")) in new_codes]

    # ── Build action groups ───────────────────────────────────────────────
    switches, gap_fills, buy_mores, holds = [], [], [], []

    for h in holdings_analysis:
        action = h.get("action","HOLD")
        cat    = h.get("category","other")
        yr     = h.get("years_held") or 0
        gain   = h.get("gain_pct")
        avg_n  = h.get("avg_nav") or 0
        cur_n  = h.get("current_nav") or 0
        units  = h.get("units") or 0

        if action in ("SWITCH", "EXIT"):
            replacement = find_replacement_fund(cat, all_scored, held_codes, held_amcs)
            exit_load   = yr < 1.0        # most equity funds: 1% if < 1Y
            ltcg_rs     = 0
            if avg_n and cur_n and units and cur_n > avg_n:
                ltcg_rs = round(units * (cur_n - avg_n))
            tax_rs = round(max(0, ltcg_rs - LTCG_EXEMPTION_PER_YEAR) * 0.125)
            switches.append({
                "from":          h,
                "to":            replacement,
                "exit_load_warn": exit_load,
                "ltcg_rs":       ltcg_rs,
                "tax_rs":        tax_rs,
                "priority":      1 if action == "EXIT" else 2,
            })

        elif action == "BUY MORE":
            slot      = _holding_slot(cat, strategy)
            gap_info  = gaps.get(slot, {})
            extra_sip = gap_info.get("sip_gap", 0)
            buy_mores.append({
                "holding":     h,
                "extra_sip":   extra_sip or round(monthly_sip * 0.10),
                "slot":        slot,
                "gap_pct":     gap_info.get("gap_pct", 0),
            })

        else:
            holds.append(h)

    # Gap fill: slots with no coverage at all
    for label, info in gaps.items():
        if info["status"] == "UNCOVERED" and info["sip_gap"] > 0:
            # Find matching category keyword for this slot
            kws    = next((kw for lbl, kw in slots if lbl == label), [label])
            top_cat = kws[0] if kws else label
            fund   = find_replacement_fund(top_cat, all_scored, held_codes, held_amcs)
            if fund:
                gap_fills.append({
                    "slot":      label,
                    "category":  top_cat,
                    "fund":      fund,
                    "sip":       info["sip_gap"] or round(monthly_sip * 0.15),
                    "gap_pct":   info["gap_pct"],
                })
                held_codes.add(str(fund.get("scheme_code","")))   # avoid duplicate suggestions

    return {
        "switches":            sorted(switches, key=lambda x: x["priority"]),
        "gap_fills":           gap_fills,
        "buy_mores":           buy_mores,
        "holds":               holds,
        "gaps":                gaps,
        "overlaps":            overlaps,
        "already_recommended": already_recommended,
        "total_value":         total_value,
        "total_sip":           total_sip,
        "strategy":            strategy,
    }


def print_action_plan(plan: dict) -> None:
    """Print the complete prioritised action plan to the terminal."""
    console.print("\n")
    console.print(Panel(
        "[bold white]COMPLETE INVESTMENT ACTION PLAN[/bold white]\n"
        "[dim]Bridges your existing portfolio with your target strategy.[/dim]",
        border_style="bright_cyan",
    ))

    step = 0

    def _step(label: str, colour: str, title: str, detail: str) -> None:
        nonlocal step
        step += 1
        console.print(
            f"  [bold {colour}]STEP {step}  [{label}][/bold {colour}]  {title}"
        )
        for line in detail.strip().splitlines():
            console.print(f"  [dim]         {line.strip()}[/dim]")
        console.print()

    # ── OVERLAP warnings (highest priority — before switches) ─────────────
    for ov in plan["overlaps"]:
        _step("OVERLAP ⚠", "red",
              f"Duplicate {ov['category']} exposure",
              f"You hold {len(ov['funds'])} funds in the same category: "
              f"{', '.join(ov['funds'][:3])}\n"
              f"Consolidate to the highest-scoring one to reduce overlap.")

    # ── SWITCHES / EXITS ──────────────────────────────────────────────────
    for sw in plan["switches"]:
        h   = sw["from"]
        rep = sw["to"]
        urgency = "EXIT NOW" if h.get("action") == "EXIT" else "SWITCH"
        from_str = (f"{h.get('scheme_name','?')[:40]}  "
                    f"[rank {h.get('category_rank','?')}/{h.get('category_size','?')}  "
                    f"score {h.get('score') or '?'}]")
        if rep:
            to_str = (f"{rep.get('name','?')[:40]}  "
                      f"[rank {rep.get('category_rank','?')}/{rep.get('category_size','?')}  "
                      f"score {rep.get('score') or '?'}]")
        else:
            to_str = "No direct replacement found in universe — consider category alternatives"
        el_warn = "⚠ Exit load likely (held < 1Y)  " if sw["exit_load_warn"] else ""
        tax_str = (f"Tax on gain: {fmt_inr(sw['tax_rs'])}  "
                   f"(LTCG: {fmt_inr(sw['ltcg_rs'])})"
                   if sw["ltcg_rs"] else "No tax impact (cost basis unavailable or no gain)")
        _step(urgency, "red" if urgency == "EXIT NOW" else "yellow",
              f"{h.get('scheme_name','?')[:34]} → {(rep or {}).get('name','?')[:30]}",
              f"FROM: {from_str}\n  TO:   {to_str}\n  {el_warn}{tax_str}")

    # ── GAP FILLS (new category coverage) ────────────────────────────────
    for gf in plan["gap_fills"]:
        f = gf["fund"]
        _step("ADD NEW", "cyan",
              f"{gf['slot'].upper()} gap — start {fmt_inr(gf['sip'])}/month",
              f"You have 0% in {gf['slot']} (target: {gf['gap_pct']:.0f}%)\n"
              f"Fund: {f.get('name','?')[:40]}\n"
              f"Rank: {f.get('category_rank','?')}/{f.get('category_size','?')}  "
              f"Score: {f.get('score') or '?'}  "
              f"3Y: {f.get('return_3y') or '?'}%")

    # ── BUY MORE (increase underweight existing positions) ────────────────
    for bm in plan["buy_mores"]:
        h = bm["holding"]
        _step("INCREASE", "green",
              f"{h.get('scheme_name','?')[:34]} — add {fmt_inr(bm['extra_sip'])}/month",
              f"Top performer in {h.get('category','?')} "
              f"[rank {h.get('category_rank','?')}/{h.get('category_size','?')}]\n"
              f"Slot '{bm['slot']}' is {bm['gap_pct']:.1f}% below target — "
              f"increase existing SIP rather than opening a new fund")

    # ── ALREADY RECOMMENDED ───────────────────────────────────────────────
    for h in plan["already_recommended"]:
        _step("CONFIRMED", "bold green",
              f"{h.get('scheme_name','?')[:40]} — already in new recommendation",
              "This holding is also the top pick in its category. No switch needed.")

    # ── HOLDS ─────────────────────────────────────────────────────────────
    if plan["holds"]:
        console.print("  [green]✓ HOLD — no action needed:[/green]")
        for h in plan["holds"]:
            cr, cs = h.get("category_rank"), h.get("category_size")
            rank_s = f"rank {cr}/{cs}" if (cr and cs) else ""
            console.print(f"    [dim]{h.get('scheme_name','?')[:44]}  {rank_s}[/dim]")
        console.print()

    # ── GAP SUMMARY TABLE ─────────────────────────────────────────────────
    gt = Table(title="Allocation Gap vs Strategy Target", border_style="dim",
               padding=(0, 1), show_lines=False)
    gt.add_column("Category Slot",  style="white", width=26)
    gt.add_column("Current", justify="right", width=9)
    gt.add_column("Target",  justify="right", width=9)
    gt.add_column("Gap",     justify="right", width=9)
    gt.add_column("SIP gap", justify="right", width=11)
    gt.add_column("Status",  width=11)
    STATUS_COL = {"OK":"green","UNDER":"yellow","OVER":"yellow",
                  "UNCOVERED":"bold red","other":"dim"}
    for slot, info in plan["gaps"].items():
        st = info["status"]
        col = STATUS_COL.get(st, "dim")
        gap_s = (f"[{col}]+{info['gap_pct']:.1f}%[/{col}]"
                 if info["gap_pct"] >= 0 else
                 f"[{col}]{info['gap_pct']:.1f}%[/{col}]")
        sip_s = fmt_inr(info["sip_gap"]) if info["sip_gap"] > 0 else "—"
        gt.add_row(
            slot,
            f"{info['current_pct']:.1f}%",
            f"{info['target_pct']:.1f}%",
            gap_s,
            sip_s,
            f"[{col}]{st}[/{col}]",
        )
    console.print(gt)
    console.print()

    if step == 0 and not plan["holds"]:
        console.print("  [green]Portfolio is well-positioned — no immediate actions needed.[/green]\n")


# ══════════════════════════════════════════════════════════════════════
# LTCG TAX HARVEST CALENDAR
# ══════════════════════════════════════════════════════════════════════

LTCG_EXEMPTION_PER_YEAR = 125_000   # ₹1.25L per FY (as of FY2025-26)

def compute_tax_harvest_plan(holdings_analysis: list, nav_data: list) -> list:
    """
    For each holding with unrealised LTCG, compute how many units to redeem
    annually to fully utilise the ₹1.25L LTCG exemption and immediately reinvest.
    Returns list of action dicts with redemption details.
    """
    nav_lookup: dict = {}
    for item in nav_data:
        code = str(item["scheme"].get("schemeCode", ""))
        nav_lookup[code] = float(item["nav_df"]["nav"].iloc[-1])

    harvest_actions = []
    remaining_exemption = float(LTCG_EXEMPTION_PER_YEAR)

    # Only HOLD funds with long-term gains qualify (held > 1Y, positive gain)
    eligible = [
        h for h in holdings_analysis
        if h.get("action") in ("HOLD", "BUY MORE")
        and (h.get("years_held") or 0) > 1.0
        and (h.get("gain_pct") or 0) > 0
        and (h.get("units") or 0) > 0
        and (h.get("avg_nav") or 0) > 0
    ]
    eligible.sort(key=lambda h: h.get("gain_pct", 0))   # harvest lowest-gain first

    for h in eligible:
        if remaining_exemption <= 0:
            break
        avg_nav  = h["avg_nav"]
        curr_nav = h.get("current_nav") or nav_lookup.get(str(h.get("scheme_code", ""))) or avg_nav
        if curr_nav <= avg_nav:
            continue
        units_held = h["units"]
        gain_per_unit = curr_nav - avg_nav
        # Units needed to realise exactly remaining_exemption in LTCG
        units_to_redeem = min(units_held * 0.95, remaining_exemption / gain_per_unit)
        units_to_redeem = max(0, units_to_redeem)
        ltcg_realised   = units_to_redeem * gain_per_unit
        redeem_value    = units_to_redeem * curr_nav
        remaining_exemption -= ltcg_realised

        harvest_actions.append({
            "scheme_name":    h["scheme_name"],
            "units_to_redeem": round(units_to_redeem, 3),
            "redeem_value":   round(redeem_value),
            "ltcg_realised":  round(ltcg_realised),
            "reinvest_value": round(redeem_value),
            "tax_saved":      round(ltcg_realised * 0.125),   # 12.5% LTCG rate
            "new_cost_basis": round(curr_nav, 2),
            "action":         h.get("action", "HOLD"),
        })

    total_tax_saved = sum(a["tax_saved"] for a in harvest_actions)
    return {"actions": harvest_actions,
            "total_ltcg_realised": round(LTCG_EXEMPTION_PER_YEAR - remaining_exemption),
            "total_tax_saved":     round(total_tax_saved),
            "exemption_used":      round(LTCG_EXEMPTION_PER_YEAR - remaining_exemption),
            "exemption_remaining": round(max(0, remaining_exemption))}


def print_tax_harvest(harvest: dict) -> None:
    if not harvest.get("actions"):
        console.print("  [dim]No eligible holdings for tax harvesting this year.[/dim]")
        return

    console.print("\n[bold cyan]═══ LTCG TAX HARVEST CALENDAR ═══[/bold cyan]")
    console.print(Panel(
        f"  LTCG exemption limit : ₹{LTCG_EXEMPTION_PER_YEAR:,.0f}/year\n"
        f"  Exemption used       : {fmt_inr(harvest['exemption_used'])}\n"
        f"  Remaining unused     : {fmt_inr(harvest['exemption_remaining'])}\n"
        f"  Estimated tax saved  : [bold green]{fmt_inr(harvest['total_tax_saved'])}[/bold green]\n\n"
        f"  [dim]Redeem → immediately reinvest same amount in same fund.\n"
        f"  Cost basis resets to current NAV — future gains start fresh.[/dim]",
        title="[bold]Annual Tax Harvest Plan[/bold]",
        border_style="green",
    ))

    ht = Table(border_style="dim", show_lines=True, padding=(0, 1))
    for col in ["Fund", "Units to Redeem", "Redeem Value", "LTCG Realised", "Tax Saved", "New Cost Basis"]:
        ht.add_column(col, style="white")
    for a in harvest["actions"]:
        ht.add_row(
            a["scheme_name"][:36],
            f"{a['units_to_redeem']:.3f}",
            fmt_inr(a["redeem_value"]),
            fmt_inr(a["ltcg_realised"]),
            f"[bold green]{fmt_inr(a['tax_saved'])}[/bold green]",
            f"₹{a['new_cost_basis']:.2f}",
        )
    console.print(ht)
    console.print(
        "  [dim]Do this in January each year. Reinvest on the same day in the same fund "
        "to avoid being out of market.[/dim]\n"
    )
