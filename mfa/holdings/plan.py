"""Action plan (switches, gap fills, buy-more) and LTCG tax-harvest plan."""

from rich.panel import Panel
from rich.table import Table

from mfa.holdings.costs import (ExemptionBudget, breakeven_months, direct_switch_advice,
                                load_exit_load_overrides, measured_cost_gap_pa,
                                required_outperformance_pa, switch_cost)
from mfa.data.nav import fetch_scheme_nav
from mfa.portfolio import ALLOCATION_WEIGHTS
from mfa.summary import plain_buy_more, plain_direct, plain_gap, plain_overlap, plain_switch
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


def _direct_switch_item(h: dict, nav_data: list, budget: ExemptionBudget, overrides: dict) -> dict | None:
    """Regular → Direct: measured annual cost gap, upfront cost, breakeven."""
    direct_code = str(h.get("scheme_code", ""))
    direct_df = next((i["nav_df"] for i in nav_data or []
                      if str(i["scheme"].get("schemeCode")) == direct_code), None)
    regular_df = fetch_scheme_nav(h.get("amfi_code")) if h.get("amfi_code") else None
    gap = measured_cost_gap_pa(regular_df, direct_df)
    cost = switch_cost(h, h["current_nav"], budget, overrides)
    be = breakeven_months(cost["upfront_rs"], cost["value_rs"], gap)
    return {
        "holding":          h,
        "direct_code":      direct_code,
        "direct_nav":       float(direct_df["nav"].iloc[-1]) if direct_df is not None else None,
        "gap_pa":           gap,
        "annual_extra_rs":  round(cost["value_rs"] * gap / 100) if gap else None,
        "cost":             cost,
        "breakeven_months": be,
        "advice":           ("ELSS units are in their 3-year lock-in — move new SIPs to Direct; "
                             "switch units as they unlock." if cost["locked_value_rs"]
                             else direct_switch_advice(be, cost["value_in_exit_load_rs"])),
    }


