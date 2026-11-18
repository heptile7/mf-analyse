# CLAUDE.md

Interactive terminal tool for Indian mutual funds. It does two things:

- **Build mode** scores the Direct-Growth fund universe and builds a SIP portfolio. It also projects outcomes with Monte Carlo, estimates tax, compares against a passive baseline, and writes an HTML report and a PNG chart pack.
- **Check mode** rates the user's existing holdings HOLD / SWITCH / EXIT and produces an action plan and an LTCG tax-harvest plan.

This is not SEBI-registered advice. Keep that disclaimer in any user-facing output you add.

## Run

```bash
.venv/Scripts/python MF_analyser.py              # interactive; 3 modes: build / check holdings / clear caches
.venv/Scripts/python -m pytest -q                # offline tests (tests/), ~2s
.venv/Scripts/python -m pyflakes mfa             # lint; only the playwright/casparser probe imports should warn
```

- Install with `pip install -r requirements.txt -r requirements-dev.txt`.
- The holdings importers install `playwright` and `casparser` on demand.
- Windows + Git Bash. The project folder is protected by Controlled Folder Access, so writes can fail with `PermissionError` (see Conventions).

## Layout

`MF_analyser.py` is only the entry point. All code lives in `mfa/`, and the import graph is acyclic:

```
config → ui, util → data.* → metrics → scoring → portfolio → report, holdings.* → cli
```

Each layer imports only from the layers to its left. Keep it that way.

| Module | Role |
|---|---|
| `mfa/config.py` | All constants: endpoints, cache paths (`_DIR` = project root), TTLs, `SCORE_WEIGHTS`, SEBI category maps, include/exclude keywords |
| `mfa/ui.py` | Shared `console` plus `rich_ask_*` prompt helpers. **All terminal output goes through Rich.** |
| `mfa/util.py` | `_save_output_file` (atomic write), `_cache_busted`, `_safe_float` |
| `mfa/data/nav.py` | mfapi.in NAV histories, which are the scoring universe (Direct + Growth only) |
| `mfa/data/amfi.py` | AMFI NAVAll master (code → category, AMC, ISINs, name with plan/option, latest NAV), AUM, monthly portfolio disclosures |
| `mfa/data/market.py` | yfinance benchmarks (`YFINANCE_OK` guard), market regime signal |
| `mfa/data/enrichment.py` | mfdata.in TER, AUM, PE and category ranks |
| `mfa/metrics.py` | CAGR, rolling returns, alpha/beta, capture ratios, cohort statistics |
| `mfa/scoring.py` | TER resolution (overrides → mfdata → `DEFAULT_TER`), `score_fund` |
| `mfa/portfolio.py` | `score_universe` (passes 1–5, with no file or network I/O, so it can be reused by the backtest), `build_portfolio` (slot selection, correlation and AMC caps), SIP allocation, Monte Carlo, tax, passive baseline, required SIP, step-up |
| `mfa/backtest.py` | Walk-forward backtest. It truncates NAV and benchmark data to each cutoff, calls `score_universe` with no enrichment or TER overrides, and judges picks on forward CAGR against a naive rule and the index fund |
| `mfa/tracking.py` | Track record. `record_recommendation` (build mode) writes `mf_recommendations.json`; `evaluate_recommendations` compares weighted NAV returns with the index fund; `since_return` is used for pending action-plan switches |
| `mfa/report.py` | `generate_charts`, `generate_html_report` (self-contained, base64 images; extra sections come in through `extra_sections_html`), `print_portfolio` |
| `mfa/holdings/fetch.py` | Importers: MFCentral browser capture (Playwright, OTP typed by the user), CAS PDF (casparser), CSV |
| `mfa/holdings/db.py` | `mf_portfolio_db.json` keyed by `pan_key(pan)`. `action_plan` is the latest plan (pending-item logic reads it); `action_plans` is the history, capped at 24. Switch items store the from/to NAVs at plan time |
| `mfa/holdings/analysis.py` | Holdings resolution and rating (see below) |
| `mfa/holdings/costs.py` | Switch economics: `build_lots` (FIFO from CAS transactions, else one approximate lot), `switch_cost` (exit load, STCG/LTCG, ELSS lock-in), the shared `ExemptionBudget`, `measured_cost_gap_pa` (Regular vs Direct NAV), breakeven |
| `mfa/holdings/plan.py` | Action plan (switch / gap-fill / buy-more) and tax-harvest plan |
| `mfa/cli.py` | `main`, `run_questionnaire`, `run_portfolio_manager`, `portfolio_check_mode` |

