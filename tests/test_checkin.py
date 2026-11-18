"""Offline tests for the monthly check-in: alert rules and a full non-interactive run."""
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from mfa import checkin as K


def _snap(action="HOLD", rank=2, size=10, code="1"):
    return {"date": "2026-10-01", "holdings": {code: {
        "name": "A Fund", "action": action, "rank": rank, "size": size,
        "quintile": K.quintile(rank, size)}}}


def test_quintile():
    assert K.quintile(1, 10) == 1 and K.quintile(10, 10) == 5 and K.quintile(None, 10) is None


def test_no_change_no_alert():
    assert K.compute_alerts(_snap(), _snap(), None, None, {}, datetime(2026, 11, 1)) == []


def test_worse_rating_and_quintile_drop_alert():
    a = K.compute_alerts(_snap("HOLD"), _snap("SWITCH", rank=7), None, None, {}, datetime(2026, 11, 1))
    assert a == ["A Fund: rating fell HOLD → SWITCH."]
    q = K.compute_alerts(_snap("HOLD", rank=2), _snap("HOLD", rank=5), None, None, {}, datetime(2026, 11, 1))
    assert "quintile 1 to 3" in q[0]


def test_first_checkin_flags_current_exits_only():
    assert K.compute_alerts(None, _snap("EXIT", rank=10), None, None, {}, datetime(2026, 11, 1))
    assert K.compute_alerts(None, _snap("HOLD"), None, None, {}, datetime(2026, 11, 1)) == []


def test_harvest_window_pending_and_data_alerts():
    harvest = {"actions": [{}], "total_tax_saved": 5000}
    pending = {"count": 2, "since": "2026-09-01"}
    bad = {"NAV history (mfapi.in)": {"ok": False, "detail": "stale"},
           "Enrichment (mfdata.in)": {"ok": False, "detail": "down"}}       # not critical
    a = K.compute_alerts(_snap(), _snap(), harvest, pending, bad, datetime(2027, 2, 1))
    assert any("31 March" in x for x in a)
    assert any("still pending" in x for x in a)
    assert sum("Data problem" in x for x in a) == 1
    prev = {**_snap(), "failed_sources": ["NAV history (mfapi.in)"]}       # already reported
    assert not any("Data problem" in x for x in
                   K.compute_alerts(prev, _snap(), None, None, bad, datetime(2027, 2, 1)))
    assert not any("31 March" in x for x in
                   K.compute_alerts(_snap(), _snap(), harvest, None, {}, datetime(2026, 6, 1)))


def test_choose_portfolio():
    assert K.choose_portfolio({"portfolios": {"k": {"h": 1}}}, None) == ("k", {"h": 1})
    assert K.choose_portfolio({"portfolios": {"a": {}, "b": {}}}, None) == (None, None)
    assert K.choose_portfolio({"portfolios": {"a": {}, "b": {}}}, "b") == ("b", {})


def test_run_checkin_end_to_end_without_prompts(tmp_path, monkeypatch):
    dates = pd.bdate_range("2019-01-01", "2026-11-17")
    rng = np.random.default_rng(0)
    nav = [{"scheme": {"schemeCode": 100000 + i, "schemeName": f"Fund{i} Mid Cap Fund - Direct Plan - Growth",
                       "_sebi_cat": "Equity Scheme - Mid Cap Fund"},
            "nav_df": pd.DataFrame({"date": dates,
                                    "nav": 10 * np.cumprod(1 + 0.0004 * i + rng.normal(0, 0.01, len(dates)))})}
           for i in range(6)]
    dbfile = tmp_path / "db.json"
    dbfile.write_text(json.dumps({"version": 2, "portfolios": {"k": {"holdings": [
        {"scheme_code": "100000", "scheme_name": "Fund0 Mid Cap Fund - Direct Plan - Growth",
         "units": 100, "avg_nav": 10, "start_date": "2020-01-01", "sip_amount": 1000}]}}}))
    from mfa.holdings import db as DB
    monkeypatch.setattr(DB, "PORTFOLIO_DB_FILE", str(dbfile))
    monkeypatch.setattr(K, "load_cache", lambda: {})
    monkeypatch.setattr(K, "fetch_and_cache", lambda: nav)
    monkeypatch.setattr(K, "fetch_amfi_master", lambda: {})
    monkeypatch.setattr(K, "fetch_benchmarks", lambda: {})
    monkeypatch.setattr(K, "fetch_fund_enrichment", lambda n: {})
    monkeypatch.setattr(K, "CHECKIN_REPORT_PATTERN", str(tmp_path / "c_{date}.html"))
    sent = []
    monkeypatch.setattr(K, "notify", lambda t, b: sent.append((t, b)) or True)
    from mfa.holdings import plan as P
    monkeypatch.setattr(P, "fetch_scheme_nav", lambda code: None)

    assert K.run_checkin(today=datetime(2026, 11, 18)) == 0       # pytest stdin raises on any prompt
    assert (tmp_path / "c_2026-11-18.html").exists()
    saved = json.loads(dbfile.read_text())["portfolios"]["k"]
    assert len(saved["checkins"]) == 1
    # second run, nothing changed -> no notification
    sent.clear()
    assert K.run_checkin(today=datetime(2026, 12, 1)) == 0
    assert sent == []
    assert len(json.loads(dbfile.read_text())["portfolios"]["k"]["checkins"]) == 2