def _cost_lines(cost: dict | None) -> str:
    if not cost:
        return "Cost: unknown (no units / NAV on record)"
    bits = []
    if cost["exit_load_rs"]:
        bits.append(f"exit load {fmt_inr(cost['exit_load_rs'])} on {fmt_inr(cost['value_in_exit_load_rs'])} held < 1Y")
    if cost["stcg_tax_rs"]:
        bits.append(f"STCG tax {fmt_inr(cost['stcg_tax_rs'])}")
    if cost["ltcg_tax_rs"]:
        bits.append(f"LTCG tax {fmt_inr(cost['ltcg_tax_rs'])}")
    if cost["exemption_used_rs"]:
        bits.append(f"uses {fmt_inr(cost['exemption_used_rs'])} of this year's ₹1.25L exemption")
    head = f"Upfront cost {fmt_inr(cost['upfront_rs'])}" if cost["upfront_rs"] else "Upfront cost ₹0"
    if not cost["cost_basis_known"]:
        head += " (tax unknown — no cost basis; import a CAS for exact figures)"
    elif cost["approx"]:
        head += " (approx — single lot from start date)"
    if cost.get("locked_value_rs"):
        bits.append(f"{fmt_inr(cost['locked_value_rs'])} still in ELSS lock-in — cannot redeem yet")
    return head + (": " + "; ".join(bits) if bits else "")


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
    switches, gap_fills, buy_mores, holds, direct_switches = [], [], [], [], []
    # One LTCG exemption per financial year, shared by every switch and the
    # harvest plan. Exits are costed first (they're the most urgent).
    budget    = ExemptionBudget()
    overrides = load_exit_load_overrides()
    holdings_analysis = sorted(holdings_analysis,
                               key=lambda h: {"EXIT": 0, "SWITCH": 1}.get(h.get("action"), 2))

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
            cost = switch_cost(h, cur_n, budget, overrides) if (cur_n and units) else None
            switches.append({
                "from":           h,
                "to":             replacement,
                "cost":           cost,
                "exit_load_warn": bool(cost and (cost["exit_load_rs"] or cost["age_unknown"])) or (not cost and yr < 1.0),
                "ltcg_rs":        (cost or {}).get("ltcg_rs", 0),
                "tax_rs":         (cost or {}).get("tax_rs", 0),
                "required_outperf_pa": required_outperformance_pa(cost["upfront_rs"], cost["value_rs"]) if cost else None,
                "ter_from":       h.get("ter"),
                "ter_to":         (replacement or {}).get("ter"),
                "priority":       1 if action == "EXIT" else 2,
            })
            continue

        # Independent of the rating: any Regular-plan holding gets a Direct check.
        if h.get("is_regular") and cur_n and units:
            direct_switches.append(_direct_switch_item(h, nav_data, budget, overrides))

        if action == "BUY MORE":
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
        "direct_switches":     [d for d in direct_switches if d],
        "exemption_remaining": round(budget.remaining),
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

    def _step(label: str, colour: str, title: str, detail: str, plain: str = "") -> None:
        nonlocal step
        step += 1
        console.print(
            f"  [bold {colour}]STEP {step}  [{label}][/bold {colour}]  {title}"
        )
        if plain:
            console.print(f"           {plain}")
        for line in detail.strip().splitlines():
            console.print(f"  [dim]         {line.strip()}[/dim]")
        console.print()

    # ── OVERLAP warnings (highest priority — before switches) ─────────────
    for ov in plan["overlaps"]:
        _step("OVERLAP ⚠", "red",
              f"Duplicate {ov['category']} exposure",
              f"You hold {len(ov['funds'])} funds in the same category: "
              f"{', '.join(ov['funds'][:3])}\n"
              f"Consolidate to the highest-scoring one to reduce overlap.", plain_overlap(ov))

    # ── SWITCHES / EXITS ──────────────────────────────────────────────────
    for sw in plan["switches"]:
        h   = sw["from"]
        rep = sw["to"]
        urgency = "EXIT NOW" if h.get("action") == "EXIT" else "SWITCH"
        from_str = (f"{h.get('scheme_name','?')[:40]}  "
                    f"(rank {h.get('category_rank','?')}/{h.get('category_size','?')}  "
                    f"score {h.get('score') or '?'})")
        if rep:
            to_str = (f"{rep.get('name','?')[:40]}  "
                      f"(rank {rep.get('category_rank','?')}/{rep.get('category_size','?')}  "
                      f"score {rep.get('score') or '?'})")
        else:
            to_str = "No direct replacement found in universe — consider category alternatives"
        cost_str = _cost_lines(sw.get("cost"))
        extra = []
        if sw.get("required_outperf_pa") is not None and sw["cost"]["upfront_rs"]:
            extra.append(f"Pays back only if the new fund beats the old by "
                         f"{sw['required_outperf_pa']:.2f}% a year for 3 years")
        if sw.get("ter_from") is not None and sw.get("ter_to") is not None:
            extra.append(f"TER {sw['ter_from']:.2f}% → {sw['ter_to']:.2f}%")
        _step(urgency, "red" if urgency == "EXIT NOW" else "yellow",
              f"{h.get('scheme_name','?')[:34]} → {(rep or {}).get('name','?')[:30]}",
              f"FROM: {from_str}\n  TO:   {to_str}\n  {cost_str}"
              + ("\n  " + " · ".join(extra) if extra else ""), plain_switch(sw))

    # ── REGULAR → DIRECT ─────────────────────────────────────────────────
    for ds in plan.get("direct_switches", []):
        h = ds["holding"]
        gap = (f"Measured cost of the Regular plan: {ds['gap_pa']:.2f}% a year"
               + (f" ≈ {fmt_inr(ds['annual_extra_rs'])}/yr on {fmt_inr(ds['cost']['value_rs'])}"
                  if ds.get("annual_extra_rs") else "")) if ds.get("gap_pa") else \
              "Cost gap could not be measured (Regular NAV history unavailable)"
        be = (f"Breakeven: {ds['breakeven_months']:.0f} months" if ds.get("breakeven_months") is not None
              else "Breakeven: n/a")
        _step("DIRECT PLAN", "magenta",
              f"{h.get('scheme_name','?')[:40]} — Regular plan",
              f"{gap}\n  {_cost_lines(ds['cost'])}\n  {be}", plain_direct(ds))

    # ── GAP FILLS (new category coverage) ────────────────────────────────
    for gf in plan["gap_fills"]:
        f = gf["fund"]
        _step("ADD NEW", "cyan",
              f"{gf['slot'].upper()} gap — start {fmt_inr(gf['sip'])}/month",
              f"You have 0% in {gf['slot']} (target: {gf['gap_pct']:.0f}%)\n"
              f"Fund: {f.get('name','?')[:40]}\n"
              f"Rank: {f.get('category_rank','?')}/{f.get('category_size','?')}  "
              f"Score: {f.get('score') or '?'}  "
              f"3Y: {f.get('return_3y') or '?'}%", plain_gap(gf))

    # ── BUY MORE (increase underweight existing positions) ────────────────
    for bm in plan["buy_mores"]:
        h = bm["holding"]
        _step("INCREASE", "green",
              f"{h.get('scheme_name','?')[:34]} — add {fmt_inr(bm['extra_sip'])}/month",
              f"Top performer in {h.get('category','?')} "
              f"(rank {h.get('category_rank','?')}/{h.get('category_size','?')})\n"
              f"Slot '{bm['slot']}' is {bm['gap_pct']:.1f}% below target — "
              f"increase existing SIP rather than opening a new fund", plain_buy_more(bm))

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

def compute_tax_harvest_plan(holdings_analysis: list, nav_data: list,
                             exemption_available: float | None = None) -> dict:
    """
    For each holding with unrealised LTCG, compute how many units to redeem
    to use the LTCG exemption and immediately reinvest.
    exemption_available: what's left after switches in this year's action plan
    (the exemption is shared); defaults to the full ₹1.25L.
    """
    available = float(LTCG_EXEMPTION_PER_YEAR if exemption_available is None else exemption_available)
    nav_lookup: dict = {}
    for item in nav_data:
        code = str(item["scheme"].get("schemeCode", ""))
        nav_lookup[code] = float(item["nav_df"]["nav"].iloc[-1])

    harvest_actions = []
    remaining_exemption = available

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
            "total_ltcg_realised": round(available - remaining_exemption),
            "total_tax_saved":     round(total_tax_saved),
            "exemption_available": round(available),
            "exemption_used":      round(available - remaining_exemption),
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
