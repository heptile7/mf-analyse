"""
Monthly check-in: non-interactive re-rating of stored holdings that speaks up
only when something changed.

    python MF_analyser.py --check-in [--profile KEY] [--no-notify]

Alerts (compute_alerts is pure and unit-tested):
  * a rating got worse (e.g. HOLD → SWITCH) or a fund dropped a category quintile
  * a holding is rated EXIT / SWITCH on the very first check-in
  * the January–March harvest window is open and the exemption is unused
  * action-plan items still pending after 30 days
  * critical data sources failed (NAV, AMFI)
Output: mf_checkin_<date>.html + a Windows toast (silent when nothing changed).
Snapshots are kept in the portfolio DB (`checkins`, capped) — local only.
"""
from datetime import datetime
import html as html_lib
import math
import os
import subprocess

from mfa.config import CHECKIN_REPORT_PATTERN
from mfa.data import status
from mfa.data.amfi import fetch_amfi_master
from mfa.data.enrichment import fetch_fund_enrichment
from mfa.data.market import YFINANCE_OK, fetch_benchmarks
from mfa.data.nav import fetch_and_cache, load_cache, restore_nav_data
from mfa.holdings.analysis import analyse_holdings, normalise_stored_holdings
from mfa.holdings.db import load_portfolio_db, save_portfolio_db
from mfa.holdings.plan import compute_tax_harvest_plan, generate_action_plan
from mfa.portfolio import build_portfolio
from mfa.report import generate_holdings_report
from mfa.summary import action_plan_html, in_short
from mfa.ui import console, fmt_inr

CHECKIN_HISTORY = 24
PENDING_STALE_DAYS = 30
SEVERITY = {"BUY MORE": 0, "HOLD": 1, "HOLD/WATCH": 2, "SWITCH": 3, "EXIT": 4, "UNKNOWN": -1}
CRITICAL_SOURCES = ("NAV history (mfapi.in)", "AMFI scheme master")


# ── pure logic ──────────────────────────────────────────────────────────
def quintile(rank, size) -> int | None:
    if not rank or not size:
        return None
    return min(5, max(1, math.ceil(rank / size * 5)))       # 1 = top fifth


def make_snapshot(analysis: list, today: str, status_snapshot: dict | None = None) -> dict:
    failed = sorted(s for s in CRITICAL_SOURCES if not (status_snapshot or {}).get(s, {"ok": True})["ok"])
    return {"date": today, "failed_sources": failed, "holdings": {
        str(h.get("amfi_code") or h.get("scheme_code")): {
            "name": h.get("scheme_name", "?"), "action": h.get("action", "UNKNOWN"),
            "rank": h.get("category_rank"), "size": h.get("category_size"),
            "quintile": quintile(h.get("category_rank"), h.get("category_size")),
        } for h in analysis}}


def compute_alerts(prev: dict | None, cur: dict, harvest: dict | None, pending: dict | None,
                   status_snapshot: dict, today: datetime) -> list:
    alerts = []
    for code, h in cur["holdings"].items():
        p = (prev or {}).get("holdings", {}).get(code)
        if p is None:
            if prev is None and h["action"] in ("SWITCH", "EXIT"):
                alerts.append(f"{h['name']} is rated {h['action']}.")
            continue
        if SEVERITY.get(h["action"], -1) > SEVERITY.get(p["action"], -1) >= 0:
            alerts.append(f"{h['name']}: rating fell {p['action']} → {h['action']}.")
        elif h["quintile"] and p.get("quintile") and h["quintile"] > p["quintile"]:
            alerts.append(f"{h['name']} dropped from quintile {p['quintile']} to {h['quintile']} "
                          f"(rank {h['rank']}/{h['size']}).")
    if today.month in (1, 2, 3) and harvest and harvest.get("actions"):
        alerts.append(f"Tax-harvest window is open until 31 March: booking gains could save about "
                      f"{fmt_inr(harvest['total_tax_saved'])}.")
    if pending and pending.get("count"):
        age = (today - datetime.fromisoformat(pending["since"])).days
        if age > PENDING_STALE_DAYS:
            alerts.append(f"{pending['count']} action-plan item(s) from {pending['since']} are still pending.")
    already = set((prev or {}).get("failed_sources", []))
    for src in CRITICAL_SOURCES:                  # only newly failing sources, not every month
        s = status_snapshot.get(src)
        if s and not s["ok"] and src not in already:
            alerts.append(f"Data problem — {src}: {s.get('detail') or 'degraded'}.")
    return alerts


def choose_portfolio(db: dict, profile: str | None) -> tuple:
    ports = db.get("portfolios", {})
    if profile:
        return (profile, ports[profile]) if profile in ports else (None, None)
    if len(ports) == 1:
        return next(iter(ports.items()))
    return None, None


