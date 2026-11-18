"""yfinance benchmarks and market-regime signal."""

import json
import os
import time

import numpy as np
import pandas as pd

from mfa.config import BENCHMARK_CACHE_FILE, BENCH_CACHE_MAX_AGE_DAYS, CATEGORY_BENCHMARK_TICKERS, MIDCAP_TICKERS
from mfa.ui import console
from mfa.util import _cache_busted, _save_output_file


try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False



def _fetch_ticker(ticker: str, cache: dict) -> pd.Series | None:
    """Fetch one yfinance ticker (full monthly history), using in-memory cache dict."""
    if ticker in cache:
        return cache[ticker]
    if not YFINANCE_OK:
        return None
    try:
        raw = yf.download(ticker, period="max", interval="1mo",
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
            # Older caches only held 10Y — the backtest needs full history
            if age_days < BENCH_CACHE_MAX_AGE_DAYS and cached.get("period") == "max":
                result = {}
                for k, pairs in cached.items():
                    if k in ("timestamp", "period"): continue
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
    serialisable = {"timestamp": time.time(), "period": "max"}
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
