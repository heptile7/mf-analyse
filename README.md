# MF Analyse

A terminal tool for Indian mutual fund investors. It builds a SIP portfolio from the full Direct-Growth fund universe and reviews the funds you already own.

> **Not SEBI-registered investment advice.** Projections are based on historical data and simulations, and they are not forecasts. Do your own research or consult a registered adviser.

## What it does

**1. Build a new portfolio recommendation**
- Asks about your timeline, monthly SIP, optional target corpus and tax bracket.
- Scores every eligible Direct-Growth equity and debt fund on 7 category-relative components: information ratio, up/down capture, consistency, cost (TER), AUM capacity, valuation and external rank. The weights depend on your strategy (aggressive, balanced or conservative).
- Picks a diversified set of funds. It limits correlation and how many funds come from one AMC, then splits your SIP across them.
- Projects outcomes with Monte Carlo, and estimates:
  - tax under LTCG 12.5% above ₹1.25 L per year
  - the advantage over a passive index baseline
  - the SIP required to reach your target corpus
  - the effect of an annual step-up
- Writes a **self-contained HTML report** (`mf_portfolio_report.html`) and a **PNG chart pack** (`mf_portfolio_charts.png`).
- **Track record:** every recommendation is saved with that day's NAVs in `mf_recommendations.json`, which stays local. Later runs show how those picks did against the Nifty 50 index fund over the same period.

**2. Check your existing portfolio**
- Rates each holding **BUY MORE / HOLD / WATCH / SWITCH / EXIT** by its rank within its category.
- Regular-plan holdings are ranked through their Direct counterpart, with a note on the cost difference.
- Builds an action plan (switches, gap fills, top-ups). Plans are kept as a history, and pending switches show how the kept fund and the suggested fund have done since the plan was made.
- Builds an **LTCG tax-harvest plan** that uses the yearly ₹1.25 L exemption.

**3. Refresh the data caches**

**4. Backtest the scoring model**
- Scores the universe as it looked at each year-end from 2015 to 2023, using only data available at the time.
- Checks whether the top picks beat their category over the next 1 and 3 years. The comparisons are a naive "last 3 years' winner" rule and a real Nifty 50 index fund.
- Writes `mf_backtest_report.html` with a plain-words verdict. Read it before trusting the rankings.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows (use .venv/bin/ on macOS/Linux)
.venv/Scripts/python MF_analyser.py
```

The first run downloads NAV histories for about 750 funds. That takes 3–5 minutes, and later runs use the 24-hour cache.

### Importing your holdings
In check mode, or after entering a PAN in build mode, you can import holdings in any of these ways:
- **MFCentral (browser)**: Chromium opens and you log in with the OTP yourself. The tool installs `playwright` the first time you use this.
- **CAS PDF**: a CAMS or KFintech consolidated account statement. The tool installs `casparser` the first time you use this.
- **CSV**: an export from Kuvera, Groww or Coin.
- **Manual entry**

The tool resolves holdings automatically, whether they come as ISINs, registrar codes or scheme names. If a source leaves out your cost basis, you're asked for invested amounts so it can calculate gains and tax harvesting.

## Data sources
- [mfapi.in](https://www.mfapi.in): NAV history
- [AMFI NAVAll.txt](https://www.amfiindia.com/spages/NAVAll.txt): official scheme master, categories and latest NAVs
- [mfdata.in](https://mfdata.in): TER, AUM, valuation and ranks. Optional; the tool falls back to NAV-only ranking.
- yfinance: Nifty 50 and Midcap benchmarks

## Privacy
- Everything stays on your machine. Your PAN is never stored: holdings are keyed by an HMAC-SHA256 of the PAN, using a random per-install secret (`.mf_salt`).
- The portfolio database, the salt, caches, CAS PDFs and generated reports are all git-ignored.
- **Don't delete `.mf_salt`.** It's the key to your saved portfolios.

## Development
```bash
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q        # offline tests
.venv/Scripts/python -m pyflakes mfa     # lint
```
The code is in the `mfa/` package. `MF_analyser.py` is the entry point. See [CLAUDE.md](CLAUDE.md) for the architecture, data notes and conventions.
