"""Resolve stored holdings to AMFI codes and rate them HOLD / SWITCH / EXIT."""

from datetime import datetime
import difflib
import re

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from mfa.metrics import cagr
from mfa.portfolio import classify_fund
from mfa.ui import console, fmt_inr, pct_str
from mfa.util import _safe_float


# ══════════════════════════════════════════════════════════════════════
# HOLDINGS RESOLUTION  (RTA codes / ISINs / names → AMFI code + scored fund)
# ══════════════════════════════════════════════════════════════════════

_NAME_STOP = {"fund", "plan", "option", "growth", "direct", "regular", "dir", "reg",
              "gr", "gro", "mf", "the", "retail", "scheme", "g", "opt"}
_NAME_SUBS = [(r"small\s*-?\s*cap", "small cap"), (r"mid\s*-?\s*cap", "mid cap"),
              (r"large\s*-?\s*cap", "large cap"), (r"flexi\s*-?\s*cap", "flexi cap"),
              (r"multi\s*-?\s*cap", "multi cap"), (r"multi\s*-?\s*asset", "multi asset"),
              (r"plangro\w*", "plan growth")]
# Tokens that change what a fund *is* — names differing in these never match fuzzily
_CATEGORY_TOKENS = {"and", "small", "mid", "large", "cap", "flexi", "multi", "value",
                    "contra", "focused", "elss", "tax", "index", "etf", "debt", "liquid",
                    "overnight", "duration", "hybrid", "asset", "allocation", "bonus",
                    "idcw", "dividend", "payout", "reinvestment", "nifty", "sensex",
                    "technology", "commodities", "retirement"}
_ISIN_RE = re.compile(r"INF[0-9A-Z]{9}")


def _fund_name_key(name: str) -> str:
    """Normalise a fund name so Direct/Regular/CAS/mfapi spellings compare equal."""
    s = (name or "").lower().replace("&", " and ")
    m = re.match(r"^.*?\bmf\s*-\s*(.+)$", s)          # CAS: "NIPPON INDIA MF-NIPPON INDIA ..."
    if m:
        s = m.group(1)
    s = re.sub(r"\([^)]*\)?", " ", s)                  # "(formerly ...)" incl. truncated "(fo"
    for pat, rep_ in _NAME_SUBS:
        s = re.sub(pat, rep_, s)
    return " ".join(t for t in re.findall(r"[a-z0-9]+", s) if t not in _NAME_STOP)


def _plan_type(name: str) -> str | None:
    n = (name or "").lower()
    if re.search(r"\b(regular|reg)\b", n): return "regular"
    if re.search(r"\b(direct|dir)\b", n):  return "direct"
    return None


