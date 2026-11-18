"""Constants: endpoints, cache paths, thresholds, scoring weights, category maps."""

import os


# ══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════

BASE_URL        = "https://api.mfapi.in/mf"
AMFI_NAV_URL    = "https://www.amfiindia.com/spages/NAVAll.txt"
MFDATA_BASE_URL = "https://mfdata.in/api/v1"

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
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
PAN_SALT_FILE        = os.path.join(_DIR, ".mf_salt")
EXTRA_NAV_CACHE_FILE = os.path.join(_DIR, "mf_extra_nav_cache.json")
BACKTEST_HTML_FILE   = os.path.join(_DIR, "mf_backtest_report.html")
BACKTEST_SUMMARY_FILE = os.path.join(_DIR, "mf_backtest_summary.json")
RECOMMENDATIONS_FILE = os.path.join(_DIR, "mf_recommendations.json")
EXIT_LOAD_OVERRIDE_FILE = os.path.join(_DIR, "mf_exit_load_overrides.json")

# Exit load (pct, days): typical equity funds charge 1% within 1 year; debt mostly 0.
# No data source publishes this reliably — per-scheme values go in EXIT_LOAD_OVERRIDE_FILE.
DEFAULT_EXIT_LOAD = {"debt": (0.0, 0), "elss": (0.0, 0), "default": (1.0, 365)}
ELSS_LOCKIN_DAYS  = 3 * 365

# Passive yardstick: a real index fund (total return, net of its own costs) beats
# comparing against the ^NSEI price index, which ignores dividends.
INDEX_FUND_CODE = "120716"
INDEX_FUND_NAME = "UTI Nifty 50 Index Fund - Direct Growth"


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