## Data and caches (project root, all git-ignored)

| File | Source | TTL |
|---|---|---|
| `mf_nav_cache.json` | mfapi.in | 24h |
| `mf_amfi_master.json` | AMFI NAVAll.txt | 24h |
| `mf_benchmark.json` | yfinance, full history (`period="max"`, back to 2007) | 7d |
| `mf_extra_nav_cache.json` | mfapi.in, single schemes outside the universe (index fund, Regular plans) via `fetch_scheme_nav` | 24h |
| `mf_enrichment_cache.json` | mfdata.in | 24h |
| `mf_holdings_cache.json`, `mf_amfi_portfolio_cache.json` | fund portfolio disclosures | |

- **NAV coverage is checked.**
  - `fetch_and_cache` retries each scheme, then runs a sequential second pass, then reuses previous-cache copies of anything that still failed.
  - It reports coverage. Below 80%, the cache is written with `partial: true` and `load_cache` refuses it.
  - Never let failures silently shrink the universe. An earlier version cached 131 of 743 funds.
- **Index yardstick:** `INDEX_FUND_CODE` (UTI Nifty 50 Index Fund Direct, 120716) is total-return and net of costs. Prefer it to `^NSEI`, which is a price index.
- An empty enrichment result is **not** cached, so a down mfdata.in is retried on the next run.
- **`mf_nav_cache.json` is about 270 MB.** Never Read or cat it. Load it with Python (`json.load` takes about 4s).
- **NAVAll.txt format changed (2026).** It now has 8 columns: `Code;ISIN Growth;ISIN Reinv;Scheme Name;Plan;Option;NAV;Date`. `fetch_amfi_master` maps columns from the header line, so keep it header-driven. The response must be decoded as UTF-8, even though the server says ISO-8859-1.
- `mf_exit_load_overrides.json` (`{"<amfi code>": [pct, days]}`) is user config and is tracked in git. Defaults are `DEFAULT_EXIT_LOAD` in config.
- **The LTCG exemption is per person per year.** `generate_action_plan` costs EXIT, then SWITCH, then Direct switches against one `ExemptionBudget`; `compute_tax_harvest_plan` gets only `exemption_remaining`. Never give each item its own ₹1.25L.
- `mf_ter_overrides.json` is user config and is tracked in git. It takes priority for TER scoring.
- Clear caches with mode 3. If files are locked, a bust flag is written to `%TEMP%\mf_cache_bust.flag`, and `_cache_busted()` honours it.

## Scoring model

- **Backtest finding (Nov 2026, balanced weights, NAV-only components):**
  - The top picks beat their category median **41%** of the time at 3 years and 47% at 1 year.
  - A naive "best trailing 3Y" rule scored 47% and 50%.
  - The rank IC is about 0.
  - **The score has not shown predictive edge.** Don't present rankings as predictions. Re-run mode 4 after any scoring change.
- TER is assigned *before* `compute_cohort_stats`, so the cost component is relative to the category median TER. An earlier version gave every fund the same cost score.
- Seven category-relative components: `info_ratio`, `capture`, `consistency`, `cost`, `capacity`, `valuation`, `ext_rank`.
- Per-strategy weights live in `SCORE_WEIGHTS`. Each row sums to 100.
- The HTML methodology section reads these weights directly, so changing the weights updates the report text automatically.