# ── notification ────────────────────────────────────────────────────────
def notify(title: str, body: str) -> bool:
    """Windows toast via built-in WinRT (Windows PowerShell 5.1). Never raises."""
    if os.name != "nt":
        return False
    q = lambda s: s.replace("'", "''")[:240]
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$x = $t.GetElementsByTagName('text'); $x[0].AppendChild($t.CreateTextNode('{q(title)}')) | Out-Null;"
        f"$x[1].AppendChild($t.CreateTextNode('{q(body)}')) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ── runner ──────────────────────────────────────────────────────────────
def alerts_html(alerts: list, prev_date: str | None) -> str:
    esc = html_lib.escape
    items = "".join(f"<li>{esc(a)}</li>" for a in alerts) or "<li>Nothing changed since the last check-in.</li>"
    since = f" since {esc(prev_date)}" if prev_date else " (first check-in)"
    return (f"<div class='section'><h2>🔔 What changed{since}</h2><ul>{items}</ul></div>")


def run_checkin(profile: str | None = None, send_notification: bool = True,
                today: datetime | None = None) -> int:
    today = today or datetime.now()
    db = load_portfolio_db()
    key, port = choose_portfolio(db, profile)
    if not port:
        n = len(db.get("portfolios", {}))
        console.print(f"[red]Check-in needs exactly one stored portfolio or --profile KEY "
                      f"({n} stored).[/red]")
        return 2
    if not port.get("holdings"):
        console.print("[yellow]No holdings stored — nothing to check.[/yellow]")
        return 0

    status.reset()
    cache = load_cache()
    nav_data = restore_nav_data(cache) if cache else fetch_and_cache()
    if not nav_data:
        console.print("[red]Could not load NAV data.[/red]")
        if send_notification:
            notify("MF Analyse check-in failed", "NAV data could not be loaded.")
        return 1
    amfi_master = fetch_amfi_master()
    benchmarks  = fetch_benchmarks() if YFINANCE_OK else {}
    enrichment  = fetch_fund_enrichment(nav_data)
    status.assess_sources(nav_data, amfi_master, benchmarks, enrichment, {"skipped": True})

    holdings, _ = normalise_stored_holdings(port["holdings"], amfi_master, nav_data)
    _, _, scored = build_portfolio(nav_data, "balanced", True, 4, 10000, 7,
                                   amfi_master=amfi_master, benchmarks=benchmarks, aum_data=enrichment)
    analysis = analyse_holdings(holdings, scored, nav_data, amfi_master)
    sip = sum(h.get("sip_amount") or 0 for h in analysis) or 10_000
    plan = generate_action_plan(analysis, scored, "balanced", sip, nav_data)
    harvest = compute_tax_harvest_plan(analysis, nav_data, plan.get("exemption_remaining"))

    stored = port.get("action_plan") or {}
    pend = [i for i in stored.get("items", []) if i.get("status") == "pending"]
    pending = {"count": len(pend), "since": stored.get("generated_on")} if pend else None

    history = port.get("checkins", [])
    prev = history[-1] if history else None
    snap = make_snapshot(analysis, today.strftime("%Y-%m-%d"), status.snapshot())
    alerts = compute_alerts(prev, snap, harvest, pending, status.snapshot(), today)

    completeness = status.score_completeness(scored)
    path = CHECKIN_REPORT_PATTERN.format(date=today.strftime("%Y-%m-%d"))
    generate_holdings_report(
        analysis, plan, harvest, path=path, title=f"Monthly check-in — {today:%d %b %Y}",
        status_section=status.status_section_html(completeness),
        status_banner=status.status_banner_html(completeness),
        extra_html=alerts_html(alerts, (prev or {}).get("date")) + action_plan_html(plan, harvest))

    db = load_portfolio_db()                       # re-read: don't clobber concurrent edits
    p = db["portfolios"].setdefault(key, port)
    p["checkins"] = (p.get("checkins", []) + [snap])[-CHECKIN_HISTORY:]
    save_portfolio_db(db)

    console.print(f"  [green]Check-in report: {path}[/green]")
    for a in alerts:
        console.print(f"  [yellow]• {a}[/yellow]")
    if not alerts:
        console.print("  [dim]Nothing changed — no notification sent.[/dim]")
    elif send_notification:
        top = in_short(plan, harvest, limit=1)
        body = alerts[0] + (f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else "")
        if top and len(body) < 160:
            body += " Next: " + top[0]
        notify(f"MF Analyse: {len(alerts)} thing(s) need attention", body + f" Report: {os.path.basename(path)}")
    return 0
