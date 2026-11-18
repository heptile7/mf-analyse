"""
Mutual Fund Portfolio Builder  v3
===================================
Sources: mfapi.in · AMFI NAVAll.txt · mfdata.in · yfinance (Nifty 50, Midcap)
Not SEBI-registered financial advice.

Scoring: 7 components — category-relative, regime-aware, TER/AUM/enrichment-aware
Output : Rich terminal · HTML report (self-contained) · PNG chart pack

Run:  python MF_analyser.py
Deps: pip install requests pandas numpy matplotlib yfinance rich openpyxl
"""

# ── stdlib ───────────────────────────────────────────────────────────
import os, re, sys, json, time, io, base64, math, hashlib, html as html_lib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── third-party ──────────────────────────────────────────────────────
import requests, pandas as pd, numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from matplotlib.gridspec import GridSpec

from rich.console import Console
from rich.table   import Table
from rich.panel   import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.text    import Text
from rich.prompt  import Prompt, IntPrompt

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False

console = Console()

# ══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════

BASE_URL        = "https://api.mfapi.in/mf"
AMFI_NAV_URL    = "https://www.amfiindia.com/spages/NAVAll.txt"
MFDATA_BASE_URL = "https://mfdata.in/api/v1"

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE           = os.path.join(_DIR, "mf_nav_cache.json")
TER_OVERRIDE_FILE    = os.path.join(_DIR, "mf_ter_overrides.json")
AMFI_CACHE_FILE      = os.path.join(_DIR, "mf_amfi_master.json")
BENCHMARK_CACHE_FILE = os.path.join(_DIR, "mf_benchmark.json")
ENRICH_CACHE_FILE         = os.path.join(_DIR, "mf_enrichment_cache.json")
HOLDINGS_CACHE_FILE       = os.path.join(_DIR, "mf_holdings_cache.json")
AMFI_PORTFOLIO_CACHE_FILE = os.path.join(_DIR, "mf_amfi_portfolio_cache.json")
CHART_FILE           = os.path.join(_DIR, "mf_portfolio_charts.png")
HTML_REPORT_FILE     = os.path.join(_DIR, "mf_portfolio_report.html")
PORTFOLIO_DB_FILE    = os.path.join(_DIR, "mf_portfolio_db.json")


def _save_output_file(write_fn, primary_path: str, auto_open: bool = False) -> str:
    """
    Write any output file reliably on Windows (atomic temp-then-replace).
    auto_open=True only for the HTML report — nothing else opens automatically.
    Returns the path actually written.
    """
    import tempfile, time as _time
    primary_dir = os.path.dirname(primary_path) or _DIR

    # Write to temp sibling first — never corrupts the target mid-write
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=primary_dir, suffix=".tmp")
        with os.fdopen(tmp_fd, "wb") as fh:
            write_fn(fh)
    except PermissionError:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=tempfile.gettempdir(), suffix=".tmp")
        with os.fdopen(tmp_fd, "wb") as fh:
            write_fn(fh)
        primary_dir  = tempfile.gettempdir()
        primary_path = os.path.join(primary_dir, os.path.basename(primary_path))

    # Evict old file to release viewer/browser lock
    if os.path.exists(primary_path):
        for _ in range(3):
            try:
                os.unlink(primary_path)
                break
            except PermissionError:
                _time.sleep(0.4)

    # Atomic rename
    try:
        os.replace(tmp_path, primary_path)
        final_path = primary_path
    except PermissionError:
        ts   = datetime.now().strftime("%H%M%S")
        ext  = os.path.splitext(primary_path)[1]
        base = os.path.splitext(os.path.basename(primary_path))[0]
        final_path = os.path.join(tempfile.gettempdir(), f"{base}_{ts}{ext}")
        os.replace(tmp_path, final_path)

    if auto_open:
        try:
            os.startfile(final_path)
        except Exception:
            pass

    return final_path


CACHE_MAX_AGE_HRS       = 24
AMFI_CACHE_MAX_AGE_HRS  = 24
BENCH_CACHE_MAX_AGE_DAYS = 7
AUM_CACHE_MAX_AGE_HRS   = 24
ENRICH_CACHE_MAX_AGE_HRS = 24

MAX_WORKERS       = 10
SLEEP_BETWEEN     = 0.1
RISK_FREE_MONTHLY = 0.5        # ≈ 6% p.a.

BENCHMARK         = {"1Y": 9.0, "3Y": 14.0, "5Y": 16.0}   # fallback
SURVIVORSHIP_HAIRCUT_EXTRA = 0.005   # 0.5% — mfapi only lists active funds

# ── benchmark tickers ────────────────────────────────────────────────
# Confirmed working 10Y tickers in yfinance (tested Apr 2026)
MIDCAP_TICKERS   = ["^NSEMDCP50", "^CNXMIDCAP", "^NSEI"]
SMALLCAP_TICKERS = ["^NSEI"]   # no reliable 10Y smallcap via yfinance

CATEGORY_BENCHMARK_TICKERS = {
    "large cap":   ["^NSEI"],
    "large & mid": ["^NSEI"],
    "mid cap":     MIDCAP_TICKERS,
    "small cap":   SMALLCAP_TICKERS,
    "flexi cap":   ["^NSEI"],
    "multi cap":   ["^NSEI"],
    "elss":        ["^NSEI"],
    "value":       ["^NSEI"],
    "contra":      ["^NSEI"],
    "focused":     ["^NSEI"],
    "debt":        ["^NSEI"],
    "other":       ["^NSEI"],
}

# ── AUM capacity ─────────────────────────────────────────────────────
AUM_SMALLCAP_WARN_CR = 8_000
AUM_MIDCAP_WARN_CR   = 20_000

# ── passive baseline ─────────────────────────────────────────────────
PASSIVE_TER_EQUITY   = 0.10   # % p.a. — Nifty 50 index fund
PASSIVE_TER_DEBT     = 0.10   # % p.a. — overnight/liquid
PASSIVE_EQUITY_SPLIT = 0.75   # 75% equity index, 25% debt
PASSIVE_DEBT_RETURN  = 7.0    # % p.a. assumed for debt leg

# ── tax (FY 2025-26) ─────────────────────────────────────────────────
LTCG_RATE_EQUITY   = 0.125     # 12.5%
LTCG_EXEMPTION_PA  = 125_000   # ₹1.25 L per year
STCG_RATE_EQUITY   = 0.20      # 20% (held < 12M — noted, not applied to SIP)

# ── portfolio construction ───────────────────────────────────────────
CORRELATION_THRESHOLD     = 0.72
AMC_MAX_FUNDS             = 2
AMC_CONCENTRATION_PENALTY = 10.0

# ── scoring weights (7 components, each row sums to 100) ────────────
# 7-component scoring grounded in active management theory:
#   info_ratio  — IR = alpha/tracking_error; canonical risk-adjusted active value.
#   capture     — up_capture / down_capture asymmetry (rally participation vs drawdown cushion).
#   consistency — % rolling 12-month windows beating benchmark (reliability, not just avg).
#   cost        — TER directly; the one guarantee about future returns.
#   capacity    — AUM fit for strategy; mid/small caps can't deploy past certain thresholds.
#   valuation   — fund PE vs mfdata.in category average PE (cheaper peers = better entry).
#   ext_rank    — mfdata.in percentile rank (1Y/3Y/5Y) vs full SEBI category universe.
SCORE_WEIGHTS = {
    "aggressive":   {"info_ratio": 29, "capture": 21, "consistency": 16, "cost": 8, "capacity": 9, "valuation": 7, "ext_rank": 10},
    "balanced":     {"info_ratio": 22, "capture": 24, "consistency": 20, "cost": 10, "capacity": 7, "valuation": 7, "ext_rank": 10},
    "conservative": {"info_ratio": 14, "capture": 28, "consistency": 24, "cost": 10, "capacity": 6, "valuation": 8, "ext_rank": 10},
}

# ── Monte Carlo ──────────────────────────────────────────────────────
MC_N_SIMS_STANDARD = 500
MC_N_SIMS_GOAL     = 2_000

# ── defaults ─────────────────────────────────────────────────────────
DEFAULT_TER = {
    "small cap":0.95,"mid cap":0.85,"flexi cap":0.75,"multi cap":0.75,
    "large cap":0.65,"large & mid":0.75,"elss":0.85,"value":0.80,
    "focused":0.75,"contra":0.85,"debt":0.40,"other":0.80,
}

SEBI_TO_INTERNAL = {
    "Equity Scheme - Small Cap Fund":            "small cap",
    "Equity Scheme - Mid Cap Fund":              "mid cap",
    "Equity Scheme - Large Cap Fund":            "large cap",
    "Equity Scheme - Large & Mid Cap Fund":      "large & mid",
    "Equity Scheme - Flexi Cap Fund":            "flexi cap",
    "Equity Scheme - Multi Cap Fund":            "multi cap",
    "Equity Scheme - ELSS":                      "elss",
    "Equity Scheme - Value Fund":                "value",
    "Equity Scheme - Contra Fund":               "contra",
    "Equity Scheme - Focused Fund":              "focused",
    "Equity Scheme - Dividend Yield Fund":       "other",
    "Equity Scheme - Sectoral/ Thematic Funds":  "other",
    "Debt Scheme - Short Duration Fund":         "debt",
    "Debt Scheme - Low Duration Fund":           "debt",
    "Debt Scheme - Ultra Short Duration Fund":   "debt",
    "Debt Scheme - Money Market Fund":           "debt",
    "Debt Scheme - Banking and PSU Fund":        "debt",
    "Debt Scheme - Corporate Bond Fund":         "debt",
    "Debt Scheme - Credit Risk Fund":            "debt",
    "Debt Scheme - Liquid Fund":                 "debt",
    "Debt Scheme - Overnight Fund":              "debt",
}

REQUIRED_KEYWORDS  = ["direct", "growth"]
EQUITY_CATEGORY_KW = [
    "flexi cap","multi cap","large cap","mid cap",
    "small cap","large & mid","large and mid",
    "focused","value","contra","elss",
]
DEBT_CATEGORY_KW = [
    "short duration","short term","low duration","ultra short",
    "money market","banking and psu","corporate bond",
    "credit risk","liquid","overnight",
]
EXCLUDE_KEYWORDS = [
    "etf","index","fof","fund of fund","hybrid","balanced",
    "arbitrage","dynamic asset","gilt","fixed maturity",
    "interval","floater","long duration","medium duration",
]


# ══════════════════════════════════════════════════════════════════════
# RICH UI HELPERS
# ══════════════════════════════════════════════════════════════════════

def fmt_inr(val: float) -> str:
    if val >= 1_00_00_000: return f"₹{val/1_00_00_000:.2f} Cr"
    if val >= 1_00_000:    return f"₹{val/1_00_000:.2f} L"
    return f"₹{val:,.0f}"

def rich_header(step: str, title: str, hint: str = "") -> None:
    body = f"[bold white]{title}[/bold white]"
    if hint:
        body += f"\n[dim]{hint}[/dim]"
    console.print(Panel(body, title=f"[cyan]{step}[/cyan]",
                        border_style="cyan", padding=(0, 2)))

def rich_ask_choice(prompt: str, options: list, hint: str = "", default: int = 0) -> int:
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("n",  style="bold cyan", width=3)
    t.add_column("opt", style="white")
    for i, o in enumerate(options, 1):
        marker = " [dim](default)[/dim]" if (i - 1) == default else ""
        t.add_row(str(i), f"{o}{marker}")
    console.print(t)
    while True:
        raw = Prompt.ask(f"  [green]{prompt}[/green]", default=str(default + 1))
        if raw.strip().isdigit():
            idx = int(raw.strip())
            if 1 <= idx <= len(options):
                return idx - 1
        console.print("  [red]Please enter a valid number.[/red]")

def rich_ask_int(prompt: str, min_val: int, max_val: int, hint: str = "") -> int:
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    while True:
        raw = Prompt.ask(f"  [green]{prompt}[/green]")
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            v = int(digits)
            if min_val <= v <= max_val:
                return v
        console.print(f"  [red]Enter a value between {min_val:,} and {max_val:,}.[/red]")

def rich_ask_corpus(hint: str = "") -> float | None:
    """Parse corpus target: '50L', '1Cr', '25,00,000', or blank/skip."""
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    raw = Prompt.ask("  [green]Target corpus (or press Enter to skip)[/green]",
                     default="").strip().lower()
    if not raw or raw in ("skip", "s", "none", "n"):
        return None
    raw_clean = raw.replace(",", "").replace("₹", "").strip()
    try:
        if raw_clean.endswith("cr"):
            return float(raw_clean[:-2]) * 1e7
        if raw_clean.endswith("l"):
            return float(raw_clean[:-1]) * 1e5
        return float(raw_clean)
    except ValueError:
        console.print("  [red]Could not parse — skipping corpus target.[/red]")
        return None

def rich_summary(items: list) -> None:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=22)
    t.add_column("v", style="bold white")
    for k, v in items:
        t.add_row(k, str(v))
    console.print(Panel(t, title="[bold cyan]Summary so far[/bold cyan]",
                        border_style="dim cyan", padding=(0, 1)))


# ══════════════════════════════════════════════════════════════════════
# NAV DATA LOADING
# ══════════════════════════════════════════════════════════════════════

def _cache_busted() -> bool:
    """Returns True if the user ran 'clear cache' but files couldn't be reset."""
    import tempfile as _tf
    return os.path.exists(os.path.join(_tf.gettempdir(), "mf_cache_bust.flag"))


def load_cache() -> dict:
    if _cache_busted() or not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f: data = json.load(f)
        age = (time.time() - data.get("timestamp", 0)) / 3600
        if age > CACHE_MAX_AGE_HRS:
            console.print(f"  [yellow]NAV cache {age:.1f}h old — will re-fetch.[/yellow]")
            return {}
        console.print(f"  [green]NAV cache: {len(data.get('data',[]))} funds, {age:.1f}h old.[/green]")
        return data
    except Exception:
        return {}

def restore_nav_data(cache: dict) -> list:
    out = []
    for item in cache.get("data", []):
        df = pd.DataFrame(item["nav_records"])
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
        df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
        df = df.dropna().sort_values("date").reset_index(drop=True)
        out.append({"scheme": item["scheme"], "nav_df": df})
    return out

def fetch_and_cache() -> list:
    console.print("  [cyan]Fetching scheme list from mfapi.in...[/cyan]")
    try:
        resp = requests.get(BASE_URL, timeout=30)
        resp.raise_for_status()
        all_schemes = resp.json()
    except Exception as e:
        console.print(f"  [red]Failed: {e}[/red]")
        return []

    cat_kw = EQUITY_CATEGORY_KW + DEBT_CATEGORY_KW
    filtered = [s for s in all_schemes
                if all(kw in s.get("schemeName","").lower() for kw in REQUIRED_KEYWORDS)
                and any(kw in s.get("schemeName","").lower() for kw in cat_kw)
                and not any(kw in s.get("schemeName","").lower() for kw in EXCLUDE_KEYWORDS)]

    console.print(f"  [green]Filtered to {len(filtered)} funds. Fetching NAVs...[/green]")
    results, completed = [], 0

    def fetch_one(scheme):
        nonlocal completed
        code = scheme.get("schemeCode")
        try:
            time.sleep(SLEEP_BETWEEN)
            r = requests.get(f"{BASE_URL}/{code}", timeout=15)
            r.raise_for_status()
            data = r.json()
            navs = data.get("data", [])
            if len(navs) < 252: return None
            df = pd.DataFrame(navs)
            df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
            df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
            df = df.dropna().sort_values("date").reset_index(drop=True)
            # Store official scheme_category from meta
            meta = data.get("meta", {})
            scheme["_sebi_cat"] = meta.get("scheme_category", "")
            return {"scheme": scheme, "nav_df": df}
        except Exception:
            return None

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  console=console) as prog:
        task = prog.add_task("Fetching NAVs", total=len(filtered))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_one, s): s for s in filtered}
            for fut in as_completed(futures):
                completed += 1
                res = fut.result()
                if res: results.append(res)
                prog.update(task, advance=1)

    serialisable = [
        {"scheme": item["scheme"],
         "nav_records": item["nav_df"][["date","nav"]].assign(
             date=item["nav_df"]["date"].dt.strftime("%d-%m-%Y")
         ).to_dict(orient="records")}
        for item in results
    ]
    payload = json.dumps({"timestamp": time.time(), "data": serialisable})
    def _write_cache(fh):
        fh.write(payload.encode())
    _save_output_file(_write_cache, CACHE_FILE)
    return results


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

    master: dict = {}
    current_cat, current_amc = "", "Unknown AMC"
    for line in resp.text.splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith(("Open Ended Schemes","Close Ended Schemes","Interval Fund")):
            m = re.search(r'\((.+?)\)', line)
            if m: current_cat = m.group(1).strip()
            continue
        if ";" in line:
            parts = line.split(";")
            if len(parts) >= 4 and parts[0].strip().isdigit():
                code = parts[0].strip()
                isin = parts[1].strip() if parts[1].strip() not in ("-","") else None
                master[code] = {"sebi_category": current_cat,
                                "amc_name": current_amc, "isin": isin}
            continue
        if not line.startswith(("Scheme Code","Open Ended","Close Ended","Interval")):
            current_amc = line

    _p = json.dumps({"timestamp": time.time(), "data": master}).encode()
    _save_output_file(lambda fh: fh.write(_p), AMFI_CACHE_FILE)
    console.print(f"  [green]AMFI master: {len(master):,} schemes cached.[/green]")
    return master


def _fetch_ticker(ticker: str, cache: dict) -> pd.Series | None:
    """Fetch one yfinance ticker (10Y monthly), using in-memory cache dict."""
    if ticker in cache:
        return cache[ticker]
    if not YFINANCE_OK:
        return None
    try:
        raw = yf.download(ticker, period="10y", interval="1mo",
                          progress=False, multi_level_index=False)
        if raw.empty or len(raw) < 24:
            return None
        s = raw["Close"].pct_change().replace([np.inf, -np.inf], np.nan).dropna() * 100
        s.index = pd.to_datetime(s.index).tz_localize(None)
        cache[ticker] = s
        return s
    except Exception:
        return None


def fetch_benchmarks() -> dict:
    """
    Returns {ticker_str: pd.Series of monthly pct returns}.
    Fetches ^NSEI always; ^NSEMDCP50 for midcap.
    Cached to BENCHMARK_CACHE_FILE for BENCH_CACHE_MAX_AGE_DAYS days.
    """
    if not _cache_busted() and os.path.exists(BENCHMARK_CACHE_FILE):
        try:
            with open(BENCHMARK_CACHE_FILE) as f: cached = json.load(f)
            age_days = (time.time() - cached.get("timestamp", 0)) / 86400
            if age_days < BENCH_CACHE_MAX_AGE_DAYS:
                result = {}
                for k, pairs in cached.items():
                    if k == "timestamp": continue
                    result[k] = pd.Series(
                        {pd.Timestamp(d): v for d, v in pairs}, dtype=float)
                n = len(result)
                console.print(f"  [green]Benchmarks: {n} tickers cached ({age_days:.1f}d old).[/green]")
                return result
        except Exception:
            pass

    if not YFINANCE_OK:
        console.print("  [yellow]yfinance not installed — benchmark features disabled.[/yellow]")
        return {}

    console.print("  [cyan]Fetching benchmark indices from yfinance...[/cyan]")
    mem_cache: dict = {}
    tickers_needed = {"^NSEI"} | set(MIDCAP_TICKERS[:2])
    result = {}
    for t in tickers_needed:
        s = _fetch_ticker(t, mem_cache)
        if s is not None:
            result[t] = s

    nifty_ok = "^NSEI" in result
    console.print(f"  [green]Benchmarks fetched: {list(result.keys())}[/green]" if nifty_ok
                  else "  [yellow]Benchmark fetch failed.[/yellow]")

    # Save cache
    serialisable = {"timestamp": time.time()}
    for k, s in result.items():
        serialisable[k] = [[str(d.date()), float(v)] for d, v in s.items()]
    _p = json.dumps(serialisable).encode()
    _save_output_file(lambda fh: fh.write(_p), BENCHMARK_CACHE_FILE)
    return result


def get_category_benchmark(category: str, benchmarks: dict) -> tuple:
    """Return (ticker_label, pd.Series) for a fund's category."""
    preferred = CATEGORY_BENCHMARK_TICKERS.get(category, ["^NSEI"])
    for t in preferred:
        if t in benchmarks:
            return t, benchmarks[t]
    # fallback to ^NSEI
    s = benchmarks.get("^NSEI", pd.Series(dtype=float))
    return "^NSEI(fb)", s


def get_market_context(benchmarks: dict) -> str:
    nifty = benchmarks.get("^NSEI")
    if nifty is None or len(nifty) < 12: return ""
    m12 = float((1 + nifty.tail(12) / 100).prod() - 1) * 100
    bull = float((nifty.tail(24) > 0).mean()) * 100
    if m12 > 22:
        return f"BULL (+{m12:.1f}% 12M, {bull:.0f}% up-months): valuations elevated — mean-reversion risk."
    if m12 < -10:
        return f"BEAR ({m12:.1f}% 12M, {bull:.0f}% up-months): potential entry, near-term volatility."
    return f"NEUTRAL ({m12:+.1f}% 12M, {bull:.0f}% up-months): moderate conditions."


def _safe_float(v) -> float | None:
    if v in (None, "", "N/A", "NA", "-"):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


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
    for code in detail_codes:
        if code in seen:
            continue
        seen.add(code)
        try:
            r = requests.get(f"{MFDATA_BASE_URL}/schemes/{code}",
                             timeout=12, headers=headers)
            if r.status_code in (404, 429):
                continue
            r.raise_for_status()
            payload = r.json()
            row = payload.get("data", payload) if isinstance(payload, dict) else {}
            if row:
                data.setdefault(str(code), {})
                _merge_enrichment(data[str(code)], row)
        except Exception:
            continue
        time.sleep(0.08)

    if not data:
        console.print("  [yellow]No enrichment data available; using NAV-only ranking.[/yellow]")

    try:
        _p = json.dumps({"timestamp": time.time(), "data": data}).encode()
        _save_output_file(lambda fh: fh.write(_p), ENRICH_CACHE_FILE)
    except Exception:
        pass
    return data


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
    for code in missing:
        if rate_limited:
            break
        for endpoint in (
            f"{MFDATA_BASE_URL}/schemes/{code}/portfolio",
            f"{MFDATA_BASE_URL}/schemes/{code}/holdings",
        ):
            try:
                r = requests.get(endpoint, timeout=8, headers=headers)
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


# ══════════════════════════════════════════════════════════════════════
# METRICS COMPUTATION
# ══════════════════════════════════════════════════════════════════════

def cagr(start: float, end: float, years: float) -> float:
    if start <= 0 or years <= 0: return 0.0
    return ((end / start) ** (1 / years) - 1) * 100

def nav_on_or_before(df: pd.DataFrame, target) -> float | None:
    sub = df[df["date"] <= target]
    return float(sub["nav"].iloc[-1]) if not sub.empty else None

def compute_metrics(df: pd.DataFrame) -> dict:
    latest  = df["date"].max()
    end_nav = float(df["nav"].iloc[-1])
    out: dict = {
        "latest_nav":     round(end_nav, 2),
        "latest_date":    latest.strftime("%d %b %Y"),
        "fund_age_years": round((latest - df["date"].min()).days / 365.25, 1),
    }
    for years, label in [(1,"1Y"),(3,"3Y"),(5,"5Y"),(10,"10Y")]:
        past = latest - timedelta(days=int(years * 365.25))
        sv = nav_on_or_before(df, past)
        out[f"return_{label.lower()}"] = round(cagr(sv, end_nav, years), 2) if sv else None

    # 3Y CAGR at T-1Y and T-2Y (for rank consistency)
    for off, key in [(1,"return_3y_tm1"),(2,"return_3y_tm2")]:
        ref_end   = latest - timedelta(days=int(off * 365.25))
        ref_start = ref_end - timedelta(days=int(3 * 365.25))
        sv = nav_on_or_before(df, ref_start)
        ev = nav_on_or_before(df, ref_end)
        out[key] = round(cagr(sv, ev, 3), 2) if (sv and ev) else None

    monthly = (df.set_index("date")["nav"].resample("ME").last()
               .pct_change().replace([np.inf, -np.inf], np.nan).dropna() * 100)
    out["_monthly_returns"] = monthly
    out["volatility"]  = round(float(monthly.std() * np.sqrt(12)), 2) if not monthly.empty else None
    out["consistency"] = round(float((monthly > 0).mean()) * 100, 1)  if not monthly.empty else None

    if len(monthly) >= 12:
        excess = monthly - RISK_FREE_MONTHLY
        std = float(monthly.std())
        out["sharpe"]  = round(float(excess.mean() / std) * np.sqrt(12), 2) if std > 0 else None
        dn  = np.minimum(excess.values, 0)
        dv  = float(np.sqrt(np.mean(dn ** 2)))
        out["sortino"] = round(float(excess.mean() / dv) * np.sqrt(12), 2) if dv > 0 else None
    else:
        out["sharpe"] = out["sortino"] = None

    nav_s = df.set_index("date")["nav"].astype(float)
    rmax  = nav_s.cummax()
    out["max_drawdown"] = round(float(((nav_s - rmax) / rmax * 100).min()), 2) if not nav_s.empty else None

    if len(monthly) >= 24:
        r12 = (1 + monthly / 100).rolling(12).apply(np.prod, raw=True) - 1
        r12 = r12.dropna() * 100
        out["rolling_consistency"] = round(float(r12.std()), 2) if not r12.empty else None
    else:
        out["rolling_consistency"] = None
    return out


