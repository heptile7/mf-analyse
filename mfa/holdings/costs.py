"""
What a switch really costs, and when it pays for itself.

  * Exit load   — DEFAULT_EXIT_LOAD by category, per-scheme overrides in
                  mf_exit_load_overrides.json ({"<amfi code>": [pct, days]}).
  * Lots        — FIFO lots from CAS transactions when stored; otherwise one
                  approximate lot dated start_date at avg_nav ("approx").
  * Tax         — lots < 1 year: STCG 20%; older: LTCG 12.5% above the yearly
                  exemption, which is SHARED (ExemptionBudget) across every
                  switch and the tax-harvest plan.
  * Direct gap  — for Regular-plan holdings, the real annual cost gap is
                  measured from the Regular vs Direct NAV histories.
"""
from datetime import datetime
import json
import os

import pandas as pd

from mfa.config import (DEFAULT_EXIT_LOAD, ELSS_LOCKIN_DAYS, EXIT_LOAD_OVERRIDE_FILE, LTCG_EXEMPTION_PA,
                        LTCG_RATE_EQUITY, STCG_RATE_EQUITY)
from mfa.metrics import cagr, nav_on_or_before

SWITCH_NOW_MAX_MONTHS = 24


class ExemptionBudget:
    """The ₹1.25L LTCG exemption is per person per financial year — share it."""

    def __init__(self, total: float = LTCG_EXEMPTION_PA):
        self.total = float(total)
        self.remaining = float(total)

    def take(self, gain: float) -> float:
        used = min(max(gain, 0.0), self.remaining)
        self.remaining -= used
        return used


def load_exit_load_overrides(path: str = EXIT_LOAD_OVERRIDE_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return {str(k): tuple(v) for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception:
        return {}


def exit_load_for(code, category: str, overrides: dict | None = None) -> tuple:
    """(pct, days): exit load of pct% if units are redeemed within `days`."""
    if overrides and str(code) in overrides:
        pct, days = overrides[str(code)]
        return float(pct), int(days)
    return DEFAULT_EXIT_LOAD.get(category, DEFAULT_EXIT_LOAD["default"])


def build_lots(h: dict) -> tuple:
    """Return (lots, approx). Lot = {date: Timestamp|None, units, nav}."""
    txns = h.get("transactions") or []
    lots: list = []
    if txns:
        for t in sorted(txns, key=lambda t: str(t.get("date", ""))):
            units = t.get("units")
            if units is None:
                continue
            units = float(units)
            if units > 0:
                nav = t.get("nav") or ((t.get("amount") or 0) / units if units else 0)
                lots.append({"date": pd.Timestamp(str(t["date"])[:10]), "units": units,
                             "nav": float(nav or 0)})
            elif units < 0:                       # redemption / switch-out: consume FIFO
                left = -units
                while left > 1e-9 and lots:
                    take = min(left, lots[0]["units"])
                    lots[0]["units"] -= take
                    left -= take
                    if lots[0]["units"] <= 1e-9:
                        lots.pop(0)
        if lots:
            return lots, False
    units = h.get("units") or 0
    if not units:
        return [], True
    start = h.get("start_date")
    try:
        date = pd.Timestamp(start) if start else None
    except (ValueError, TypeError):
        date = None
    return [{"date": date, "units": float(units), "nav": float(h.get("avg_nav") or 0)}], True


def switch_cost(h: dict, current_nav: float, budget: ExemptionBudget,
                overrides: dict | None = None, today: datetime | None = None) -> dict:
    """Upfront cost of redeeming the whole holding today."""
    today = pd.Timestamp(today or datetime.now())
    load_pct, load_days = exit_load_for(h.get("amfi_code") or h.get("scheme_code"),
                                        h.get("category", ""), overrides)
    lots, approx = build_lots(h)
    out = {"value_rs": 0.0, "exit_load_rs": 0.0, "value_in_exit_load_rs": 0.0, "locked_value_rs": 0.0,
           "stcg_rs": 0.0, "ltcg_rs": 0.0, "approx": approx, "age_unknown": False,
           "cost_basis_known": bool(lots) and all(l["nav"] > 0 for l in lots)}
    for lot in lots:
        value = lot["units"] * current_nav
        out["value_rs"] += value
        age = (today - lot["date"]).days if lot["date"] is not None else None
        if age is None:
            out["age_unknown"] = True
        if h.get("category") == "elss" and (age is None or age < ELSS_LOCKIN_DAYS):
            out["locked_value_rs"] += value      # 3-year lock-in: cannot be redeemed yet
        if age is not None and age < load_days:
            out["exit_load_rs"] += value * load_pct / 100
            out["value_in_exit_load_rs"] += value
        if lot["nav"] > 0:
            gain = lot["units"] * (current_nav - lot["nav"])
            if age is not None and age < 365:
                out["stcg_rs"] += gain
            else:
                out["ltcg_rs"] += gain
    out["stcg_tax_rs"] = max(out["stcg_rs"], 0) * STCG_RATE_EQUITY
    ltcg_pos = max(out["ltcg_rs"], 0)
    out["exemption_used_rs"] = budget.take(ltcg_pos)
    out["ltcg_tax_rs"] = (ltcg_pos - out["exemption_used_rs"]) * LTCG_RATE_EQUITY
    out["tax_rs"] = out["stcg_tax_rs"] + out["ltcg_tax_rs"]
    out["upfront_rs"] = out["exit_load_rs"] + out["tax_rs"]
    return {k: (round(v) if isinstance(v, float) else v) for k, v in out.items()}


def measured_cost_gap_pa(regular_df, direct_df, years: int = 3) -> float | None:
    """Annual return gap Direct − Regular over the last `years` they both cover (≈ TER gap)."""
    if regular_df is None or direct_df is None or regular_df.empty or direct_df.empty:
        return None
    end = min(regular_df["date"].max(), direct_df["date"].max())
    for y in (years, 1):
        start = end - pd.DateOffset(years=y)
        if regular_df["date"].min() > start or direct_df["date"].min() > start:
            continue
        r = cagr(nav_on_or_before(regular_df, start), nav_on_or_before(regular_df, end), y)
        d = cagr(nav_on_or_before(direct_df, start), nav_on_or_before(direct_df, end), y)
        return round(d - r, 2)
    return None


def breakeven_months(upfront_rs: float, value_rs: float, gap_pa_pct: float | None) -> float | None:
    if not gap_pa_pct or gap_pa_pct <= 0 or not value_rs:
        return None
    monthly_saving = value_rs * gap_pa_pct / 100 / 12
    return round(upfront_rs / monthly_saving, 1) if monthly_saving > 0 else None


def required_outperformance_pa(upfront_rs: float, value_rs: float, years: int = 3) -> float | None:
    """How much better (p.a.) the new fund must do to recover the switch cost in `years`."""
    if not value_rs:
        return None
    return round(upfront_rs / value_rs / years * 100, 2)


def direct_switch_advice(breakeven: float | None, in_exit_load_rs: float) -> str:
    if breakeven is None:
        return "Gap not measurable — move new SIPs to Direct; review the switch later."
    if breakeven <= SWITCH_NOW_MAX_MONTHS and not in_exit_load_rs:
        return f"Switch now — the cost is recovered in about {breakeven:.0f} months."
    return ("Move new SIPs to the Direct plan now, and switch existing units as each lot "
            "passes 1 year (no exit load, LTCG rates).")
