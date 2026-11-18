"""Offline smoke tests for the pure maths and holdings-resolution logic."""
import json

import pandas as pd
import pytest

import MF_analyser as M


# ── maths ─────────────────────────────────────────────────────────────
def test_cagr_doubling_in_one_year():
    assert M.cagr(100, 200, 1) == pytest.approx(100.0)


def test_cagr_guards_bad_input():
    assert M.cagr(0, 200, 1) == 0.0
    assert M.cagr(100, 200, 0) == 0.0


def test_sip_corpus_zero_rate_is_sum_of_instalments():
    assert M.sip_corpus(1000, 12, 0) == 12_000


def test_sip_corpus_grows_with_rate():
    assert M.sip_corpus(1000, 120, 0.12) > M.sip_corpus(1000, 120, 0.06) > 120_000


@pytest.mark.parametrize("rank,size,expected", [
    (1, 100, "BUY MORE"), (30, 100, "HOLD"), (50, 100, "HOLD/WATCH"),
    (70, 100, "SWITCH"), (95, 100, "EXIT"),
])
def test_holding_action_thresholds(rank, size, expected):
    action, _, _ = M._holding_action(rank, size, fund_age=10, aum_warn=False)
    assert action == expected


def test_holding_action_downgrades_young_or_bloated_funds():
    assert M._holding_action(1, 100, fund_age=2, aum_warn=False)[0] == "HOLD"
    assert M._holding_action(1, 100, fund_age=10, aum_warn=True)[0] == "HOLD"


def _fund(cat, corpus, weight=50, rate=0.12, ter=0.7):
    return {"category": cat, "weight_pct": weight, "projected_corpus": corpus,
            "estimated_rate": rate, "ter": ter}


def test_compute_tax_ltcg_exemption_applies():
    # ₹10k/mo for 10y = ₹12L invested; small gain stays inside the 10y exemption
    port = [_fund("flexi cap", 6_50_000), _fund("mid cap", 6_50_000)]
    tax = M.compute_tax(port, 10_000, 10, 30)
    assert tax["p50"]["eq_tax"] == 0
    assert tax["p50"]["net"] == tax["p50"]["gross"]


def test_compute_tax_debt_taxed_at_slab():
    port = [_fund("debt", 2_00_000, weight=100)]
    tax = M.compute_tax(port, 1_000, 10, 30)          # ₹1.2L invested, ₹80k gain
    assert tax["p50"]["db_tax"] == pytest.approx(80_000 * 0.30)


def test_stepup_beats_flat():
    port = [_fund("flexi cap", 0, weight=100)]
    port[0]["projected_corpus"] = M.sip_corpus(10_000, 120, 0.12)
    s = M.compute_stepup_projection(port, 10_000, 10, 10)
    assert s["stepup_schedule"][1]["monthly_sip"] == 11_000
    assert s["stepup_p50"] > s["flat_p50"]


# ── holdings cleaning / resolution ────────────────────────────────────
JUNK = ["Below 100", "100 - 200", "More than 1000", "Blue", "Very High",
        "Moderately Low", "Low to Moderate"]


def test_clean_holdings_drops_scraped_ui_labels():
    rows = [{"scheme_name": n} for n in JUNK] + [{"scheme_name": "SBI Small Cap Fund Regular Growth"}]
    assert [h["scheme_name"] for h in M._clean_holdings(rows)] == ["SBI Small Cap Fund Regular Growth"]


@pytest.mark.parametrize("a,b", [
    ("NIPPON INDIA MF-NIPPON INDIA LARGE CAP FUND-DIR-GROWTH",
     "Nippon India Large Cap Fund - Direct Plan Growth Plan - Growth Option"),
    ("ICICI Prudential Smallcap Fund - Direct Plan - Growth",
     "ICICI Prudential Small Cap Fund - Direct Plan - Growth"),
    ("HDFC Mid Cap Fund - Regular Plan - Growth",
     "HDFC Mid Cap Fund - Growth Option - Direct Plan"),
    ("Aditya Birla Sun Life Large Cap Fund -Growth-Regular Plan(fo",
     "Aditya Birla Sun Life Large Cap Fund - Growth - Direct Plan"),
])
def test_fund_name_key_equates_spellings(a, b):
    assert M._fund_name_key(a) == M._fund_name_key(b)


