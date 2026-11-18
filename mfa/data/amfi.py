"""AMFI NAVAll master, AUM and monthly portfolio disclosures."""

from datetime import datetime
from datetime import timedelta
import json
import os
import re
import time

import requests

from mfa.config import AMFI_CACHE_FILE, AMFI_CACHE_MAX_AGE_HRS, AMFI_NAV_URL, AMFI_PORTFOLIO_CACHE_FILE, ENRICH_CACHE_MAX_AGE_HRS, HOLDINGS_CACHE_FILE, MFDATA_BASE_URL
from mfa.data.enrichment import fetch_fund_enrichment
from mfa.ui import console
from mfa.util import CircuitBreaker, _cache_busted, _safe_float, _save_output_file


# ══════════════════════════════════════════════════════════════════════
# EXTERNAL DATA SOURCES
# ══════════════════════════════════════════════════════════════════════

def fetch_amfi_master() -> dict:
    if not _cache_busted() and os.path.exists(AMFI_CACHE_FILE):
        try:
            with open(AMFI_CACHE_FILE) as f: cached = json.load(f)
            age = (time.time() - cached.get("timestamp", 0)) / 3600
            if age < AMFI_CACHE_MAX_AGE_HRS:
                data = cached.get("data", {})
                console.print(f"  [green]AMFI master: {len(data):,} schemes (cache {age:.1f}h).[/green]")
                return data
        except Exception:
            pass
    console.print("  [cyan]Fetching AMFI NAVAll.txt...[/cyan]")
    try:
        resp = requests.get(AMFI_NAV_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        console.print(f"  [yellow]AMFI master fetch failed: {e}[/yellow]")
        return {}

    resp.encoding = "utf-8"          # server says ISO-8859-1; content is UTF-8
    master: dict = {}
    current_cat, current_amc = "", "Unknown AMC"
    # Column positions come from the header line. Current layout (2026):
    #   Scheme Code;ISIN Growth;ISIN Reinv;Scheme Name;Plan;Option;Net Asset Value;Date
    # Older layout had no Plan/Option columns (plan was part of the name).
    cols = {"scheme name": 3, "net asset value": 4, "date": 5}
    for line in resp.text.splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith("Scheme Code;"):
            cols = {c.strip().lower(): i for i, c in enumerate(line.split(";"))}
            continue
        if line.startswith(("Open Ended Schemes","Close Ended Schemes","Interval Fund")):
            m = re.search(r'\((.+?)\)', line)
            if m: current_cat = m.group(1).strip()
            continue
        if ";" in line:
            parts = line.split(";")
            if len(parts) >= 4 and parts[0].strip().isdigit():
                code = parts[0].strip()
                def _isin(v: str):
                    v = v.strip()
                    return v if v and v != "-" else None
                def _col(label: str) -> str:
                    i = cols.get(label)
                    return parts[i].strip() if i is not None and i < len(parts) else ""
                # Full name incl. plan/option so Direct vs Regular, Growth vs IDCW stay distinct
                full_name = " - ".join(p for p in (_col("scheme name"), _col("plan"),
                                                    _col("option")) if p)
                master[code] = {"sebi_category": current_cat,
                                "amc_name":   current_amc,
                                "isin":       _isin(parts[1]),
                                "isin_reinv": _isin(parts[2]),
                                "name":       full_name,
                                "nav":        _safe_float(_col("net asset value")),
                                "nav_date":   _col("date")}
            continue
        if not line.startswith(("Scheme Code","Open Ended","Close Ended","Interval")):
            current_amc = line

    _p = json.dumps({"timestamp": time.time(), "data": master}).encode()
    _save_output_file(lambda fh: fh.write(_p), AMFI_CACHE_FILE)
    console.print(f"  [green]AMFI master: {len(master):,} schemes cached.[/green]")
    return master


def fetch_amfi_aum() -> dict:
    """Compatibility wrapper for older callers."""
    return {code: v.get("aum_cr") for code, v in fetch_fund_enrichment().items()
            if v.get("aum_cr") is not None}


AMFI_PORTFOLIO_URL = "https://www.amfiindia.com/modules/PortfolioSchemeDownload"


def _parse_amfi_portfolio_bulk(text: str) -> dict:
    """
    Parse AMFI's consolidated monthly portfolio text file.

    Each scheme block starts with a header line:
      SchemeCode;ISIN-Growth;ISIN-IDCW;SchemeName;NetAssets;Date
    followed by a column-header line:
      Company Name;Rating;Industry;% to NAV
    then holding rows, then a blank line.

    Returns {scheme_code_str: {top_holdings, sector_allocation}}.
    """
    data: dict = {}
    current_code: str | None = None
    current_holdings: list = []
    sector_totals: dict = {}
    in_holdings = False

    def _flush():
        if not current_code or not current_holdings:
            return
        top = sorted(current_holdings, key=lambda x: x.get("weight", 0), reverse=True)[:10]
        entry: dict = {"top_holdings": top}
        if sector_totals:
            entry["sector_allocation"] = [
                {"sector": s, "weight": round(w, 2)}
                for s, w in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)[:10]
            ]
        data[current_code] = entry

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Blank line ends current scheme block
        if not line:
            _flush()
            current_code = None
            current_holdings = []
            sector_totals = {}
            in_holdings = False
            continue

        parts = [p.strip() for p in line.split(";")]

        # Scheme header: first token is the numeric scheme code
        if parts[0].isdigit() and 1 <= len(parts[0]) <= 8:
            _flush()
            current_code = parts[0]
            current_holdings = []
            sector_totals = {}
            in_holdings = False
            continue

        # Column header row
        if parts[0].lower() in ("company name", "name", "instrument", "issuer name"):
            in_holdings = True
            continue

        # Holding data row
        if in_holdings and current_code and len(parts) >= 2:
            name = parts[0]
            if not name or name.lower() in ("total", "grand total", ""):
                continue
            # Format: Company Name ; Rating ; Industry ; % to NAV
            industry = parts[2] if len(parts) > 2 else ""
            pct_str  = parts[3] if len(parts) > 3 else parts[-1]
            pct = _safe_float(pct_str.replace("%", "").strip()) or 0.0
            if name and pct > 0:
                current_holdings.append({"name": name, "weight": pct})
                if industry:
                    sector_totals[industry] = sector_totals.get(industry, 0) + pct

    _flush()   # handle file with no trailing blank line
    return data


