"""Down data sources must fail fast, not hang per fund."""
import requests

from mfa.data import amfi, enrichment
from mfa.util import CircuitBreaker


def test_circuit_breaker_opens_and_resets():
    b = CircuitBreaker(limit=3)
    for _ in range(2):
        b.result(False)
    assert not b.open
    b.result(True)
    for _ in range(3):
        b.result(False)
    assert b.open


def _nav(n):
    return [{"scheme": {"schemeCode": 100000 + i}} for i in range(n)]


def test_holdings_loop_stops_after_consecutive_timeouts(monkeypatch, tmp_path):
    calls = []

    def dead(url, **kw):
        calls.append(url)
        raise requests.Timeout("down")
    monkeypatch.setattr(amfi, "HOLDINGS_CACHE_FILE", str(tmp_path / "h.json"))
    monkeypatch.setattr(amfi, "fetch_amfi_portfolio_all", lambda: {})
    monkeypatch.setattr(amfi.requests, "get", dead)
    monkeypatch.setattr(amfi.time, "sleep", lambda s: None)
    amfi.fetch_fund_portfolios(_nav(300), {})
    assert len(calls) <= 6                       # was 600 (300 funds x 2 endpoints)


def test_enrichment_detail_loop_stops_on_5xx(monkeypatch, tmp_path):
    calls = []

    class R:
        status_code = 522
        def raise_for_status(self):
            raise requests.HTTPError("522")
    monkeypatch.setattr(enrichment, "ENRICH_CACHE_FILE", str(tmp_path / "e.json"))
    monkeypatch.setattr(enrichment.requests, "get", lambda url, **kw: calls.append(url) or R())
    monkeypatch.setattr(enrichment.time, "sleep", lambda s: None)
    assert enrichment.fetch_fund_enrichment(_nav(200)) == {}
    assert len(calls) <= 7                       # 1 list call + 5 detail calls, not ~51
