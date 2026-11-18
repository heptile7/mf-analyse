"""Offline tests for plain-words summaries, action styles and the holdings report."""
import itertools

from mfa import report, summary as S
from mfa.holdings.analysis import _holding_action


def test_every_possible_rating_has_a_style():
    actions = {_holding_action(r, 100, age, warn)[0]
               for r, age, warn in itertools.product(range(1, 101, 3), (1, 10), (True, False))}
    actions |= {"UNKNOWN"}
    for a in actions:
        assert a in S.ACTION_STYLES, a
    assert "UNKNOWN" not in S.action_style("SWITCH")[1]


def _plan():
    exit_sw = {"from": {"scheme_name": "Bad Fund - Regular Plan", "action": "EXIT",
                        "category_rank": 9, "category_size": 10},
               "to": {"name": "Good Fund"}, "cost": {"upfront_rs": 1200, "cost_basis_known": True,
                                                     "value_rs": 100_000, "exit_load_rs": 0,
                                                     "stcg_tax_rs": 0, "ltcg_tax_rs": 1200}}
    direct = {"holding": {"scheme_name": "Mid Fund"}, "annual_extra_rs": 900, "breakeven_months": 6,
              "advice": "Switch now.", "gap_pa": 0.9, "cost": {"upfront_rs": 450}}
    gap = {"slot": "small cap", "sip": 2000, "fund": {"name": "Small Fund"}, "gap_pct": 15}
    return {"switches": [exit_sw], "direct_switches": [direct], "gap_fills": [gap],
            "buy_mores": [], "overlaps": []}


def test_plain_lines_are_specific():
    p = _plan()
    assert "Exit Bad Fund" in S.plain_switch(p["switches"][0])
    assert "₹1,200" in S.plain_switch(p["switches"][0])
    assert "Regular plan" in S.plain_direct(p["direct_switches"][0])
    assert "6 months" in S.plain_direct(p["direct_switches"][0])
    assert "₹2,000/month" in S.plain_gap(p["gap_fills"][0])


def test_in_short_orders_by_urgency_and_has_fallback():
    lines = S.in_short(_plan(), {"actions": [{"redeem_value": 50_000}], "total_tax_saved": 6_250})
    assert lines[0].startswith("Exit")
    assert "Regular plan" in lines[1]
    assert any("31 March" in l for l in lines)
    assert S.in_short({"switches": [], "direct_switches": [], "gap_fills": [],
                       "buy_mores": [], "overlaps": []})[0].startswith("Nothing urgent")


def test_action_plan_html_and_holdings_report(tmp_path, monkeypatch):
    html = S.action_plan_html(_plan(), None)
    assert "In short" in html and "DIRECT PLAN" in html and "&lt;" not in "Exit"
    monkeypatch.setattr(report, "HOLDINGS_REPORT_FILE", str(tmp_path / "h.html"))
    path = report.generate_holdings_report(
        [{"scheme_name": "A Fund", "action": "SWITCH", "category_rank": 7, "category_size": 9,
          "current_value": 1000, "holding_cagr": 8.0, "action_reason": "below median"}],
        _plan(), None, extra_html=html)
    page = open(path, encoding="utf-8").read()
    assert "🔁 SWITCH" in page and "~8.0%" in page and "In short" in page