def compute_alpha_beta(fund_m: pd.Series, bench_m: pd.Series) -> dict:
    empty = {k: None for k in ("alpha","beta","r_squared","tracking_error",
                                "info_ratio","bear_alpha","bull_participation",
                                "up_capture","down_capture")}
    if fund_m is None or bench_m is None: return empty
    try:
        # Normalize both series to monthly Period index so that
        # month-end fund dates (Jun 30) align with month-start yfinance
        # dates (Jun 1) — without this inner-join yields 0 rows.
        f_norm = fund_m.copy()
        b_norm = bench_m.copy()
        f_norm.index = f_norm.index.to_period("M")
        b_norm.index = b_norm.index.to_period("M")
        aln = pd.concat([f_norm, b_norm], axis=1, join="inner").dropna()
    except Exception:
        return empty
    aln.columns = ["fund","bench"]
    aln = aln.replace([np.inf, -np.inf], np.nan).dropna()
    if len(aln) < 24: return empty

    f, b = aln["fund"].values, aln["bench"].values
    X = np.column_stack([b, np.ones(len(b))])
    coeffs, *_ = np.linalg.lstsq(X, f, rcond=None)
    beta, alpha_m = float(coeffs[0]), float(coeffs[1])

    y_hat  = X @ coeffs
    ss_res = float(np.sum((f - y_hat)**2))
    ss_tot = float(np.sum((f - f.mean())**2))
    r_sq   = round(1 - ss_res/ss_tot, 3) if ss_tot > 0 else 0.0

    exc = f - b
    te  = round(float(np.std(exc, ddof=1) * np.sqrt(12)), 2)
    ir  = round(float(np.mean(exc) / np.std(exc, ddof=1)) * np.sqrt(12), 2) \
          if np.std(exc, ddof=1) > 0 else None

    up_m = aln["bench"] > 0; dn_m = aln["bench"] < 0
    bull  = round(float(aln.loc[up_m,"fund"].mean()), 2) if up_m.any() else None
    bear  = round(float((aln.loc[dn_m,"fund"] - aln.loc[dn_m,"bench"]).mean()), 2) \
            if dn_m.sum() >= 3 else None
    f_up  = float(aln.loc[up_m,"fund"].mean())  if up_m.any() else None
    b_up  = float(aln.loc[up_m,"bench"].mean()) if up_m.any() else None
    f_dn  = float(aln.loc[dn_m,"fund"].mean())  if dn_m.any() else None
    b_dn  = float(aln.loc[dn_m,"bench"].mean()) if dn_m.any() else None
    uc = round(f_up/b_up*100, 1) if (f_up is not None and b_up) else None
    dc = round(f_dn/b_dn*100, 1) if (f_dn is not None and b_dn) else None

    # Capture ratio: up_capture / down_capture — >1 means fund rises more than it falls
    cap_ratio = round(uc / dc, 3) if (uc and dc and dc != 0) else None

    return {"alpha":round(alpha_m*12,2),"beta":round(beta,2),"r_squared":r_sq,
            "tracking_error":te,"info_ratio":ir,"bear_alpha":bear,
            "capture_ratio":cap_ratio,
            "bull_participation":bull,"up_capture":uc,"down_capture":dc}


def compute_cohort_stats(scored: list) -> dict:
    """Per-category medians used by score_fund and display. Only the stats
    actually consumed downstream are computed."""
    cohort: dict = {}
    by_cat: dict = {}
    for f in scored: by_cat.setdefault(f["category"], []).append(f)
    for cat, funds in by_cat.items():
        def col(k):
            v = [f[k] for f in funds if f.get(k) is not None]
            return np.array(v, dtype=float) if v else np.array([], dtype=float)
        def med(a): return float(np.median(a)) if len(a) else None
        ir_arr  = col("info_ratio")
        cap_arr = col("capture_ratio")
        ter_arr = col("ter")
        aum_arr = col("aum_cr")
        r3_arr  = col("return_3y")
        r10_arr = col("return_10y")
        vol_arr = col("volatility")
        dd_arr  = col("max_drawdown")
        cohort[cat] = {
            "median_ir":    med(ir_arr),
            "median_cap":   med(cap_arr),
            "median_ter":   med(ter_arr),
            "median_aum":   med(aum_arr),
            "median_r3":    med(r3_arr),
            "median_r10":   med(r10_arr),
            "median_vol":   med(vol_arr),
            "median_dd":    med(dd_arr),
            "n":            len(funds),
        }
    return cohort


def _pct_rank(val: float, arr: np.ndarray) -> float:
    valid = arr[~np.isnan(arr)]
    if len(valid) < 2 or np.isnan(val): return 50.0
    return float(np.sum(valid <= val) / len(valid)) * 100


def compute_rank_consistency(scored: list) -> None:
    by_cat: dict = {}
    for f in scored: by_cat.setdefault(f["category"], []).append(f)
    for _, funds in by_cat.items():
        for win in ("return_3y","return_3y_tm1","return_3y_tm2"):
            vals = np.array([f[win] for f in funds if f.get(win) is not None], dtype=float)
            if len(vals) < 3: continue
            for f in funds:
                v = f.get(win)
                if v is not None:
                    f.setdefault("_pct_ranks", []).append(_pct_rank(float(v), vals))
        for f in funds:
            ranks = f.pop("_pct_ranks", [])
            # Funds with <2 windows (insufficient history) score 0, not neutral:
            # a fund that only existed during a bull run hasn't been tested.
            f["rank_consistency"] = round(float(np.mean(ranks)) - float(np.std(ranks)), 1) \
                                    if len(ranks) >= 2 else 0.0


# ══════════════════════════════════════════════════════════════════════
# TER
# ══════════════════════════════════════════════════════════════════════

def load_ter_overrides() -> dict:
    if not os.path.exists(TER_OVERRIDE_FILE): return {}
    try:
        with open(TER_OVERRIDE_FILE) as f: raw = json.load(f)
        return {str(k): float(v) for k, v in raw.items()}
    except Exception as e:
        console.print(f"  [yellow]TER override unreadable: {e}[/yellow]")
        return {}

