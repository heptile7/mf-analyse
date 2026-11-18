"""Offline tests for switch cost, shared exemption, and Regular->Direct breakeven."""
from datetime import datetime

import pandas as pd
import pytest

from mfa.holdings import costs as C
from mfa.holdings import plan as P

TODAY = datetime(2026, 11, 18)


def test_fifo_lots_consume_oldest_first():
    h = {"transactions": [
        {"date": "2023-01-10", "units": 100, "nav": 10},
        {"date": "2024-01-10", "units": 50, "nav": 20},
        {"date": "2025-06-01", "units": -120, "nav": 25},   # redeems all of lot 1 + 20 of lot 2
        {"date": "2026-06-01", "units": 10, "nav": 30},
        {"date": "2026-06-01", "units": None, "amount": -1},  # stamp-duty row
    ]}
    lots, approx = C.build_lots(h)
    assert not approx
    assert [(round(l["units"]), l["nav"]) for l in lots] == [(30, 20), (10, 30)]


def test_switch_cost_splits_stcg_ltcg_and_exit_load():
    h = {"category": "mid cap", "transactions": [
        {"date": "2024-01-01", "units": 100, "nav": 10},   # > 1Y: LTCG
        {"date": "2026-06-01", "units": 100, "nav": 18}]}  # < 1Y: STCG + exit load
    budget = C.ExemptionBudget(1_000)
    c = C.switch_cost(h, 20.0, budget, today=TODAY)
    assert c["value_rs"] == 4000
    assert c["exit_load_rs"] == 20                          # 1% of the young lot's ₹2,000
    assert c["stcg_rs"] == 200 and c["stcg_tax_rs"] == 40   # 20%
    assert c["ltcg_rs"] == 1000 and c["exemption_used_rs"] == 1000 and c["ltcg_tax_rs"] == 0
    assert budget.remaining == 0
    assert c["upfront_rs"] == 60


def test_exemption_budget_is_shared():
    b = C.ExemptionBudget(125_000)
    old = {"category": "flexi cap", "units": 1000, "avg_nav": 100, "start_date": "2020-01-01"}
    c1 = C.switch_cost(old, 200.0, b, today=TODAY)          # ₹1L gain
    c2 = C.switch_cost(old, 200.0, b, today=TODAY)          # second ₹1L gain: only ₹25k exempt
    assert c1["ltcg_tax_rs"] == 0
    assert c2["exemption_used_rs"] == 25_000
    assert c2["ltcg_tax_rs"] == pytest.approx(75_000 * 0.125, abs=1)
    assert c1["approx"] and c1["cost_basis_known"]


def test_unknown_cost_basis_is_flagged_not_taxed():
    c = C.switch_cost({"category": "mid cap", "units": 10, "avg_nav": 0}, 50.0, C.ExemptionBudget())
    assert not c["cost_basis_known"] and c["tax_rs"] == 0 and c["age_unknown"]


def test_elss_lockin_and_overrides():
    h = {"category": "elss", "units": 10, "avg_nav": 10, "start_date": "2025-01-01"}
    assert C.switch_cost(h, 20.0, C.ExemptionBudget(), today=TODAY)["locked_value_rs"] == 200
    assert C.exit_load_for("999", "mid cap", {"999": (0.5, 90)}) == (0.5, 90)
    assert C.exit_load_for("1", "debt") == (0.0, 0)


def test_breakeven_and_required_outperformance():
    assert C.breakeven_months(1_200, 100_000, 1.2) == pytest.approx(12)   # ₹100/month saving
    assert C.breakeven_months(1_200, 100_000, None) is None
    assert C.required_outperformance_pa(3_000, 100_000) == pytest.approx(1.0)
    assert "Switch now" in C.direct_switch_advice(12, 0)
    assert "new SIPs" in C.direct_switch_advice(40, 0)
    assert "new SIPs" in C.direct_switch_advice(6, 5_000)               # units still in exit load


def test_measured_cost_gap():
    dates = pd.bdate_range("2021-01-01", "2026-11-17")
    yrs = (dates - dates[0]).days / 365.25
    direct = pd.DataFrame({"date": dates, "nav": 10 * 1.13 ** yrs})
    regular = pd.DataFrame({"date": dates, "nav": 10 * 1.12 ** yrs})
    assert C.measured_cost_gap_pa(regular, direct) == pytest.approx(1.0, abs=0.05)


def test_action_plan_costs_switches_and_shares_exemption_with_harvest(monkeypatch):
    monkeypatch.setattr(P, "fetch_scheme_nav", lambda code: None)
    monkeypatch.setattr(P, "load_exit_load_overrides", lambda: {})
    common = {"category": "mid cap", "category_rank": 9, "category_size": 10, "score": 20,
              "start_date": "2020-01-01", "years_held": 6.9, "units": 1000, "avg_nav": 100,
              "current_nav": 200.0, "current_value": 200_000, "gain_pct": 100}
    exit_h = {**common, "scheme_name": "Bad Fund", "scheme_code": "1", "action": "EXIT"}
    reg_h  = {**common, "scheme_name": "Good Fund Regular", "scheme_code": "2", "amfi_code": "22",
              "action": "BUY MORE", "is_regular": True, "category_rank": 1, "gain_pct": 50}
    plan = P.generate_action_plan([reg_h, exit_h], [], "balanced", 10_000, [])
    assert plan["switches"][0]["cost"]["exemption_used_rs"] == 100_000   # EXIT costed first
    assert len(plan["direct_switches"]) == 1                              # BUY MORE + Regular
    assert len(plan["buy_mores"]) == 1                                    # still gets BUY MORE
    assert plan["exemption_remaining"] == 0
    harvest = P.compute_tax_harvest_plan([reg_h], [], plan["exemption_remaining"])
    assert harvest["actions"] == [] and harvest["exemption_available"] == 0
