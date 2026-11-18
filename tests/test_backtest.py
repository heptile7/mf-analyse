"""Offline tests for the walk-forward backtest."""
import numpy as np
import pandas as pd
import pytest

from mfa import backtest as bt


def _item(code, name, sebi, annual_pct, start="2012-01-02", end="2020-12-31"):
    dates = pd.bdate_range(start, end)
    daily = (1 + annual_pct / 100) ** (1 / 252) - 1
    rng = np.random.default_rng(code)
    nav = 10 * np.cumprod(1 + daily + rng.normal(0, 0.001, len(dates)))
    return {"scheme": {"schemeCode": code, "schemeName": name, "_sebi_cat": sebi},
            "nav_df": pd.DataFrame({"date": dates, "nav": nav})}


def test_truncate_has_no_lookahead():
    item = _item(1, "A Mid Cap Fund - Direct Plan - Growth", "Equity Scheme - Mid Cap Fund", 10)
    cutoff = pd.Timestamp("2016-12-31")
    [t] = bt.truncate_nav_data([item], cutoff)
    assert t["nav_df"]["date"].max() <= cutoff


def test_truncate_drops_funds_without_a_year_of_history():
    young = _item(2, "Young Mid Cap Fund - Direct Plan - Growth", "Equity Scheme - Mid Cap Fund", 10,
                  start="2016-06-01")
    assert bt.truncate_nav_data([young], pd.Timestamp("2016-12-31")) == []


def test_forward_cagr_needs_full_horizon():
    dates = pd.bdate_range("2014-01-01", "2018-06-30")
    years = (dates - dates[0]).days / 365.25
    df = pd.DataFrame({"date": dates, "nav": 10 * 1.12 ** years})      # exactly 12% p.a.
    assert bt.forward_cagr(df, pd.Timestamp("2016-12-31"), 1) == pytest.approx(12, abs=0.3)
    assert bt.forward_cagr(df, pd.Timestamp("2016-12-31"), 3) is None   # data ends too early


def test_benchmark_truncation_keeps_cutoff_month_only():
    s = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2016-11-01", "2016-12-01", "2017-01-01"]))
    out = bt.truncate_benchmarks({"^NSEI": s}, pd.Timestamp("2016-12-31"))["^NSEI"]
    assert list(out.values) == [1.0, 2.0]


def test_spearman():
    assert bt.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert bt.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert bt.spearman([1, 1, 1], [1, 2, 3]) is None


def test_persistent_skill_is_detected():
    # Each fund compounds at a constant rate, so past winners stay winners:
    # both the model and the naive baseline should find them.
    sebi = "Equity Scheme - Mid Cap Fund"
    nav = [_item(300 + i, f"Fund{i} Mid Cap Fund - Direct Plan - Growth", sebi, 4 + 3 * i)
           for i in range(8)]
    rows = bt.evaluate_cutoff(nav, {}, None, pd.Timestamp("2016-12-31"), horizons=(1, 3))
    by_h = {r["horizon"]: r for r in rows}
    assert set(by_h) == {1, 3}
    assert by_h[3]["naive"]["hit_rate"] == 1.0
    assert by_h[3]["naive"]["ic"] > 0.9
    assert by_h[3]["model"]["ic"] > 0.5
    summary = bt.summarise(rows)
    assert summary["3"]["n_cutoffs"] == 1
    assert "edge" in bt.verdict(summary) or "coin flip" in bt.verdict(summary)
