"""mfapi.in NAV history: fetch, cache, restore."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
import json
import os
import time

from rich.progress import BarColumn
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TextColumn
import pandas as pd
import requests

from mfa.config import BASE_URL, CACHE_FILE, CACHE_MAX_AGE_HRS, EXTRA_NAV_CACHE_FILE, DEBT_CATEGORY_KW, EQUITY_CATEGORY_KW, EXCLUDE_KEYWORDS, MAX_WORKERS, REQUIRED_KEYWORDS, SLEEP_BETWEEN
from mfa.data import status
from mfa.ui import console
from mfa.util import _cache_busted, _save_output_file


def load_cache() -> dict:
    if _cache_busted() or not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f: data = json.load(f)
        age = (time.time() - data.get("timestamp", 0)) / 3600
        if data.get("partial"):
            console.print(f"  [yellow]NAV cache is partial ({data.get('coverage', 0):.0%} coverage) — will re-fetch.[/yellow]")
            return {}
        if age > CACHE_MAX_AGE_HRS:
            console.print(f"  [yellow]NAV cache {age:.1f}h old — will re-fetch.[/yellow]")
            return {}
        console.print(f"  [green]NAV cache: {len(data.get('data',[]))} funds, {age:.1f}h old.[/green]")
        cov = data.get("coverage")
        status.record("NAV history (mfapi.in)", cov is None or cov >= 0.95,
                      f"{len(data.get('data', []))} funds (cache {age:.1f}h old)", coverage=cov)
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
    results = []
    n_short, failed = 0, []

    def fetch_one(scheme):
        """Returns a nav_data item, "short" (<1Y history — legitimately excluded)
        or "failed" after 3 attempts (rate limit / timeout / server error)."""
        code = scheme.get("schemeCode")
        for attempt in range(3):
            try:
                time.sleep(SLEEP_BETWEEN * (1 + 4 * attempt))
                r = requests.get(f"{BASE_URL}/{code}", timeout=20)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2.0 * (attempt + 1))        # back off, then retry
                    continue
                r.raise_for_status()
                data = r.json()
                navs = data.get("data", [])
                if len(navs) < 252:
                    return "short"
                df = pd.DataFrame(navs)
                df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
                df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
                df = df.dropna().sort_values("date").reset_index(drop=True)
                # Store official scheme_category from meta
                meta = data.get("meta", {})
                scheme["_sebi_cat"] = meta.get("scheme_category", "")
                return {"scheme": scheme, "nav_df": df}
            except Exception:
                time.sleep(1.0 + attempt)
        return "failed"

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  console=console) as prog:
        task = prog.add_task("Fetching NAVs", total=len(filtered))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(fetch_one, s): s for s in filtered}
            for fut in as_completed(futures):
                res = fut.result()
                if res == "short":
                    n_short += 1
                elif res == "failed":
                    failed.append(futures[fut])
                else:
                    results.append(res)
                prog.update(task, advance=1)

    # Second pass: failures are usually rate limiting under 10 workers — retry
    # them one at a time, which mfapi.in tolerates.
    if failed:
        console.print(f"  [dim]Retrying {len(failed)} failed fetches sequentially...[/dim]")
        still_failed = []
        for s in failed:
            time.sleep(0.5)
            res = fetch_one(s)
            if res == "short":
                n_short += 1
            elif res == "failed":
                still_failed.append(s)
            else:
                results.append(res)
        failed = still_failed

    # Failed fetches must not silently shrink the universe: reuse the previous
    # cache's copy of those funds (stale by a day or so beats missing).
    merged = 0
    if failed and os.path.exists(CACHE_FILE):
        failed_codes = {str(s.get("schemeCode")) for s in failed}
        try:
            with open(CACHE_FILE) as f:
                prev = json.load(f)
            prev_items = [it for it in prev.get("data", [])
                          if str(it["scheme"].get("schemeCode")) in failed_codes]
            results.extend(restore_nav_data({"data": prev_items}))
            merged = len(prev_items)
        except Exception:
            pass
    attempted = max(len(filtered) - n_short, 1)
    coverage  = len(results) / attempted
    partial   = coverage < 0.80
    colour    = "green" if coverage >= 0.95 else ("yellow" if not partial else "red")
    console.print(
        f"  [{colour}]NAV coverage {coverage:.0%}: fetched {len(results) - merged}/{attempted}"
        f", {merged} from previous cache, {attempted - len(results)} missing"
        f" ({n_short} skipped: <1Y history).[/{colour}]")
    status.record("NAV history (mfapi.in)", coverage >= 0.95,
                  f"{len(results)} funds; {merged} reused from previous cache, "
                  f"{attempted - len(results)} missing", coverage=round(coverage, 4))
    if partial:
        console.print("  [red]Too many NAV fetches failed — cache marked partial; "
                      "re-run later for a complete universe.[/red]")

    serialisable = [
        {"scheme": item["scheme"],
         "nav_records": item["nav_df"][["date","nav"]].assign(
             date=item["nav_df"]["date"].dt.strftime("%d-%m-%Y")
         ).to_dict(orient="records")}
        for item in results
    ]
    payload = json.dumps({"timestamp": time.time(), "data": serialisable,
                          "coverage": round(coverage, 4), "partial": partial})
    def _write_cache(fh):
        fh.write(payload.encode())
    _save_output_file(_write_cache, CACHE_FILE)
    return results


def fetch_scheme_nav(code, max_age_hrs: float = CACHE_MAX_AGE_HRS) -> pd.DataFrame | None:
    """
    NAV history for one scheme outside the scored universe (the index-fund
    yardstick, a Regular-plan holding, ...). Cached per code in
    EXTRA_NAV_CACHE_FILE. Returns DataFrame[date, nav] or None.
    """
    code = str(code)
    cache: dict = {}
    if os.path.exists(EXTRA_NAV_CACHE_FILE):
        try:
            with open(EXTRA_NAV_CACHE_FILE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    entry = cache.get(code)
    if entry and (time.time() - entry.get("timestamp", 0)) / 3600 < max_age_hrs and not _cache_busted():
        return restore_nav_data({"data": [{"scheme": {}, "nav_records": entry["nav_records"]}]})[0]["nav_df"]

    records = None
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE_URL}/{code}", timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            records = [{"date": d["date"], "nav": d["nav"]} for d in r.json().get("data", [])]
            break
        except Exception:
            time.sleep(1.0 + attempt)
    if not records:
        if entry:      # stale copy beats nothing
            return restore_nav_data({"data": [{"scheme": {}, "nav_records": entry["nav_records"]}]})[0]["nav_df"]
        return None
    cache[code] = {"timestamp": time.time(), "nav_records": records}
    try:
        _p = json.dumps(cache).encode()
        _save_output_file(lambda fh: fh.write(_p), EXTRA_NAV_CACHE_FILE)
    except Exception:
        pass
    return restore_nav_data({"data": [{"scheme": {}, "nav_records": records}]})[0]["nav_df"]
