"""mfdata.in enrichment (TER, AUM, PE, category ranks)."""

import json
import os
import time

import requests

from mfa.config import ENRICH_CACHE_FILE, ENRICH_CACHE_MAX_AGE_HRS, MFDATA_BASE_URL
from mfa.ui import console
from mfa.util import CircuitBreaker, _cache_busted, _safe_float, _save_output_file


def _normalise_enrichment(row: dict) -> dict:
    """Keep only fields that strengthen ranking and reports."""
    if not isinstance(row, dict):
        return {}
    out = dict(row)
    code = row.get("scheme_code") or row.get("amfi_code") or row.get("code")
    if code is not None:
        out["scheme_code"] = str(code)
    if row.get("aum_cr") in (None, "", "N/A") and row.get("aum") not in (None, "", "N/A"):
        aum_rs = _safe_float(row.get("aum"))
        if aum_rs is not None:
            out["aum_cr"] = round(aum_rs / 10_000_000, 2)
    for src, dst in (
        ("aum_cr", "aum_cr"),
        ("expense_ratio", "expense_ratio"),
        ("rating", "rating"),
        ("morningstar", "rating"),
        ("min_investment", "min_investment"),
    ):
        val = _safe_float(row.get(src))
        if val is not None and dst not in out:
            out[dst] = val
        elif val is not None:
            out[dst] = val
    # Preserve fund manager fields
    for fld in ("fund_manager", "managers", "manager", "fund_managers"):
        if row.get(fld) not in (None, "", [], {}):
            out[fld] = row[fld]
    # Preserve portfolio / holdings fields for report display
    for fld in ("top_holdings", "holdings", "sector_allocation", "portfolio"):
        if row.get(fld) not in (None, "", [], {}):
            out[fld] = row[fld]
    return out


def _merge_enrichment(dst: dict, src: dict) -> None:
    for k, v in _normalise_enrichment(src).items():
        if v not in (None, "", [], {}):
            dst[k] = v


def fetch_fund_enrichment(nav_data: list = None) -> dict:
    """
    Fetch scheme metadata from mfdata.in.

    mfdata.in publishes a no-auth Indian mutual fund API with AUM, TER,
    ratings, return ranks and ratios. It is used as an enrichment layer:
    ranking improves when available, but the NAV-only model still works.
    """
    wanted = {str(x["scheme"].get("schemeCode")) for x in (nav_data or [])
              if x.get("scheme", {}).get("schemeCode")}

    if not _cache_busted() and os.path.exists(ENRICH_CACHE_FILE):
        try:
            with open(ENRICH_CACHE_FILE, encoding="utf-8") as f:
                c = json.load(f)
            age = (time.time() - c.get("timestamp", 0)) / 3600
            data = c.get("data", {})
            enough = (not wanted) or len(data) >= min(100, max(20, int(len(wanted) * 0.30)))
            if age < ENRICH_CACHE_MAX_AGE_HRS and data and enough:
                console.print(f"  [green]Enrichment cache: {len(data)} schemes.[/green]")
                return {str(k): _normalise_enrichment(v) for k, v in data.items()}
            if data and not enough:
                console.print("  [yellow]Enrichment cache is partial; refreshing.[/yellow]")
        except Exception:
            pass

    data: dict = {}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        offset, limit = 0, 1000
        while True:
            params = {
                "plan_type": "direct",
                "exclude_fmp": "true",
                "limit": limit,
                "offset": offset,
            }
            r = requests.get(f"{MFDATA_BASE_URL}/schemes", params=params,
                             timeout=20, headers=headers)
            if r.status_code == 429:
                console.print("  [yellow]mfdata.in rate limit hit; using partial enrichment.[/yellow]")
                break
            r.raise_for_status()
            payload = r.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not rows:
                break
            for row in rows:
                norm = _normalise_enrichment(row)
                code = norm.get("scheme_code")
                if code and (not wanted or code in wanted):
                    data.setdefault(code, {})
                    _merge_enrichment(data[code], norm)
            if len(rows) < limit:
                break
            offset += limit
            if offset >= 20000:
                break
        if data:
            console.print(f"  [green]mfdata.in enrichment: {len(data)} schemes.[/green]")
    except Exception as e:
        console.print(f"  [yellow]mfdata.in enrichment unavailable: {e}[/yellow]")

    # Fill deeper fields only for schemes that survived the list endpoint.
    # This stays conservative with rate limits and gives selected funds better TER/rank data.
    missing_detail = [c for c in wanted if c not in data]
    detail_codes = list(data.keys())[:50] + missing_detail[:50]
    seen: set = set()
    breaker = CircuitBreaker()
    for code in detail_codes:
        if code in seen:
            continue
        if breaker.open:
            console.print("  [yellow]mfdata.in not responding — skipped remaining detail lookups.[/yellow]")
            break
        seen.add(code)
        try:
            r = requests.get(f"{MFDATA_BASE_URL}/schemes/{code}",
                             timeout=12, headers=headers)
            breaker.result(r.status_code < 500)
            if r.status_code in (404, 429):
                continue
            r.raise_for_status()
            payload = r.json()
            row = payload.get("data", payload) if isinstance(payload, dict) else {}
            if row:
                data.setdefault(str(code), {})
                _merge_enrichment(data[str(code)], row)
        except requests.RequestException:
            breaker.result(False)
            continue
        except Exception:
            continue
        time.sleep(0.08)

    if not data:
        console.print("  [yellow]No enrichment data available; using NAV-only ranking.[/yellow]")

    if data:          # an empty result (source down) must not block retries for 24h
        try:
            _p = json.dumps({"timestamp": time.time(), "data": data}).encode()
            _save_output_file(lambda fh: fh.write(_p), ENRICH_CACHE_FILE)
        except Exception:
            pass
    return data
