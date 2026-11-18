"""Offline tests for the data-status registry and score completeness."""
import pandas as pd
import pytest

from mfa.data import status as S


@pytest.fixture(autouse=True)
def clean_registry():
    S.reset()
    yield
    S.reset()


def _nav(last_date):
    return [{"scheme": {"schemeCode": 1}, "nav_df": pd.DataFrame(
        {"date": pd.to_datetime(["2020-01-01", last_date]), "nav": [1.0, 2.0]})}]


def test_fresh_sources_are_ok_and_no_banner():
    S.record("NAV history (mfapi.in)", True, "650 funds", coverage=0.99)
    S.assess_sources(_nav("2026-11-17"), {"1": {"nav_date": "17-Nov-2026"}},
                     {"^NSEI": pd.Series([1.0], index=pd.to_datetime(["2026-11-01"]))},
                     {"1": {}}, {"1": {}}, today="2026-11-18")
    snap = S.snapshot()
    assert all(v["ok"] for v in snap.values())
    assert snap["AMFI scheme master"]["as_of"] == "2026-11-17"
    assert S.status_banner_html({"missing": [], "n_real": 7, "n_total": 7, "shares": {}}) == ""


def test_stale_nav_and_missing_enrichment_degrade():
    S.assess_sources(_nav("2026-10-01"), {}, {}, {}, {}, today="2026-11-18")
    snap = S.snapshot()
    assert not snap["NAV history (mfapi.in)"]["ok"]
    assert "business days old" in snap["NAV history (mfapi.in)"]["detail"]
    assert not snap["Enrichment (mfdata.in)"]["ok"]
    assert "Degraded" in S.status_banner_html()


def test_refusal_below_coverage_threshold():
    S.record("NAV history (mfapi.in)", False, coverage=0.5)
    assert "50%" in S.nav_refusal_reason()
    S.record("NAV history (mfapi.in)", True, coverage=0.93)
    assert S.nav_refusal_reason() is None


def test_score_completeness_without_enrichment():
    funds = [{"category": "mid cap", "info_ratio": 0.3, "capture_ratio": 1.1,
              "rank_consistency": 0.6, "ter": 0.85}] * 4     # 0.85 = mid-cap default TER
    c = S.score_completeness(funds)
    assert c["n_real"] == 3 and c["n_total"] == 7
    assert set(c["missing"]) == {"cost", "capacity", "valuation", "ext_rank"}
    funds2 = [{**funds[0], "ter": 0.55, "aum_cr": 5000}] * 4
    assert set(S.score_completeness(funds2)["real"]) >= {"cost", "capacity"}
