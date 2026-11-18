"""Interactive entry points: questionnaire, portfolio manager, check mode, main()."""

from datetime import datetime
import json
import os
import sys
import time

from rich.panel import Panel
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TextColumn
from rich.prompt import Prompt
from rich.table import Table

from mfa.checkin import run_checkin
from mfa.backtest import load_backtest_summary, print_backtest, run_backtest, save_backtest
from mfa.summary import action_plan_html, in_short, print_in_short
from mfa.tracking import evaluate_recommendations, nav_lookup_from, print_track_record, record_recommendation, track_record_html
from mfa.config import INDEX_FUND_CODE, INDEX_FUND_NAME, AMFI_CACHE_FILE, AMFI_PORTFOLIO_CACHE_FILE, BENCHMARK_CACHE_FILE, CACHE_FILE, CHART_FILE, ENRICH_CACHE_FILE, HOLDINGS_CACHE_FILE
from mfa.data import status
from mfa.data.amfi import fetch_amfi_master, fetch_fund_portfolios
from mfa.data.enrichment import fetch_fund_enrichment
from mfa.data.market import YFINANCE_OK, fetch_benchmarks, get_market_context
from mfa.data.nav import fetch_and_cache, fetch_scheme_nav, load_cache, restore_nav_data
from mfa.holdings.analysis import _clean_holdings, analyse_holdings, display_holdings_analysis, normalise_stored_holdings, prompt_missing_cost_basis
from mfa.holdings.db import get_user_portfolio, pan_key, save_action_plan, save_user_portfolio, show_pending_action_plan
from mfa.holdings.fetch import fetch_portfolio_external, import_from_csv
from mfa.holdings.plan import compute_tax_harvest_plan, generate_action_plan, print_action_plan, print_tax_harvest
from mfa.portfolio import active_vs_passive, build_portfolio, classify_fund, compute_required_sip, compute_stepup_projection, compute_tax, compute_weight_drift, simulate_passive_baseline
from mfa.report import generate_charts, generate_holdings_report, generate_html_report, print_portfolio, print_stepup_analysis
from mfa.ui import console, fmt_inr, rich_ask_choice, rich_ask_corpus, rich_ask_int, rich_header, rich_summary
from mfa.util import _safe_float


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
    holdings, changed = normalise_stored_holdings(
        user_port.get("holdings", []), amfi_master, nav_data)

    # Persist cleaned + code-resolved version (removes junk scraped from DOM)
    if changed:
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

    if action_idx == 1 and prompt_missing_cost_basis(holdings):
        user_port["holdings"] = holdings
        save_user_portfolio(pan_hash, user_port)

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
    pan_hash = pan_key(raw_pan)

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
    status.assess_sources(nav_data, amfi_master, benchmarks, enrichment, portfolio_holdings)

    holdings, changed = normalise_stored_holdings(holdings, amfi_master, nav_data)
    if prompt_missing_cost_basis(holdings):
        changed = True
    if changed:
        user_port["holdings"] = holdings
        save_user_portfolio(pan_hash, user_port)

    # ── Score universe using balanced strategy as neutral reference ───────
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Scoring universe...", total=None)
        _, _, all_scored = build_portfolio(
            nav_data, "balanced", True, 4, 10000, 7,
            amfi_master=amfi_master, benchmarks=benchmarks, aum_data=enrichment,
        )
        prog.update(t, description="Universe scored.")
    status.print_status(status.score_completeness(all_scored))

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
    harvest = compute_tax_harvest_plan(analysis, nav_data, action_plan.get("exemption_remaining"))
    print_tax_harvest(harvest)
    print_in_short(in_short(action_plan, harvest))
    completeness = status.score_completeness(all_scored)
    report_path = generate_holdings_report(
        analysis, action_plan, harvest,
        status_section=status.status_section_html(completeness),
        status_banner=status.status_banner_html(completeness),
        extra_html=action_plan_html(action_plan, harvest))
    console.print(f"  [green]Holdings report: {report_path}[/green]")

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