def sync_ter_overrides_from_enrichment(enrichment: dict, existing: dict) -> dict:
    """
    Seed mf_ter_overrides.json from live enrichment data.

    Existing entries are treated as user overrides and are not overwritten.
    The file remains a simple {scheme_code: expense_ratio_pct} mapping.
    """
    if not enrichment:
        return existing
    merged = dict(existing)
    added = 0
    for code, row in enrichment.items():
        if str(code) in merged or not isinstance(row, dict):
            continue
        ter = _safe_float(row.get("expense_ratio"))
        if ter is not None and 0 <= ter <= 3:
            merged[str(code)] = round(ter, 3)
            added += 1
    if not added:
        return merged
    try:
        with open(TER_OVERRIDE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
        console.print(f"  [green]TER overrides auto-seeded: +{added} schemes.[/green]")
    except Exception as e:
        console.print(f"  [yellow]Could not update TER overrides: {e}[/yellow]")
    return merged

def get_ter(scheme_code, category: str, overrides: dict,
            amfi_master: dict = None, enrichment: dict = None) -> float:
    code = str(scheme_code)
    if code in overrides: return overrides[code]
    if enrichment:
        exp = _safe_float((enrichment.get(code) or {}).get("expense_ratio"))
        if exp is not None and 0 <= exp <= 3:
            return exp
    # Try ISIN lookup from amfi_master (placeholder - no TER in NAVAll.txt)
    return DEFAULT_TER.get(category, 0.80)


# ══════════════════════════════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════════════════════════════

def compute_external_rank_score(fund: dict) -> float | None:
    """Blend mfdata category ranks and ratings when present."""
    scores: list[float] = []
    used_w = 0.0
    returns = fund.get("mfdata_returns") or fund.get("returns")
    if isinstance(returns, dict):
        for key, wt in (("1y", 0.25), ("3y", 0.45), ("5y", 0.30)):
            item = returns.get(key) or returns.get(key.upper())
            if isinstance(item, dict):
                rank = _safe_float(item.get("rank"))
                total = _safe_float(item.get("total_in_category") or item.get("total"))
            else:
                rank = _safe_float(returns.get(f"rank_{key}"))
                total = _safe_float(returns.get("rank_total"))
            if rank and total and total > 1:
                pct = max(0.0, min(100.0, (1 - (rank - 1) / (total - 1)) * 100))
                scores.append(pct * wt)
                used_w += wt
    if scores:
        return float(sum(scores) / max(0.01, used_w))
    rating = _safe_float(fund.get("rating") or fund.get("morningstar"))
    if rating is not None and 0 < rating <= 5:
        return float(rating / 5 * 100)
    return None


def score_fund(m: dict, optimise: str, cohort: dict, ter_pct: float) -> dict:
    """
    7-component scoring (sums to 100 at full marks):

    info_ratio  — IR = alpha/tracking_error (annualised).  IR ≥ 1.0 = full marks.
                  The canonical measure of risk-adjusted active management value.
    capture     — up_capture / down_capture ratio.  ≥ 1.3 = full marks.
                  A fund that rises 110% and falls only 85% of benchmark has a
                  ratio of 1.29 — exactly the asymmetry investors want.
    consistency — % of rolling 12-month windows beating the benchmark.
                  ≥ 65% = full marks; computed from rank_consistency proxy when
                  rolling-window benchmark data is unavailable.
    cost        — TER directly.  Scored relative to category median TER so that
                  a cheap mid-cap fund isn't unfairly rewarded vs a cheap large-cap.
    capacity    — AUM fit for strategy.  Logarithmically penalises funds where
                  AUM makes it impossible to be nimble (mid/small cap capacity).
    """
    w  = SCORE_WEIGHTS[optimise]
    cs = cohort.get(m.get("category","other"), {})
    bd: dict = {}

    # ── 1. Information Ratio ──────────────────────────────────────────────
    # IR > 1.0 is exceptional; IR > 0.5 is good.  Scale: 0 at IR=0, full at IR≥1.
    ir = m.get("info_ratio")
    if ir is not None:
        bd["info_ratio"] = max(0, min(w["info_ratio"], w["info_ratio"] * ir))
    else:
        # Fallback: use alpha alone when tracking_error not available
        alpha = m.get("alpha") or 0
        bd["info_ratio"] = max(0, min(w["info_ratio"], w["info_ratio"] * (0.5 + alpha / 12)))

    # ── 2. Capture ratio (upside / downside participation) ────────────────
    cap = m.get("capture_ratio")
    if cap is not None:
        # 1.0 = neutral (rises and falls same as benchmark), 1.3 = full marks
        bd["capture"] = max(0, min(w["capture"], w["capture"] * (cap - 0.7) / 0.6))
    else:
        # Fallback: use bear_alpha as proxy for downside protection
        ba = m.get("bear_alpha") or 0
        bd["capture"] = max(0, min(w["capture"], w["capture"] * (0.5 + ba / 4)))

    # ── 3. Consistency (% rolling periods beating benchmark) ─────────────
    rc = m.get("rank_consistency")   # 0–100 scale; 50 = median, 70+ = strong
    if rc is not None:
        # 50 = baseline (half the time beating benchmark), 70 = full marks
        bd["consistency"] = max(0, min(w["consistency"], w["consistency"] * (rc - 30) / 40))
    else:
        bd["consistency"] = w["consistency"] * 0.40   # unknown = below average assumption

    # ── 4. Cost (TER relative to category median) ────────────────────────
    median_ter = cs.get("median_ter") or ter_pct
    if median_ter and median_ter > 0:
        # 0 TER = full score; at median = 50%; above median → penalty
        ter_score = 1.0 - (ter_pct / (median_ter * 2))
    else:
        ter_score = max(0, (1.5 - ter_pct) / 1.2)
    bd["cost"] = max(0, min(w["cost"], w["cost"] * ter_score))

    # ── 5. Capacity (AUM fit for category) ───────────────────────────────
    aum = m.get("aum_cr") or 0
    cat = m.get("category", "other")
    median_aum = cs.get("median_aum") or 5000
    if aum > 0 and median_aum > 0:
        import math as _m
        # Log ratio: at 3× median AUM → score ≈ 50%; at median → ~85%; at 0.3× → full
        log_ratio = _m.log(aum / median_aum + 1e-9)
        cap_score = max(0, 1.0 - max(0, log_ratio) / 3.0)   # penalty starts above median
        # Extra hard cap for small/mid categories where capacity genuinely constrains
        if cat == "small cap" and aum > AUM_SMALLCAP_WARN_CR:
            cap_score = min(cap_score, 0.40)
        elif cat == "mid cap" and aum > AUM_MIDCAP_WARN_CR:
            cap_score = min(cap_score, 0.55)
    else:
        cap_score = 0.70   # unknown AUM — mild assumption
    bd["capacity"] = max(0, min(w["capacity"], w["capacity"] * cap_score))

    # ── 6. Valuation (fund PE vs mfdata.in category average PE) ─────────
    # Rewards equity funds trading cheaper than their SEBI category peers.
    # Debt funds and funds with no PE data get a neutral 50% score.
    cat_pe  = m.get("category_pe")
    fund_pe = m.get("pe_ratio")
    if fund_pe and cat_pe and cat_pe > 0 and cat not in ("debt", "other"):
        rel_pe = fund_pe / cat_pe
        # rel_pe ≤ 0.90 (≥10% cheaper than category) → full marks
        # rel_pe ≥ 1.25 (≥25% more expensive)        → 0 marks
        val_score = max(0.0, 1.0 - max(0.0, rel_pe - 0.90) / 0.35)
        bd["valuation"] = max(0, min(w["valuation"], w["valuation"] * val_score))
    else:
        bd["valuation"] = w["valuation"] * 0.5   # neutral for debt / missing data

    # ── 7. External rank (mfdata.in percentile in full SEBI category universe) ──
    # Uses mfdata.in 1Y/3Y/5Y rank-vs-total, weighted 25/45/30.
    # Broader than the internal cohort (265+ funds vs NAV-history survivors only).
    # Falls back to Morningstar rating when rank data absent.
    ext = compute_external_rank_score(m)
    if ext is not None:
        bd["ext_rank"] = max(0, min(w["ext_rank"], w["ext_rank"] * ext / 100))
    else:
        bd["ext_rank"] = w["ext_rank"] * 0.5   # neutral when mfdata unavailable

    raw_total = sum(bd.values())

    # Fund-age multiplier — a fund that hasn't seen a full bull+bear cycle
    # cannot be ranked reliably.  <3Y gets a heavy penalty.
    age = m.get("fund_age_years") or 0
    if age < 3:   age_mult = 0.55
    elif age < 5: age_mult = 0.70 + 0.075 * (age - 3)
    else:         age_mult = 1.0

    total = raw_total * age_mult
    return {"score": round(total, 1), "breakdown": {k: round(v, 2) for k, v in bd.items()}}


# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO CONSTRUCTION
# ══════════════════════════════════════════════════════════════════════

CATEGORY_SLOTS = {
    "aggressive":   [("small cap",1),("mid cap",1),("flexi cap|multi cap|focused",1),
                     ("large cap|large & mid",1),("flexi cap|multi cap|value|elss",1)],
    "balanced":     [("flexi cap|multi cap",1),("large cap|large & mid",1),
                     ("mid cap",1),("small cap",1),
                     ("short duration|low duration|ultra short|money market|corporate bond",1)],
    "conservative": [("large cap|large & mid",1),("flexi cap|multi cap",1),
                     ("short duration|corporate bond",1),
                     ("low duration|ultra short|money market|banking and psu",1),
                     ("mid cap|value",1)],
}
ALLOCATION_WEIGHTS = {
    "aggressive":   [30,25,20,15,10],
    "balanced":     [25,20,20,15,20],
    "conservative": [20,20,25,25,10],
}

def _norm_text(s: str) -> str:
    return (s or "").lower().replace("&", "and")

def slot_matches_fund(fund: dict, keywords: list[str]) -> bool:
    cat  = _norm_text(fund.get("category", ""))
    name = _norm_text(fund.get("name", ""))
    for kw in keywords:
        k = _norm_text(kw.strip())
        if not k:
            continue
        if k == cat or k in cat:
            return True
        if cat == "debt" and k in _norm_text("short duration low duration ultra short money market corporate bond banking and psu credit risk liquid overnight"):
            return True
        if k in name:
            return True
    return False

def mark_aum_capacity(fund: dict) -> None:
    aum = fund.get("aum_cr")
    if aum is None:
        return
    cat = fund.get("category")
    if cat == "small cap" and aum > AUM_SMALLCAP_WARN_CR:
        fund["aum_warn"] = True
    elif cat == "mid cap" and aum > AUM_MIDCAP_WARN_CR:
        fund["aum_warn"] = True

def allocate_sip(portfolio: list, monthly_sip: int) -> None:
    """Allocate in Rs 500 units without creating negative fund allocations."""
    if not portfolio:
        return
    units = max(1, monthly_sip // 500)
    if len(portfolio) > units:
        del portfolio[units:]

    raw_weights = [float(f.get("slot_weight", 0)) for f in portfolio]
    total_weight = sum(raw_weights) or len(portfolio)
    target_units = [w / total_weight * units for w in raw_weights]
    alloc_units = [max(1, int(math.floor(u))) for u in target_units]

    while sum(alloc_units) > units:
        idx = max(range(len(alloc_units)),
                  key=lambda i: (alloc_units[i], -target_units[i]))
        alloc_units[idx] -= 1
    remainders = [target_units[i] - math.floor(target_units[i]) for i in range(len(portfolio))]
    while sum(alloc_units) < units:
        idx = max(range(len(alloc_units)), key=lambda i: remainders[i])
        alloc_units[idx] += 1
        remainders[idx] = -1

    for f, u in zip(portfolio, alloc_units):
        f["monthly_alloc"] = u * 500
        f["weight_pct"] = round(f["monthly_alloc"] / monthly_sip * 100, 1)

def classify_fund(name: str, scheme_code=None, amfi_master: dict = None) -> str:
    if amfi_master and scheme_code is not None:
        entry = amfi_master.get(str(scheme_code), {})
        sebi  = entry.get("sebi_category") or ""
        cat   = SEBI_TO_INTERNAL.get(sebi)
        if cat: return cat
        # Also try the _sebi_cat stored from mfapi.in meta
    name = name.lower()
    for kw in ["small cap"]:
        if kw in name: return "small cap"
    for kw in ["mid cap"]:
        if kw in name: return "mid cap"
    for kw in ["large & mid","large and mid"]:
        if kw in name: return "large & mid"
    for kw in ["large cap"]:
        if kw in name: return "large cap"
    if "flexi cap" in name: return "flexi cap"
    if "multi cap" in name: return "multi cap"
    if "elss"      in name: return "elss"
    for kw in ["value","contra","focused"]:
        if kw in name: return kw
    for kw in ["short duration","short term","low duration","ultra short","money market",
               "corporate bond","banking and psu","credit risk","liquid","overnight"]:
        if kw in name: return "debt"
    return "other"

def get_amc(scheme_code, amfi_master: dict) -> str:
    return amfi_master.get(str(scheme_code), {}).get("amc_name", "Unknown AMC")

def pairwise_correlation(a: pd.Series, b: pd.Series) -> float:
    if a is None or b is None: return 0.0
    try:
        aln = pd.concat([a, b], axis=1, join="inner").replace([np.inf, -np.inf], np.nan).dropna()
    except Exception:
        return 0.0
    if len(aln) < 6: return 0.0
    c = aln.corr().iloc[0, 1]
    return float(c) if pd.notna(c) else 0.0  # type: ignore[arg-type]

def sip_corpus(monthly: float, months: float, annual_rate: float) -> float:
    r = annual_rate / 12
    if r == 0: return monthly * months
    return monthly * ((pow(1 + r, months) - 1) / r) * (1 + r)

def monte_carlo_projection(
    monthly_returns: pd.Series, alloc: float, months: int,
    n_sims: int = MC_N_SIMS_STANDARD,
) -> dict:
    if monthly_returns is None or len(monthly_returns) < 12: return {}
    rng   = np.random.default_rng(42)
    ret   = monthly_returns.replace([np.inf, -np.inf], np.nan).dropna().values / 100.0
    ret   = ret[ret > -1.0]
    if len(ret) < 12: return {}
    draws = rng.choice(ret, size=(n_sims, months), replace=True)
    gf    = np.fliplr(np.cumprod(1 + np.fliplr(draws), axis=1))
    fc    = alloc * np.sum(gf, axis=1)
    q     = np.percentile(fc, [10, 25, 50, 75, 90])
    return {"p10":float(q[0]),"p25":float(q[1]),"p50":float(q[2]),
            "p75":float(q[3]),"p90":float(q[4])}

def portfolio_risk_metrics(portfolio: list) -> dict:
    """Estimate portfolio-level risk from weighted historical monthly returns."""
    series = []
    weights = []
    for f in portfolio:
        s = f.get("_monthly_returns")
        w = float(f.get("weight_pct", 0)) / 100.0
        if s is not None and not s.empty and w > 0:
            cleaned = s.replace([np.inf, -np.inf], np.nan).dropna().astype(float)
            if not cleaned.empty:
                series.append(cleaned)
                weights.append(w)
    if not series:
        return {}

    aligned = pd.concat(series, axis=1, join="inner").replace([np.inf, -np.inf], np.nan).dropna()
    if aligned.empty:
        return {}

    w = np.array(weights, dtype=float)
    w = w / w.sum()
    monthly = pd.Series(aligned.values @ w, index=aligned.index)
    volatility = float(monthly.std() * np.sqrt(12)) if len(monthly) > 1 else None
    excess = monthly - RISK_FREE_MONTHLY
    std = float(monthly.std()) if len(monthly) > 1 else 0.0
    sharpe = float(excess.mean() / std * np.sqrt(12)) if std > 0 else None
    downside = np.minimum(excess.values, 0)
    downside_dev = float(np.sqrt(np.mean(downside ** 2))) if len(downside) else 0.0
    sortino = float(excess.mean() / downside_dev * np.sqrt(12)) if downside_dev > 0 else None
    wealth = (1 + monthly / 100.0).cumprod()
    drawdown = ((wealth - wealth.cummax()) / wealth.cummax() * 100.0).min()

    return {
        "months": int(len(monthly)),
        "volatility": round(volatility, 2) if volatility is not None else None,
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown": round(float(drawdown), 2) if pd.notna(drawdown) else None,
    }

def estimate_rate(fund: dict, optimise: str, cohort: dict) -> float:
    cat       = fund.get("category","other")
    c_r3      = cohort.get(cat,{}).get("median_r3") or BENCHMARK["3Y"]
    c_r5      = cohort.get(cat,{}).get("median_r5") or BENCHMARK["5Y"]
    r3        = min(fund.get("return_3y") or c_r3, 20.0)
    r5        = min(fund.get("return_5y") or c_r5, 20.0)
    blended   = r5 * 0.60 + r3 * 0.40
    haircut   = {"aggressive":0.85,"balanced":0.75,"conservative":0.65}[optimise]
    # Apply survivorship-bias haircut
    return round(blended * haircut / 100 - SURVIVORSHIP_HAIRCUT_EXTRA, 4)

def build_portfolio(
    nav_data: list, optimise: str, include_debt: bool, num_funds: int,
    monthly_sip: int, timeline_years: float,
    amfi_master: dict = None, benchmarks: dict = None, aum_data: dict = None,
) -> tuple:
    """8-pass pipeline. Returns (portfolio, cohort)."""
    amfi_master = amfi_master or {}
    benchmarks  = benchmarks  or {}
    enrichment  = aum_data    or {}
    overrides   = sync_ter_overrides_from_enrichment(enrichment, load_ter_overrides())
    months      = int(timeline_years * 12)

    # PASS 1: metrics + classify
    scored = []
    for item in nav_data:
        name  = item["scheme"].get("schemeName","")
        code  = item["scheme"].get("schemeCode")
        # Use _sebi_cat from mfapi.in meta if available
        sebi_override = item["scheme"].get("_sebi_cat","")
        cat   = SEBI_TO_INTERNAL.get(sebi_override) or classify_fund(name, code, amfi_master)
        if not include_debt and cat == "debt": continue
        m   = compute_metrics(item["nav_df"])
        amc = get_amc(code, amfi_master)
        ext = enrichment.get(str(code), {})
        if not isinstance(ext, dict):
            ext = {"aum_cr": ext}
        aum_cr = _safe_float(ext.get("aum_cr"))
        mfdata_ratios = ext.get("ratios") or {}
        pe_ratio    = _safe_float((mfdata_ratios.get("valuation") or {}).get("pe_ratio"))
        category_pe = _safe_float((mfdata_ratios.get("category_averages") or {}).get("pe"))
        fund_manager = (ext.get("fund_manager") or ext.get("managers")
                        or ext.get("manager") or ext.get("fund_managers"))
        row = {"name":name,"scheme_code":code,"category":cat,
               "amc_name":amc,"aum_cr":aum_cr,
               "expense_ratio":_safe_float(ext.get("expense_ratio")),
               "rating":_safe_float(ext.get("rating") or ext.get("morningstar")),
               "mfdata_returns":ext.get("returns"),
               "mfdata_ratios":mfdata_ratios,
               "family_id":ext.get("family_id"),
               "pe_ratio":pe_ratio,
               "category_pe":category_pe,
               "fund_manager":fund_manager,
               **m}
        scored.append(row)

    # DEDUP: mfapi.in occasionally lists the same fund twice (different plan codes
    # but identical NAV series). Keep only the highest-returning copy per name stem.
    seen_names: dict = {}
    deduped = []
    for f in scored:
        stem = re.sub(r'\s*(growth|option|plan|direct|gr)\s*', ' ', f["name"].lower()).strip()
        prev = seen_names.get(stem)
        if prev is None:
            seen_names[stem] = len(deduped)
            deduped.append(f)
        else:
            # Keep whichever copy has a better 3Y return
            prev_r3 = deduped[prev].get("return_3y") or -999
            this_r3 = f.get("return_3y") or -999
            if this_r3 > prev_r3:
                deduped[prev] = f
    scored = deduped

    # PASS 2: category-specific alpha/beta
    cat_bench_used: dict = {}
    n_alpha = 0
    for f in scored:
        cat = f["category"]
        if cat not in cat_bench_used:
            ticker, series = get_category_benchmark(cat, benchmarks)
            cat_bench_used[cat] = (ticker, series)
        ticker, bench_s = cat_bench_used[cat]
        if not bench_s.empty:
            ab = compute_alpha_beta(f.get("_monthly_returns"), bench_s)
            f.update(ab)
            f["category_benchmark"] = ticker
            if ab.get("alpha") is not None: n_alpha += 1
        else:
            f["category_benchmark"] = "N/A"

    if n_alpha:
        console.print(f"  [green]Alpha/beta computed for {n_alpha}/{len(scored)} funds.[/green]")

    # PASS 3: cohort stats
    cohort = compute_cohort_stats(scored)

    # PASS 4: rank consistency
    compute_rank_consistency(scored)

    # PASS 5: score + AMC soft penalty
    for f in scored:
        ter = get_ter(f["scheme_code"], f["category"], overrides, amfi_master, enrichment)
        f["ter"] = ter
        res = score_fund(f, optimise, cohort, ter)
        f["score"] = res["score"]; f["score_breakdown"] = res["breakdown"]
        mark_aum_capacity(f)

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Record per-category rank so each fund knows where it sits in its universe
    _cat_rank: dict = {}
    for f in scored:
        cat = f["category"]
        _cat_rank[cat] = _cat_rank.get(cat, 0) + 1
        f["category_rank"] = _cat_rank[cat]
    _cat_sizes = {cat: count for cat, count in _cat_rank.items()}
    for f in scored:
        f["category_size"] = _cat_sizes[f["category"]]
    assert num_funds <= len(CATEGORY_SLOTS[optimise])

    slots   = CATEGORY_SLOTS[optimise][:num_funds]
    weights = ALLOCATION_WEIGHTS[optimise][:num_funds]

    # PASS 6: slot selection (correlation-aware)
    portfolio: list = []
    used: set       = set()
    for (pat, _), w in zip(slots, weights):
        kws = [k.strip() for k in pat.split("|")]
        candidates = []
        for f in scored:
            if f["scheme_code"] in used: continue
            if not slot_matches_fund(f, kws): continue
            max_c = max((pairwise_correlation(f.get("_monthly_returns"),
                                               p.get("_monthly_returns")) for p in portfolio),
                        default=0.0)
            penalty = max(0.0, (max_c - CORRELATION_THRESHOLD) * 50)
            amc = f.get("amc_name", "Unknown AMC")
            amc_count = sum(1 for p in portfolio if p.get("amc_name") == amc)
            if amc != "Unknown AMC" and amc_count >= AMC_MAX_FUNDS:
                penalty += AMC_CONCENTRATION_PENALTY
            candidates.append((f["score"] - penalty, max_c, f))
        if not candidates: continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, max_c, chosen = candidates[0]
        chosen["max_corr_with_prior"] = round(max_c, 2)
        chosen["slot_weight"]         = w
        portfolio.append(chosen)
        used.add(chosen["scheme_code"])

    # PASS 7: allocations
    allocate_sip(portfolio, monthly_sip)
    for f in portfolio:
        alloc = f["monthly_alloc"]
        rate  = estimate_rate(f, optimise, cohort)
        f["estimated_rate"]   = rate
        f["projected_corpus"] = sip_corpus(alloc, months, rate)
        f["mc_projection"]    = monte_carlo_projection(
            f.get("_monthly_returns"), alloc, months)

    # PASS 8: attach full per-category leaderboard so report can show runners-up
    _cat_leaders: dict = {}
    for f in scored:
        cat = f["category"]
        _cat_leaders.setdefault(cat, []).append({
            "name":        f["name"],
            "scheme_code": f["scheme_code"],
            "score":       round(f["score"], 1),
            "return_3y":   f.get("return_3y"),
            "return_10y":  f.get("return_10y"),
            "alpha":       f.get("alpha"),
            "aum_cr":      f.get("aum_cr"),
            "aum_warn":    f.get("aum_warn", False),
            "rank":        f["category_rank"],
            "selected":    f["scheme_code"] in {p["scheme_code"] for p in portfolio},
        })
    cohort["_category_leaders"] = _cat_leaders   # keyed by category name

    return portfolio, cohort, scored


# ══════════════════════════════════════════════════════════════════════
# TAX ENGINE
# ══════════════════════════════════════════════════════════════════════

def compute_tax(portfolio: list, monthly_sip: int,
                timeline_years: float, tax_bracket_pct: int) -> dict:
    """
    Equity: LTCG 12.5% on gains above ₹1.25L × timeline_years total exemption.
    Debt:   slab rate on full gains (no indexation post 2023).
    Applied to P10/P50/P90 scenarios.
    """
    eq_wt = sum(f["weight_pct"] for f in portfolio if f["category"] != "debt")
    db_wt = sum(f["weight_pct"] for f in portfolio if f["category"] == "debt")
    total_wt = max(eq_wt + db_wt, 1)
    eq_frac = eq_wt / total_wt

    invested       = monthly_sip * timeline_years * 12
    eq_invested    = invested * eq_frac
    db_invested    = invested * (1 - eq_frac)
    ltcg_exemption = LTCG_EXEMPTION_PA * timeline_years

    result: dict = {"equity_fraction": eq_frac, "tax_bracket_pct": tax_bracket_pct,
                    "ltcg_exemption": ltcg_exemption}

    for pname in ("p10","p50","p90"):
        gross = sum((f.get("mc_projection") or {}).get(pname, f["projected_corpus"])
                    for f in portfolio)
        eq_gross  = gross * eq_frac
        db_gross  = gross * (1 - eq_frac)

        eq_gain   = max(0, eq_gross - eq_invested)
        taxable   = max(0, eq_gain - ltcg_exemption)
        eq_tax    = taxable * LTCG_RATE_EQUITY

        db_gain   = max(0, db_gross - db_invested)
        db_tax    = db_gain * (tax_bracket_pct / 100)

        result[pname] = {
            "gross": gross,
            "eq_tax": eq_tax, "db_tax": db_tax,
            "total_tax": eq_tax + db_tax,
            "net": gross - eq_tax - db_tax,
        }

    # TER drag: cost of active TER vs passive 0.10%
    avg_ter   = float(np.mean([f["ter"] for f in portfolio])) if portfolio else 0
    ter_diff  = max(0, avg_ter - PASSIVE_TER_EQUITY) / 100      # per year
    months    = int(timeline_years * 12)
    r_actual  = float(np.mean([f["estimated_rate"] for f in portfolio])) if portfolio else 0
    r_passive = r_actual + ter_diff
    c_actual  = sip_corpus(monthly_sip, months, r_actual)
    c_passive = sip_corpus(monthly_sip, months, r_passive)
    result["ter_drag_rs"] = round(max(0, c_passive - c_actual), 0)
    result["avg_ter"]     = round(avg_ter, 2)
    return result


# ══════════════════════════════════════════════════════════════════════
# PASSIVE BASELINE
# ══════════════════════════════════════════════════════════════════════

def simulate_passive_baseline(
    monthly_sip: int, timeline_years: float,
    benchmarks: dict, optimise: str,
    n_sims: int = MC_N_SIMS_STANDARD,
) -> dict:
    splits = {"aggressive":0.80,"balanced":0.60,"conservative":0.30}
    eq_split = splits.get(optimise, PASSIVE_EQUITY_SPLIT)

    eq_alloc  = monthly_sip * eq_split
    db_alloc  = monthly_sip * (1 - eq_split)
    months    = int(timeline_years * 12)

    nifty = benchmarks.get("^NSEI", pd.Series(dtype=float))
    if not nifty.empty:
        ter_monthly = PASSIVE_TER_EQUITY / 100 / 12
        rng   = np.random.default_rng(0)
        ret   = nifty.replace([np.inf, -np.inf], np.nan).dropna().values / 100.0 - ter_monthly
        ret   = ret[ret > -1.0]
        if len(ret) < 12:
            r = (12.0 - PASSIVE_TER_EQUITY) / 100
            eq_fc = np.full(n_sims, sip_corpus(eq_alloc, months, r))
        else:
            draws = rng.choice(ret, size=(n_sims, months), replace=True)
            gf    = np.fliplr(np.cumprod(1 + np.fliplr(draws), axis=1))
            eq_fc = eq_alloc * np.sum(gf, axis=1)
    else:
        # deterministic fallback at 12% - TER
        r = (12.0 - PASSIVE_TER_EQUITY) / 100
        eq_fc = np.full(n_sims, sip_corpus(eq_alloc, months, r))

    db_rate = (PASSIVE_DEBT_RETURN - PASSIVE_TER_DEBT) / 100
    db_corp = sip_corpus(db_alloc, months, db_rate)
    total   = eq_fc + db_corp

    q = np.percentile(total, [10, 25, 50, 75, 90])
    return {"p10":float(q[0]),"p25":float(q[1]),"p50":float(q[2]),
            "p75":float(q[3]),"p90":float(q[4]),
            "equity_split":round(eq_split*100), "debt_split":round((1-eq_split)*100)}


def active_vs_passive(active_mc: dict, passive_mc: dict) -> dict:
    ap, pp = active_mc.get("p50",0), passive_mc.get("p50",0)
    diff   = ap - pp
    pct    = (ap/pp - 1)*100 if pp else 0
    verdict = ("Active ahead on median corpus" if pct > 5 else
               "Passive ahead on median corpus" if pct < -5 else
               "Median result marginal")
    return {"active_p50":ap,"passive_p50":pp,
            "advantage_rs":round(diff),"advantage_pct":round(pct,1),"verdict":verdict}


# ══════════════════════════════════════════════════════════════════════
# GOAL-BASED: corpus → required SIP
# ══════════════════════════════════════════════════════════════════════

def compute_required_sip(
    target_corpus: float, timeline_years: float,
    portfolio: list, n_sims: int = MC_N_SIMS_GOAL,
) -> dict:
    """Binary-search monthly SIP so MC corpus at P50 ≈ target_corpus."""
    months = int(timeline_years * 12)
    # Build blended monthly return distribution from portfolio
    all_returns: list = []
    for f in portfolio:
        s = f.get("_monthly_returns")
        if s is not None and not s.empty:
            cleaned = s.replace([np.inf, -np.inf], np.nan).dropna()
            all_returns.extend(cleaned.values.tolist())
    if not all_returns:
        # deterministic fallback
        avg_rate = float(np.mean([f["estimated_rate"] for f in portfolio])) if portfolio else 0.08
        r = avg_rate / 12
        req = target_corpus * r / ((pow(1+r, months)-1) * (1+r)) if r else target_corpus / months
        req = max(500, round(req / 500) * 500)
        return {"p50_sip":req,"p10_sip":int(req*0.75),"p90_sip":int(req*1.4)}

    rng = np.random.default_rng(42)
    ret = np.array(all_returns) / 100.0
    ret = ret[ret > -1.0]
    if len(ret) < 12:
        avg_rate = float(np.mean([f["estimated_rate"] for f in portfolio])) if portfolio else 0.08
        r = avg_rate / 12
        req = target_corpus * r / ((pow(1+r, months)-1) * (1+r)) if r else target_corpus / months
        req = max(500, round(req / 500) * 500)
        return {"p50_sip":req,"p10_sip":int(req*0.75),"p90_sip":int(req*1.4)}

    def mc_corpus_at(sip_amt: float, pct: int) -> float:
        draws = rng.choice(ret, size=(n_sims, months), replace=True)
        gf    = np.fliplr(np.cumprod(1 + np.fliplr(draws), axis=1))
        fc    = sip_amt * np.sum(gf, axis=1)
        return float(np.percentile(fc, pct))

    result = {}
    for pname, pct_mc in [("p50_sip",50),("p10_sip",90),("p90_sip",10)]:
        lo, hi = 500.0, 200_000.0
        for _ in range(35):
            mid  = (lo + hi) / 2
            corp = mc_corpus_at(mid, pct_mc)
            if corp < target_corpus: lo = mid
            else: hi = mid
        result[pname] = max(500, round((lo+hi)/2 / 500) * 500)
    return result


# ══════════════════════════════════════════════════════════════════════
# REBALANCING SCHEDULE
# ══════════════════════════════════════════════════════════════════════

def compute_weight_drift(portfolio: list, check_years: list = None) -> dict:
    if check_years is None: check_years = [1, 3, 5]
    schedule: dict = {}
    for year in check_years:
        months = year * 12
        corpora = []
        for f in portfolio:
            r = f["estimated_rate"] / 12
            a = f["monthly_alloc"]
            c = sip_corpus(a, months, f["estimated_rate"]) if r else a * months
            corpora.append(c)
        total = sum(corpora) or 1
        yr_data: dict = {"year": year, "funds": [], "trigger": False}
        for f, corp in zip(portfolio, corpora):
            actual = corp / total * 100
            target = f["weight_pct"]
            dev    = actual - target
            yr_data["funds"].append({
                "name": f["name"][:30], "target": target,
                "actual": round(actual, 1), "deviation": round(dev, 1),
                "flag": abs(dev) > 5,
            })
        yr_data["trigger"]      = any(fd["flag"] for fd in yr_data["funds"])
        yr_data["max_deviation"] = max(abs(fd["deviation"]) for fd in yr_data["funds"])
        schedule[year] = yr_data
    return schedule


# ══════════════════════════════════════════════════════════════════════
# VISUALIZATION
# ══════════════════════════════════════════════════════════════════════

def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

def generate_charts(
    portfolio: list, cohort: dict,
    monthly_sip: int, timeline_years: float, optimise: str,
    benchmarks: dict = None, market_context: str = "",
    passive_mc: dict = None,
    holdings_analysis: list = None,
) -> dict:
    """
    4-panel 2×2 chart — each panel earns its space:
      TL: Allocation donut + IR/capture bar per fund
      TR: Peer scatter — every category peer shown, portfolio funds highlighted
      BL: Monte Carlo projection fan (P10/P50/P90) vs passive baseline
      BR: Existing portfolio HOLD/SELL/BUY bar chart  OR  score breakdown if no holdings
    """
    from matplotlib.patches import Patch
    benchmarks      = benchmarks or {}
    passive_mc      = passive_mc or {}
    holdings_analysis = holdings_analysis or []
    nifty_b         = benchmarks.get("^NSEI", pd.Series(dtype=float))
    n               = len(portfolio)
    pal             = matplotlib.colormaps["tab10"]
    clr             = [pal(i / max(n - 1, 1)) for i in range(n)]
    snm             = [f["name"][:28] for f in portfolio]
    months_arr      = np.arange(0, int(timeline_years * 12) + 1)

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(2, 2, figsize=(18, 12), constrained_layout=True)
    fig.suptitle(
        f"MF Portfolio — {optimise.title()}  ·  ₹{monthly_sip:,}/mo  ·  {timeline_years:.0f}Y"
        + (f"  ·  {market_context[:40]}" if market_context else ""),
        fontsize=13, fontweight="bold", y=1.01,
    )
    ax_tl, ax_tr, ax_bl, ax_br = axes[0,0], axes[0,1], axes[1,0], axes[1,1]

    # ── TL: Allocation donut + IR·capture bar ────────────────────────────
    ax_tl.axis("off")
    # Donut (inner axes)
    ax_donut = ax_tl.inset_axes([0.0, 0.35, 0.45, 0.62])
    sizes = [f["weight_pct"] for f in portfolio]
    wedges, _ = ax_donut.pie(sizes, colors=clr, startangle=90,
                              wedgeprops=dict(width=0.5, edgecolor="white"))
    ax_donut.set_title("Allocation", fontsize=9, fontweight="bold", pad=2)

    # IR + capture ratio mini-bars (right side of TL)
    ax_bar = ax_tl.inset_axes([0.50, 0.05, 0.48, 0.92])
    yp = np.arange(n)
    ir_vals  = [f.get("info_ratio")   or 0 for f in portfolio]
    cap_vals = [f.get("capture_ratio") or 0 for f in portfolio]
    ax_bar.barh(yp - 0.18, ir_vals,  height=0.32, color=clr, alpha=0.85, label="Info Ratio")
    ax_bar.barh(yp + 0.18, cap_vals, height=0.32, color=clr, alpha=0.45, label="Capture Ratio", hatch="///")
    ax_bar.axvline(0, color="black", linewidth=0.7)
    ax_bar.axvline(1, color="green", linewidth=0.7, linestyle="--", alpha=0.5)
    ax_bar.set_yticks(yp)
    ax_bar.set_yticklabels([f"{nm}\n[bold]{f['score']:.0f}pts[/bold]".replace("[bold]","").replace("[/bold]","")
                             for nm, f in zip(snm, portfolio)], fontsize=7)
    ax_bar.set_xlabel("IR  /  Capture ratio", fontsize=8)
    ax_bar.set_title("Info Ratio & Capture", fontsize=9, fontweight="bold")
    ax_bar.legend(fontsize=7, loc="lower right")
    # Legend for donut
    ax_tl.legend(wedges, [f"{nm} {w}%" for nm, w in zip(snm, sizes)],
                 loc="lower left", fontsize=7, frameon=True)

    # ── TR: Peer scatter (all category peers + portfolio highlighted) ─────
    plotted_cats: set = set()
    for cat, cs in cohort.items():
        if cs.get("median_vol") and cs.get("median_r3"):
            lbl = cat.title() if cat not in plotted_cats else None
            ax_tr.scatter(cs["median_vol"], cs["median_r3"],
                          s=30, alpha=0.18, color="gray", zorder=1)
            if lbl:
                ax_tr.annotate(cat[:12], (cs["median_vol"], cs["median_r3"]),
                               fontsize=6, color="#94a3b8", alpha=0.7)
            plotted_cats.add(cat)
    for f, c in zip(portfolio, clr):
        v, r = f.get("volatility"), f.get("return_3y")
        if v and r:
            ax_tr.scatter(v, r, s=200, color=c, edgecolor="black",
                          linewidth=1.5, zorder=5)
            ax_tr.annotate(f["name"][:20], (v, r),
                           fontsize=7, xytext=(5, 5), textcoords="offset points",
                           fontweight="bold")
    ax_tr.set_xlabel("Volatility (ann. %)", fontsize=9)
    ax_tr.set_ylabel("3Y Return (%)", fontsize=9)
    ax_tr.set_title("Risk vs Return — portfolio vs category peers", fontsize=10, fontweight="bold")

    # ── BL: Monte Carlo fan + passive baseline ───────────────────────────
    p_mc   = [f.get("mc_projection") or {} for f in portfolio]
    p10tot = [sum(p.get("p10", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]
    p50tot = [sum(p.get("p50", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]
    p90tot = [sum(p.get("p90", f["projected_corpus"]) for p, f in zip(p_mc, portfolio))]

    # Per-fund deterministic growth lines
    det_total = np.zeros(len(months_arr))
    for f, c in zip(portfolio, clr):
        r  = f["estimated_rate"] / 12
        al = f["monthly_alloc"]
        dt = al * ((np.power(1 + r, months_arr) - 1) / r) * (1 + r) if r else al * months_arr
        ax_bl.plot(months_arr / 12, dt / 1e5, color=c, linewidth=1.0, alpha=0.35)
        det_total += dt

    # Portfolio MC fan
    ax_bl.fill_between([0, timeline_years], [0, p10tot[0] / 1e5], [0, p90tot[0] / 1e5],
                       alpha=0.12, color="steelblue", label="Active P10–P90")
    ax_bl.plot([0, timeline_years], [0, p50tot[0] / 1e5], color="steelblue",
               linewidth=2.5, label=f"Active P50 {fmt_inr(p50tot[0])}")

    # Passive baseline
    if passive_mc:
        pv50 = passive_mc.get("p50", 0)
        pv10 = passive_mc.get("p10", 0)
        pv90 = passive_mc.get("p90", 0)
        ax_bl.fill_between([0, timeline_years], [0, pv10 / 1e5], [0, pv90 / 1e5],
                           alpha=0.08, color="gray")
        ax_bl.plot([0, timeline_years], [0, pv50 / 1e5], color="gray",
                   linewidth=1.5, linestyle="--", label=f"Passive P50 {fmt_inr(pv50)}")

    # Invested amount
    invested = monthly_sip * timeline_years * 12
    ax_bl.plot(months_arr / 12, monthly_sip * months_arr / 1e5, color="red",
               linewidth=1.0, linestyle=":", label=f"Invested {fmt_inr(invested)}")
    ax_bl.set_xlabel("Years", fontsize=9)
    ax_bl.set_ylabel("₹ Lakhs", fontsize=9)
    ax_bl.set_title("Monte Carlo Projection — Active vs Passive", fontsize=10, fontweight="bold")
    ax_bl.legend(fontsize=7, loc="upper left")

    # ── BR: Existing portfolio (if available) OR score breakdown ─────────
    valid_h = [h for h in holdings_analysis if (h.get("current_value") or 0) > 0]
    if valid_h:
        ACTION_COLORS = {"HOLD": "#16a34a", "BUY MORE": "#2563eb",
                         "SELL": "#dc2626", "UNKNOWN": "#94a3b8"}
        names_h  = [h.get("scheme_name", "?")[:30] for h in valid_h]
        vals_h   = [h.get("current_value", 0) / 1e5 for h in valid_h]
        bar_cols = [ACTION_COLORS.get(h.get("action", "UNKNOWN"), "#94a3b8") for h in valid_h]
        bars     = ax_br.barh(names_h, vals_h, color=bar_cols, edgecolor="white", height=0.6)
        for bar, v in zip(bars, vals_h):
            ax_br.text(bar.get_width() + max(vals_h) * 0.01,
                       bar.get_y() + bar.get_height() / 2,
                       f"₹{v:.1f}L", va="center", fontsize=7.5)
        total_v = sum(vals_h)
        ax_br.set_title(f"Your Portfolio — {fmt_inr(total_v * 1e5)} current value",
                         fontsize=10, fontweight="bold")
        ax_br.set_xlabel("₹ Lakhs", fontsize=9)
        ax_br.tick_params(axis="y", labelsize=7.5)
        ax_br.legend(handles=[Patch(color=c, label=a) for a, c in ACTION_COLORS.items()
                               if a != "UNKNOWN"], fontsize=7, loc="lower right")
    else:
        # Score component breakdown as stacked bar
        comps = list(SCORE_WEIGHTS["balanced"].keys())
        cc = matplotlib.colormaps["Set2"](np.linspace(0, 1, len(comps)))
        bots = np.zeros(n)
        yp2  = np.arange(n)
        for i, comp in enumerate(comps):
            vals = np.array([f.get("score_breakdown", {}).get(comp, 0) for f in portfolio])
            ax_br.barh(yp2, vals, left=bots, color=cc[i], label=comp, edgecolor="white")
            bots += vals
        ax_br.set_yticks(yp2)
        ax_br.set_yticklabels(snm, fontsize=8)
        ax_br.invert_yaxis()
        ax_br.set_xlabel("Score contribution", fontsize=9)
        ax_br.set_title(f"Score Breakdown ({len(comps)} components)", fontsize=10, fontweight="bold")
        ax_br.legend(fontsize=7, loc="lower right", ncol=2)

    buf = _b64(fig)

    def _write_png(fh):
        fig.savefig(fh, format="png", dpi=110, bbox_inches="tight")

    chart_path = _save_output_file(_write_png, CHART_FILE)
    if chart_path != CHART_FILE:
        console.print(f"  [yellow]Chart saved to: {chart_path}[/yellow]")

    plt.close(fig)
    return {"main": buf, "_path": chart_path}


# ══════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════

def generate_html_report(
    portfolio: list, params: dict,
    tax: dict, passive_mc: dict, advantage: dict,
    drift: dict, req_sip: dict,
    charts_b64: dict, market_context: str,
    holdings_analysis: list = None,
    cohort: dict = None,
    portfolio_holdings: dict = None,
) -> str:

    def money(v): return fmt_inr(v) if v else "-"
    def pct(v, dp=1): return f"{v:.{dp}f}%" if v is not None else "-"
    def val(v, dp=2): return f"{v:.{dp}f}" if v is not None else "-"
    def esc(v): return html_lib.escape(str(v if v is not None else ""))

    date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    total_p50 = sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"])
                    for f in portfolio)
    invested  = params["monthly_sip"] * params["timeline_years"] * 12
    net_p50   = tax.get("p50", {}).get("net", total_p50)
    risk      = portfolio_risk_metrics(portfolio)
    months    = int(params["timeline_years"] * 12)

    category_totals: dict[str, dict] = {}
    for f in portfolio:
        cat = f.get("category", "other").title()
        bucket = category_totals.setdefault(cat, {"weight": 0.0, "sip": 0.0})
        bucket["weight"] += float(f.get("weight_pct", 0))
        bucket["sip"] += float(f.get("monthly_alloc", 0))

    allocation_rows = "".join(
        f"<tr><td>{i}</td><td><b>{esc(f.get('name'))}</b><br>"
        f"<span style='color:#64748b;font-size:11px;'>{esc(f.get('amc_name','Unknown AMC'))}</span></td>"
        f"<td>{esc(str(f.get('category','other')).title())}</td>"
        f"<td>{money(f.get('monthly_alloc',0))}</td><td>{pct(f.get('weight_pct'))}</td>"
        f"<td>{pct(f.get('return_3y'))}</td><td>{pct(f.get('volatility'))}</td>"
        f"<td>{pct(f.get('max_drawdown'))}</td><td>{val(f.get('sharpe'))}</td>"
        f"<td>{pct(f.get('ter'))}</td><td>{f.get('score',0):.0f}/100</td></tr>"
        for i, f in enumerate(portfolio, 1)
    )
    category_rows = "".join(
        f"<tr><td>{esc(cat)}</td><td>{pct(data['weight'])}</td><td>{money(data['sip'])}</td></tr>"
        for cat, data in sorted(category_totals.items())
    )

    risk_tiles = "".join([
        f"<div class='metric'><div class='mlbl'>Portfolio Volatility</div><b>{pct(risk.get('volatility'))}</b><span>Annualised from weighted monthly NAV returns</span></div>",
        f"<div class='metric'><div class='mlbl'>Max Drawdown</div><b class='bad'>{pct(risk.get('max_drawdown'))}</b><span>Worst historical peak-to-trough fall in blended series</span></div>",
        f"<div class='metric'><div class='mlbl'>Sharpe</div><b>{val(risk.get('sharpe'))}</b><span>Excess return over {RISK_FREE_MONTHLY:.1f}% monthly risk-free proxy</span></div>",
        f"<div class='metric'><div class='mlbl'>Sortino</div><b>{val(risk.get('sortino'))}</b><span>Downside-risk adjusted return</span></div>",
    ])

    active_range = (tax.get('p90') or {}).get('gross', 0) - (tax.get('p10') or {}).get('gross', 0)
    passive_range = passive_mc.get('p90', 0) - passive_mc.get('p10', 0)
    mc_assumptions = f"""
      <div class="note-grid">
        <div><b>Active simulation</b><span>Each fund bootstraps its own historical monthly NAV returns with replacement. Fund P10/P50/P90 values are summed into the portfolio range.</span></div>
        <div><b>Passive simulation</b><span>{passive_mc.get('equity_split',75)}% Nifty 50 monthly returns less {PASSIVE_TER_EQUITY:.2f}% TER, plus {passive_mc.get('debt_split',25)}% debt at {PASSIVE_DEBT_RETURN:.1f}% p.a. less {PASSIVE_TER_DEBT:.2f}% TER.</span></div>
        <div><b>Simulation count</b><span>{MC_N_SIMS_STANDARD} paths over {months} months. Random seeds are fixed for reproducible reports.</span></div>
        <div><b>Model risk</b><span>Bootstrap sampling assumes historical monthly return shapes are useful proxies. It does not forecast valuation, rates, manager changes, fund closure, or liquidity stress.</span></div>
      </div>
    """

    # ── Existing portfolio section (only when holdings were fetched) ─────
    _existing_portfolio_html = ""
    if holdings_analysis:
        _ACTION_STYLE = {
            "HOLD":    ("background:#dcfce7;color:#15803d;", "✅ HOLD"),
            "SELL":    ("background:#fee2e2;color:#b91c1c;", "🔴 SELL"),
            "BUY MORE":("background:#dbeafe;color:#1d4ed8;", "💰 BUY MORE"),
            "UNKNOWN": ("background:#f1f5f9;color:#64748b;", "❓ UNKNOWN"),
        }
        total_cur_val = sum(h.get("current_value") or 0 for h in holdings_analysis)
        action_counts = {}
        for h in holdings_analysis:
            action_counts[h.get("action","?")] = action_counts.get(h.get("action","?"), 0) + 1

        _unk = _ACTION_STYLE["UNKNOWN"]
        summary_chips = " &nbsp; ".join(
            "<span style='{s}padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600;'>"
            "{lbl} &times; {n}</span>".format(
                s=_ACTION_STYLE.get(a, _unk)[0],
                lbl=_ACTION_STYLE.get(a, _unk)[1],
                n=n,
            )
            for a, n in sorted(action_counts.items())
        )

        holding_rows = ""
        for h in holdings_analysis:
            action     = h.get("action", "UNKNOWN")
            astyle, alabel = _ACTION_STYLE.get(action, _ACTION_STYLE["UNKNOWN"])
            cr, cs     = h.get("category_rank"), h.get("category_size")
            rank_s     = f"{cr}/{cs}" if cr and cs else "—"
            top_pct    = f"{(cs-cr)/cs*100:.0f}%" if (cr and cs) else "—"
            cagr_s     = f"{h['holding_cagr']:.1f}%" if h.get("holding_cagr") is not None else "—"
            gain_s     = (f"<span style='color:{'#15803d' if (h.get('gain_pct') or 0)>=0 else '#b91c1c'}'>"
                          f"{h['gain_pct']:+.1f}%</span>" if h.get("gain_pct") is not None else "—")
            score_s    = f"{h['score']:.0f}/100" if h.get("score") else "—"
            alpha_s    = f"{h['alpha']:.2f}%" if h.get("alpha") is not None else "—"
            val_s      = money(h.get("current_value") or 0)
            sip_s      = money(h.get("sip_amount") or 0)
            holding_rows += (
                f"<tr>"
                f"<td><b>{esc(h.get('scheme_name','?')[:40])}</b><br>"
                f"<span style='color:#64748b;font-size:11px;'>{esc(h.get('category','?'))}</span></td>"
                f"<td>{sip_s}<br><span style='color:#94a3b8;font-size:11px;'>SIP/mo</span></td>"
                f"<td><b>{val_s}</b></td>"
                f"<td>{cagr_s} CAGR<br>{gain_s} total</td>"
                f"<td>{rank_s}<br><span style='color:#64748b;font-size:11px;'>Top {top_pct}</span></td>"
                f"<td>{score_s}</td>"
                f"<td>{alpha_s}</td>"
                f"<td><span style='{astyle}padding:3px 8px;border-radius:6px;"
                f"font-size:12px;font-weight:600;'>{alabel}</span><br>"
                f"<span style='color:#64748b;font-size:11px;'>{esc(h.get('action_reason',''))}</span></td>"
                f"</tr>"
            )

        _existing_portfolio_html = f"""
<div class="section" id="existing-portfolio">
  <h2>📂 Your Existing Portfolio</h2>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px;">
    <div>
      <span style="font-size:22px;font-weight:700;">{money(total_cur_val)}</span>
      <span style="color:#64748b;font-size:13px;margin-left:8px;">current portfolio value</span>
    </div>
    <div>{summary_chips}</div>
  </div>
  <table>
    <thead><tr>
      <th>Fund</th><th>SIP / Value</th><th>Current Value</th>
      <th>Returns</th><th>Rank in Category</th><th>Score</th><th>Alpha</th><th>Recommendation</th>
    </tr></thead>
    <tbody>{holding_rows}</tbody>
  </table>
  <p style="margin-top:14px;font-size:12px;color:#64748b;">
    <b>HOLD</b> = top-50% of category, age ≥ 5Y or rank strong enough. &nbsp;
    <b>SELL</b> = bottom quartile of category for 2+ years (consider switching). &nbsp;
    <b>BUY MORE</b> = top-25% and above average alpha — strong conviction candidate.
    Rank is within the fund's SEBI category. Returns are simple CAGR from first purchase date.
  </p>
</div>"""

    # ── Category Scorecard — shows why each fund was chosen ─────────────
    leaders = (cohort or {}).get("_category_leaders", {})
    _scorecard_html = ""
    if leaders:
        cat_blocks = ""
        selected_codes = {f["scheme_code"] for f in portfolio}
        for cat, funds in sorted(leaders.items()):
            top = sorted(funds, key=lambda x: x["score"], reverse=True)[:8]
            rows = ""
            for rank, fd in enumerate(top, 1):
                is_sel = fd["scheme_code"] in selected_codes
                bg     = "background:#f0fdf4;" if is_sel else ""
                badge  = (" <span style='background:#16a34a;color:#fff;padding:1px 6px;"
                          "border-radius:4px;font-size:10px;'>SELECTED</span>") if is_sel else ""
                aum_tag = (" <span style='color:#b91c1c;font-size:10px;'>⚠AUM</span>"
                           if fd.get("aum_warn") else "")
                ret_s  = f"{fd['return_3y']:.1f}%" if fd.get("return_3y") is not None else "—"
                alp_s  = f"{fd['alpha']:.2f}%" if fd.get("alpha") is not None else "—"
                aum_s  = f"{fd['aum_cr']:,.0f} Cr" if fd.get("aum_cr") else "—"
                rows += (
                    f"<tr style='{bg}'>"
                    f"<td style='font-weight:{'700' if is_sel else '400'};'>"
                    f"#{rank} {esc(fd['name'][:48])}{badge}{aum_tag}</td>"
                    f"<td style='text-align:right;font-weight:{'700' if is_sel else '400'};'>"
                    f"{fd['score']:.1f}</td>"
                    f"<td style='text-align:right;'>{ret_s}</td>"
                    f"<td style='text-align:right;'>{alp_s}</td>"
                    f"<td style='text-align:right;color:#64748b;font-size:11px;'>{aum_s}</td>"
                    f"</tr>"
                )
            cat_blocks += f"""
<div style="margin-bottom:20px;">
  <h3 style="font-size:14px;font-weight:600;color:#1e3a5f;margin-bottom:6px;">
    {esc(cat.title())}
    <span style="font-weight:400;color:#64748b;font-size:12px;">
      — {len(funds)} funds scored, top 8 shown
    </span>
  </h3>
  <table>
    <thead><tr><th>Fund</th><th>Score</th><th>3Y Return</th><th>Alpha</th><th>AUM</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""
        _scorecard_html = f"""
<div class="section" id="scorecard">
  <h2>🏆 Category Scorecard — Why These Funds Were Chosen</h2>
  <p style="font-size:12px;color:#64748b;margin-bottom:16px;">
    Every fund in the NAV universe is scored (0–100) within its SEBI category.
    The highest scorer per slot is selected. Green rows = chosen for your portfolio.
    ⚠AUM = fund's AUM exceeds category capacity threshold, score penalised.
  </p>
  {cat_blocks}
</div>"""

    # market banner colour
    if "BULL" in market_context:  mcol = "#744210"; mbg = "#fefce8"
    elif "BEAR" in market_context: mcol = "#7f1d1d"; mbg = "#fff1f2"
    else:                          mcol = "#1e3a5f"; mbg = "#eff6ff"

    fund_cards = ""
    for i, f in enumerate(portfolio, 1):
        mc  = f.get("mc_projection") or {}
        cat = esc(f["category"].title())
        fund_name = esc(f["name"])
        amc_name = esc(f.get("amc_name", "N/A"))
        scheme_code = esc(f.get("scheme_code", ""))
        isin = esc(f.get("isin", "N/A"))
        bench = esc(f.get("category_benchmark", "^NSEI"))
        is_debt = f["category"] == "debt"
        bdg_col = "#0369a1" if not is_debt else "#15803d"
        aum_warn = (f"<span style='background:#fef2f2;color:#b91c1c;padding:2px 6px;"
                    f"border-radius:4px;font-size:11px;margin-left:8px;'>⚠ AUM high</span>"
                    if f.get("aum_warn") else "")

        # score bar — 7 actual components matching SCORE_WEIGHTS keys
        score_comp_colors = {
            "info_ratio": "#3b82f6", "capture": "#8b5cf6",
            "consistency": "#06b6d4", "cost": "#f97316",
            "capacity": "#84cc16", "valuation": "#10b981",
            "ext_rank": "#f59e0b",
        }
        score_bar = ""
        bd = f.get("score_breakdown", {})
        for comp, color in score_comp_colors.items():
            w_px = int(bd.get(comp, 0) / 100 * 280)
            if w_px > 0:
                score_bar += (
                    f"<div style='width:{w_px}px;height:12px;background:{color};"
                    f"display:inline-block;' title='{comp}: {bd.get(comp,0):.1f}'></div>"
                )

        up_dn = (f"↑{f.get('up_capture','?'):.0f}% / ↓{f.get('down_capture','?'):.0f}%"
                 if f.get("up_capture") and f.get("down_capture") else "N/A")

        # PE vs category from mfdata.in
        fund_pe = f.get("pe_ratio")
        cat_pe  = f.get("category_pe")
        pe_html = ""
        if fund_pe is not None:
            pe_color = "#16a34a" if (cat_pe and fund_pe < cat_pe * 0.95) else (
                       "#dc2626" if (cat_pe and fund_pe > cat_pe * 1.15) else "#1e293b")
            cat_pe_str = f" (cat avg {cat_pe:.1f})" if cat_pe else ""
            pe_html = (f"<div><div style='color:#64748b;'>PE Ratio</div>"
                       f"<b style='color:{pe_color};'>{fund_pe:.1f}{cat_pe_str}</b></div>")

        # Fund manager from enrichment / portfolio holdings
        ph = (portfolio_holdings or {}).get(str(f.get("scheme_code", "")), {})
        raw_mgr = f.get("fund_manager") or ph.get("fund_manager")
        mgr_html = ""
        if raw_mgr:
            if isinstance(raw_mgr, list):
                mgr_names = ", ".join(
                    (m.get("name") or m.get("manager_name") or str(m))[:30]
                    if isinstance(m, dict) else str(m)[:30]
                    for m in raw_mgr[:3]
                )
            elif isinstance(raw_mgr, dict):
                mgr_names = raw_mgr.get("name") or raw_mgr.get("manager_name") or str(raw_mgr)[:30]
            else:
                mgr_names = str(raw_mgr)[:60]
            mgr_html = (
                f"<div style='margin-bottom:10px;font-size:12px;'>"
                f"<span style='color:#64748b;'>Fund Manager: </span>"
                f"<b>{esc(mgr_names)}</b></div>"
            )

        # Top holdings chips
        raw_holdings = ph.get("top_holdings") or []
        holdings_html = ""
        if raw_holdings:
            chips = ""
            for h in raw_holdings[:8]:
                if isinstance(h, dict):
                    hname = h.get("name") or h.get("stock_name") or h.get("issuer") or ""
                    hpct  = h.get("weight") or h.get("percentage") or h.get("pct") or ""
                    hpct_str = f" {float(hpct):.1f}%" if hpct else ""
                else:
                    hname, hpct_str = str(h), ""
                if hname:
                    chips += (
                        f"<span style='background:#f1f5f9;border:1px solid #e2e8f0;"
                        f"border-radius:4px;padding:2px 7px;font-size:11px;margin:2px;'>"
                        f"{esc(str(hname)[:22])}{hpct_str}</span>"
                    )
            if chips:
                holdings_html = (
                    f"<div style='margin-bottom:10px;'>"
                    f"<div style='color:#64748b;font-size:11px;margin-bottom:4px;'>Top Holdings</div>"
                    f"<div style='display:flex;flex-wrap:wrap;gap:2px;'>{chips}</div></div>"
                )

        # Sector allocation mini bar
        raw_sectors = ph.get("sector_allocation") or []
        sector_html = ""
        if raw_sectors:
            bars = ""
            for s in raw_sectors[:6]:
                if isinstance(s, dict):
                    sname = s.get("sector") or s.get("name") or ""
                    spct  = _safe_float(s.get("weight") or s.get("percentage") or s.get("pct") or 0) or 0
                else:
                    sname, spct = str(s), 0
                if sname and spct:
                    bar_w = min(100, int(spct))
                    bars += (
                        f"<div style='display:flex;align-items:center;gap:6px;margin-bottom:3px;font-size:11px;'>"
                        f"<div style='width:90px;color:#475569;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;'>{esc(str(sname)[:14])}</div>"
                        f"<div style='flex:1;background:#f1f5f9;border-radius:3px;height:8px;'>"
                        f"<div style='width:{bar_w}%;background:#6366f1;height:100%;border-radius:3px;'></div></div>"
                        f"<div style='width:32px;text-align:right;color:#64748b;'>{spct:.0f}%</div></div>"
                    )
            if bars:
                sector_html = (
                    f"<div style='margin-bottom:10px;'>"
                    f"<div style='color:#64748b;font-size:11px;margin-bottom:4px;'>Sector Allocation</div>"
                    f"{bars}</div>"
                )

        fund_cards += f"""
        <div style="border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
            <div>
              <h3 style="font-size:16px;font-weight:600;margin-bottom:4px;">#{i} {fund_name}</h3>
              <div style="color:#64748b;font-size:12px;">{amc_name} &nbsp;|&nbsp;
                Code: {scheme_code} &nbsp;|&nbsp; ISIN: {isin}</div>
            </div>
            <div style="text-align:right;">
              <span style="background:{'#dbeafe' if not is_debt else '#dcfce7'};color:{bdg_col};
                           padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600;">{cat}</span>
              {aum_warn}
              <div style="color:#94a3b8;font-size:11px;margin-top:4px;">vs {bench}</div>
            </div>
          </div>
          {mgr_html}
          <div style="background:#e0f2fe;border-radius:4px;height:6px;margin-bottom:16px;">
            <div style="background:#0284c7;width:{f['weight_pct']}%;height:100%;border-radius:4px;"></div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;font-size:12px;">
            <div><div style="color:#64748b;">Monthly SIP</div><b>{money(f['monthly_alloc'])}</b> ({f['weight_pct']}%)</div>
            <div><div style="color:#64748b;">3Y Return</div><b>{pct(f.get('return_3y'))}</b></div>
            <div><div style="color:#64748b;">5Y Return</div><b>{pct(f.get('return_5y'))}</b></div>
            <div><div style="color:#64748b;">Alpha (vs idx)</div><b>{val(f.get('alpha'))}%</b></div>
            <div><div style="color:#64748b;">Sharpe</div><b>{val(f.get('sharpe'))}</b></div>
            <div><div style="color:#64748b;">Max Drawdown</div><b style="color:#dc2626;">{pct(f.get('max_drawdown'))}</b></div>
            <div><div style="color:#64748b;">Volatility</div><b>{pct(f.get('volatility'))}</b></div>
            <div><div style="color:#64748b;">TER</div><b>{pct(f.get('ter'))}</b></div>
            <div><div style="color:#64748b;">AUM</div><b>{val(f.get('aum_cr'))} Cr</b></div>
            <div><div style="color:#64748b;">Rating</div><b>{val(f.get('rating'))}</b></div>
            <div><div style="color:#64748b;">Bear Alpha</div><b>{val(f.get('bear_alpha'))}%/mo</b></div>
            <div><div style="color:#64748b;">Up/Down Cap</div><b>{up_dn}</b></div>
            <div><div style="color:#64748b;">Rank Consistency</div><b>{val(f.get('rank_consistency'))}</b></div>
            <div><div style="color:#64748b;">Score</div><b>{f['score']}/100</b></div>
            {pe_html}
          </div>
          <div style="margin-bottom:10px;">
            <div style="color:#64748b;font-size:11px;margin-bottom:4px;">
              Score breakdown — <span style='color:#3b82f6;'>IR</span>
              <span style='color:#8b5cf6;'> Capture</span>
              <span style='color:#06b6d4;'> Consistency</span>
              <span style='color:#f97316;'> Cost</span>
              <span style='color:#84cc16;'> Capacity</span>
              <span style='color:#10b981;'> Valuation</span>
              <span style='color:#f59e0b;'> Ext Rank</span>
            </div>
            <div style="border-radius:4px;overflow:hidden;height:12px;">{score_bar}</div>
          </div>
          {holdings_html}{sector_html}
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:12px;">
            {"".join(f"<div style='background:{'#f0fdf4' if k=='p50' else '#f8fafc'};border-radius:6px;padding:8px;text-align:center;'><div style='color:#64748b;font-size:10px;'>{k.upper()}</div><b>{money(v)}</b></div>" for k,v in mc.items() if k in ('p10','p50','p90'))}
          </div>
        </div>"""

    # Rebalancing table
    reb_rows = ""
    for yr, data in sorted(drift.items()):
        trig_col = "#dc2626" if data["trigger"] else "#16a34a"
        trig_txt = "⚠ Rebalance" if data["trigger"] else "✓ OK"
        reb_rows += f"<tr><td style='font-weight:600;'>Year {yr}</td>"
        for fd in data["funds"]:
            dev_col = "#dc2626" if fd["flag"] else ("#d97706" if abs(fd["deviation"])>3 else "#16a34a")
            reb_rows += f"<td>{fd['target']:.0f}% → <span style='color:{dev_col};font-weight:600;'>{fd['actual']:.1f}%</span></td>"
        reb_rows += f"<td style='color:{trig_col};font-weight:600;'>{trig_txt} (max {data['max_deviation']:.1f}pp)</td></tr>"

    reb_headers = "".join(f"<th>{fd['name'][:16]}</th>" for fd in (drift.get(1,{}).get("funds") or []))

    # TER drag
    ter_drag = tax.get("ter_drag_rs", 0)

    # passive comparison block
    adv_col = "#16a34a" if advantage.get("advantage_rs",0) > 0 else "#dc2626"

    # required SIP block
    req_sip_html = ""
    if req_sip:
        req_sip_html = f"""
        <div class="section">
          <h2>🎯 Goal-Based Planning</h2>
          <p style="color:#64748b;margin-bottom:16px;">
            Target corpus: <b>{money(params.get('target_corpus',0))}</b> in
            <b>{params['timeline_years']:.0f} years</b>
          </p>
          <table><thead><tr><th>Confidence Level</th><th>Required Monthly SIP</th><th>Interpretation</th></tr></thead>
          <tbody>
            <tr><td>P90 (90% likely)</td><td><b>{money(req_sip.get('p90_sip',0))}</b></td><td>Conservative — invest this to be 90% sure of reaching target</td></tr>
            <tr><td>P50 (50% likely)</td><td><b>{money(req_sip.get('p50_sip',0))}</b></td><td>Median scenario</td></tr>
            <tr><td>P10 (10% likely)</td><td><b>{money(req_sip.get('p10_sip',0))}</b></td><td>Optimistic — only if markets do very well</td></tr>
          </tbody></table>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MF Portfolio Report — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b}}
.container{{max-width:1200px;margin:0 auto;padding:24px}}
.hero{{background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 100%);color:white;border-radius:16px;padding:32px;margin-bottom:20px}}
.hero h1{{font-size:24px;font-weight:700;margin-bottom:6px}}
.hero .sub{{font-size:13px;opacity:.75;margin-bottom:20px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.kpi{{background:rgba(255,255,255,.15);border-radius:10px;padding:14px}}
.kpi .lbl{{font-size:10px;text-transform:uppercase;letter-spacing:1px;opacity:.8}}
.kpi .val{{font-size:20px;font-weight:700;margin-top:4px}}
.kpi .sub2{{font-size:11px;opacity:.65;margin-top:2px}}
.section{{background:white;border-radius:12px;padding:24px;margin-bottom:16px;
           box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.section h2{{font-size:17px;font-weight:600;color:#1e40af;margin-bottom:16px;
             border-bottom:2px solid #dbeafe;padding-bottom:8px}}
.metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.metric{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px}}
.metric .mlbl{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#64748b;margin-bottom:6px}}
.metric b{{display:block;font-size:20px;margin-bottom:4px}}
.metric span{{display:block;font-size:11px;color:#64748b;line-height:1.4}}
.note-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;font-size:12px;color:#475569}}
.note-grid div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px}}
.note-grid b{{display:block;color:#1e293b;margin-bottom:4px}}
.note-grid span{{display:block;line-height:1.5}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f1f5f9;padding:10px 12px;text-align:left;font-weight:600;color:#475569}}
td{{padding:10px 12px;border-bottom:1px solid #f1f5f9}}
tr:hover td{{background:#f8fafc}}
.good{{color:#16a34a}} .warn{{color:#d97706}} .bad{{color:#dc2626}}
.footer{{text-align:center;color:#94a3b8;font-size:11px;padding:24px;line-height:1.8}}
</style></head>
<body><div class="container">

<div class="hero">
  <h1>MF SIP Portfolio Report</h1>
  <div class="sub">Generated {date_str} &nbsp;|&nbsp; Strategy: {params['optimise'].title()}
   &nbsp;|&nbsp; {params['timeline_years']:.0f}Y · ₹{params['monthly_sip']:,}/mo</div>
  <div class="kpi-grid">
    <div class="kpi"><div class="lbl">P50 Gross Corpus</div>
      <div class="val">{money(total_p50)}</div>
      <div class="sub2">P10: {money((tax.get('p10') or {}).get('gross',0))} &nbsp; P90: {money((tax.get('p90') or {}).get('gross',0))}</div></div>
    <div class="kpi"><div class="lbl">Net After Tax (P50)</div>
      <div class="val">{money(net_p50)}</div>
      <div class="sub2">Tax bracket: {params.get('tax_bracket_pct',0)}% slab</div></div>
    <div class="kpi"><div class="lbl">TER Drag vs Passive</div>
      <div class="val" style="color:#fca5a5;">{money(ter_drag)}</div>
      <div class="sub2">Cost of active TER over {params['timeline_years']:.0f}Y</div></div>
    <div class="kpi"><div class="lbl">Active vs Passive</div>
      <div class="val" style="color:{'#86efac' if advantage.get('advantage_rs',0)>0 else '#fca5a5'};">
        {money(abs(advantage.get('advantage_rs',0)))}</div>
      <div class="sub2">{advantage.get('verdict','—')}</div></div>
  </div>
</div>

{"<div style='background:" + mbg + ";color:" + mcol + ";border-radius:10px;padding:14px;margin-bottom:16px;font-size:13px;font-weight:500;'>📊 Market Signal: " + market_context + "</div>" if market_context else ""}

{req_sip_html}

<div class="section"><h2>Portfolio Breakdown</h2>
<table><thead><tr><th>#</th><th>Fund</th><th>Category</th><th>Monthly SIP</th><th>Allocation</th>
  <th>3Y Return</th><th>Volatility</th><th>Max Drawdown</th><th>Sharpe</th><th>TER</th><th>Score</th></tr></thead>
<tbody>{allocation_rows}</tbody></table>
</div>

<div class="section"><h2>Category Allocation</h2>
<table><thead><tr><th>Category</th><th>Allocation</th><th>Monthly SIP</th></tr></thead>
<tbody>{category_rows}</tbody></table>
</div>

<div class="section"><h2>Risk Metrics</h2>
  <div class="metric-grid">{risk_tiles}</div>
  <p style="margin-top:12px;font-size:12px;color:#64748b;">
    Risk metrics use the weighted historical monthly return series for the selected funds
    ({risk.get('months','-')} overlapping monthly observations). They are context for the
    median corpus comparison, not a guarantee of future risk.
  </p>
</div>

<div class="section"><h2>Monte Carlo Assumptions</h2>
  {mc_assumptions}
</div>

<div class="section"><h2>📊 Main Chart Pack</h2>
  <p style="color:#64748b;font-size:12px;margin-bottom:12px;">
    Chart panels cover allocation, risk-return, score components, fund Monte Carlo ranges,
    rolling behaviour, correlation, regime behaviour, and active/passive corpus fan.
  </p>
  <img src="data:image/png;base64,{charts_b64.get('main','')}" style="width:100%;border-radius:8px;">
</div>

<div class="section"><h2>📁 Fund Details</h2>{fund_cards}</div>

<div class="section"><h2>💰 Tax Impact</h2>
<table><thead><tr><th>Scenario</th><th>Gross Corpus</th><th>LTCG Tax</th>
  <th>Debt Tax</th><th>Total Tax</th><th>Net Corpus</th></tr></thead>
<tbody>{"".join(f"<tr><td><b>{s.upper()}</b></td><td>{money((tax.get(s) or {}).get('gross',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('eq_tax',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('db_tax',0))}</td><td class='bad'>{money((tax.get(s) or {}).get('total_tax',0))}</td><td class='good'><b>{money((tax.get(s) or {}).get('net',0))}</b></td></tr>" for s in ('p10','p50','p90'))}</tbody>
</table>
<p style="margin-top:12px;font-size:12px;color:#64748b;">
  LTCG exemption used: {money(tax.get('ltcg_exemption',0))} total (₹1.25L/yr × {params['timeline_years']:.0f}Y).
  TER drag estimate vs {PASSIVE_TER_EQUITY:.2f}% passive TER: <b class="bad">{money(tax.get('ter_drag_rs',0))}</b>
  (avg active TER {tax.get('avg_ter',0):.2f}%). This is a fee-only approximation, not a performance forecast.
</p></div>

<div class="section"><h2>⚖️ Active vs Passive Portfolio</h2>
<table><thead><tr><th>Metric</th><th>Active Portfolio</th><th>Passive Basket</th><th>Difference</th></tr></thead>
<tbody>
  <tr><td>P10 Corpus</td><td>{money((tax.get('p10') or {}).get('gross',0))}</td><td>{money(passive_mc.get('p10',0))}</td>
      <td style="color:{adv_col};">{money((tax.get('p10') or {}).get('gross',0) - passive_mc.get('p10',0))}</td></tr>
  <tr><td><b>P50 Corpus</b></td><td><b>{money(total_p50)}</b></td><td><b>{money(passive_mc.get('p50',0))}</b></td>
      <td style="color:{adv_col};font-weight:700;">{money(advantage.get('advantage_rs',0))}</td></tr>
  <tr><td>P90 Corpus</td><td>{money((tax.get('p90') or {}).get('gross',0))}</td><td>{money(passive_mc.get('p90',0))}</td>
      <td style="color:{adv_col};">{money((tax.get('p90') or {}).get('gross',0) - passive_mc.get('p90',0))}</td></tr>
  <tr><td>P10-P90 Range</td><td>{money(active_range)}</td><td>{money(passive_range)}</td>
      <td>{money(active_range - passive_range)}</td></tr>
</tbody></table>
<p style="margin-top:12px;font-size:12px;color:#64748b;">
  Passive basket: {passive_mc.get('equity_split',75)}% Nifty 50 index (TER {PASSIVE_TER_EQUITY:.2f}%) +
  {passive_mc.get('debt_split',25)}% debt (TER {PASSIVE_TER_DEBT:.2f}%, {PASSIVE_DEBT_RETURN:.1f}% p.a.).
  Verdict is based on median simulated corpus only and must be read with the risk metrics above:
  <b>{advantage.get('verdict','-')}</b>.
</p></div>

<div class="section"><h2>🔄 Rebalancing Schedule</h2>
<table><thead><tr><th>Checkpoint</th>{reb_headers}<th>Status</th></tr></thead>
<tbody>{reb_rows}</tbody></table>
<p style="margin-top:10px;font-size:12px;color:#64748b;">
  Rebalance when any fund deviates &gt;5pp from target. Growth-heavy (small/mid cap) funds typically
  drift upward in bull markets.
</p></div>

{_existing_portfolio_html}

{_scorecard_html}

<div class="section"><h2>📋 Methodology & Disclosures</h2>
<ul style="font-size:12px;color:#475569;line-height:2;padding-left:20px;">
  <li><b>Data sources:</b> mfapi.in (NAV history), AMFI NAVAll.txt (official categories + AMC names), mfdata.in (AUM, TER, ratings, return ranks where available), yfinance (Nifty 50 / Midcap benchmarks).</li>
  <li><b>Category benchmarks:</b> Mid cap → {benchmarks_used_str if 'benchmarks_used_str' in dir() else '^NSEMDCP50 or ^NSEI'}; Small cap → ^NSEI (no reliable 10Y smallcap ticker in yfinance).</li>
  <li><b>Survivorship bias:</b> mfapi.in lists active schemes only. Closed/merged funds (usually worst performers) excluded → historical returns overstated by ~0.5%. Addressed via extra 0.5% haircut on all projections.</li>
  <li><b>TER:</b> Uses mf_ter_overrides.json first, then mfdata.in expense ratio, then category defaults.</li>
  <li><b>Scoring:</b> 11 components — ret_rel, alpha, sharpe, bear_alpha, vol, dd, rank_cons, cons, ter, aum, ext_rank. Most components are category-relative; AUM is treated as a liquidity/capacity guardrail, not pure size worship.</li>
  <li><b>Monte Carlo:</b> Bootstrap resampling of fund's own historical monthly returns ({MC_N_SIMS_STANDARD} simulations). Not a forecast — past return distribution may not repeat.</li>
  <li><b>Tax:</b> LTCG 12.5% on equity gains above ₹1.25L/year (FY25-26). Debt: slab rate on full gains (no indexation post Apr 2023). Simplified — actual tax depends on redemption timing.</li>
  <li><b>Exit load:</b> Most equity funds charge 1% if redeemed within 12M of each SIP instalment. SIP investors holding &gt;12M typically pay 0%.</li>
  <li><b>Not SEBI-registered financial advice.</b> Consult a qualified financial planner before investing.</li>
</ul></div>

<div class="footer">
  MF Portfolio Builder v3 &nbsp;|&nbsp; mfapi.in · AMFI · mfdata.in · yfinance
  &nbsp;|&nbsp; Generated {date_str}
</div>
</div></body></html>"""

    html_bytes = html.encode("utf-8")

    def _write_html(fh):
        fh.write(html_bytes)

    html_path = _save_output_file(_write_html, HTML_REPORT_FILE, auto_open=True)
    if html_path != HTML_REPORT_FILE:
        console.print(f"  [yellow]HTML report saved to: {html_path}[/yellow]")
    return html_path


# ══════════════════════════════════════════════════════════════════════
# TERMINAL OUTPUT
# ══════════════════════════════════════════════════════════════════════

def print_portfolio(portfolio: list, params: dict, tax: dict,
                    passive_mc: dict, advantage: dict, req_sip: dict):
    console.print(Panel("[bold white]YOUR PERSONALISED SIP PORTFOLIO[/bold white]",
                        border_style="cyan"))

    monthly_sip    = params["monthly_sip"]
    timeline_years = params["timeline_years"]
    optimise       = params["optimise"]
    total_invested = monthly_sip * timeline_years * 12
    total_p50      = sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"])
                         for f in portfolio)
    net_p50        = (tax.get("p50") or {}).get("net", total_p50)
    gain           = (total_p50 / total_invested - 1) * 100 if total_invested else 0

    # Summary KPIs
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=22); t.add_column("v", style="bold white")
    rows_kpi = [
        ("Monthly SIP",       fmt_inr(monthly_sip)),
        ("Timeline",          f"{timeline_years:.1f} years ({int(timeline_years*12)} months)"),
        ("Strategy",          optimise.title()),
        ("Total invested",    fmt_inr(total_invested)),
        ("P50 gross corpus",  fmt_inr(total_p50)),
        ("P50 net (post-tax)",fmt_inr(net_p50)),
        ("P50 gain",          f"{gain:.1f}%"),
        ("TER drag",          f"{fmt_inr(tax.get('ter_drag_rs',0))} vs passive 0.10% TER"),
        ("vs Passive P50",    f"{fmt_inr(abs(advantage.get('advantage_rs',0)))} "
                              f"— {advantage.get('verdict','—')}"),
    ]
    for k, v in rows_kpi: t.add_row(k, v)
    console.print(t)
    console.print()

    if req_sip:
        console.print(Panel(
            f"  Target corpus: [bold]{fmt_inr(params.get('target_corpus',0))}[/bold]\n"
            f"  Required SIP — P90 (90% confidence): [bold green]{fmt_inr(req_sip.get('p90_sip',0))}[/bold green]/mo\n"
            f"  Required SIP — P50 (median):         [bold]{fmt_inr(req_sip.get('p50_sip',0))}[/bold]/mo\n"
            f"  Required SIP — P10 (optimistic):     [bold cyan]{fmt_inr(req_sip.get('p10_sip',0))}[/bold cyan]/mo",
            title="[bold yellow]GOAL-BASED SIP REQUIREMENT[/bold yellow]",
            border_style="yellow",
        ))

    # Main fund table
    ft = Table(title="Fund Details", border_style="dim",
               show_lines=True, padding=(0, 1))
    for col in ["#","Fund","Cat","SIP","Wt","3Y%","10Y%","α","MaxDD","Up/Dn","TER","AUM","Score","P50"]:
        ft.add_column(col, style="white", no_wrap=True)
    for i, f in enumerate(portfolio, 1):
        mc = f.get("mc_projection") or {}
        ft.add_row(
            str(i), f["name"][:30], f["category"].title(),
            fmt_inr(f["monthly_alloc"]), f"{f['weight_pct']}%",
            pct_str(f.get("return_3y")),
            pct_str(f.get("return_10y")) if f.get("return_10y") is not None else "[dim]N/A[/dim]",
            val_str(f.get("alpha")),
            pct_str(f.get("max_drawdown")),
            f"{f.get('up_capture','?'):.0f}/{f.get('down_capture','?'):.0f}" if f.get("up_capture") else "N/A",
            pct_str(f.get("ter")),
            f"{f.get('aum_cr'):.0f}Cr" if f.get("aum_cr") is not None else "N/A",
            f"{f['score']:.0f}",
            fmt_inr(mc.get("p50", f["projected_corpus"])),
        )
    console.print(ft)
    console.print()

    # MC range
    rt = Table(title="Monte Carlo Range", border_style="dim", padding=(0,1))
    for col in ["Fund","P10","P25","P50","P75","P90"]:
        rt.add_column(col)
    for f in portfolio:
        mc = f.get("mc_projection") or {}
        rt.add_row(f["name"][:28],
                   *[fmt_inr(mc.get(k, 0)) for k in ("p10","p25","p50","p75","p90")])
    console.print(rt)
    console.print()

    # ── Auto-validation panel ─────────────────────────────────────────
    vt = Table(title="Portfolio Confidence (how funds rank in their universe)",
               border_style="dim", padding=(0, 1))
    vt.add_column("Fund", style="white")
    vt.add_column("Category", style="dim", width=12)
    vt.add_column("Rank", justify="center", width=10)
    vt.add_column("Top %", justify="center", width=8)
    vt.add_column("Alpha vs idx", justify="center", width=12)
    vt.add_column("Bear α/mo", justify="center", width=10)
    vt.add_column("Confidence", width=14)

    for f in portfolio:
        cr = f.get("category_rank"); cs = f.get("category_size")
        if cr and cs:
            top_pct = (cs - cr) / cs * 100
            rank_str = f"{cr}/{cs}"
            pct_s = f"{top_pct:.0f}%"
            if top_pct >= 85:  conf = "[bold green]VERY HIGH[/]"
            elif top_pct >= 70: conf = "[green]HIGH[/]"
            elif top_pct >= 50: conf = "[yellow]MODERATE[/]"
            else:               conf = "[red]LOW[/]"
        else:
            rank_str = pct_s = "—"; conf = "—"

        alpha_s = val_str(f.get("alpha")) + "%" if f.get("alpha") is not None else "—"
        bear_s  = val_str(f.get("bear_alpha")) + "%" if f.get("bear_alpha") is not None else "—"
        vt.add_row(f["name"][:32], f["category"].title(), rank_str, pct_s,
                   alpha_s, bear_s, conf)
    console.print(vt)
    console.print()

    console.print("[yellow]NOTES[/yellow]")
    console.print("  • MC bootstraps each fund's own historical monthly returns — not a forecast.")
    console.print("  • LTCG 12.5% above ₹1.25L/yr applied to equity; slab rate to debt.")
    console.print("  • TER drag = cost of active mgmt vs 0.10% index fund over the timeline.")
    console.print("  • ELSS: 3-yr lock-in per instalment. Not SEBI-registered advice.\n")


def pct_str(v) -> str: return f"{v:.1f}%" if v is not None else "—"
def val_str(v, dp=2) -> str: return f"{v:.{dp}f}" if v is not None else "—"


# ══════════════════════════════════════════════════════════════════════
# EXISTING PORTFOLIO TRACKER  (PAN-based, local storage)
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO FETCHER  (OTP via browser or CAS PDF)
# ══════════════════════════════════════════════════════════════════════

def _pip_install(package: str) -> bool:
    """Attempt pip install. Returns True on success."""
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "-q"],
            capture_output=True, text=True, timeout=180,
        )
        return r.returncode == 0
    except Exception:
        return False


def _ensure_playwright() -> bool:
    """Check / install playwright + chromium browser."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        console.print("  [yellow]Installing playwright (first-time, ~1 min)...[/yellow]")
        if not _pip_install("playwright"):
            return False
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       capture_output=True, timeout=300)
        return True
    except Exception:
        return False


def _ensure_casparser() -> bool:
    """Check / install casparser."""
    try:
        import casparser  # noqa: F401
        return True
    except ImportError:
        console.print("  [yellow]Installing casparser...[/yellow]")
        return _pip_install("casparser")


# ── Playwright: headless=False so WAF sees a real browser session ──────

async def _playwright_mfcentral_async(pan: str) -> list:
    """
    Open a persistent Chromium profile on MFCentral (saves cookies between runs).
    - Fixes CAPTCHA: persistent profile looks like a real returning user.
    - Fixes 'active session' error: same cookies = same session, no conflict.
    - Auto-proceeds when portfolio API data is detected (no manual Enter needed).
    """
    import asyncio, threading as _th
    from playwright.async_api import async_playwright  # type: ignore

    # Store profile in user home (~/.mfc_browser_profile) — always writable,
    # avoids Windows Controlled Folder Access restrictions on the project dir.
    profile_dir = os.path.join(os.path.expanduser("~"), ".mfc_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    captured: list = []
    dom_data: list = []

    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--lang=en-IN",
    ]
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    async with async_playwright() as p:
        # Try persistent context first (preserves cookies across runs).
        # Falls back to a fresh context if the profile dir is locked by another process.
        context = None
        try:
            context = await p.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                slow_mo=80,
                args=_LAUNCH_ARGS,
                user_agent=_UA,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as _pe:
            if "ProcessSingleton" in str(_pe) or "profile" in str(_pe).lower():
                console.print(
                    "  [yellow]Browser profile locked (another Chrome using it?) — "
                    "starting a fresh session.[/yellow]"
                )
                browser = await p.chromium.launch(
                    headless=False, slow_mo=80, args=_LAUNCH_ARGS
                )
                context = await browser.new_context(
                    user_agent=_UA, viewport={"width": 1280, "height": 900}
                )
            else:
                raise

        # Override navigator.webdriver so CAPTCHA solvers don't see automation flag
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en', 'hi']});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)

        page = context.pages[0] if context.pages else await context.new_page()

        # ── Intercept ALL JSON responses ──────────────────────────────────
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "json" in ct:
                    data = await response.json()
                    if data:
                        captured.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", on_response)
        await page.goto("https://www.mfcentral.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # ── Detect existing session ───────────────────────────────────────
        page_text = (await page.evaluate("document.body.innerText")).lower()
        active_session_msg = "active session" in page_text or "already logged" in page_text
        already_in = any(kw in page.url.lower()
                         for kw in ("dashboard", "portfolio", "home", "folio", "holdings"))

        if active_session_msg and not already_in:
            # Previous session still alive on the server but we somehow lack cookies.
            # Try navigating directly to the portfolio page first.
            console.print("  [yellow]Server reports active session — trying direct navigation...[/yellow]")
            await page.goto("https://www.mfcentral.com/portfolio", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            already_in = any(kw in page.url.lower()
                             for kw in ("dashboard", "portfolio", "home", "folio", "holdings"))

        if already_in:
            console.print("  [green]Already logged in (session restored from saved profile).[/green]")
        else:
            # Try to auto-fill PAN
            for sel in ['input[id*="pan" i]', 'input[name*="pan" i]',
                        'input[placeholder*="PAN" i]', 'input[type="text"]']:
                try:
                    await page.fill(sel, pan, timeout=2000)
                    break
                except Exception:
                    pass

            console.print(Panel(
                f"[bold white]Complete login in the browser:[/bold white]\n\n"
                f"  1. PAN [bold cyan]{pan}[/bold cyan] may be pre-filled — verify it\n"
                f"  2. Enter your registered mobile / email\n"
                f"  3. Click 'Send OTP' and enter the OTP you receive\n"
                f"  4. Navigate to your [bold]Portfolio / Holdings[/bold] page\n"
                f"  5. Scroll down so all holdings are visible\n\n"
                f"  [dim]Script auto-proceeds when it detects portfolio data.\n"
                f"  Your login session is saved — next run skips this step.[/dim]",
                border_style="blue", title="[bold blue]MFCentral Login[/bold blue]",
            ))

        # ── Smart wait: auto-proceed on data OR manual Enter ─────────────
        enter_evt = _th.Event()
        def _wait_enter():
            try:
                input("\n  >>> Press Enter to extract now, or wait for auto-detect: ")
            except Exception:
                pass
            enter_evt.set()
        _th.Thread(target=_wait_enter, daemon=True).start()

        KEYWORDS = ("folio", "scheme", "units", "nav", "isin",
                    "holding", "investment", "amc", "fund")
        MAX_WAIT_S = 480

        def _has_portfolio_data() -> bool:
            for c in captured:
                snip = str(c.get("data", ""))[:3000].lower()
                if sum(1 for kw in KEYWORDS if kw in snip) >= 3:
                    return True
            return False

        waited, auto_ok = 0, False
        while waited < MAX_WAIT_S:
            await page.wait_for_timeout(3000)
            waited += 3
            if enter_evt.is_set():
                break
            if _has_portfolio_data():
                await page.wait_for_timeout(4000)   # let remaining calls settle
                auto_ok = True
                break
            if waited % 30 == 0:
                console.print(f"  [dim]Waiting... {waited}s, {len(captured)} response(s) captured[/dim]")

        if waited >= MAX_WAIT_S:
            console.print("  [yellow]Timeout — proceeding with data captured so far.[/yellow]")
        elif auto_ok:
            console.print(f"  [green]Portfolio detected ({waited}s) — extracting...[/green]")
        else:
            console.print("  [cyan]Manual proceed — extracting...[/cyan]")

        await page.wait_for_timeout(2000)

        # ── Extract auth token from storage ──────────────────────────────
        token_headers: dict = {}
        try:
            storage = await page.evaluate("""() => {
                const r = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    r['ls_' + k] = localStorage.getItem(k);
                }
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    r['ss_' + k] = sessionStorage.getItem(k);
                }
                return r;
            }""")
            for k, v in storage.items():
                if any(t in k.lower() for t in ("token", "auth", "jwt", "bearer", "access")):
                    token_headers["Authorization"] = f"Bearer {v}"
                    break
        except Exception:
            pass

        # ── Probe direct API endpoints — only if we don't already have data ─
        # Skip if auto-detect already found portfolio data (saves ~48 s of timeouts).
        if token_headers and not auto_ok:
            console.print("  [dim]Probing API endpoints with auth token...[/dim]")
            for ep in [
                "https://www.mfcentral.com/api/portfolio",
                "https://www.mfcentral.com/api/v1/portfolio",
                "https://www.mfcentral.com/api/holdings",
                "https://www.mfcentral.com/api/cas",
            ]:
                try:
                    r = await page.request.get(ep, headers=token_headers, timeout=2000)
                    if r.ok:
                        d = await r.json()
                        if d:
                            captured.append({"url": ep, "data": d, "source": "direct_api"})
                            break   # stop after first successful response
                except Exception:
                    pass

        # ── DOM scrape: tables + Next.js state + card text ────────────────
        try:
            dom_scraped = await page.evaluate("""() => {
                const res = {tables: [], lists: [], next_data: null};
                try { res.next_data = window.__NEXT_DATA__; } catch(e) {}
                document.querySelectorAll('table').forEach(tbl => {
                    const rows = [];
                    tbl.querySelectorAll('tr').forEach(tr => {
                        const cells = [];
                        tr.querySelectorAll('th,td').forEach(td => cells.push(td.innerText.trim()));
                        if (cells.some(c => c)) rows.push(cells);
                    });
                    if (rows.length > 1) res.tables.push(rows);
                });
                document.querySelectorAll(
                    '[class*="scheme"],[class*="folio"],[class*="fund"],'
                    '[class*="holding"],[class*="portfolio"]'
                ).forEach(el => {
                    const t = el.innerText.trim();
                    if (t.length > 20) res.lists.push(t);
                });
                return res;
            }""")
            if dom_scraped:
                dom_data.append({"source": "dom", "data": dom_scraped, "url": page.url})
        except Exception:
            pass

        try:
            dom_data.append({"source": "html", "data": await page.content(), "url": page.url})
        except Exception:
            pass

        # Close context (NOT logout) — cookies persist so next run skips login
        # Close with a timeout guard — persistent profile flush can be slow on Windows
        try:
            await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            pass

    console.print(f"  [green]Captured {len(captured)} API response(s) + {len(dom_data)} DOM snapshot(s).[/green]")
    return captured + dom_data


def _parse_mfcentral_captured(captured: list) -> list:
    """
    Parse raw API responses + DOM snapshots captured from MFCentral browser session.
    Handles: folio JSON schemas, Next.js __NEXT_DATA__, DOM tables, raw HTML.
    """
    import re as _re
    holdings: list = []
    seen: set = set()

    def _add(h: dict):
        key = (h.get("scheme_name", "").lower().strip(), round(h.get("units", 0), 2))
        if key[0] and key not in seen:
            seen.add(key)
            holdings.append(h)

    def _parse_folio_dict(data: dict):
        # Schema A-1: {"folios": [...]}  or  {"data": {"folios": [...]}}
        folio_list = (data.get("folios")
                      or (data.get("data") or {}).get("folios")
                      or (data.get("result") or {}).get("folios")
                      or [])
        for folio in folio_list:
            if not isinstance(folio, dict): continue
            amc = folio.get("amc") or folio.get("amcName") or ""
            for scheme in folio.get("schemes", []) or folio.get("funds", []):
                if not isinstance(scheme, dict): continue
                val = scheme.get("valuation") or scheme.get("currentValuation") or {}
                _add({
                    "scheme_code":   str(scheme.get("amfiCode") or scheme.get("amfi_code") or scheme.get("schemeCode") or ""),
                    "scheme_name":   scheme.get("scheme") or scheme.get("name") or scheme.get("schemeName") or "",
                    "category":      "",
                    "amc":           amc,
                    "isin":          scheme.get("isin") or "",
                    "sip_amount":    0,
                    "units":         _safe_float(scheme.get("units") or scheme.get("close_calculated") or scheme.get("closingBalance")) or 0,
                    "avg_nav":       _safe_float(scheme.get("avg_nav") or scheme.get("average_nav") or scheme.get("avgNav")) or 0,
                    "current_nav":   _safe_float(val.get("nav") or val.get("navValue")) or 0,
                    "current_value": _safe_float(val.get("value") or val.get("currentValue") or val.get("marketValue")) or 0,
                    "start_date":    str(scheme.get("open") or scheme.get("firstPurchaseDate") or "").split("T")[0],
                    "notes":         "",
                })

        # Schema A-2: direct schemes/holdings list under the dict
        for key in ("schemes", "holdings", "funds", "investments"):
            items = data.get(key)
            if isinstance(items, list) and items:
                for rec in items:
                    if not isinstance(rec, dict): continue
                    name = rec.get("schemeName") or rec.get("scheme") or rec.get("fundName") or rec.get("name")
                    if not name: continue
                    val = rec.get("valuation") or rec.get("currentValuation") or {}
                    _add({
                        "scheme_code":   str(rec.get("schemeCode") or rec.get("amfiCode") or ""),
                        "scheme_name":   name,
                        "category":      rec.get("category") or "",
                        "amc":           rec.get("amc") or rec.get("amcName") or "",
                        "isin":          rec.get("isin") or "",
                        "sip_amount":    0,
                        "units":         _safe_float(rec.get("units") or rec.get("closingBalance")) or 0,
                        "avg_nav":       _safe_float(rec.get("avgNav") or rec.get("averageNav") or rec.get("avg_nav")) or 0,
                        "current_nav":   _safe_float(rec.get("nav") or rec.get("currentNav") or val.get("nav")) or 0,
                        "current_value": _safe_float(rec.get("value") or rec.get("currentValue") or val.get("value")) or 0,
                        "start_date":    "",
                        "notes":         "",
                    })

    def _parse_scheme_list(data: list):
        # Schema B: flat list of fund records
        for rec in data:
            if not isinstance(rec, dict): continue
            name = rec.get("fundName") or rec.get("schemeName") or rec.get("scheme") or rec.get("name")
            if not name: continue
            _add({
                "scheme_code":   str(rec.get("schemeCode") or rec.get("amfiCode") or ""),
                "scheme_name":   name,
                "category":      rec.get("category") or "",
                "amc":           rec.get("amc") or rec.get("amcName") or "",
                "isin":          rec.get("isin") or "",
                "sip_amount":    0,
                "units":         _safe_float(rec.get("units") or rec.get("closingBalance")) or 0,
                "avg_nav":       _safe_float(rec.get("avgNav") or rec.get("averageNav") or rec.get("avg_nav")) or 0,
                "current_nav":   _safe_float(rec.get("nav") or rec.get("currentNav")) or 0,
                "current_value": _safe_float(rec.get("value") or rec.get("currentValue")) or 0,
                "start_date":    "",
                "notes":         "",
            })

    def _walk_json(obj, depth=0):
        """Recursively search any JSON structure for folio/scheme data."""
        if depth > 6: return
        if isinstance(obj, dict):
            if obj.get("folios") or obj.get("schemes") or obj.get("holdings") or obj.get("funds"):
                _parse_folio_dict(obj)
            for v in obj.values():
                _walk_json(v, depth + 1)
        elif isinstance(obj, list) and obj:
            first = obj[0]
            if isinstance(first, dict):
                keys = set(first.keys())
                fund_keys = {"schemeName", "scheme", "fundName", "name", "units", "nav"}
                if keys & fund_keys:
                    _parse_scheme_list(obj)
                    return
            for v in obj:
                _walk_json(v, depth + 1)

    def _parse_dom_tables(tables: list):
        """Extract holdings from HTML tables scraped via JS."""
        NAV_RE = _re.compile(r'\d+\.\d{2,4}')
        for rows in tables:
            if len(rows) < 2: continue
            header = [c.lower() for c in rows[0]]
            fund_col = next((i for i, h in enumerate(header)
                             if any(k in h for k in ("scheme", "fund", "name"))), None)
            if fund_col is None: continue
            units_col = next((i for i, h in enumerate(header) if "unit" in h), None)
            nav_col   = next((i for i, h in enumerate(header) if "nav" in h), None)
            val_col   = next((i for i, h in enumerate(header)
                              if any(k in h for k in ("value", "amount", "worth"))), None)
            for row in rows[1:]:
                if len(row) <= fund_col: continue
                name = row[fund_col].strip()
                if not name or len(name) < 5: continue
                units = _safe_float(row[units_col]) if units_col and units_col < len(row) else 0
                nav   = _safe_float(row[nav_col])   if nav_col   and nav_col   < len(row) else 0
                val   = _safe_float(row[val_col])   if val_col   and val_col   < len(row) else 0
                _add({
                    "scheme_code": "", "scheme_name": name, "category": "",
                    "amc": "", "isin": "", "sip_amount": 0,
                    "units": units or 0, "avg_nav": 0, "current_nav": nav or 0,
                    "current_value": val or 0, "start_date": "", "notes": "dom_table",
                })

    # ── Process each captured item ────────────────────────────────────────
    for item in captured:
        source = item.get("source", "api")
        data   = item.get("data")

        if source == "dom" and isinstance(data, dict):
            # Next.js hydration payload
            nd = data.get("next_data")
            if nd:
                _walk_json(nd)
            # DOM tables
            _parse_dom_tables(data.get("tables") or [])
            # Card/list text — minimal extraction (fund name + value patterns)
            for text_block in (data.get("lists") or []):
                lines = [l.strip() for l in text_block.splitlines() if l.strip()]
                if len(lines) >= 2:
                    name_candidate = lines[0]
                    if len(name_candidate) > 8 and not name_candidate[0].isdigit():
                        _add({
                            "scheme_code": "", "scheme_name": name_candidate,
                            "category": "", "amc": "", "isin": "",
                            "sip_amount": 0, "units": 0, "avg_nav": 0,
                            "current_nav": 0, "current_value": 0,
                            "start_date": "", "notes": "dom_card",
                        })

        elif source == "html" and isinstance(data, str):
            # BeautifulSoup parse of full HTML
            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(data, "html.parser")
                for tbl in soup.find_all("table"):
                    rows_raw = []
                    for tr in tbl.find_all("tr"):
                        cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                        if cells: rows_raw.append(cells)
                    _parse_dom_tables([rows_raw])
            except ImportError:
                pass

        else:
            # API / direct JSON response — deep walk
            _walk_json(data)

    return [h for h in holdings if h.get("scheme_name")]


def _parse_cas_pdf(pdf_path: str) -> list:
    """Parse a CAMS or KFintech CAS PDF using casparser."""
    from casparser import read_cas_pdf  # type: ignore

    console.print(Panel(
        "CAS PDF password:\n"
        "  CAMS:     first 4 chars of registered email (UPPERCASE) + DOB as DDMMYYYY\n"
        "  KFintech: PAN first 4 chars (UPPERCASE) + DOB as DDMMYYYY\n"
        "  Example:  ABCD01012000",
        border_style="dim", title="[dim]Password format[/dim]",
    ))
    pw = Prompt.ask("  [green]PDF password (Enter to try blank)[/green]",
                    default="", password=True)

    data = read_cas_pdf(pdf_path, pw)
    holdings: list = []
    for folio in data.get("folios", []):
        amc = folio.get("amc", "")
        for scheme in folio.get("schemes", []):
            val  = scheme.get("valuation") or {}
            # casparser uses 'close' for current units, 'open' for start units
            units = (_safe_float(scheme.get("close"))
                     or _safe_float(scheme.get("units")) or 0)
            holdings.append({
                "scheme_code":   str(scheme.get("amfi_code") or ""),
                "scheme_name":   scheme.get("scheme", ""),
                "category":      "",
                "amc":           amc,
                "isin":          scheme.get("isin", ""),
                "sip_amount":    0,
                "units":         units,
                "avg_nav":       _safe_float(scheme.get("avg_nav")) or 0,
                "current_nav":   _safe_float(val.get("nav")) or 0,
                "current_value": _safe_float(val.get("value")) or 0,
                "start_date":    str(scheme.get("open", "")).split("T")[0],
                "notes":         "",
            })
    return [h for h in holdings if h.get("scheme_name")]


def fetch_portfolio_external(pan: str, amfi_master: dict) -> list:
    """
    Fetch existing MF portfolio via one of three methods:
      1. Browser automation  — opens real Chromium, user logs in at MFCentral with OTP
      2. CAS PDF parsing     — parse PDF downloaded from CAMS/KFintech
      3. CSV import          — Kuvera / Groww / Coin export (handled by import_from_csv)

    Returns a list of holding dicts (same schema as manual entry).
    MFCentral and CAMS block direct REST API calls via WAF; the browser method
    is the only reliable programmatic approach that bypasses this.
    """
    import asyncio

    method = rich_ask_choice(
        "Choose fetch method:",
        [
            "Browser (recommended) — opens MFCentral in Chromium, you enter OTP there",
            "CAS PDF — you download PDF from CAMS / KFintech, we parse it",
            "CSV — import Kuvera / Groww / Coin export file",
        ],
        hint="All three work. Browser is most automated; PDF is most reliable.",
    )

    holdings: list = []

    if method == 0:  # Browser / Playwright
        console.print(Panel(
            "[dim]Note: MFCentral and CAMS block direct API calls via WAF.\n"
            "Opening a real browser is the only way to bypass this.[/dim]",
            border_style="dim",
        ))
        if not _ensure_playwright():
            console.print(Panel(
                "[red]Could not install Playwright automatically.[/red]\n\n"
                "Install manually:\n"
                "  pip install playwright\n"
                "  playwright install chromium\n\n"
                "Then restart the script.",
                border_style="red",
            ))
            return []
        try:
            captured = asyncio.run(_playwright_mfcentral_async(pan))
            holdings = _parse_mfcentral_captured(captured)
            if holdings:
                console.print(f"  [green]{len(holdings)} holdings captured from MFCentral.[/green]")
            else:
                console.print("  [yellow]No portfolio data captured.[/yellow]")
                console.print("  [dim]The page structure may have changed — try PDF method.[/dim]")
        except Exception as e:
            console.print(f"  [red]Browser automation error: {e}[/red]")
            console.print("  [dim]Fallback: use PDF or CSV method.[/dim]")

    elif method == 1:  # CAS PDF
        console.print(Panel(
            "[bold white]Download your CAS PDF first:[/bold white]\n\n"
            "  CAMS   : https://www.camsonline.com → Investors → Statements → Consolidated\n"
            "  KFintech: https://mfs.kfintech.com → Investor Login → Statements\n\n"
            "  Select: Type = Detailed, Period = Since Inception\n"
            "  The PDF will be emailed to your registered address.",
            border_style="cyan", title="[bold]Step 1: Download CAS[/bold]",
        ))
        pdf_path = Prompt.ask("  [green]Enter path to CAS PDF[/green]").strip().strip('"')
        if not os.path.exists(pdf_path):
            console.print(f"  [red]File not found: {pdf_path}[/red]")
            return []
        if not _ensure_casparser():
            console.print(Panel(
                "[red]Could not install casparser.[/red]\n"
                "Run: pip install casparser",
                border_style="red",
            ))
            return []
        try:
            holdings = _parse_cas_pdf(pdf_path)
            console.print(f"  [green]Parsed {len(holdings)} holdings from PDF.[/green]")
        except Exception as e:
            console.print(f"  [red]PDF parse error: {e}[/red]")

    else:  # CSV
        csv_path = Prompt.ask("  [green]Enter path to CSV file[/green]").strip().strip('"')
        holdings = import_from_csv(csv_path)

    # Classify categories
    for h in holdings:
        if not h.get("category"):
            h["category"] = classify_fund(
                h.get("scheme_name", ""), h.get("scheme_code", ""), amfi_master)

    return holdings


# ══════════════════════════════════════════════════════════════════════

def hash_pan(pan: str) -> str:
    """SHA-256 of PAN (first 16 hex chars) — PAN never stored in plaintext."""
    return hashlib.sha256(pan.upper().strip().encode()).hexdigest()[:16]


def load_portfolio_db() -> dict:
    if not os.path.exists(PORTFOLIO_DB_FILE):
        return {"version": 1, "portfolios": {}}
    try:
        with open(PORTFOLIO_DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "portfolios": {}}


def save_portfolio_db(db: dict) -> None:
    import time, tempfile
    ts = datetime.now().strftime("%H%M%S")
    candidates = [
        PORTFOLIO_DB_FILE,
        PORTFOLIO_DB_FILE.replace(".json", f"_{ts}.json"),
        os.path.join(tempfile.gettempdir(), f"mf_portfolio_db_{ts}.json"),
    ]
    for attempt, target in enumerate(candidates):
        try:
            # Write via temp file + atomic rename to avoid partial writes
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(target), suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2)
                os.replace(tmp_path, target)
            except Exception:
                try: os.unlink(tmp_path)
                except Exception: pass
                raise
            if target != PORTFOLIO_DB_FILE:
                console.print(f"  [yellow]Portfolio DB saved to: {target}[/yellow]")
            return
        except PermissionError:
            time.sleep(0.4)
            continue
    console.print("  [red]Could not save portfolio DB — all paths denied.[/red]")


def get_user_portfolio(pan_hash: str) -> dict:
    db = load_portfolio_db()
    return db["portfolios"].get(pan_hash, {"holdings": [], "last_updated": ""})


def save_user_portfolio(pan_hash: str, user_port: dict) -> None:
    db = load_portfolio_db()
    user_port["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    db["portfolios"][pan_hash] = user_port
    save_portfolio_db(db)


def _serialise_plan_item(item: dict, kind: str) -> dict:
    """Strip non-serialisable objects (pandas Series, full fund dicts) for storage."""
    def _fund_stub(f: dict | None) -> dict:
        if not f: return {}
        return {k: f.get(k) for k in
                ("name","scheme_code","category","score","category_rank","category_size",
                 "amc_name","return_3y","info_ratio")}
    if kind == "switch":
        return {
            "kind":           "switch",
            "from_name":      item["from"].get("scheme_name",""),
            "from_code":      item["from"].get("scheme_code",""),
            "from_rank":      item["from"].get("category_rank"),
            "from_score":     item["from"].get("score"),
            "to":             _fund_stub(item.get("to")),
            "exit_load_warn": item.get("exit_load_warn", False),
            "ltcg_rs":        item.get("ltcg_rs", 0),
            "tax_rs":         item.get("tax_rs", 0),
            "status":         "pending",
        }
    if kind == "gap_fill":
        return {
            "kind":     "gap_fill",
            "slot":     item.get("slot",""),
            "category": item.get("category",""),
            "fund":     _fund_stub(item.get("fund")),
            "sip":      item.get("sip", 0),
            "gap_pct":  item.get("gap_pct", 0),
            "status":   "pending",
        }
    if kind == "buy_more":
        h = item.get("holding", {})
        return {
            "kind":       "buy_more",
            "fund_name":  h.get("scheme_name",""),
            "fund_code":  h.get("scheme_code",""),
            "extra_sip":  item.get("extra_sip", 0),
            "gap_pct":    item.get("gap_pct", 0),
            "status":     "pending",
        }
    return {}


def save_action_plan(pan_hash: str, plan: dict) -> None:
    """Persist the action plan items with 'pending' status into the portfolio DB."""
    items = []
    for sw in plan.get("switches", []):
        items.append(_serialise_plan_item(sw, "switch"))
    for gf in plan.get("gap_fills", []):
        items.append(_serialise_plan_item(gf, "gap_fill"))
    for bm in plan.get("buy_mores", []):
        items.append(_serialise_plan_item(bm, "buy_more"))

    db = load_portfolio_db()
    port = db["portfolios"].setdefault(pan_hash, {"holdings": [], "last_updated": ""})
    port["action_plan"] = {
        "generated_on": datetime.now().strftime("%Y-%m-%d"),
        "strategy":     plan.get("strategy", "balanced"),
        "items":        items,
    }
    db["portfolios"][pan_hash] = port
    save_portfolio_db(db)


def load_action_plan(pan_hash: str) -> dict:
    """Return the stored action plan for this PAN, or empty dict."""
    return get_user_portfolio(pan_hash).get("action_plan", {})


def update_action_plan_status(pan_hash: str, item_index: int, status: str) -> None:
    """Mark a stored action plan item as 'done' or 'dismissed'."""
    db   = load_portfolio_db()
    port = db["portfolios"].get(pan_hash, {})
    plan = port.get("action_plan", {})
    items = plan.get("items", [])
    if 0 <= item_index < len(items):
        items[item_index]["status"] = status
        plan["items"] = items
        port["action_plan"] = plan
        db["portfolios"][pan_hash] = port
        save_portfolio_db(db)


def show_pending_action_plan(pan_hash: str) -> bool:
    """
    On startup, show any pending items from the previous run.
    Returns True if pending items exist (caller can offer to mark done).
    """
    plan = load_action_plan(pan_hash)
    if not plan:
        return False
    items     = plan.get("items", [])
    pending   = [it for it in items if it.get("status") == "pending"]
    done      = [it for it in items if it.get("status") == "done"]
    dismissed = [it for it in items if it.get("status") == "dismissed"]
    if not pending:
        return False

    generated = plan.get("generated_on", "?")
    console.print(Panel(
        f"[bold yellow]⚡ {len(pending)} pending action(s) from {generated}[/bold yellow]\n"
        f"[dim]{len(done)} completed · {len(dismissed)} dismissed[/dim]",
        title="[bold]Saved Action Plan[/bold]",
        border_style="yellow",
    ))

    KIND_LABEL = {"switch": "SWITCH", "gap_fill": "ADD NEW", "buy_more": "INCREASE"}
    KIND_COL   = {"switch": "yellow", "gap_fill": "cyan",    "buy_more": "green"}
    for i, it in enumerate(items):
        if it.get("status") != "pending":
            continue
        kind  = it.get("kind","?")
        col   = KIND_COL.get(kind, "white")
        label = KIND_LABEL.get(kind, kind.upper())
        if kind == "switch":
            desc = (f"{it.get('from_name','?')[:36]} → "
                    f"{(it.get('to') or {}).get('name','?')[:30]}")
        elif kind == "gap_fill":
            desc = (f"{it.get('slot','?')} gap — "
                    f"add {fmt_inr(it.get('sip',0))}/mo to "
                    f"{(it.get('fund') or {}).get('name','?')[:30]}")
        else:
            desc = (f"{it.get('fund_name','?')[:36]} — "
                    f"add {fmt_inr(it.get('extra_sip',0))}/mo")
        console.print(f"  [{col}]{i+1}. [{label}][/{col}]  {desc}")

    console.print()
    update_choice = rich_ask_choice(
        "Mark any items as done?",
        ["No — keep all pending",
         "Mark specific items done",
         "Mark all done",
         "Dismiss all (ignore these recommendations)"],
        default=0,
    )
    if update_choice == 1:
        raw = Prompt.ask("  Enter item numbers to mark done (e.g. 1,3)").strip()
        for tok in raw.split(","):
            try:
                idx = int(tok.strip()) - 1
                update_action_plan_status(pan_hash, idx, "done")
            except (ValueError, IndexError):
                pass
        console.print("  [green]Updated.[/green]")
    elif update_choice == 2:
        for i, it in enumerate(items):
            if it.get("status") == "pending":
                update_action_plan_status(pan_hash, i, "done")
        console.print("  [green]All items marked done.[/green]")
    elif update_choice == 3:
        for i, it in enumerate(items):
            if it.get("status") == "pending":
                update_action_plan_status(pan_hash, i, "dismissed")
        console.print("  [dim]All dismissed.[/dim]")

    return True


def import_from_csv(filepath: str) -> list:
    """
    Import holdings from a CSV file.
    Supports Kuvera / Groww / Coin export formats.
    Expected columns (flexible): Fund Name, Scheme Code, Units,
      Average NAV / Avg Cost, Current NAV / NAV, Current Value, SIP Amount.
    Returns list of holding dicts.
    """
    import csv
    holdings = []
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalise column names
                rn = {k.strip().lower().replace(" ","_"): v.strip() for k, v in row.items()}

                def pick(*keys):
                    for k in keys:
                        v = rn.get(k) or rn.get(k.replace("_"," "))
                        if v and v not in ("", "-", "N/A"): return v
                    return ""

                name      = pick("fund_name","scheme_name","fund","name")
                code      = pick("scheme_code","amfi_code","code")
                avg_nav   = _safe_float(pick("average_nav","avg_nav","avg_cost","average_cost"))
                cur_nav   = _safe_float(pick("current_nav","nav","latest_nav"))
                units     = _safe_float(pick("units","quantity"))
                cur_val   = _safe_float(pick("current_value","value","current_amount"))
                sip_amt   = _safe_float(pick("sip_amount","monthly_sip","sip"))
                start_dt  = pick("start_date","investment_date","since")

                if not name: continue
                holdings.append({
                    "scheme_code":   code,
                    "scheme_name":   name,
                    "category":      "",        # filled later via classify_fund
                    "sip_amount":    int(sip_amt) if sip_amt else 0,
                    "lump_sum":      0,
                    "units":         units or 0,
                    "avg_nav":       avg_nav or 0,
                    "current_nav":   cur_nav or 0,
                    "current_value": cur_val or 0,
                    "start_date":    start_dt,
                    "notes":         "",
                })
    except Exception as e:
        console.print(f"  [red]CSV import error: {e}[/red]")
    return holdings


def _holding_action(category_rank: int, category_size: int,
                    fund_age: float, aum_warn: bool) -> tuple:
    """
    Return (action, colour, reason) based on universe rank.
    """
    top_pct = (category_size - category_rank) / category_size * 100
    if top_pct >= 80:
        action, col = "BUY MORE",  "bold green"
        reason = f"Top {100-top_pct:.0f}% of {category_size} funds — strong conviction"
    elif top_pct >= 60:
        action, col = "HOLD",      "green"
        reason = f"Above median (rank {category_rank}/{category_size}) — keep SIP"
    elif top_pct >= 40:
        action, col = "HOLD/WATCH","yellow"
        reason = f"Mid-table (rank {category_rank}/{category_size}) — monitor quarterly"
    elif top_pct >= 20:
        action, col = "SWITCH",    "bold yellow"
        reason = f"Below median (rank {category_rank}/{category_size}) — consider top-ranked peer"
    else:
        action, col = "EXIT",      "bold red"
        reason = f"Bottom quintile (rank {category_rank}/{category_size}) — switch immediately"

    if aum_warn and action == "BUY MORE":
        action, col = "HOLD", "yellow"
        reason += " (AUM capacity warning — don't add more)"
    if (fund_age or 0) < 3 and action == "BUY MORE":
        action, col = "HOLD", "yellow"
        reason += " (fund <3Y old — insufficient track record)"
    return action, col, reason


def analyse_holdings(
    user_holdings: list,
    all_scored: list,     # full universe scored during build_portfolio
    nav_data: list,
    amfi_master: dict,
) -> list:
    """
    For each user holding, find it in the scored universe and compute:
    • Current NAV, unrealised gain, simple CAGR
    • Category rank and percentile
    • Hold / Sell / Buy recommendation
    Returns enriched holding dicts.
    """
    # Build lookups: by numeric scheme_code, by ISIN (from amfi_master), by name
    by_code = {str(f["scheme_code"]): f for f in all_scored}
    by_name = {f["name"].lower(): f for f in all_scored}

    # ISIN → numeric AMFI code (amfi_master is keyed by numeric code; isin is in value)
    isin_to_code: dict = {v["isin"]: k
                          for k, v in amfi_master.items() if v.get("isin")}

    # Current NAV lookup (by numeric scheme_code)
    nav_lookup: dict = {}
    for item in nav_data:
        code = str(item["scheme"].get("schemeCode", ""))
        nav_lookup[code] = float(item["nav_df"]["nav"].iloc[-1])

    results = []
    for h in user_holdings:
        raw_code = str(h.get("scheme_code", ""))
        isin     = str(h.get("isin", "") or "").upper()
        name     = h.get("scheme_name", "")

        # ISIN may be stored in scheme_code field (MFCentral DOM scrape)
        looks_isin = raw_code.startswith("INF") or (len(raw_code) > 8 and raw_code[:2].isupper())
        if looks_isin and not isin:
            isin = raw_code.upper()

        # Resolve to numeric AMFI scheme_code via amfi_master reverse map
        code = raw_code
        if looks_isin or not by_code.get(raw_code):
            resolved = isin_to_code.get(isin)
            if resolved:
                code = resolved

        # 1. Exact numeric scheme_code match
        scored_fund = by_code.get(code)
        # 2. Fuzzy name match as fallback
        if not scored_fund:
            name_l = name.lower()
            scored_fund = next(
                (f for fname, f in by_name.items()
                 if name_l[:22] in fname or fname[:22] in name_l), None)

        # Current NAV: prefer nav_lookup (live from cache), fall back to stored value
        current_nav = nav_lookup.get(code) or h.get("current_nav") or 0
        avg_nav     = h.get("avg_nav") or 0
        units       = h.get("units") or 0
        cur_value   = units * current_nav if units and current_nav else h.get("current_value") or 0
        sip_amt     = h.get("sip_amount") or 0

        gain_pct = ((current_nav / avg_nav) - 1) * 100 if (avg_nav and avg_nav > 0 and current_nav) else None
        start    = h.get("start_date","")
        try:
            years = (datetime.now() - datetime.fromisoformat(start)).days / 365.25 if start else None
        except Exception:
            years = None
        holding_cagr = cagr(avg_nav, current_nav, years) if (avg_nav and current_nav and years and years > 0.25) else None

        # Recommendation
        if scored_fund:
            cat       = scored_fund.get("category", amfi_master.get(code, {}).get("sebi_category","other"))
            cat_rank  = scored_fund.get("category_rank")
            cat_size  = scored_fund.get("category_size")
            age       = scored_fund.get("fund_age_years", 0)
            aum_warn  = scored_fund.get("aum_warn", False)
            score     = scored_fund.get("score", 0)
            alpha     = scored_fund.get("alpha")
            action, col, reason = _holding_action(cat_rank or 1, cat_size or 1, age, aum_warn)
        else:
            cat = classify_fund(name, code, amfi_master)
            cat_rank = cat_size = score = alpha = None
            action, col, reason = "UNKNOWN", "dim", "Fund not found in universe — add to cache"

        results.append({
            **h,
            "scheme_name":    name,
            "category":       cat,
            "current_nav":    round(current_nav, 2) if current_nav else None,
            "current_value":  round(cur_value, 0) if cur_value else None,
            "gain_pct":       round(gain_pct, 1) if gain_pct is not None else None,
            "holding_cagr":   round(holding_cagr, 1) if holding_cagr is not None else None,
            "years_held":     round(years, 1) if years else None,
            "category_rank":  cat_rank,
            "category_size":  cat_size,
            "score":          score,
            "alpha":          alpha,
            "action":         action,
            "action_col":     col,
            "action_reason":  reason,
        })
    return results


def display_holdings_analysis(analysis: list) -> None:
    """Rich terminal display of existing portfolio with hold/sell/buy."""
    console.print(Panel("[bold white]YOUR EXISTING PORTFOLIO ANALYSIS[/bold white]",
                        border_style="blue"))

    ht = Table(border_style="dim", show_lines=True, padding=(0, 1))
    for col in ["Fund", "Cat", "SIP/mo", "Cur Value", "CAGR", "Rank", "Score", "Action"]:
        ht.add_column(col, style="white")

    for h in analysis:
        cr, cs = h.get("category_rank"), h.get("category_size")
        rank_s = f"{cr}/{cs}" if cr and cs else "N/A"
        col    = h.get("action_col","white")
        ht.add_row(
            h["scheme_name"][:32],
            h.get("category","—")[:12],
            fmt_inr(h.get("sip_amount") or 0),
            fmt_inr(h.get("current_value") or 0),
            pct_str(h.get("holding_cagr")),
            rank_s,
            f"{h['score']:.0f}" if h.get("score") else "—",
            f"[{col}]{h['action']}[/{col}]",
        )
    console.print(ht)
    console.print()

    # Action details
    for h in analysis:
        col = h.get("action_col","white")
        console.print(f"  [{col}]{h['action']:12s}[/{col}] {h['scheme_name'][:40]}")
        console.print(f"  [dim]           {h['action_reason']}[/dim]")
    console.print()


_JUNK_NAMES = {
    "below 100", "100 - 200", "200 - 300", "300 - 500", "500 - 1000",
    "more than 1000", "blue", "yellow", "brown", "red", "green",
    "low", "moderate", "moderately low", "moderately high", "high",
    "very high", "low to moderate", "na", "n/a", "total", "grand total",
    "folio number", "scheme name", "nav", "units", "value",
}

def _clean_holdings(holdings: list) -> list:
    """Remove UI artefacts and zero-value junk scraped from browser DOM."""
    cleaned = []
    for h in holdings:
        name = (h.get("scheme_name") or "").strip()
        if not name:
            continue
        if name.lower() in _JUNK_NAMES:
            continue
        # Drop entries that are clearly not fund names (short, purely numeric, or colour words)
        if len(name) < 6:
            continue
        if name.replace("-","").replace(" ","").replace(",","").isdigit():
            continue
        cleaned.append(h)
    return cleaned


# ── Broad category normaliser ─────────────────────────────────────────────
# Maps fine-grained SEBI categories → slot-level buckets for gap analysis.
_SLOT_KEYWORDS = {
    "aggressive": [
        ("small cap",                                         ["small cap"]),
        ("mid cap",                                           ["mid cap"]),
        ("flexi / multi / focused",                          ["flexi cap","multi cap","focused"]),
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("value / elss",                                     ["value","elss","contra"]),
    ],
    "balanced": [
        ("flexi / multi",                                     ["flexi cap","multi cap"]),
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("mid cap",                                           ["mid cap"]),
        ("small cap",                                         ["small cap"]),
        ("debt",                                              ["debt","short duration","low duration",
                                                               "ultra short","money market",
                                                               "corporate bond","banking and psu"]),
    ],
    "conservative": [
        ("large cap / large & mid",                          ["large cap","large & mid"]),
        ("flexi / multi",                                     ["flexi cap","multi cap"]),
        ("debt",                                              ["debt","short duration","low duration",
                                                               "ultra short","money market",
                                                               "corporate bond","banking and psu"]),
        ("mid / value",                                       ["mid cap","value"]),
    ],
}


def _holding_slot(category: str, strategy: str) -> str:
    """Map a holding's SEBI category to the slot label for the given strategy."""
    cat_l = (category or "").lower()
    for slot_label, kws in _SLOT_KEYWORDS.get(strategy, _SLOT_KEYWORDS["balanced"]):
        if any(k in cat_l for k in kws):
            return slot_label
    return "other"


def find_replacement_fund(category: str, all_scored: list,
                           held_codes: set, held_amcs: set) -> dict | None:
    """
    Return the top-scoring fund in `category` that the user doesn't already hold.
    Prefers a different AMC from those already held to maintain diversification.
    """
    cat_l = category.lower()
    candidates = [
        f for f in all_scored
        if (f.get("category") or "").lower() == cat_l
        and str(f.get("scheme_code", "")) not in held_codes
        and f.get("category_rank", 99) <= 10
    ]
    # Prefer different AMC
    diff_amc = [f for f in candidates
                if (f.get("amc_name","").split()[0].upper() not in held_amcs)]
    return (diff_amc or candidates or [None])[0]


def generate_action_plan(
    holdings_analysis: list,
    all_scored: list,
    strategy: str,
    monthly_sip: int,
    nav_data: list,
    new_portfolio: list = None,
) -> dict:
    """
    Unified action plan bridging existing portfolio management and new value addition.

    Produces five prioritised action groups:
      SWITCH  — underperforming funds with a specific replacement named
      GAP FILL — categories with no coverage; exact fund + SIP amount
      BUY MORE — existing top funds that are underweight; exact ₹ top-up
      HARVEST  — LTCG tax harvest (reuse compute_tax_harvest_plan output)
      HOLD     — funds requiring no action
      OVERLAP  — duplicate category warnings
    """
    held_codes = {str(h.get("scheme_code","")) for h in holdings_analysis}
    held_amcs  = {(h.get("amc","") or "").split()[0].upper()
                  for h in holdings_analysis if h.get("amc")}

    # ── Current allocation by slot ────────────────────────────────────────
    slots = _SLOT_KEYWORDS.get(strategy, _SLOT_KEYWORDS["balanced"])
    weights = ALLOCATION_WEIGHTS.get(strategy, [25,20,20,15,20])
    target = {label: weights[i] for i, (label, _) in enumerate(slots)
              if i < len(weights)}

    total_value = sum(h.get("current_value") or 0 for h in holdings_analysis)
    total_sip   = sum(h.get("sip_amount")    or 0 for h in holdings_analysis) or monthly_sip

    slot_value: dict = {}
    slot_holdings: dict = {}
    for h in holdings_analysis:
        slot = _holding_slot(h.get("category",""), strategy)
        slot_value[slot]    = slot_value.get(slot, 0) + (h.get("current_value") or 0)
        slot_holdings.setdefault(slot, []).append(h)

    # ── Gap analysis per slot ─────────────────────────────────────────────
    gaps: dict = {}
    for label, tgt_pct in target.items():
        cur_val  = slot_value.get(label, 0)
        cur_pct  = (cur_val / total_value * 100) if total_value else 0
        gap_pct  = tgt_pct - cur_pct
        gaps[label] = {
            "target_pct":  tgt_pct,
            "current_pct": round(cur_pct, 1),
            "gap_pct":     round(gap_pct, 1),
            "sip_gap":     max(0, round(monthly_sip * gap_pct / 100)),
            "status":      ("UNCOVERED" if cur_pct < 1
                            else ("UNDER" if gap_pct > 7
                                  else ("OVER" if gap_pct < -7 else "OK"))),
        }

    # ── Overlap detection (two holdings in same fine-grained category) ────
    cat_counts: dict = {}
    for h in holdings_analysis:
        c = h.get("category","other")
        cat_counts.setdefault(c, []).append(h.get("scheme_name","?"))
    overlaps = [{"category": c, "funds": names}
                for c, names in cat_counts.items() if len(names) > 1]

    # ── Cross-reference with new portfolio recommendation ─────────────────
    new_portfolio = new_portfolio or []
    new_codes = {str(f.get("scheme_code","")) for f in new_portfolio}
    already_recommended = [h for h in holdings_analysis
                           if str(h.get("scheme_code","")) in new_codes]

    # ── Build action groups ───────────────────────────────────────────────
    switches, gap_fills, buy_mores, holds = [], [], [], []

    for h in holdings_analysis:
        action = h.get("action","HOLD")
        cat    = h.get("category","other")
        yr     = h.get("years_held") or 0
        gain   = h.get("gain_pct")
        avg_n  = h.get("avg_nav") or 0
        cur_n  = h.get("current_nav") or 0
        units  = h.get("units") or 0

        if action in ("SWITCH", "EXIT"):
            replacement = find_replacement_fund(cat, all_scored, held_codes, held_amcs)
            exit_load   = yr < 1.0        # most equity funds: 1% if < 1Y
            ltcg_rs     = 0
            if avg_n and cur_n and units and cur_n > avg_n:
                ltcg_rs = round(units * (cur_n - avg_n))
            tax_rs = round(max(0, ltcg_rs - LTCG_EXEMPTION_PER_YEAR) * 0.125)
            switches.append({
                "from":          h,
                "to":            replacement,
                "exit_load_warn": exit_load,
                "ltcg_rs":       ltcg_rs,
                "tax_rs":        tax_rs,
                "priority":      1 if action == "EXIT" else 2,
            })

        elif action == "BUY MORE":
            slot      = _holding_slot(cat, strategy)
            gap_info  = gaps.get(slot, {})
            extra_sip = gap_info.get("sip_gap", 0)
            buy_mores.append({
                "holding":     h,
                "extra_sip":   extra_sip or round(monthly_sip * 0.10),
                "slot":        slot,
                "gap_pct":     gap_info.get("gap_pct", 0),
            })

        else:
            holds.append(h)

    # Gap fill: slots with no coverage at all
    for label, info in gaps.items():
        if info["status"] == "UNCOVERED" and info["sip_gap"] > 0:
            # Find matching category keyword for this slot
            kws    = next((kw for lbl, kw in slots if lbl == label), [label])
            top_cat = kws[0] if kws else label
            fund   = find_replacement_fund(top_cat, all_scored, held_codes, held_amcs)
            if fund:
                gap_fills.append({
                    "slot":      label,
                    "category":  top_cat,
                    "fund":      fund,
                    "sip":       info["sip_gap"] or round(monthly_sip * 0.15),
                    "gap_pct":   info["gap_pct"],
                })
                held_codes.add(str(fund.get("scheme_code","")))   # avoid duplicate suggestions

    return {
        "switches":            sorted(switches, key=lambda x: x["priority"]),
        "gap_fills":           gap_fills,
        "buy_mores":           buy_mores,
        "holds":               holds,
        "gaps":                gaps,
        "overlaps":            overlaps,
        "already_recommended": already_recommended,
        "total_value":         total_value,
        "total_sip":           total_sip,
        "strategy":            strategy,
    }


def print_action_plan(plan: dict) -> None:
    """Print the complete prioritised action plan to the terminal."""
    console.print("\n")
    console.print(Panel(
        "[bold white]COMPLETE INVESTMENT ACTION PLAN[/bold white]\n"
        "[dim]Bridges your existing portfolio with your target strategy.[/dim]",
        border_style="bright_cyan",
    ))

    step = 0

    def _step(label: str, colour: str, title: str, detail: str) -> None:
        nonlocal step
        step += 1
        console.print(
            f"  [bold {colour}]STEP {step}  [{label}][/bold {colour}]  {title}"
        )
        for line in detail.strip().splitlines():
            console.print(f"  [dim]         {line.strip()}[/dim]")
        console.print()

    # ── OVERLAP warnings (highest priority — before switches) ─────────────
    for ov in plan["overlaps"]:
        _step("OVERLAP ⚠", "red",
              f"Duplicate {ov['category']} exposure",
              f"You hold {len(ov['funds'])} funds in the same category: "
              f"{', '.join(ov['funds'][:3])}\n"
              f"Consolidate to the highest-scoring one to reduce overlap.")

    # ── SWITCHES / EXITS ──────────────────────────────────────────────────
    for sw in plan["switches"]:
        h   = sw["from"]
        rep = sw["to"]
        urgency = "EXIT NOW" if h.get("action") == "EXIT" else "SWITCH"
        from_str = (f"{h.get('scheme_name','?')[:40]}  "
                    f"[rank {h.get('category_rank','?')}/{h.get('category_size','?')}  "
                    f"score {h.get('score') or '?'}]")
        if rep:
            to_str = (f"{rep.get('name','?')[:40]}  "
                      f"[rank {rep.get('category_rank','?')}/{rep.get('category_size','?')}  "
                      f"score {rep.get('score') or '?'}]")
        else:
            to_str = "No direct replacement found in universe — consider category alternatives"
        el_warn = "⚠ Exit load likely (held < 1Y)  " if sw["exit_load_warn"] else ""
        tax_str = (f"Tax on gain: {fmt_inr(sw['tax_rs'])}  "
                   f"(LTCG: {fmt_inr(sw['ltcg_rs'])})"
                   if sw["ltcg_rs"] else "No tax impact (cost basis unavailable or no gain)")
        _step(urgency, "red" if urgency == "EXIT NOW" else "yellow",
              f"{h.get('scheme_name','?')[:34]} → {(rep or {}).get('name','?')[:30]}",
              f"FROM: {from_str}\n  TO:   {to_str}\n  {el_warn}{tax_str}")

    # ── GAP FILLS (new category coverage) ────────────────────────────────
    for gf in plan["gap_fills"]:
        f = gf["fund"]
        _step("ADD NEW", "cyan",
              f"{gf['slot'].upper()} gap — start {fmt_inr(gf['sip'])}/month",
              f"You have 0% in {gf['slot']} (target: {gf['gap_pct']:.0f}%)\n"
              f"Fund: {f.get('name','?')[:40]}\n"
              f"Rank: {f.get('category_rank','?')}/{f.get('category_size','?')}  "
              f"Score: {f.get('score') or '?'}  "
              f"3Y: {f.get('return_3y') or '?'}%")

    # ── BUY MORE (increase underweight existing positions) ────────────────
    for bm in plan["buy_mores"]:
        h = bm["holding"]
        _step("INCREASE", "green",
              f"{h.get('scheme_name','?')[:34]} — add {fmt_inr(bm['extra_sip'])}/month",
              f"Top performer in {h.get('category','?')} "
              f"[rank {h.get('category_rank','?')}/{h.get('category_size','?')}]\n"
              f"Slot '{bm['slot']}' is {bm['gap_pct']:.1f}% below target — "
              f"increase existing SIP rather than opening a new fund")

    # ── ALREADY RECOMMENDED ───────────────────────────────────────────────
    for h in plan["already_recommended"]:
        _step("CONFIRMED", "bold green",
              f"{h.get('scheme_name','?')[:40]} — already in new recommendation",
              "This holding is also the top pick in its category. No switch needed.")

    # ── HOLDS ─────────────────────────────────────────────────────────────
    if plan["holds"]:
        console.print("  [green]✓ HOLD — no action needed:[/green]")
        for h in plan["holds"]:
            cr, cs = h.get("category_rank"), h.get("category_size")
            rank_s = f"rank {cr}/{cs}" if (cr and cs) else ""
            console.print(f"    [dim]{h.get('scheme_name','?')[:44]}  {rank_s}[/dim]")
        console.print()

    # ── GAP SUMMARY TABLE ─────────────────────────────────────────────────
    gt = Table(title="Allocation Gap vs Strategy Target", border_style="dim",
               padding=(0, 1), show_lines=False)
    gt.add_column("Category Slot",  style="white", width=26)
    gt.add_column("Current", justify="right", width=9)
    gt.add_column("Target",  justify="right", width=9)
    gt.add_column("Gap",     justify="right", width=9)
    gt.add_column("SIP gap", justify="right", width=11)
    gt.add_column("Status",  width=11)
    STATUS_COL = {"OK":"green","UNDER":"yellow","OVER":"yellow",
                  "UNCOVERED":"bold red","other":"dim"}
    for slot, info in plan["gaps"].items():
        st = info["status"]
        col = STATUS_COL.get(st, "dim")
        gap_s = (f"[{col}]+{info['gap_pct']:.1f}%[/{col}]"
                 if info["gap_pct"] >= 0 else
                 f"[{col}]{info['gap_pct']:.1f}%[/{col}]")
        sip_s = fmt_inr(info["sip_gap"]) if info["sip_gap"] > 0 else "—"
        gt.add_row(
            slot,
            f"{info['current_pct']:.1f}%",
            f"{info['target_pct']:.1f}%",
            gap_s,
            sip_s,
            f"[{col}]{st}[/{col}]",
        )
    console.print(gt)
    console.print()

    if step == 0 and not plan["holds"]:
        console.print("  [green]Portfolio is well-positioned — no immediate actions needed.[/green]\n")


def run_portfolio_manager(
    pan_hash: str,
    pan: str,
    nav_data: list,
    all_scored: list,
    amfi_master: dict,
) -> list:
    """
    Interactive: view existing holdings, add/update, get hold/sell/buy analysis.
    Always returns the enriched analysis (for HTML report inclusion) if holdings exist.
    """
    # Show any pending recommendations from the previous run first
    show_pending_action_plan(pan_hash)

    user_port = get_user_portfolio(pan_hash)
    holdings  = _clean_holdings(user_port.get("holdings", []))

    # Persist cleaned version (removes junk scraped from DOM)
    if len(holdings) != len(user_port.get("holdings", [])):
        user_port["holdings"] = holdings
        save_user_portfolio(pan_hash, user_port)

    # ── Staleness check ───────────────────────────────────────────────────
    # Holdings are fetched once and stored; NAV prices change daily.
    # Policy: warn after 7 days, prompt to refresh after 30 days.
    STALE_WARN_DAYS    = 7
    STALE_REFRESH_DAYS = 30
    last_updated = user_port.get("last_updated", "")
    days_old     = None
    if last_updated:
        try:
            days_old = (datetime.now() - datetime.fromisoformat(last_updated)).days
        except Exception:
            pass

    if not holdings:
        console.print("  [dim]No holdings stored for this PAN yet.[/dim]")
    else:
        age_str = f"{days_old}d ago" if days_old is not None else "unknown date"
        if days_old is not None and days_old >= STALE_REFRESH_DAYS:
            console.print(Panel(
                f"[bold yellow]⚠ Holdings last updated {days_old} days ago[/bold yellow]\n"
                f"[dim]NAV values and recommendations may be significantly outdated.\n"
                f"Consider re-fetching via option 0 (OTP) to get current values.[/dim]",
                border_style="yellow",
            ))
        elif days_old is not None and days_old >= STALE_WARN_DAYS:
            console.print(
                f"  [yellow]Holdings are {days_old} days old — "
                f"NAV values refreshed from live cache but quantities may have changed.[/yellow]"
            )
        else:
            console.print(f"  [green]{len(holdings)} holdings found "
                          f"(last updated: {age_str}).[/green]")

    action_idx = rich_ask_choice(
        "What would you like to do?",
        [
            "Auto-fetch portfolio via OTP  (browser opens MFCentral, or parse CAS PDF)",
            "View existing portfolio + get Hold/Sell/Buy recommendations",
            "Add or update a holding manually",
            "Import from CSV (Kuvera / Groww / Coin export)",
            "Remove a holding",
            "Skip — proceed to new recommendation only",
        ],
    )

    # Skip = don't do any management, but STILL run analysis so report is populated
    skip = (action_idx == 5)

    def _merge_into_holdings(new_items: list) -> None:
        """Merge new_items into holdings, deduplicating by scheme_code / name."""
        exist_codes = {str(h.get("scheme_code","")) for h in holdings if h.get("scheme_code")}
        exist_names = {h.get("scheme_name","").lower() for h in holdings}
        for h in new_items:
            code = str(h.get("scheme_code",""))
            name_l = h.get("scheme_name","").lower()
            if (code and code in exist_codes) or name_l in exist_names:
                continue
            holdings.append(h)
            if code: exist_codes.add(code)
            exist_names.add(name_l)
        user_port["holdings"] = holdings
        save_user_portfolio(pan_hash, user_port)

    if not skip:
        if action_idx == 0:      # OTP / Browser / PDF fetch
            fetched = fetch_portfolio_external(pan, amfi_master)
            if fetched:
                console.print("  [dim]Saving holdings to database...[/dim]")
                _merge_into_holdings(fetched)
                console.print(f"  [green]{len(fetched)} holdings fetched and saved.[/green]")

        elif action_idx == 3:    # CSV import
            csv_path = Prompt.ask("  [green]Enter path to CSV file[/green]")
            imported = import_from_csv(csv_path.strip().strip('"'))
            if imported:
                for h in imported:
                    if not h.get("category"):
                        h["category"] = classify_fund(h.get("scheme_name",""),
                                                       h.get("scheme_code",""), amfi_master)
                _merge_into_holdings(imported)
                console.print(f"  [green]Imported {len(imported)} holdings from CSV.[/green]")
            else:
                console.print("  [yellow]No holdings imported.[/yellow]")

        elif action_idx == 2:    # Manual add/update
            console.print("\n  Enter fund details (scheme code from mfapi.in or AMFI):")
            code  = Prompt.ask("  [green]Scheme code[/green]").strip()
            name  = Prompt.ask("  [green]Fund name (short)[/green]").strip()
            sip   = rich_ask_int("Monthly SIP amount (0 if none)", 0, 100_000)
            avg_n = Prompt.ask("  [green]Average NAV (0 if unknown)[/green]", default="0").strip()
            units = Prompt.ask("  [green]Units held (0 if unknown)[/green]", default="0").strip()
            start = Prompt.ask("  [green]Start date YYYY-MM-DD (blank to skip)[/green]",
                                default="").strip()
            new_h = {
                "scheme_code": code, "scheme_name": name,
                "category": classify_fund(name, code, amfi_master),
                "sip_amount": sip, "lump_sum": 0,
                "units": _safe_float(units) or 0,
                "avg_nav": _safe_float(avg_n) or 0,
                "current_nav": 0, "current_value": 0,
                "start_date": start, "notes": "",
            }
            updated = False
            for i, h in enumerate(holdings):
                if str(h.get("scheme_code","")) == code:
                    holdings[i] = new_h; updated = True; break
            if not updated:
                holdings.append(new_h)
            user_port["holdings"] = holdings
            save_user_portfolio(pan_hash, user_port)
            console.print(f"  [green]Holding {'updated' if updated else 'added'}: {name}[/green]")

        elif action_idx == 4:    # Remove
            if not holdings:
                console.print("  [yellow]No holdings to remove.[/yellow]")
            else:
                opts = [f"{h.get('scheme_name','?')[:50]} ({h.get('category','')})"
                        for h in holdings]
                idx  = rich_ask_choice("Select holding to remove:", opts)
                removed = holdings.pop(idx)
                user_port["holdings"] = holdings
                save_user_portfolio(pan_hash, user_port)
                console.print(f"  [green]Removed: {removed.get('scheme_name','')}[/green]")

    # Always run analysis if holdings exist — so HTML report always has this section
    user_port = get_user_portfolio(pan_hash)
    holdings  = _clean_holdings(user_port.get("holdings", []))
    if not holdings:
        console.print("  [dim]No holdings to analyse — add funds via option 0, 2, or 3.[/dim]")
        return []

    console.print(f"  [dim]Matching {len(holdings)} holdings against scored universe...[/dim]")
    analysis = analyse_holdings(holdings, all_scored, nav_data, amfi_master)
    console.print(f"  [dim]Analysis complete — {len(analysis)} holdings enriched.[/dim]")
    if not skip:
        display_holdings_analysis(analysis)   # raw table for reference
    return analysis   # action plan printed by caller with full context


# ══════════════════════════════════════════════════════════════════════
# QUESTIONNAIRE  (6-step Rich flow)
# ══════════════════════════════════════════════════════════════════════

def run_questionnaire() -> dict:
    """
    3-question setup. Time horizon is the single strongest predictor of appropriate
    risk capacity — the 4-question risk quiz produced inconsistent answers and was
    replaced with a direct strategy choice validated against the timeline.
    """
    console.print(Panel(
        "[bold cyan]MF Portfolio Builder[/bold cyan]\n"
        "[dim]mfapi.in · AMFI · mfdata.in · yfinance[/dim]\n"
        "[dim]Scores funds on: Information Ratio · Capture · Consistency · Cost · Capacity[/dim]",
        border_style="cyan", padding=(1, 4),
    ))

    # ── Q1: Timeline ──────────────────────────────────────────────────────
    rich_header("Q1/3", "INVESTMENT TIMELINE",
                "How many years will you stay invested?  This is the biggest driver of strategy.")
    timeline_years = float(rich_ask_int("Years", 1, 35))

    # Auto-derive strategy from timeline
    if timeline_years < 3:
        optimise     = "conservative"
        include_debt = True
        console.print(Panel(
            f"[yellow]{timeline_years:.0f}Y horizon → Conservative + debt forced.[/yellow]\n"
            "[dim]Equity can lose 30-40% in a single year — not suitable for < 3Y.[/dim]",
            border_style="yellow",
        ))
    elif timeline_years <= 7:
        optimise     = "balanced"
        include_debt = True
        console.print(f"  [dim]Timeline {timeline_years:.0f}Y → Balanced suggested.[/dim]\n")
    else:
        optimise     = "aggressive"
        include_debt = False
        console.print(f"  [dim]Timeline {timeline_years:.0f}Y → Aggressive suggested.[/dim]\n")

    # Allow override (one question, not a quiz)
    if timeline_years >= 3:
        override = rich_ask_choice(
            "Strategy (auto-selected above — override if needed):",
            ["Keep auto-selected strategy",
             "Aggressive — max growth, higher swings",
             "Balanced   — steady growth with cushion",
             "Conservative — capital preservation first"],
        )
        if override == 1: optimise = "aggressive"; include_debt = False
        elif override == 2: optimise = "balanced";  include_debt = True
        elif override == 3: optimise = "conservative"; include_debt = True

    # ── Q2: Monthly SIP ───────────────────────────────────────────────────
    rich_header("Q2/3", "MONTHLY SIP AMOUNT", "Rounded to nearest ₹500")
    monthly_sip = rich_ask_int("Amount in ₹", 500, 500_000)
    monthly_sip = max(500, round(monthly_sip / 500) * 500)

    # ── Q3: Target corpus (optional) ──────────────────────────────────────
    rich_header("Q3/3", "TARGET CORPUS  (optional)",
                "E.g. 50L, 1Cr — press Enter to skip")
    target_corpus = rich_ask_corpus()
    if target_corpus:
        console.print(f"  [green]Target: {fmt_inr(target_corpus)}[/green]\n")

    # Derive number of funds from strategy (no extra question needed)
    num_funds = {"aggressive": 5, "balanced": 4, "conservative": 3}[optimise]

    # Tax bracket — infer from SIP as reasonable proxy; override trivially
    if monthly_sip >= 50_000:  tax_bracket = 30
    elif monthly_sip >= 20_000: tax_bracket = 20
    else:                       tax_bracket = 0
    tax_idx = rich_ask_choice(
        f"Income tax slab (auto: {tax_bracket}% — override if needed):",
        ["0% / 5% — new regime / low income",
         "20% — mid slab",
         "30% — highest slab"],
        default=0 if tax_bracket == 0 else (1 if tax_bracket == 20 else 2),
    )
    tax_bracket = [0, 20, 30][tax_idx]

    rich_summary([
        ("Timeline",      f"{timeline_years:.0f} years"),
        ("Strategy",      optimise.title()),
        ("Monthly SIP",   fmt_inr(monthly_sip)),
        ("Funds",         f"{num_funds}  ·  Debt included: {'Yes' if include_debt else 'No'}"),
        ("Target corpus", fmt_inr(target_corpus) if target_corpus else "—"),
        ("Tax bracket",   f"{tax_bracket}%"),
    ])

    return {
        "monthly_sip": monthly_sip, "timeline_years": timeline_years,
        "optimise": optimise, "include_debt": include_debt, "num_funds": num_funds,
        "goal": f"{timeline_years:.0f}Y investment", "target_corpus": target_corpus,
        "tax_bracket_pct": tax_bracket, "risk_score": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# STEP-UP SIP PROJECTION
# ══════════════════════════════════════════════════════════════════════

def compute_stepup_projection(portfolio: list, monthly_sip: int,
                               timeline_years: float, stepup_pct: float) -> dict:
    """
    Project corpus with annual SIP step-up vs flat SIP.
    stepup_pct = annual increase e.g. 10 means SIP grows 10% each year.
    Returns {flat_p50, stepup_p50, extra_corpus, stepup_schedule}.
    """
    months    = int(timeline_years * 12)
    flat_p50  = sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"])
                    for f in portfolio)

    # Step-up: SIP increases by stepup_pct% each year
    stepup_total = 0.0
    schedule     = []
    sip_this_year = monthly_sip
    for year in range(1, int(timeline_years) + 1):
        months_this_year = min(12, months - (year - 1) * 12)
        if months_this_year <= 0: break
        stepup_total += sip_this_year * months_this_year
        schedule.append({"year": year, "monthly_sip": round(sip_this_year)})
        sip_this_year *= (1 + stepup_pct / 100)

    # Estimate step-up corpus using blended rate from portfolio
    avg_rate = np.mean([f["estimated_rate"] / 12 for f in portfolio]) if portfolio else 0.01
    stepup_corpus = 0.0
    sip_y = monthly_sip
    for year in range(1, int(timeline_years) + 1):
        m_start = (year - 1) * 12
        m_end   = min(year * 12, months)
        for m in range(m_start, m_end):
            remaining = months - m
            stepup_corpus += sip_y * ((1 + avg_rate) ** remaining)
        sip_y *= (1 + stepup_pct / 100)

    return {
        "flat_p50":       round(flat_p50),
        "stepup_p50":     round(stepup_corpus),
        "extra_corpus":   round(stepup_corpus - flat_p50),
        "total_invested": round(sum(s["monthly_sip"] * 12 for s in schedule)),
        "stepup_schedule": schedule,
        "stepup_pct":     stepup_pct,
    }


def print_stepup_analysis(stepup: dict) -> None:
    console.print("\n[bold cyan]═══ STEP-UP SIP ANALYSIS ═══[/bold cyan]")
    pct   = stepup["stepup_pct"]
    extra = stepup["extra_corpus"]
    flat  = stepup["flat_p50"]
    adv   = (extra / flat * 100) if flat else 0

    console.print(Panel(
        f"  Flat SIP P50 corpus   : [white]{fmt_inr(flat)}[/white]\n"
        f"  Step-up {pct:.0f}%/yr P50   : [bold green]{fmt_inr(stepup['stepup_p50'])}[/bold green]\n"
        f"  Extra corpus from step-up: [bold yellow]+{fmt_inr(extra)} (+{adv:.0f}%)[/bold yellow]\n"
        f"  Total invested (step-up) : {fmt_inr(stepup['total_invested'])}",
        title=f"[bold]Annual {pct:.0f}% SIP Step-Up vs Flat SIP[/bold]",
        border_style="green",
    ))

    st = Table(title="Step-Up Schedule", border_style="dim", padding=(0, 1))
    st.add_column("Year", justify="right", width=6)
    st.add_column("Monthly SIP", justify="right", width=14)
    for s in stepup["stepup_schedule"][:10]:
        st.add_row(str(s["year"]), fmt_inr(s["monthly_sip"]))
    if len(stepup["stepup_schedule"]) > 10:
        st.add_row("...", "...")
    console.print(st)


# ══════════════════════════════════════════════════════════════════════
# LTCG TAX HARVEST CALENDAR
# ══════════════════════════════════════════════════════════════════════

LTCG_EXEMPTION_PER_YEAR = 125_000   # ₹1.25L per FY (as of FY2025-26)

def compute_tax_harvest_plan(holdings_analysis: list, nav_data: list) -> list:
    """
    For each holding with unrealised LTCG, compute how many units to redeem
    annually to fully utilise the ₹1.25L LTCG exemption and immediately reinvest.
    Returns list of action dicts with redemption details.
    """
    nav_lookup: dict = {}
    for item in nav_data:
        code = str(item["scheme"].get("schemeCode", ""))
        nav_lookup[code] = float(item["nav_df"]["nav"].iloc[-1])

    harvest_actions = []
    remaining_exemption = float(LTCG_EXEMPTION_PER_YEAR)

    # Only HOLD funds with long-term gains qualify (held > 1Y, positive gain)
    eligible = [
        h for h in holdings_analysis
        if h.get("action") in ("HOLD", "BUY MORE")
        and (h.get("years_held") or 0) > 1.0
        and (h.get("gain_pct") or 0) > 0
        and (h.get("units") or 0) > 0
        and (h.get("avg_nav") or 0) > 0
    ]
    eligible.sort(key=lambda h: h.get("gain_pct", 0))   # harvest lowest-gain first

    for h in eligible:
        if remaining_exemption <= 0:
            break
        avg_nav  = h["avg_nav"]
        curr_nav = h.get("current_nav") or nav_lookup.get(str(h.get("scheme_code", ""))) or avg_nav
        if curr_nav <= avg_nav:
            continue
        units_held = h["units"]
        gain_per_unit = curr_nav - avg_nav
        # Units needed to realise exactly remaining_exemption in LTCG
        units_to_redeem = min(units_held * 0.95, remaining_exemption / gain_per_unit)
        units_to_redeem = max(0, units_to_redeem)
        ltcg_realised   = units_to_redeem * gain_per_unit
        redeem_value    = units_to_redeem * curr_nav
        remaining_exemption -= ltcg_realised

        harvest_actions.append({
            "scheme_name":    h["scheme_name"],
            "units_to_redeem": round(units_to_redeem, 3),
            "redeem_value":   round(redeem_value),
            "ltcg_realised":  round(ltcg_realised),
            "reinvest_value": round(redeem_value),
            "tax_saved":      round(ltcg_realised * 0.125),   # 12.5% LTCG rate
            "new_cost_basis": round(curr_nav, 2),
            "action":         h.get("action", "HOLD"),
        })

    total_tax_saved = sum(a["tax_saved"] for a in harvest_actions)
    return {"actions": harvest_actions,
            "total_ltcg_realised": round(LTCG_EXEMPTION_PER_YEAR - remaining_exemption),
            "total_tax_saved":     round(total_tax_saved),
            "exemption_used":      round(LTCG_EXEMPTION_PER_YEAR - remaining_exemption),
            "exemption_remaining": round(max(0, remaining_exemption))}


def print_tax_harvest(harvest: dict) -> None:
    if not harvest.get("actions"):
        console.print("  [dim]No eligible holdings for tax harvesting this year.[/dim]")
        return

    console.print("\n[bold cyan]═══ LTCG TAX HARVEST CALENDAR ═══[/bold cyan]")
    console.print(Panel(
        f"  LTCG exemption limit : ₹{LTCG_EXEMPTION_PER_YEAR:,.0f}/year\n"
        f"  Exemption used       : {fmt_inr(harvest['exemption_used'])}\n"
        f"  Remaining unused     : {fmt_inr(harvest['exemption_remaining'])}\n"
        f"  Estimated tax saved  : [bold green]{fmt_inr(harvest['total_tax_saved'])}[/bold green]\n\n"
        f"  [dim]Redeem → immediately reinvest same amount in same fund.\n"
        f"  Cost basis resets to current NAV — future gains start fresh.[/dim]",
        title="[bold]Annual Tax Harvest Plan[/bold]",
        border_style="green",
    ))

    ht = Table(border_style="dim", show_lines=True, padding=(0, 1))
    for col in ["Fund", "Units to Redeem", "Redeem Value", "LTCG Realised", "Tax Saved", "New Cost Basis"]:
        ht.add_column(col, style="white")
    for a in harvest["actions"]:
        ht.add_row(
            a["scheme_name"][:36],
            f"{a['units_to_redeem']:.3f}",
            fmt_inr(a["redeem_value"]),
            fmt_inr(a["ltcg_realised"]),
            f"[bold green]{fmt_inr(a['tax_saved'])}[/bold green]",
            f"₹{a['new_cost_basis']:.2f}",
        )
    console.print(ht)
    console.print(
        "  [dim]Do this in January each year. Reinvest on the same day in the same fund "
        "to avoid being out of market.[/dim]\n"
    )


# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO-ONLY CHECK MODE
# ══════════════════════════════════════════════════════════════════════

def portfolio_check_mode() -> None:
    """
    Fast mode: load stored holdings, score them against live NAV data,
    show HOLD/SELL/BUY recommendations, tax harvest plan — no questionnaire.
    """
    console.print(Panel(
        "[bold white]PORTFOLIO CHECK MODE[/bold white]\n"
        "[dim]Loads your stored holdings, scores them against the full universe,\n"
        "and shows HOLD / SELL / BUY recommendations + tax harvest plan.\n"
        "No new portfolio recommendation is generated.[/dim]",
        border_style="blue",
    ))

    raw_pan = Prompt.ask("  [green]Enter PAN[/green]").strip().upper()
    if not raw_pan:
        console.print("  [red]PAN required for portfolio check.[/red]")
        return
    pan_hash = hash_pan(raw_pan)

    user_port = get_user_portfolio(pan_hash)
    holdings  = _clean_holdings(user_port.get("holdings", []))
    if not holdings:
        console.print(Panel(
            "No holdings stored for this PAN.\n"
            "Run the full mode and use option 0 (auto-fetch) or 2 (manual add) first.",
            border_style="yellow",
        ))
        return

    console.print(f"  [green]{len(holdings)} holdings found.[/green]")

    # Show pending items from last run before loading data
    show_pending_action_plan(pan_hash)

    # ── Load data (same pipeline as full mode) ───────────────────────────
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Loading NAV data...", total=None)
        cache    = load_cache()
        nav_data = restore_nav_data(cache) if cache else None
        if not nav_data:
            prog.update(t, description="Fetching live data...")
            nav_data = fetch_and_cache()
        prog.update(t, description="Data ready.")

    if not nav_data:
        console.print("[red]Could not load NAV data.[/red]"); return

    amfi_master        = fetch_amfi_master()
    benchmarks         = fetch_benchmarks() if YFINANCE_OK else {}
    enrichment         = fetch_fund_enrichment(nav_data)
    portfolio_holdings = fetch_fund_portfolios(nav_data, enrichment)

    # ── Score universe using balanced strategy as neutral reference ───────
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Scoring universe...", total=None)
        _, _, all_scored = build_portfolio(
            nav_data, "balanced", True, 4, 10000, 7,
            amfi_master=amfi_master, benchmarks=benchmarks, aum_data=enrichment,
        )
        prog.update(t, description="Universe scored.")

    # ── Analyse holdings ─────────────────────────────────────────────────
    console.print("  [dim]Matching holdings against universe...[/dim]")
    analysis = analyse_holdings(holdings, all_scored, nav_data, amfi_master)
    display_holdings_analysis(analysis)   # raw table

    # ── Full action plan (portfolio management + new value gaps) ──────────
    # Ask which strategy to use as the target for gap analysis
    strat_idx = rich_ask_choice(
        "Which strategy should the gap analysis target?",
        ["Balanced (default)", "Aggressive", "Conservative"],
        default=0,
    )
    target_strategy = ["balanced", "aggressive", "conservative"][strat_idx]

    # Derive SIP budget from stored holdings — only ask if none recorded
    derived_sip = sum(h.get("sip_amount") or 0 for h in analysis)
    if derived_sip > 0:
        new_sip = derived_sip
        console.print(f"  [dim]Using total SIP from holdings: {fmt_inr(new_sip)}/mo[/dim]")
    else:
        new_sip = rich_ask_int(
            "Monthly SIP budget (₹) — needed for gap/buy-more amounts", 500, 500_000)

    action_plan = generate_action_plan(
        analysis, all_scored, target_strategy, new_sip, nav_data)
    print_action_plan(action_plan)
    save_action_plan(pan_hash, action_plan)
    console.print("  [dim]Action plan saved — pending items will show on next run.[/dim]")

    # ── Tax harvest plan ─────────────────────────────────────────────────
    harvest = compute_tax_harvest_plan(analysis, nav_data)
    print_tax_harvest(harvest)

    # ── Option to update holdings ─────────────────────────────────────────
    update = rich_ask_choice(
        "Want to update your holdings?",
        ["No — done", "Fetch latest via MFCentral OTP",
         "Add/edit manually", "Import from CSV"],
        default=0,
    )
    if update == 1:
        fetched = fetch_portfolio_external(raw_pan, amfi_master)
        if fetched:
            exist_names = {h.get("scheme_name","").lower() for h in holdings}
            for h in fetched:
                if h.get("scheme_name","").lower() not in exist_names:
                    holdings.append(h)
            user_port["holdings"] = holdings
            save_user_portfolio(pan_hash, user_port)
            console.print(f"  [green]Updated — {len(fetched)} holdings refreshed.[/green]")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    console.print(Panel(
        "[bold cyan]MF Portfolio Analyser[/bold cyan]\n"
        "[dim]Data: mfapi.in · AMFI · mfdata.in · yfinance[/dim]\n"
        "[dim]Scoring: Info Ratio · Capture · Consistency · Cost · Capacity[/dim]",
        border_style="bright_cyan", padding=(1, 4),
    ))

    mode = rich_ask_choice(
        "What would you like to do?",
        [
            "Build a new portfolio recommendation",
            "Check my existing portfolio  (HOLD / SELL / BUY + tax harvest)",
            "Refresh data cache  (re-fetch NAV, AMFI, benchmarks)",
        ],
    )

    if mode == 1:
        portfolio_check_mode()
        return

    if mode == 2:
        import tempfile as _tf
        console.print("[cyan]Clearing caches...[/cyan]")
        cleared, failed = [], []
        for fpath in [CACHE_FILE, AMFI_CACHE_FILE, BENCHMARK_CACHE_FILE,
                      ENRICH_CACHE_FILE, HOLDINGS_CACHE_FILE, AMFI_PORTFOLIO_CACHE_FILE]:
            if not os.path.exists(fpath):
                continue
            # Strategy 1: delete outright
            try:
                os.unlink(fpath)
                cleared.append(os.path.basename(fpath))
                continue
            except PermissionError:
                pass
            # Strategy 2: write expired-timestamp JSON via temp+replace
            # (os.replace uses MoveFileEx which can bypass Controlled Folder
            #  Access restrictions that block direct open("w") calls)
            try:
                tmp_fd, tmp_path = _tf.mkstemp(
                    dir=_tf.gettempdir(), suffix=".json")
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump({"timestamp": 0, "data": {}}, fh)
                os.replace(tmp_path, fpath)
                cleared.append(os.path.basename(fpath) + " (expired)")
                continue
            except Exception:
                try: os.unlink(tmp_path)
                except Exception: pass
            # Strategy 3: write a bust-flag to temp dir; cache loaders check it
            failed.append(os.path.basename(fpath))

        # If any files couldn't be reset, write a cache-bust flag to temp dir.
        # Cache loaders will check for this flag and ignore on-disk data.
        bust_flag = os.path.join(_tf.gettempdir(), "mf_cache_bust.flag")
        if failed:
            try:
                with open(bust_flag, "w") as fh:
                    fh.write("1")
                console.print(
                    f"  [yellow]Could not reset: {', '.join(failed)}\n"
                    f"  Bust-flag written to temp dir — caches will be ignored on next run.[/yellow]"
                )
            except Exception:
                console.print(
                    f"  [red]Could not reset {', '.join(failed)}.\n"
                    f"  Close VS Code / any program that has these files open, then try again.[/red]"
                )
        else:
            # Remove any stale bust-flag from a previous failed clear
            try: os.unlink(bust_flag)
            except Exception: pass

        if cleared:
            console.print(f"  [green]Cleared: {', '.join(cleared)}[/green]")
        console.print("[green]Done — re-run to fetch fresh data.[/green]")
        return

    # ── mode 0: full recommendation flow ─────────────────────────────────
    raw_pan = Prompt.ask(
        "  [green]Enter PAN for portfolio tracking (or Enter to skip)[/green]",
        default="").strip().upper()
    pan_hash = hash_pan(raw_pan) if raw_pan else None

    params = run_questionnaire()

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Loading NAV data...", total=None)
        cache    = load_cache()
        nav_data = restore_nav_data(cache) if cache else None
        prog.update(t, description="NAV data ready.")

    if not nav_data:
        console.print("[yellow]No cache — fetching live data (3-5 min)...[/yellow]")
        nav_data = fetch_and_cache()
    if not nav_data:
        console.print("[bold red]Could not load fund data. Check internet.[/bold red]")
        sys.exit(1)

    console.print("\n[bold cyan]═══ EXTERNAL DATA ═══[/bold cyan]")
    amfi_master        = fetch_amfi_master()
    benchmarks         = fetch_benchmarks() if YFINANCE_OK else {}
    enrichment         = fetch_fund_enrichment(nav_data)
    portfolio_holdings = fetch_fund_portfolios(nav_data, enrichment)
    market_ctx         = get_market_context(benchmarks)

    if market_ctx:
        col = "yellow" if "BULL" in market_ctx else ("red" if "BEAR" in market_ctx else "cyan")
        console.print(Panel(f"[bold]{market_ctx}[/bold]",
                            title="[bold]Market Signal[/bold]",
                            border_style=col))

    # ── Build full scored universe (needed for portfolio tracker) ───────
    console.print("\n[bold cyan]═══ BUILDING PORTFOLIO ═══[/bold cyan]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Running 8-pass scoring pipeline...", total=None)
        portfolio, cohort, all_scored_universe = build_portfolio(
            nav_data, params["optimise"], params["include_debt"],
            params["num_funds"], params["monthly_sip"], params["timeline_years"],
            amfi_master=amfi_master, benchmarks=benchmarks, aum_data=enrichment,
        )
        prog.update(t, description="Portfolio built.")

    if not portfolio:
        console.print("[bold red]Could not build portfolio. Try deleting mf_nav_cache.json.[/bold red]")
        sys.exit(1)

    # ── Existing portfolio analysis (if PAN provided) ───────────────────
    # Reuse the already-scored universe from build_portfolio — no re-computation needed.
    holdings_analysis: list = []
    if pan_hash:
        console.print("\n[bold blue]═══ EXISTING PORTFOLIO TRACKER ═══[/bold blue]")
        holdings_analysis = run_portfolio_manager(
            pan_hash, raw_pan, nav_data, all_scored_universe, amfi_master)

    # ── Action plan: bridges existing holdings with new recommendation ────
    if holdings_analysis:
        action_plan = generate_action_plan(
            holdings_analysis, all_scored_universe,
            strategy=params["optimise"],
            monthly_sip=params["monthly_sip"],
            nav_data=nav_data,
            new_portfolio=portfolio,       # cross-reference so duplicates are flagged
        )
        print_action_plan(action_plan)
        save_action_plan(pan_hash, action_plan)
        console.print("  [dim]Action plan saved — pending items will show on next run.[/dim]")
        harvest = compute_tax_harvest_plan(holdings_analysis, nav_data)
        print_tax_harvest(harvest)

    # Tax, passive, advantage
    console.print("[dim]Computing tax estimate...[/dim]")
    tax        = compute_tax(portfolio, params["monthly_sip"],
                             params["timeline_years"], params["tax_bracket_pct"])
    console.print("[dim]Running passive baseline simulation...[/dim]")
    passive_mc = simulate_passive_baseline(
        params["monthly_sip"], params["timeline_years"], benchmarks, params["optimise"])
    total_mc   = {"p10": sum((f.get("mc_projection") or {}).get("p10", f["projected_corpus"]) for f in portfolio),
                  "p50": sum((f.get("mc_projection") or {}).get("p50", f["projected_corpus"]) for f in portfolio),
                  "p90": sum((f.get("mc_projection") or {}).get("p90", f["projected_corpus"]) for f in portfolio)}
    advantage  = active_vs_passive(total_mc, passive_mc)
    drift      = compute_weight_drift(portfolio)

    # Required SIP (if corpus target given)
    req_sip: dict = {}
    if params.get("target_corpus"):
        console.print("[cyan]Computing required SIP via Monte Carlo (may take ~15s)...[/cyan]")
        req_sip = compute_required_sip(
            params["target_corpus"], params["timeline_years"], portfolio)

    print_portfolio(portfolio, params, tax, passive_mc, advantage, req_sip)

    # ── Step-up SIP analysis ──────────────────────────────────────────────
    stepup_pct = rich_ask_int(
        "Annual SIP step-up % (e.g. 10 = grow SIP 10%/yr; 0 to skip)", 0, 50)
    if stepup_pct > 0:
        stepup = compute_stepup_projection(
            portfolio, params["monthly_sip"], params["timeline_years"], stepup_pct)
        print_stepup_analysis(stepup)

    # Terminal: show top-3 runners-up per category so user sees why HDFC/ICICI weren't picked
    leaders = (cohort or {}).get("_category_leaders", {})
    selected_codes = {f["scheme_code"] for f in portfolio}
    if leaders:
        rt = Table(title="Category Scorecard (top 5 per slot — see HTML report for full list)",
                   border_style="dim", show_lines=False, padding=(0,1))
        rt.add_column("Category",  style="cyan",  width=14)
        rt.add_column("Fund",      style="white", width=38)
        rt.add_column("Score",     justify="right", width=6)
        rt.add_column("3Y%",       justify="right", width=6)
        rt.add_column("10Y%",      justify="right", width=6)
        rt.add_column("Alpha",     justify="right", width=7)
        rt.add_column("AUM Cr",    justify="right", width=9)
        rt.add_column("",          width=10)
        for cat, funds in sorted(leaders.items()):
            top5 = sorted(funds, key=lambda x: x["score"], reverse=True)[:5]
            for i, fd in enumerate(top5):
                is_sel = fd["scheme_code"] in selected_codes
                badge  = "[bold green]CHOSEN[/bold green]" if is_sel else ""
                aum_w  = "[red]⚠AUM[/red]" if fd.get("aum_warn") else ""
                ret_s  = f"{fd['return_3y']:.1f}"  if fd.get("return_3y")  is not None else "—"
                r10_s  = f"{fd['return_10y']:.1f}" if fd.get("return_10y") is not None else "—"
                alp_s  = f"{fd['alpha']:.2f}"      if fd.get("alpha")      is not None else "—"
                aum_s  = f"{fd['aum_cr']:,.0f}"    if fd.get("aum_cr")                 else "—"
                name_s = fd["name"][:36] + ("…" if len(fd["name"]) > 36 else "")
                row_style = "bold" if is_sel else "dim" if i >= 3 else ""
                rt.add_row(
                    cat.title() if i == 0 else "",
                    f"[{row_style}]{name_s}[/{row_style}]" if row_style else name_s,
                    f"[{row_style}]{fd['score']:.1f}[/{row_style}]" if row_style else f"{fd['score']:.1f}",
                    ret_s, r10_s, alp_s, aum_s,
                    f"{badge} {aum_w}".strip(),
                )
        console.print(rt)
        console.print()

    # Charts — up to 3 attempts with a short pause between
    console.print("[cyan]Rendering charts...[/cyan]")
    charts_b64 = {}
    for _attempt in range(3):
        try:
            charts_b64 = generate_charts(
                portfolio, cohort, params["monthly_sip"],
                params["timeline_years"], params["optimise"],
                benchmarks=benchmarks, market_context=market_ctx,
                passive_mc=passive_mc,
                holdings_analysis=holdings_analysis or None,
            )
            console.print(f"  [green]Chart pack : {charts_b64.get('_path', CHART_FILE)}[/green]")
            break
        except Exception as e:
            if _attempt < 2:
                console.print(f"  [yellow]Chart attempt {_attempt+1} failed ({e}) — retrying...[/yellow]")
                time.sleep(1)
            else:
                console.print(f"  [yellow]Chart failed after 3 attempts: {e}[/yellow]")

    # HTML report — up to 3 attempts
    console.print("[cyan]Generating HTML report...[/cyan]")
    for _attempt in range(3):
        try:
            html_path = generate_html_report(
                portfolio=portfolio, params=params,
                tax=tax, passive_mc=passive_mc, advantage=advantage,
                drift=drift, req_sip=req_sip,
                charts_b64=charts_b64, market_context=market_ctx,
                holdings_analysis=holdings_analysis or None,
                cohort=cohort,
                portfolio_holdings=portfolio_holdings,
            )
            console.print(f"  [green]HTML report: {html_path}[/green]")
            break
        except Exception as e:
            if _attempt < 2:
                console.print(f"  [yellow]Report attempt {_attempt+1} failed ({e}) — retrying...[/yellow]")
                time.sleep(1)
            else:
                console.print(f"  [yellow]HTML report failed after 3 attempts: {e}[/yellow]")
                html_path = None

    console.print(Panel(
        "[dim]Tip: populate mf_ter_overrides.json for exact TER scoring.\n"
        "Delete mf_nav_cache.json to force a fresh NAV fetch.[/dim]",
        border_style="dim",
    ))


if __name__ == "__main__":
    main()