## Holdings resolution (`mfa/holdings/analysis.py`)

Stored holdings arrive with messy identifiers:

- CAMS/KFin registrar product codes such as `MCOG`, `EAAG`, `51`
- ISINs in the code field
- CAS-style names, e.g. `HDFC MF-HDFC ... -DIR-GROWTH`
- Regular plans

`resolve_holding()` works through these steps in order:

1. A 6-digit AMFI code. Shorter numeric codes are registrar codes and are never trusted.
2. The ISIN, matched against either ISIN column in AMFI or mfapi.
3. `_fund_name_key()` name normalisation:
   - Exact match first.
   - Then fuzzy match (≥0.88) or partial match, both restricted to the same AMC.
   - Never across category tokens (`_CATEGORY_TOKENS`), so small cap ≠ mid cap.

How Regular plans are handled:

- They are *ranked* through their Direct sibling, because the universe is Direct-only.
- They are *valued* at their own NAV from the AMFI master. Never value a holding at its sibling's NAV.

`normalise_stored_holdings()` does two things whenever holdings are loaded:

- Drops scraped UI junk (`_JUNK_NAMES`).
- Writes resolved AMFI codes back to the DB. The original code is kept in `rta_code`.

## Privacy rules

- **Never print, log or persist a raw PAN.** DB keys come from `pan_key()`: HMAC-SHA256 with a per-install `.mf_salt` (24 hex characters).
- Legacy unsalted v1 keys are migrated automatically on first use (DB `version` 2).
- `mf_portfolio_db.json`, any `mf_portfolio_db*.json` backups, `.mf_salt` and CAS PDFs are git-ignored. Do not commit them, and do not send their contents to external services.
- Deleting `.mf_salt` orphans the stored portfolios. Treat it as a key.

## Automation (`.claude/`)

- **`hooks/cleanup.py`**: a `PostToolUse` hook on `Bash` and `PowerShell`.
  - Deletes `*.tmp` files older than 10 minutes. Younger ones may belong to a write that is still running.
  - Deletes `__pycache__/` and `.pytest_cache/`. It never touches anything under `.venv/`.
  - Deletes the HTML report and PNG once they are older than 7 days.
  - Only *warns*, once per session, about DB fallback copies (`mf_portfolio_db_HHMMSS.json`). Never delete those automatically; they may hold the newest holdings.
- **`hooks/end_of_turn.py`**: a `Stop` hook that runs at most one check per stop:
  1. If code changed but `README.md` and `CLAUDE.md` didn't, update the docs or say in one line that no update is needed.
  2. Otherwise, if there are uncommitted changes, invoke the `commit-push` skill.

  Each check fires at most once per working-tree state. Loop-guard state lives in `%TEMP%/claude-mfa-hooks-<session>.json`. The hook is skipped in plan mode.
- **`skills/commit-push/`**: commits and pushes to the **public** `heptile7/mf-analyse` repo automatically. It is gated by:
  - `pytest`
  - `pyflakes`
  - `scripts/guard.py`, which blocks personal-data files, files over 2 MB, PAN-like strings and email addresses in staged changes

  It never force-pushes and never bypasses the guard.

## Conventions

- Write output files through `_save_output_file` (temp file, then `os.replace`, with fallbacks). This works around Windows file locks and Controlled Folder Access.
- `save_portfolio_db` has its own fallback chain of paths.
- Network fetchers must degrade gracefully: print a yellow warning and return `{}` or `[]`. They must never crash the run.
- Put new constants in `mfa/config.py`. Import names explicitly (`from mfa.x import a, b`); there are no star imports.
- Tests patch module globals where they are *used*, e.g. `monkeypatch.setattr(mfa.holdings.db, "PORTFOLIO_DB_FILE", ...)`.
- Tests must stay offline: build tiny fake `amfi_master` and `nav_data` fixtures (see `tests/test_core.py`).