@pytest.fixture
def world():
    amfi = {
        "100001": {"isin": "INF000A01AA1", "isin_reinv": None, "nav": 50.0,
                   "name": "Alpha Mid Cap Fund - Direct Plan - Growth"},
        "100002": {"isin": "INF000A01BB2", "isin_reinv": None, "nav": 45.0,
                   "name": "Alpha Mid Cap Fund - Regular Plan - Growth"},
        "100003": {"isin": "INF000A01CC3", "isin_reinv": None, "nav": 20.0,
                   "name": "Alpha Large and Mid Cap Fund - Direct Plan - Growth"},
    }
    nav_data = [{"scheme": {"schemeCode": 100001, "schemeName": "Alpha Mid Cap Fund - Direct Plan - Growth",
                            "isinGrowth": "INF000A01AA1"},
                 "nav_df": pd.DataFrame({"nav": [49.0, 50.0]})}]
    universe = [{"scheme_code": "100001", "name": "Alpha Midcap Fund - Direct Growth"},
                {"scheme_code": "100003", "name": "Alpha Large & Mid Cap Fund - Direct Growth"}]
    return amfi, nav_data, M.build_holdings_index(universe, nav_data, amfi)


def test_resolve_by_isin(world):
    amfi, _, idx = world
    r = M.resolve_holding({"scheme_code": "INF000A01AA1", "scheme_name": "x"}, idx, amfi)
    assert r["amfi_code"] == "100001" and r["scored"]["scheme_code"] == "100001"


def test_rta_code_resolved_by_name_and_regular_uses_direct_sibling(world):
    amfi, _, idx = world
    h = {"scheme_code": "MCOG", "scheme_name": "Alpha Mid Cap Fund - Regular Plan - Growth"}
    r = M.resolve_holding(h, idx, amfi)
    assert r["amfi_code"] == "100002"            # the Regular scheme actually held
    assert r["is_regular"]
    assert r["scored"]["scheme_code"] == "100001"  # ranked via Direct sibling


def test_short_numeric_rta_code_is_not_trusted(world):
    amfi, _, idx = world
    amfi = {**amfi, "51": {"name": "Unrelated Gilt Fund - Direct Plan - Growth"}}
    r = M.resolve_holding({"scheme_code": "51", "scheme_name": "Alpha Mid Cap Fund - Direct Plan - Growth"},
                          idx, amfi)
    assert r["amfi_code"] == "100001"


def test_name_match_never_crosses_category(world):
    amfi, _, idx = world
    r = M.resolve_holding({"scheme_code": "", "scheme_name": "Alpha Small Cap Fund - Direct"}, idx, amfi)
    assert r["scored"] is None


def test_regular_holding_valued_at_its_own_nav(world):
    amfi, nav_data, _ = world
    h = {"scheme_code": "MCOG", "scheme_name": "Alpha Mid Cap Fund - Regular Plan - Growth",
         "units": 10, "avg_nav": 40}
    universe = [{"scheme_code": "100001", "name": "Alpha Midcap Fund - Direct Growth",
                 "category": "mid cap", "category_rank": 1, "category_size": 10,
                 "fund_age_years": 10, "score": 80}]
    [a] = M.analyse_holdings([h], universe, nav_data, amfi)
    assert a["current_nav"] == 45.0                # not the Direct NAV of 50
    assert a["is_regular"] and "Regular plan" in a["action_reason"]


def test_normalise_rewrites_codes_and_keeps_original(world):
    amfi, nav_data, _ = world
    rows = [{"scheme_code": "MCOG", "scheme_name": "Alpha Mid Cap Fund - Regular Plan - Growth"},
            {"scheme_code": "", "scheme_name": "Very High"}]
    out, changed = M.normalise_stored_holdings(rows, amfi, nav_data)
    assert changed and len(out) == 1
    assert out[0]["scheme_code"] == "100002" and out[0]["rta_code"] == "MCOG"
    assert out[0]["isin"] == "INF000A01BB2"


# ── PAN hashing / migration ───────────────────────────────────────────
def test_pan_key_migrates_legacy_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "PAN_SALT_FILE", str(tmp_path / ".mf_salt"))
    monkeypatch.setattr(M, "PORTFOLIO_DB_FILE", str(tmp_path / "db.json"))
    pan = "ABCDE1234F"
    legacy = M._legacy_hash_pan(pan)
    (tmp_path / "db.json").write_text(json.dumps(
        {"version": 1, "portfolios": {legacy: {"holdings": [{"scheme_name": "x"}]}}}))

    key = M.pan_key(pan)
    db = json.loads((tmp_path / "db.json").read_text())
    assert key != legacy and len(key) == 24
    assert legacy not in db["portfolios"] and db["portfolios"][key]["holdings"]
    assert db["version"] == 2
    assert M.pan_key(pan) == key                     # stable across calls (salt persisted)


def test_pan_hash_depends_on_salt(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "PAN_SALT_FILE", str(tmp_path / "a"))
    a = M.hash_pan("ABCDE1234F")
    monkeypatch.setattr(M, "PAN_SALT_FILE", str(tmp_path / "b"))
    assert M.hash_pan("ABCDE1234F") != a
