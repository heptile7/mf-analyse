"""Offline tests for XIRR, transaction cashflows and unit reconciliation."""
import pandas as pd
import pytest

from mfa.holdings import analysis as HA
from mfa.metrics import holding_cashflows, xirr


def test_lump_sum_xirr_equals_cagr():
    assert xirr([("2020-01-01", -100), ("2023-01-01", 133.1)]) == pytest.approx(10, abs=0.1)


def test_monthly_sip_xirr():
    # 12 monthly ₹1,000 SIPs growing at exactly 1%/month ≈ 12.68% p.a.
    dates = pd.date_range("2025-01-01", periods=13, freq="MS")
    value = sum(1000 * 1.01 ** (12 - i) for i in range(12))
    flows = [(d, -1000) for d in dates[:12]] + [(dates[12], value)]
    assert xirr(flows) == pytest.approx(12.68, abs=0.3)


def test_xirr_rejects_one_sided_flows():
    assert xirr([("2020-01-01", -100), ("2021-01-01", -100)]) is None
    assert xirr([]) is None


def test_cashflows_skip_tax_rows_and_add_value():
    txns = [{"date": "2024-01-01", "amount": 5000, "units": 100},
            {"date": "2024-01-01", "amount": 0.25, "units": None},       # stamp duty
            {"date": "2025-01-01", "amount": -2000, "units": -30}]       # redemption
    flows = holding_cashflows(txns, 6000, today="2026-01-01")
    assert [a for _, a in flows] == [-5000, 2000, 6000]


def test_analyse_holdings_reports_xirr_and_mismatch():
    nav = [{"scheme": {"schemeCode": 100001, "schemeName": "A Mid Cap Fund - Direct Plan - Growth"},
            "nav_df": pd.DataFrame({"date": pd.to_datetime(["2026-11-17"]), "nav": [20.0]})}]
    h = {"scheme_code": "100001", "scheme_name": "A Mid Cap Fund - Direct Plan - Growth",
         "units": 105, "avg_nav": 10, "start_date": "2023-11-17",
         "transactions": [{"date": "2023-11-17", "amount": 1000, "units": 100, "nav": 10}]}
    [a] = HA.analyse_holdings([h], [], nav, {})
    assert a["return_basis"] == "xirr" and a["xirr"] > 0
    assert a["units_mismatch"] == pytest.approx(5)
    assert HA.return_str(a).endswith("%") and not HA.return_str(a).startswith("~")
    assert HA.return_str({"holding_cagr": 9.5}) == "~9.5%"
