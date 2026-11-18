"""Holdings importers: MFCentral browser capture, CAS PDF, CSV."""

import os
import sys

from rich.panel import Panel
from rich.prompt import Prompt

from mfa.portfolio import classify_fund
from mfa.ui import console, rich_ask_choice
from mfa.util import _safe_float


# ══════════════════════════════════════════════════════════════════════
# EXISTING PORTFOLIO TRACKER  (PAN-based, local storage)
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO FETCHER  (OTP via browser or CAS PDF)
# ══════════════════════════════════════════════════════════════════════

def _pip_install(package: str) -> bool:
    """Attempt pip install. Returns True on success."""
    import subprocess
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "-q"],
            capture_output=True, text=True, timeout=180,
        )
        return r.returncode == 0
    except Exception:
        return False


def _ensure_playwright() -> bool:
    """Check / install playwright + chromium browser."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        console.print("  [yellow]Installing playwright (first-time, ~1 min)...[/yellow]")
        if not _pip_install("playwright"):
            return False
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       capture_output=True, timeout=300)
        return True
    except Exception:
        return False


def _ensure_casparser() -> bool:
    """Check / install casparser."""
    try:
        import casparser  # noqa: F401
        return True
    except ImportError:
        console.print("  [yellow]Installing casparser...[/yellow]")
        return _pip_install("casparser")


# ── Playwright: headless=False so WAF sees a real browser session ──────

async def _playwright_mfcentral_async(pan: str) -> list:
    """
    Open a persistent Chromium profile on MFCentral (saves cookies between runs).
    - Fixes CAPTCHA: persistent profile looks like a real returning user.
    - Fixes 'active session' error: same cookies = same session, no conflict.
    - Auto-proceeds when portfolio API data is detected (no manual Enter needed).
    """
    import asyncio, threading as _th
    from playwright.async_api import async_playwright  # type: ignore

    # Store profile in user home (~/.mfc_browser_profile) — always writable,
    # avoids Windows Controlled Folder Access restrictions on the project dir.
    profile_dir = os.path.join(os.path.expanduser("~"), ".mfc_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    captured: list = []
    dom_data: list = []

    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--lang=en-IN",
    ]
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    async with async_playwright() as p:
        # Try persistent context first (preserves cookies across runs).
        # Falls back to a fresh context if the profile dir is locked by another process.
        context = None
        try:
            context = await p.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                slow_mo=80,
                args=_LAUNCH_ARGS,
                user_agent=_UA,
                viewport={"width": 1280, "height": 900},
            )
        except Exception as _pe:
            if "ProcessSingleton" in str(_pe) or "profile" in str(_pe).lower():
                console.print(
                    "  [yellow]Browser profile locked (another Chrome using it?) — "
                    "starting a fresh session.[/yellow]"
                )
                browser = await p.chromium.launch(
                    headless=False, slow_mo=80, args=_LAUNCH_ARGS
                )
                context = await browser.new_context(
                    user_agent=_UA, viewport={"width": 1280, "height": 900}
                )
            else:
                raise

        # Override navigator.webdriver so CAPTCHA solvers don't see automation flag
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en', 'hi']});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        """)

        page = context.pages[0] if context.pages else await context.new_page()

        # ── Intercept ALL JSON responses ──────────────────────────────────
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "json" in ct:
                    data = await response.json()
                    if data:
                        captured.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", on_response)
        await page.goto("https://www.mfcentral.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # ── Detect existing session ───────────────────────────────────────
        page_text = (await page.evaluate("document.body.innerText")).lower()
        active_session_msg = "active session" in page_text or "already logged" in page_text
        already_in = any(kw in page.url.lower()
                         for kw in ("dashboard", "portfolio", "home", "folio", "holdings"))

        if active_session_msg and not already_in:
            # Previous session still alive on the server but we somehow lack cookies.
            # Try navigating directly to the portfolio page first.
            console.print("  [yellow]Server reports active session — trying direct navigation...[/yellow]")
            await page.goto("https://www.mfcentral.com/portfolio", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            already_in = any(kw in page.url.lower()
                             for kw in ("dashboard", "portfolio", "home", "folio", "holdings"))

        if already_in:
            console.print("  [green]Already logged in (session restored from saved profile).[/green]")
        else:
            # Try to auto-fill PAN
            for sel in ['input[id*="pan" i]', 'input[name*="pan" i]',
                        'input[placeholder*="PAN" i]', 'input[type="text"]']:
                try:
                    await page.fill(sel, pan, timeout=2000)
                    break
                except Exception:
                    pass

            console.print(Panel(
                f"[bold white]Complete login in the browser:[/bold white]\n\n"
                f"  1. PAN [bold cyan]{pan}[/bold cyan] may be pre-filled — verify it\n"
                f"  2. Enter your registered mobile / email\n"
                f"  3. Click 'Send OTP' and enter the OTP you receive\n"
                f"  4. Navigate to your [bold]Portfolio / Holdings[/bold] page\n"
                f"  5. Scroll down so all holdings are visible\n\n"
                f"  [dim]Script auto-proceeds when it detects portfolio data.\n"
                f"  Your login session is saved — next run skips this step.[/dim]",
                border_style="blue", title="[bold blue]MFCentral Login[/bold blue]",
            ))

        # ── Smart wait: auto-proceed on data OR manual Enter ─────────────
        enter_evt = _th.Event()
        def _wait_enter():
            try:
                input("\n  >>> Press Enter to extract now, or wait for auto-detect: ")
            except Exception:
                pass
            enter_evt.set()
        _th.Thread(target=_wait_enter, daemon=True).start()

        KEYWORDS = ("folio", "scheme", "units", "nav", "isin",
                    "holding", "investment", "amc", "fund")
        MAX_WAIT_S = 480

        def _has_portfolio_data() -> bool:
            for c in captured:
                snip = str(c.get("data", ""))[:3000].lower()
                if sum(1 for kw in KEYWORDS if kw in snip) >= 3:
                    return True
            return False

        waited, auto_ok = 0, False
        while waited < MAX_WAIT_S:
            await page.wait_for_timeout(3000)
            waited += 3
            if enter_evt.is_set():
                break
            if _has_portfolio_data():
                await page.wait_for_timeout(4000)   # let remaining calls settle
                auto_ok = True
                break
            if waited % 30 == 0:
                console.print(f"  [dim]Waiting... {waited}s, {len(captured)} response(s) captured[/dim]")

        if waited >= MAX_WAIT_S:
            console.print("  [yellow]Timeout — proceeding with data captured so far.[/yellow]")
        elif auto_ok:
            console.print(f"  [green]Portfolio detected ({waited}s) — extracting...[/green]")
        else:
            console.print("  [cyan]Manual proceed — extracting...[/cyan]")

        await page.wait_for_timeout(2000)

        # ── Extract auth token from storage ──────────────────────────────
        token_headers: dict = {}
        try:
            storage = await page.evaluate("""() => {
                const r = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    r['ls_' + k] = localStorage.getItem(k);
                }
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    r['ss_' + k] = sessionStorage.getItem(k);
                }
                return r;
            }""")
            for k, v in storage.items():
                if any(t in k.lower() for t in ("token", "auth", "jwt", "bearer", "access")):
                    token_headers["Authorization"] = f"Bearer {v}"
                    break
        except Exception:
            pass

        # ── Probe direct API endpoints — only if we don't already have data ─
        # Skip if auto-detect already found portfolio data (saves ~48 s of timeouts).
        if token_headers and not auto_ok:
            console.print("  [dim]Probing API endpoints with auth token...[/dim]")
            for ep in [
                "https://www.mfcentral.com/api/portfolio",
                "https://www.mfcentral.com/api/v1/portfolio",
                "https://www.mfcentral.com/api/holdings",
                "https://www.mfcentral.com/api/cas",
            ]:
                try:
                    r = await page.request.get(ep, headers=token_headers, timeout=2000)
                    if r.ok:
                        d = await r.json()
                        if d:
                            captured.append({"url": ep, "data": d, "source": "direct_api"})
                            break   # stop after first successful response
                except Exception:
                    pass

        # ── DOM scrape: tables + Next.js state + card text ────────────────
        try:
            dom_scraped = await page.evaluate("""() => {
                const res = {tables: [], lists: [], next_data: null};
                try { res.next_data = window.__NEXT_DATA__; } catch(e) {}
                document.querySelectorAll('table').forEach(tbl => {
                    const rows = [];
                    tbl.querySelectorAll('tr').forEach(tr => {
                        const cells = [];
                        tr.querySelectorAll('th,td').forEach(td => cells.push(td.innerText.trim()));
                        if (cells.some(c => c)) rows.push(cells);
                    });
                    if (rows.length > 1) res.tables.push(rows);
                });
                document.querySelectorAll(
                    '[class*="scheme"],[class*="folio"],[class*="fund"],'
                    '[class*="holding"],[class*="portfolio"]'
                ).forEach(el => {
                    const t = el.innerText.trim();
                    if (t.length > 20) res.lists.push(t);
                });
                return res;
            }""")
            if dom_scraped:
                dom_data.append({"source": "dom", "data": dom_scraped, "url": page.url})
        except Exception:
            pass

        try:
            dom_data.append({"source": "html", "data": await page.content(), "url": page.url})
        except Exception:
            pass

        # Close context (NOT logout) — cookies persist so next run skips login
        # Close with a timeout guard — persistent profile flush can be slow on Windows
        try:
            await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            pass

    console.print(f"  [green]Captured {len(captured)} API response(s) + {len(dom_data)} DOM snapshot(s).[/green]")
    return captured + dom_data


def _parse_mfcentral_captured(captured: list) -> list:
    """
    Parse raw API responses + DOM snapshots captured from MFCentral browser session.
    Handles: folio JSON schemas, Next.js __NEXT_DATA__, DOM tables, raw HTML.
    """
    import re as _re
    holdings: list = []
    seen: set = set()

    def _add(h: dict):
        cost = h.pop("_cost", None)
        if not h.get("avg_nav") and cost and h.get("units"):
            h["avg_nav"] = round(cost / h["units"], 4)
        key = (h.get("scheme_name", "").lower().strip(), round(h.get("units", 0), 2))
        if key[0] and key not in seen:
            seen.add(key)
            holdings.append(h)

    def _cost_of(rec: dict, val: dict):
        for src in (rec, val or {}):
            for k in ("cost", "costValue", "investedValue", "investedAmount",
                      "invested_value", "purchaseValue", "totalInvestment"):
                v = _safe_float(src.get(k))
                if v: return v
        return None

    def _parse_folio_dict(data: dict):
        # Schema A-1: {"folios": [...]}  or  {"data": {"folios": [...]}}
        folio_list = (data.get("folios")
                      or (data.get("data") or {}).get("folios")
                      or (data.get("result") or {}).get("folios")
                      or [])
        for folio in folio_list:
            if not isinstance(folio, dict): continue
            amc = folio.get("amc") or folio.get("amcName") or ""
            for scheme in folio.get("schemes", []) or folio.get("funds", []):
                if not isinstance(scheme, dict): continue
                val = scheme.get("valuation") or scheme.get("currentValuation") or {}
                _add({
                    "scheme_code":   str(scheme.get("amfiCode") or scheme.get("amfi_code") or scheme.get("schemeCode") or ""),
                    "scheme_name":   scheme.get("scheme") or scheme.get("name") or scheme.get("schemeName") or "",
                    "category":      "",
                    "amc":           amc,
                    "isin":          scheme.get("isin") or "",
                    "sip_amount":    0,
                    "units":         _safe_float(scheme.get("units") or scheme.get("close_calculated") or scheme.get("closingBalance")) or 0,
                    "avg_nav":       _safe_float(scheme.get("avg_nav") or scheme.get("average_nav") or scheme.get("avgNav")) or 0,
                    "current_nav":   _safe_float(val.get("nav") or val.get("navValue")) or 0,
                    "current_value": _safe_float(val.get("value") or val.get("currentValue") or val.get("marketValue")) or 0,
                    "start_date":    str(scheme.get("firstPurchaseDate") or "").split("T")[0],
                    "notes":         "",
                    "_cost":         _cost_of(scheme, val),
                })

        # Schema A-2: direct schemes/holdings list under the dict
        for key in ("schemes", "holdings", "funds", "investments"):
            items = data.get(key)
            if isinstance(items, list) and items:
                for rec in items:
                    if not isinstance(rec, dict): continue
                    name = rec.get("schemeName") or rec.get("scheme") or rec.get("fundName") or rec.get("name")
                    if not name: continue
                    val = rec.get("valuation") or rec.get("currentValuation") or {}
                    _add({
                        "scheme_code":   str(rec.get("schemeCode") or rec.get("amfiCode") or ""),
                        "scheme_name":   name,
                        "category":      rec.get("category") or "",
                        "amc":           rec.get("amc") or rec.get("amcName") or "",
                        "isin":          rec.get("isin") or "",
                        "sip_amount":    0,
                        "units":         _safe_float(rec.get("units") or rec.get("closingBalance")) or 0,
                        "avg_nav":       _safe_float(rec.get("avgNav") or rec.get("averageNav") or rec.get("avg_nav")) or 0,
                        "current_nav":   _safe_float(rec.get("nav") or rec.get("currentNav") or val.get("nav")) or 0,
                        "current_value": _safe_float(rec.get("value") or rec.get("currentValue") or val.get("value")) or 0,
                        "start_date":    "",
                        "notes":         "",
                        "_cost":         _cost_of(rec, val),
                    })

    def _parse_scheme_list(data: list):
        # Schema B: flat list of fund records
        for rec in data:
            if not isinstance(rec, dict): continue
            name = rec.get("fundName") or rec.get("schemeName") or rec.get("scheme") or rec.get("name")
            if not name: continue
            _add({
                "scheme_code":   str(rec.get("schemeCode") or rec.get("amfiCode") or ""),
                "scheme_name":   name,
                "category":      rec.get("category") or "",
                "amc":           rec.get("amc") or rec.get("amcName") or "",
                "isin":          rec.get("isin") or "",
                "sip_amount":    0,
                "units":         _safe_float(rec.get("units") or rec.get("closingBalance")) or 0,
                "avg_nav":       _safe_float(rec.get("avgNav") or rec.get("averageNav") or rec.get("avg_nav")) or 0,
                "current_nav":   _safe_float(rec.get("nav") or rec.get("currentNav")) or 0,
                "current_value": _safe_float(rec.get("value") or rec.get("currentValue")) or 0,
                "start_date":    "",
                "notes":         "",
                "_cost":         _cost_of(rec, {}),
            })

    def _walk_json(obj, depth=0):
        """Recursively search any JSON structure for folio/scheme data."""
        if depth > 6: return
        if isinstance(obj, dict):
            if obj.get("folios") or obj.get("schemes") or obj.get("holdings") or obj.get("funds"):
                _parse_folio_dict(obj)
            for v in obj.values():
                _walk_json(v, depth + 1)
        elif isinstance(obj, list) and obj:
            first = obj[0]
            if isinstance(first, dict):
                keys = set(first.keys())
                fund_keys = {"schemeName", "scheme", "fundName", "name", "units", "nav"}
                if keys & fund_keys:
                    _parse_scheme_list(obj)
                    return
            for v in obj:
                _walk_json(v, depth + 1)

    def _parse_dom_tables(tables: list):
        """Extract holdings from HTML tables scraped via JS."""
        NAV_RE = _re.compile(r'\d+\.\d{2,4}')
        for rows in tables:
            if len(rows) < 2: continue
            header = [c.lower() for c in rows[0]]
            fund_col = next((i for i, h in enumerate(header)
                             if any(k in h for k in ("scheme", "fund", "name"))), None)
            if fund_col is None: continue
            units_col = next((i for i, h in enumerate(header) if "unit" in h), None)
            nav_col   = next((i for i, h in enumerate(header) if "nav" in h), None)
            val_col   = next((i for i, h in enumerate(header)
                              if any(k in h for k in ("value", "amount", "worth"))), None)
            for row in rows[1:]:
                if len(row) <= fund_col: continue
                name = row[fund_col].strip()
                if not name or len(name) < 5: continue
                units = _safe_float(row[units_col]) if units_col and units_col < len(row) else 0
                nav   = _safe_float(row[nav_col])   if nav_col   and nav_col   < len(row) else 0
                val   = _safe_float(row[val_col])   if val_col   and val_col   < len(row) else 0
                _add({
                    "scheme_code": "", "scheme_name": name, "category": "",
                    "amc": "", "isin": "", "sip_amount": 0,
                    "units": units or 0, "avg_nav": 0, "current_nav": nav or 0,
                    "current_value": val or 0, "start_date": "", "notes": "dom_table",
                })

    # ── Process each captured item ────────────────────────────────────────
    for item in captured:
        source = item.get("source", "api")
        data   = item.get("data")

        if source == "dom" and isinstance(data, dict):
            # Next.js hydration payload
            nd = data.get("next_data")
            if nd:
                _walk_json(nd)
            # DOM tables
            _parse_dom_tables(data.get("tables") or [])
            # Card/list text — minimal extraction (fund name + value patterns)
            for text_block in (data.get("lists") or []):
                lines = [l.strip() for l in text_block.splitlines() if l.strip()]
                if len(lines) >= 2:
                    name_candidate = lines[0]
                    if len(name_candidate) > 8 and not name_candidate[0].isdigit():
                        _add({
                            "scheme_code": "", "scheme_name": name_candidate,
                            "category": "", "amc": "", "isin": "",
                            "sip_amount": 0, "units": 0, "avg_nav": 0,
                            "current_nav": 0, "current_value": 0,
                            "start_date": "", "notes": "dom_card",
                        })

        elif source == "html" and isinstance(data, str):
            # BeautifulSoup parse of full HTML
            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(data, "html.parser")
                for tbl in soup.find_all("table"):
                    rows_raw = []
                    for tr in tbl.find_all("tr"):
                        cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                        if cells: rows_raw.append(cells)
                    _parse_dom_tables([rows_raw])
            except ImportError:
                pass

        else:
            # API / direct JSON response — deep walk
            _walk_json(data)

    return [h for h in holdings if h.get("scheme_name")]


def _parse_cas_pdf(pdf_path: str) -> list:
    """Parse a CAMS or KFintech CAS PDF using casparser."""
    from casparser import read_cas_pdf  # type: ignore

    console.print(Panel(
        "CAS PDF password:\n"
        "  CAMS:     first 4 chars of registered email (UPPERCASE) + DOB as DDMMYYYY\n"
        "  KFintech: PAN first 4 chars (UPPERCASE) + DOB as DDMMYYYY\n"
        "  Example:  ABCD01012000",
        border_style="dim", title="[dim]Password format[/dim]",
    ))
    pw = Prompt.ask("  [green]PDF password (Enter to try blank)[/green]",
                    default="", password=True)

    data = read_cas_pdf(pdf_path, pw)
    holdings: list = []
    for folio in data.get("folios", []):
        amc = folio.get("amc", "")
        for scheme in folio.get("schemes", []):
            val  = scheme.get("valuation") or {}
            # casparser uses 'close' for current units, 'open' for start units
            units = (_safe_float(scheme.get("close"))
                     or _safe_float(scheme.get("units")) or 0)
            # Cost basis: casparser reports total invested cost in valuation.cost
            cost    = _safe_float(val.get("cost")) or _safe_float(scheme.get("cost"))
            avg_nav = _safe_float(scheme.get("avg_nav")) or (
                round(cost / units, 4) if cost and units else 0)
            # 'open' is the opening unit balance, not a date — use earliest transaction
            txn_dates = sorted(str(t.get("date", ""))[:10]
                               for t in (scheme.get("transactions") or [])
                               if isinstance(t, dict) and t.get("date"))
            holdings.append({
                "scheme_code":   str(scheme.get("amfi_code") or ""),
                "scheme_name":   scheme.get("scheme", ""),
                "category":      "",
                "amc":           amc,
                "isin":          scheme.get("isin", ""),
                "sip_amount":    0,
                "units":         units,
                "avg_nav":       avg_nav or 0,
                "current_nav":   _safe_float(val.get("nav")) or 0,
                "current_value": _safe_float(val.get("value")) or 0,
                "start_date":    txn_dates[0] if txn_dates else "",
                "notes":         "",
            })
    return [h for h in holdings if h.get("scheme_name")]


def fetch_portfolio_external(pan: str, amfi_master: dict) -> list:
    """
    Fetch existing MF portfolio via one of three methods:
      1. Browser automation  — opens real Chromium, user logs in at MFCentral with OTP
      2. CAS PDF parsing     — parse PDF downloaded from CAMS/KFintech
      3. CSV import          — Kuvera / Groww / Coin export (handled by import_from_csv)

    Returns a list of holding dicts (same schema as manual entry).
    MFCentral and CAMS block direct REST API calls via WAF; the browser method
    is the only reliable programmatic approach that bypasses this.
    """
    import asyncio

    method = rich_ask_choice(
        "Choose fetch method:",
        [
            "Browser (recommended) — opens MFCentral in Chromium, you enter OTP there",
            "CAS PDF — you download PDF from CAMS / KFintech, we parse it",
            "CSV — import Kuvera / Groww / Coin export file",
        ],
        hint="All three work. Browser is most automated; PDF is most reliable.",
    )

    holdings: list = []

    if method == 0:  # Browser / Playwright
        console.print(Panel(
            "[dim]Note: MFCentral and CAMS block direct API calls via WAF.\n"
            "Opening a real browser is the only way to bypass this.[/dim]",
            border_style="dim",
        ))
        if not _ensure_playwright():
            console.print(Panel(
                "[red]Could not install Playwright automatically.[/red]\n\n"
                "Install manually:\n"
                "  pip install playwright\n"
                "  playwright install chromium\n\n"
                "Then restart the script.",
                border_style="red",
            ))
            return []
        try:
            captured = asyncio.run(_playwright_mfcentral_async(pan))
            holdings = _parse_mfcentral_captured(captured)
            if holdings:
                console.print(f"  [green]{len(holdings)} holdings captured from MFCentral.[/green]")
            else:
                console.print("  [yellow]No portfolio data captured.[/yellow]")
                console.print("  [dim]The page structure may have changed — try PDF method.[/dim]")
        except Exception as e:
            console.print(f"  [red]Browser automation error: {e}[/red]")
            console.print("  [dim]Fallback: use PDF or CSV method.[/dim]")

    elif method == 1:  # CAS PDF
        console.print(Panel(
            "[bold white]Download your CAS PDF first:[/bold white]\n\n"
            "  CAMS   : https://www.camsonline.com → Investors → Statements → Consolidated\n"
            "  KFintech: https://mfs.kfintech.com → Investor Login → Statements\n\n"
            "  Select: Type = Detailed, Period = Since Inception\n"
            "  The PDF will be emailed to your registered address.",
            border_style="cyan", title="[bold]Step 1: Download CAS[/bold]",
        ))
        pdf_path = Prompt.ask("  [green]Enter path to CAS PDF[/green]").strip().strip('"')
        if not os.path.exists(pdf_path):
            console.print(f"  [red]File not found: {pdf_path}[/red]")
            return []
        if not _ensure_casparser():
            console.print(Panel(
                "[red]Could not install casparser.[/red]\n"
                "Run: pip install casparser",
                border_style="red",
            ))
            return []
        try:
            holdings = _parse_cas_pdf(pdf_path)
            console.print(f"  [green]Parsed {len(holdings)} holdings from PDF.[/green]")
        except Exception as e:
            console.print(f"  [red]PDF parse error: {e}[/red]")

    else:  # CSV
        csv_path = Prompt.ask("  [green]Enter path to CSV file[/green]").strip().strip('"')
        holdings = import_from_csv(csv_path)

    # Classify categories
    for h in holdings:
        if not h.get("category"):
            h["category"] = classify_fund(
                h.get("scheme_name", ""), h.get("scheme_code", ""), amfi_master)

    return holdings


def import_from_csv(filepath: str) -> list:
    """
    Import holdings from a CSV file.
    Supports Kuvera / Groww / Coin export formats.
    Expected columns (flexible): Fund Name, Scheme Code, Units,
      Average NAV / Avg Cost, Current NAV / NAV, Current Value, SIP Amount.
    Returns list of holding dicts.
    """
    import csv
    holdings = []
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalise column names
                rn = {k.strip().lower().replace(" ","_"): v.strip() for k, v in row.items()}

                def pick(*keys):
                    for k in keys:
                        v = rn.get(k) or rn.get(k.replace("_"," "))
                        if v and v not in ("", "-", "N/A"): return v
                    return ""

                name      = pick("fund_name","scheme_name","fund","name")
                code      = pick("scheme_code","amfi_code","code")
                avg_nav   = _safe_float(pick("average_nav","avg_nav","avg_cost","average_cost"))
                cur_nav   = _safe_float(pick("current_nav","nav","latest_nav"))
                units     = _safe_float(pick("units","quantity"))
                cur_val   = _safe_float(pick("current_value","value","current_amount"))
                sip_amt   = _safe_float(pick("sip_amount","monthly_sip","sip"))
                start_dt  = pick("start_date","investment_date","since")

                if not name: continue
                holdings.append({
                    "scheme_code":   code,
                    "scheme_name":   name,
                    "category":      "",        # filled later via classify_fund
                    "sip_amount":    int(sip_amt) if sip_amt else 0,
                    "lump_sum":      0,
                    "units":         units or 0,
                    "avg_nav":       avg_nav or 0,
                    "current_nav":   cur_nav or 0,
                    "current_value": cur_val or 0,
                    "start_date":    start_dt,
                    "notes":         "",
                })
    except Exception as e:
        console.print(f"  [red]CSV import error: {e}[/red]")
    return holdings
