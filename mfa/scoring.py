"""TER lookup and the 7-component fund score."""

import json
import os

from mfa.config import AUM_MIDCAP_WARN_CR, AUM_SMALLCAP_WARN_CR, DEFAULT_TER, SCORE_WEIGHTS, TER_OVERRIDE_FILE
from mfa.ui import console
from mfa.util import _safe_float


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
