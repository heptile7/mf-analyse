"""Offline tests for recommendation tracking and action-plan history."""
import json
from datetime import datetime

import pytest

from mfa import tracking as T
from mfa.holdings import db as DB


def test_pct_change_and_annualise():
    assert T.pct_change(100, 110) == pytest.approx(10)
    assert T.pct_change(0, 110) is None
    assert T.annualise(21, 730) == pytest.approx(10, abs=0.05)
    assert T.annualise(5, 200) is None                  # under a year: not annualised


def test_evaluate_entry_weights_and_index():
    entry = {"date": "2025-01-01", "strategy": "balanced", "index_nav": 100,
             "funds": [{"code": "1", "name": "A", "weight_pct": 75, "nav": 10},
                       {"code": "2", "name": "B", "weight_pct": 25, "nav": 20},
                       {"code": "3", "name": "Gone", "weight_pct": 10, "nav": 5}]}
    now = {"1": 12.0, "2": 20.0, "3": None}.get        # +20%, 0%, missing
    r = T.evaluate_entry(entry, now, index_now=110, today=datetime(2026, 1, 1))
    assert r["portfolio_pct"] == pytest.approx(15)       # 0.75*20 + 0.25*0, missing fund dropped
    assert r["index_pct"] == pytest.approx(10)
    assert r["diff_pct"] == pytest.approx(5)
    assert r["missing"] == ["Gone"] and not r["too_early"]


def test_record_recommendation_dedupes(tmp_path):
    path = str(tmp_path / "recs.json")
    port = [{"scheme_code": 1, "name": "A", "weight_pct": 60, "latest_nav": 10.0},
            {"scheme_code": 2, "name": "B", "weight_pct": 40, "latest_nav": 20.0}]
    assert T.record_recommendation(port, "balanced", 100.0, path=path, today="2026-01-01")
    assert not T.record_recommendation(port, "balanced", 100.0, path=path, today="2026-01-01")
    assert T.record_recommendation(port, "balanced", 101.0, path=path, today="2026-02-01")
    assert len(T.load_recommendations(path)) == 2


def test_action_plan_history_seeds_from_legacy_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(DB, "PORTFOLIO_DB_FILE", str(tmp_path / "db.json"))
    legacy = {"generated_on": "2026-01-01", "strategy": "balanced", "items": []}
    (tmp_path / "db.json").write_text(json.dumps(
        {"version": 2, "portfolios": {"k": {"holdings": [], "action_plan": legacy}}}))
    plan = {"strategy": "aggressive", "switches": [{
        "from": {"scheme_name": "Old", "scheme_code": "111", "amfi_code": "222", "current_nav": 50.0},
        "to": {"name": "New", "scheme_code": "333", "latest_nav": 20.0},
        "exit_load_warn": False, "ltcg_rs": 0, "tax_rs": 0}]}
    DB.save_action_plan("k", plan)
    port = json.loads((tmp_path / "db.json").read_text())["portfolios"]["k"]
    assert [p["generated_on"] for p in port["action_plans"]][0] == "2026-01-01"
    assert len(port["action_plans"]) == 2
    item = port["action_plan"]["items"][0]
    assert (item["from_amfi_code"], item["from_nav"], item["to_nav"]) == ("222", 50.0, 20.0)


def test_since_plan_note(monkeypatch):
    navs = {"222": 55.0, "333": 24.0}                    # kept +10%, suggested +20%
    monkeypatch.setattr(T, "fetch_scheme_nav", lambda code: None)
    monkeypatch.setattr(DB, "since_return",
                        lambda code, nav_then, lookup: T.pct_change(nav_then, navs.get(str(code))))
    note = DB._since_plan_note({"from_amfi_code": "222", "from_nav": 50.0,
                                "to": {"scheme_code": "333"}, "to_nav": 20.0})
    assert "kept +10.0%" in note and "suggested +20.0%" in note
