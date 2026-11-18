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

from mfa.config import BASE_URL, CACHE_FILE, CACHE_MAX_AGE_HRS, DEBT_CATEGORY_KW, EQUITY_CATEGORY_KW, EXCLUDE_KEYWORDS, MAX_WORKERS, REQUIRED_KEYWORDS, SLEEP_BETWEEN
from mfa.ui import console
from mfa.util import _cache_busted, _save_output_file


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
