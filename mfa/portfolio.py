"""Portfolio construction, Monte Carlo, tax, passive baseline, goal SIP, step-up."""

import math
import re

import numpy as np
import pandas as pd

from mfa.config import AMC_CONCENTRATION_PENALTY, AMC_MAX_FUNDS, AUM_MIDCAP_WARN_CR, AUM_SMALLCAP_WARN_CR, BENCHMARK, CORRELATION_THRESHOLD, LTCG_EXEMPTION_PA, LTCG_RATE_EQUITY, MC_N_SIMS_GOAL, MC_N_SIMS_STANDARD, PASSIVE_DEBT_RETURN, PASSIVE_EQUITY_SPLIT, PASSIVE_TER_DEBT, PASSIVE_TER_EQUITY, RISK_FREE_MONTHLY, SEBI_TO_INTERNAL, SURVIVORSHIP_HAIRCUT_EXTRA
from mfa.data.market import get_category_benchmark
from mfa.metrics import compute_alpha_beta, compute_cohort_stats, compute_metrics, compute_rank_consistency
from mfa.scoring import get_ter, load_ter_overrides, score_fund, sync_ter_overrides_from_enrichment
from mfa.ui import console
from mfa.util import _safe_float


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