def _same_kind(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    return not ((ta ^ tb) & _CATEGORY_TOKENS)


def build_holdings_index(universe: list, nav_data: list, amfi_master: dict) -> dict:
    """Lookup tables for resolve_holding(). universe = scored fund dicts (may be [])."""
    idx = {"by_code": {}, "by_key": {}, "by_amc": {}, "isin_to_code": {},
           "amfi_key": {}, "nav": {}}
    for f in universe or []:
        code = str(f.get("scheme_code", ""))
        idx["by_code"].setdefault(code, f)
        key = _fund_name_key(f.get("name", ""))
        if key:
            idx["by_key"].setdefault(key, f)
            idx["by_amc"].setdefault(key.split()[0], []).append((key, f))
    for item in nav_data or []:
        sch  = item["scheme"]
        code = str(sch.get("schemeCode", ""))
        for k in ("isinGrowth", "isinDivReinvestment"):
            if sch.get(k):
                idx["isin_to_code"][sch[k].upper()] = code
        try:
            idx["nav"][code] = float(item["nav_df"]["nav"].iloc[-1])
        except Exception:
            pass
    for code, v in (amfi_master or {}).items():
        for k in ("isin", "isin_reinv"):
            if v.get(k):
                idx["isin_to_code"].setdefault(v[k].upper(), code)
        if v.get("name"):
            idx["amfi_key"].setdefault(
                (_fund_name_key(v["name"]), _plan_type(v["name"])), code)
    return idx


def _match_by_name(key: str, idx: dict) -> tuple:
    if not key:
        return None, None
    f = idx["by_key"].get(key)
    if f:
        return f, "name"
    cands = [(k, f) for k, f in idx["by_amc"].get(key.split()[0], []) if _same_kind(key, k)]
    best, best_r = None, 0.0
    for k, f in cands:
        r = difflib.SequenceMatcher(None, key, k).ratio()
        if r > best_r:
            best, best_r = f, r
    if best is not None and best_r >= 0.88:
        return best, "name_fuzzy"
    toks = set(key.split())
    subset = {k: f for k, f in cands if toks <= set(k.split()) or set(k.split()) <= toks}
    if len(subset) == 1:
        return next(iter(subset.values())), "name_partial"
    return None, None


def resolve_holding(h: dict, idx: dict, amfi_master: dict) -> dict:
    """
    Resolve a stored holding to
      amfi_code — the scheme actually held (for NAV / valuation), may be a Regular plan
      scored    — the matching fund in the scored (Direct-Growth) universe, or None
      match     — how `scored` was found: code / isin / name / name_fuzzy / name_partial
      is_regular— True when the holding is a Regular plan (scored = its Direct sibling)
    Order: 6-digit AMFI code → ISIN (either ISIN column) → normalised name.
    Short codes like "51", "MCOG", "EAAG" are CAMS/KFin RTA product codes and are ignored.
    """
    raw  = str(h.get("scheme_code") or "").strip()
    isin = str(h.get("isin") or "").strip().upper()
    if not isin and _ISIN_RE.fullmatch(raw.upper()):
        isin = raw.upper()
    name = h.get("scheme_name", "")
    key  = _fund_name_key(name)
    plan = _plan_type(name)

    own = raw if (raw.isdigit() and len(raw) >= 6
                  and (raw in amfi_master or raw in idx["by_code"] or raw in idx["nav"])) else None
    if not own and isin:
        own = idx["isin_to_code"].get(isin)
    if not own and key:
        own = idx["amfi_key"].get((key, plan)) or (
            idx["amfi_key"].get((key, None)) if plan is None else None)
    if plan is None and own and amfi_master.get(own, {}).get("name"):
        plan = _plan_type(amfi_master[own]["name"])
    is_regular = plan == "regular"

    scored, how = None, None
    if own and own in idx["by_code"]:
        scored, how = idx["by_code"][own], ("isin" if own != raw else "code")
    if scored is None:
        scored, how = _match_by_name(key, idx)
    return {"amfi_code": own, "scored": scored, "match": how, "is_regular": is_regular}


def normalise_stored_holdings(holdings: list, amfi_master: dict, nav_data: list = None) -> tuple:
    """
    Clean junk rows and rewrite RTA codes / ISINs to AMFI scheme codes.
    The original code is preserved in `rta_code`. Returns (holdings, changed).
    """
    cleaned = _clean_holdings(holdings)
    changed = len(cleaned) != len(holdings)
    idx = build_holdings_index([], nav_data or [], amfi_master)
    for h in cleaned:
        res  = resolve_holding(h, idx, amfi_master)
        code = res["amfi_code"]
        raw  = str(h.get("scheme_code") or "")
        if code and code != raw:
            if _ISIN_RE.fullmatch(raw.upper()) and not h.get("isin"):
                h["isin"] = raw.upper()
            elif raw:
                h.setdefault("rta_code", raw)
            h["scheme_code"] = code
            changed = True
        if code and not h.get("isin") and (amfi_master.get(code) or {}).get("isin"):
            h["isin"] = amfi_master[code]["isin"]
            changed = True
    return cleaned, changed


def prompt_missing_cost_basis(holdings: list) -> bool:
    """Offer to enter invested amounts for holdings with units but no avg NAV."""
    missing = [h for h in holdings if (h.get("units") or 0) > 0 and not (h.get("avg_nav") or 0)]
    if not missing:
        return False
    ans = Prompt.ask(
        f"  [green]{len(missing)} holdings have no cost basis (gain/tax-harvest can't be computed). "
        f"Enter invested amounts now?[/green]", choices=["y", "n"], default="n")
    if ans != "y":
        return False
    changed = False
    for h in missing:
        amt = _safe_float(Prompt.ask(
            f"  Invested ₹ in [cyan]{h.get('scheme_name','')[:50]}[/cyan] "
            f"({h['units']:.3f} units) — blank to skip", default="").replace(",", ""))
        if not amt:
            continue
        h["avg_nav"] = round(amt / h["units"], 4)
        if not h.get("start_date"):
            sd = Prompt.ask("    First investment date YYYY-MM-DD (blank to skip)", default="").strip()
            try:
                datetime.fromisoformat(sd); h["start_date"] = sd
            except ValueError:
                pass
        changed = True
    return changed


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
    idx = build_holdings_index(all_scored, nav_data, amfi_master)

    results = []
    for h in user_holdings:
        name        = h.get("scheme_name", "")
        res         = resolve_holding(h, idx, amfi_master)
        scored_fund = res["scored"]
        own_code    = res["amfi_code"]
        is_regular  = res["is_regular"]
        code        = own_code or str(h.get("scheme_code", ""))

        # Current NAV of the scheme actually held. Never value a Regular holding
        # at its Direct sibling's NAV — the two NAVs diverge by the TER gap.
        current_nav = (idx["nav"].get(own_code or "")
                       or (amfi_master.get(own_code or "") or {}).get("nav")
                       or (idx["nav"].get(str(scored_fund["scheme_code"]))
                           if scored_fund and not is_regular else None)
                       or h.get("current_nav") or 0)
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
            if is_regular:
                reason += (" · Regular plan — ranked via its Direct sibling; Direct typically "
                           "costs ~0.5–1% p.a. less (route new SIPs there; check exit load/LTCG before switching)")
        else:
            cat = classify_fund(name, code, amfi_master)
            cat_rank = cat_size = score = alpha = None
            action, col, reason = "UNKNOWN", "dim", "Fund not found in universe — add to cache"

        results.append({
            **h,
            "scheme_code":    str(scored_fund["scheme_code"]) if scored_fund else code,
            "amfi_code":      own_code,
            "match":          res["match"],
            "ter":            scored_fund.get("ter") if scored_fund else None,
            "is_regular":     is_regular,
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