def backtest_mode() -> None:
    """Walk-forward test: score the universe as of past year-ends and check what happened next."""
    console.print(Panel(
        "Scores the fund universe as it looked at each past year-end (2015–2023), using only\n"
        "data available then, and checks whether the top picks beat their category over the\n"
        "next 1 and 3 years — against a naive 'last 3Y winner' rule and a Nifty 50 index fund.",
        title="[bold]Backtest[/bold]", border_style="cyan"))
    strat = ["balanced", "aggressive", "conservative"][rich_ask_choice(
        "Which strategy weights should be tested?", ["Balanced", "Aggressive", "Conservative"])]

    cache    = load_cache()
    nav_data = restore_nav_data(cache) if cache else fetch_and_cache()
    if not nav_data:
        console.print("[bold red]Could not load NAV data.[/bold red]")
        return
    benchmarks = fetch_benchmarks() if YFINANCE_OK else {}
    index_df   = fetch_scheme_nav(INDEX_FUND_CODE)
    if index_df is None:
        console.print(f"  [yellow]{INDEX_FUND_NAME} NAV unavailable — index comparison skipped.[/yellow]")

    result = run_backtest(nav_data, benchmarks, index_df, strat)
    if not result["rows"]:
        console.print("[yellow]Not enough history in the cached universe to backtest.[/yellow]")
        return
    print_backtest(result)
    path = save_backtest(result)
    console.print(f"  [green]Backtest report: {path}[/green]")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="MF_analyser.py", description="Mutual fund portfolio analyser")
    ap.add_argument("--check-in", action="store_true",
                    help="non-interactive monthly check of stored holdings (alerts only on change)")
    ap.add_argument("--profile", help="portfolio key when more than one is stored")
    ap.add_argument("--no-notify", action="store_true", help="skip the Windows notification")
    args = ap.parse_args(argv)
    if args.check_in:
        sys.exit(run_checkin(args.profile, send_notification=not args.no_notify))

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
            "Backtest the scoring model  (did past top picks actually win?)",
        ],
    )

    if mode == 1:
        portfolio_check_mode()
        return

    if mode == 3:
        backtest_mode()
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
    pan_hash = pan_key(raw_pan) if raw_pan else None

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
    status.assess_sources(nav_data, amfi_master, benchmarks, enrichment, portfolio_holdings)
    refusal = status.nav_refusal_reason()
    if refusal:
        status.print_status()
        console.print(Panel(f"[bold red]{refusal}[/bold red]", title="Not recommending",
                            border_style="red"))
        sys.exit(1)

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
    completeness = status.score_completeness(all_scored_universe)
    status.print_status(completeness)

    # ── Existing portfolio analysis (if PAN provided) ───────────────────
    # Reuse the already-scored universe from build_portfolio — no re-computation needed.
    holdings_analysis: list = []
    action_plan = harvest = None
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
        harvest = compute_tax_harvest_plan(holdings_analysis, nav_data,
                                           action_plan.get("exemption_remaining"))
        print_tax_harvest(harvest)
        print_in_short(in_short(action_plan, harvest))

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

    # ── Track record: how did past recommendations do vs the index fund? ──
    index_df  = fetch_scheme_nav(INDEX_FUND_CODE)
    index_now = float(index_df["nav"].iloc[-1]) if index_df is not None and not index_df.empty else None
    track_rows = evaluate_recommendations(nav_lookup_from(nav_data), index_now)
    print_track_record(track_rows)
    if record_recommendation(portfolio, params["optimise"], index_now):
        console.print("  [dim]Recommendation saved — future runs will show how it did vs the index fund.[/dim]")
    extra_html = (action_plan_html(action_plan, harvest)
                  + track_record_html(track_rows, load_backtest_summary())
                  + status.status_section_html(completeness))

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
                extra_sections_html=extra_html,
                status_banner_html=status.status_banner_html(completeness),
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
