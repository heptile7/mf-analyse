"""Return / risk metrics, alpha-beta, cohort statistics."""

from datetime import timedelta

import numpy as np
import pandas as pd

from mfa.config import RISK_FREE_MONTHLY


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