def fetch_amfi_portfolio_all() -> dict:
    """
    Fetch AMFI's consolidated monthly portfolio for all schemes.
    Returns {scheme_code_str: {top_holdings, sector_allocation}}.
    Cached for 30 days — AMFI publishes monthly data.
    """
    if not _cache_busted() and os.path.exists(AMFI_PORTFOLIO_CACHE_FILE):
        try:
            with open(AMFI_PORTFOLIO_CACHE_FILE, encoding="utf-8") as f:
                c = json.load(f)
            age_days = (time.time() - c.get("timestamp", 0)) / 86400
            if age_days < 30 and c.get("data"):
                console.print(f"  [green]AMFI portfolio cache: {len(c['data'])} schemes.[/green]")
                return c["data"]
        except Exception:
            pass

    # Target last full month (AMFI publishes ~10th of following month)
    now = datetime.now()
    target = (now.replace(day=1) - timedelta(days=1))
    month_str = target.strftime("%b")   # e.g. "Apr"
    year_str  = target.strftime("%Y")   # e.g. "2026"

    console.print(f"  [cyan]Fetching AMFI portfolio data ({month_str} {year_str})...[/cyan]")
    headers = {"User-Agent": "Mozilla/5.0"}
    data: dict = {}

    for attempt_params in (
        {"download": "yes", "frmMnth": month_str, "frmYear": year_str},
        {"DOWNLOAD": "YES", "frmMnth": month_str, "frmYear": year_str},
    ):
        try:
            r = requests.get(AMFI_PORTFOLIO_URL, params=attempt_params,
                             timeout=60, headers=headers)
            if r.status_code != 200:
                continue
            ct = r.headers.get("Content-Type", "")
            # Accept text/plain, text/csv, application/octet-stream (AMFI varies)
            if "html" in ct and "<html" in r.text[:200].lower():
                continue   # got a login/error page, not data
            data = _parse_amfi_portfolio_bulk(r.text)
            if data:
                break
        except Exception as e:
            console.print(f"  [yellow]AMFI portfolio attempt failed: {e}[/yellow]")
            continue

    if data:
        console.print(f"  [green]AMFI portfolio parsed: {len(data)} schemes.[/green]")
    else:
        console.print("  [yellow]AMFI portfolio unavailable — will use mfdata.in holdings only.[/yellow]")

    try:
        _p = json.dumps({"timestamp": time.time(), "data": data}).encode()
        _save_output_file(lambda fh: fh.write(_p), AMFI_PORTFOLIO_CACHE_FILE)
    except Exception:
        pass
    return data


def fetch_fund_portfolios(nav_data: list = None, enrichment: dict = None) -> dict:
    """
    Fetch per-scheme portfolio holdings, sector allocation, and fund manager.
    Priority: (1) AMFI official monthly CSV  (2) mfdata.in per-scheme endpoint.
    Returns {scheme_code: {top_holdings, sector_allocation, fund_manager}}.
    Cached 24 h.
    """
    if not _cache_busted() and os.path.exists(HOLDINGS_CACHE_FILE):
        try:
            with open(HOLDINGS_CACHE_FILE, encoding="utf-8") as f:
                c = json.load(f)
            age = (time.time() - c.get("timestamp", 0)) / 3600
            if age < ENRICH_CACHE_MAX_AGE_HRS and c.get("data"):
                console.print(f"  [green]Holdings cache: {len(c['data'])} schemes.[/green]")
                return c["data"]
        except Exception:
            pass

    # ── Step 1: AMFI official monthly CSV (authoritative, free, no rate-limit) ──
    amfi_data = fetch_amfi_portfolio_all()
    data: dict = {code: dict(entry) for code, entry in amfi_data.items()}
    amfi_hits = len(data)

    # Collect codes we still need holdings for (enrichment or NAV universe)
    if enrichment:
        all_codes = list(enrichment.keys())[:300]
    elif nav_data:
        all_codes = [str(item["scheme"].get("schemeCode", ""))
                     for item in nav_data if item["scheme"].get("schemeCode")]
    else:
        all_codes = []

    # ── Step 2: mfdata.in per-scheme for codes AMFI didn't cover ─────────
    missing = [c for c in all_codes if c and not data.get(c, {}).get("top_holdings")]
    headers = {"User-Agent": "Mozilla/5.0"}
    mfdata_hits = 0
    rate_limited = False

    if missing:
        console.print(f"  [cyan]Filling {len(missing)} gaps via mfdata.in...[/cyan]")
    breaker = CircuitBreaker()
    for code in missing:
        if rate_limited:
            break
        if breaker.open:
            console.print("  [yellow]mfdata.in not responding — skipped remaining holdings lookups.[/yellow]")
            break
        for endpoint in (
            f"{MFDATA_BASE_URL}/schemes/{code}/portfolio",
            f"{MFDATA_BASE_URL}/schemes/{code}/holdings",
        ):
            try:
                r = requests.get(endpoint, timeout=8, headers=headers)
                breaker.result(r.status_code < 500)
                if r.status_code == 404:
                    continue
                if r.status_code == 429:
                    console.print("  [yellow]mfdata.in rate limit on holdings — stopping.[/yellow]")
                    rate_limited = True
                    break
                if r.status_code != 200:
                    continue
                payload = r.json()
                row = payload.get("data", payload) if isinstance(payload, dict) else {}
                if not row or not isinstance(row, dict):
                    continue
                entry = data.setdefault(code, {})
                raw_h = row.get("top_holdings") or row.get("holdings") or row.get("portfolio") or []
                if raw_h and isinstance(raw_h, list):
                    entry["top_holdings"] = raw_h[:10]
                raw_s = row.get("sector_allocation") or row.get("sectors") or []
                if raw_s and isinstance(raw_s, list):
                    entry["sector_allocation"] = raw_s[:10]
                mgr = row.get("fund_manager") or row.get("manager") or row.get("managers")
                if mgr:
                    entry["fund_manager"] = mgr
                if entry:
                    mfdata_hits += 1
                break
            except requests.RequestException:
                breaker.result(False)
                if breaker.open:
                    break
                continue
            except Exception:
                continue
        time.sleep(0.05)

    # ── Step 3: back-fill fund_manager from enrichment cache ──────────────
    for code, enr in (enrichment or {}).items():
        entry = data.setdefault(code, {})
        if not entry.get("fund_manager"):
            mgr = enr.get("fund_manager") or enr.get("managers") or enr.get("manager")
            if mgr:
                entry["fund_manager"] = mgr

    console.print(f"  [green]Portfolio data: {amfi_hits} from AMFI + {mfdata_hits} from mfdata.in.[/green]")

    try:
        _p = json.dumps({"timestamp": time.time(), "data": data}).encode()
        _save_output_file(lambda fh: fh.write(_p), HOLDINGS_CACHE_FILE)
    except Exception:
        pass
    return data
