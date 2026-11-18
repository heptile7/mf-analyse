"""Local portfolio DB (PAN-keyed via salted hash) and stored action plans."""

from datetime import datetime
import hashlib
import hmac
import json
import os

from rich.panel import Panel
from rich.prompt import Prompt

from mfa.config import PAN_SALT_FILE, PORTFOLIO_DB_FILE
from mfa.tracking import since_return
from mfa.ui import console, fmt_inr, rich_ask_choice


# ══════════════════════════════════════════════════════════════════════

PORTFOLIO_DB_VERSION = 2


def _get_salt() -> bytes | None:
    """Per-install secret for PAN hashing (.mf_salt, git-ignored). None if unwritable."""
    try:
        if os.path.exists(PAN_SALT_FILE):
            with open(PAN_SALT_FILE, "rb") as fh:
                salt = fh.read()
            if len(salt) >= 16:
                return salt
        salt = os.urandom(32)
        with open(PAN_SALT_FILE, "wb") as fh:
            fh.write(salt)
        return salt
    except OSError:
        return None


def _legacy_hash_pan(pan: str) -> str:
    """v1 key: unsalted SHA-256 (brute-forceable — kept only for migration)."""
    return hashlib.sha256(pan.upper().strip().encode()).hexdigest()[:16]


def hash_pan(pan: str) -> str:
    """HMAC-SHA256 of PAN with a per-install salt — PAN never stored in plaintext."""
    salt = _get_salt()
    if salt is None:
        console.print("  [yellow]Could not create .mf_salt — using legacy PAN key.[/yellow]")
        return _legacy_hash_pan(pan)
    return hmac.new(salt, pan.upper().strip().encode(), hashlib.sha256).hexdigest()[:24]


def pan_key(pan: str) -> str:
    """Hash the PAN and migrate any v1 (unsalted) DB entry to the new key."""
    new_key, old_key = hash_pan(pan), _legacy_hash_pan(pan)
    if new_key == old_key:
        return new_key
    db = load_portfolio_db()
    ports = db.get("portfolios", {})
    if old_key in ports and new_key not in ports:
        ports[new_key] = ports.pop(old_key)
        db["version"] = PORTFOLIO_DB_VERSION
        save_portfolio_db(db)
        console.print("  [dim]Portfolio DB key upgraded to salted hash.[/dim]")
    return new_key


def load_portfolio_db() -> dict:
    if not os.path.exists(PORTFOLIO_DB_FILE):
        return {"version": PORTFOLIO_DB_VERSION, "portfolios": {}}
    try:
        with open(PORTFOLIO_DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": PORTFOLIO_DB_VERSION, "portfolios": {}}


def save_portfolio_db(db: dict) -> None:
    import time, tempfile
    ts = datetime.now().strftime("%H%M%S")
    candidates = [
        PORTFOLIO_DB_FILE,
        PORTFOLIO_DB_FILE.replace(".json", f"_{ts}.json"),
        os.path.join(tempfile.gettempdir(), f"mf_portfolio_db_{ts}.json"),
    ]
    for attempt, target in enumerate(candidates):
        try:
            # Write via temp file + atomic rename to avoid partial writes
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(target), suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2)
                os.replace(tmp_path, target)
            except Exception:
                try: os.unlink(tmp_path)
                except Exception: pass
                raise
            if target != PORTFOLIO_DB_FILE:
                console.print(f"  [yellow]Portfolio DB saved to: {target}[/yellow]")
            return
        except PermissionError:
            time.sleep(0.4)
            continue
    console.print("  [red]Could not save portfolio DB — all paths denied.[/red]")


def get_user_portfolio(pan_hash: str) -> dict:
    db = load_portfolio_db()
    return db["portfolios"].get(pan_hash, {"holdings": [], "last_updated": ""})


def save_user_portfolio(pan_hash: str, user_port: dict) -> None:
    db = load_portfolio_db()
    user_port["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    db["portfolios"][pan_hash] = user_port
    save_portfolio_db(db)


def _serialise_plan_item(item: dict, kind: str) -> dict:
    """Strip non-serialisable objects (pandas Series, full fund dicts) for storage."""
    def _fund_stub(f: dict | None) -> dict:
        if not f: return {}
        return {k: f.get(k) for k in
                ("name","scheme_code","category","score","category_rank","category_size",
                 "amc_name","return_3y","info_ratio")}
    if kind == "switch":
        return {
            "kind":           "switch",
            "from_name":      item["from"].get("scheme_name",""),
            "from_code":      item["from"].get("scheme_code",""),
            "from_rank":      item["from"].get("category_rank"),
            "from_score":     item["from"].get("score"),
            # NAVs at plan time, so later runs can show kept-vs-suggested since then
            "from_amfi_code": item["from"].get("amfi_code") or item["from"].get("scheme_code",""),
            "from_nav":       item["from"].get("current_nav"),
            "to_nav":         (item.get("to") or {}).get("latest_nav"),
            "to":             _fund_stub(item.get("to")),
            "exit_load_warn": item.get("exit_load_warn", False),
            "ltcg_rs":        item.get("ltcg_rs", 0),
            "tax_rs":         item.get("tax_rs", 0),
            "status":         "pending",
        }
    if kind == "gap_fill":
        return {
            "kind":     "gap_fill",
            "slot":     item.get("slot",""),
            "category": item.get("category",""),
            "fund":     _fund_stub(item.get("fund")),
            "sip":      item.get("sip", 0),
            "gap_pct":  item.get("gap_pct", 0),
            "status":   "pending",
        }
    if kind == "buy_more":
        h = item.get("holding", {})
        return {
            "kind":       "buy_more",
            "fund_name":  h.get("scheme_name",""),
            "fund_code":  h.get("scheme_code",""),
            "extra_sip":  item.get("extra_sip", 0),
            "gap_pct":    item.get("gap_pct", 0),
            "status":     "pending",
        }
    return {}


ACTION_PLAN_HISTORY = 24


def save_action_plan(pan_hash: str, plan: dict) -> None:
    """Persist the action plan items with 'pending' status into the portfolio DB."""
    items = []
    for sw in plan.get("switches", []):
        items.append(_serialise_plan_item(sw, "switch"))
    for gf in plan.get("gap_fills", []):
        items.append(_serialise_plan_item(gf, "gap_fill"))
    for bm in plan.get("buy_mores", []):
        items.append(_serialise_plan_item(bm, "buy_more"))

    db = load_portfolio_db()
    port = db["portfolios"].setdefault(pan_hash, {"holdings": [], "last_updated": ""})
    entry = {
        "generated_on": datetime.now().strftime("%Y-%m-%d"),
        "strategy":     plan.get("strategy", "balanced"),
        "items":        items,
    }
    # Keep a history instead of overwriting; seed it with a pre-history plan.
    history = port.setdefault("action_plans", [])
    if not history and port.get("action_plan"):
        history.append(port["action_plan"])
    history.append(entry)
    port["action_plans"] = history[-ACTION_PLAN_HISTORY:]
    port["action_plan"]  = entry          # latest — what pending-items logic reads
    db["portfolios"][pan_hash] = port
    save_portfolio_db(db)


def load_action_plan(pan_hash: str) -> dict:
    """Return the stored action plan for this PAN, or empty dict."""
    return get_user_portfolio(pan_hash).get("action_plan", {})


def update_action_plan_status(pan_hash: str, item_index: int, status: str) -> None:
    """Mark a stored action plan item as 'done' or 'dismissed'."""
    db   = load_portfolio_db()
    port = db["portfolios"].get(pan_hash, {})
    plan = port.get("action_plan", {})
    items = plan.get("items", [])
    if 0 <= item_index < len(items):
        items[item_index]["status"] = status
        plan["items"] = items
        port["action_plan"] = plan
        db["portfolios"][pan_hash] = port
        save_portfolio_db(db)


def _since_plan_note(it: dict) -> str:
    """' · since then: kept +a% vs suggested +b%' when NAVs at plan time are known."""
    try:
        kept = since_return(it.get("from_amfi_code"), it.get("from_nav"), {})
        sugg = since_return((it.get("to") or {}).get("scheme_code"), it.get("to_nav"), {})
    except Exception:
        return ""
    if kept is None or sugg is None:
        return ""
    col = "green" if sugg >= kept else "red"
    return f"  [dim]· since then: kept {kept:+.1f}% vs[/dim] [{col}]suggested {sugg:+.1f}%[/{col}]"


def show_pending_action_plan(pan_hash: str) -> bool:
    """
    On startup, show any pending items from the previous run.
    Returns True if pending items exist (caller can offer to mark done).
    """
    plan = load_action_plan(pan_hash)
    if not plan:
        return False
    items     = plan.get("items", [])
    pending   = [it for it in items if it.get("status") == "pending"]
    done      = [it for it in items if it.get("status") == "done"]
    dismissed = [it for it in items if it.get("status") == "dismissed"]
    if not pending:
        return False

    generated = plan.get("generated_on", "?")
    console.print(Panel(
        f"[bold yellow]⚡ {len(pending)} pending action(s) from {generated}[/bold yellow]\n"
        f"[dim]{len(done)} completed · {len(dismissed)} dismissed[/dim]",
        title="[bold]Saved Action Plan[/bold]",
        border_style="yellow",
    ))

    KIND_LABEL = {"switch": "SWITCH", "gap_fill": "ADD NEW", "buy_more": "INCREASE"}
    KIND_COL   = {"switch": "yellow", "gap_fill": "cyan",    "buy_more": "green"}
    for i, it in enumerate(items):
        if it.get("status") != "pending":
            continue
        kind  = it.get("kind","?")
        col   = KIND_COL.get(kind, "white")
        label = KIND_LABEL.get(kind, kind.upper())
        if kind == "switch":
            desc = (f"{it.get('from_name','?')[:36]} → "
                    f"{(it.get('to') or {}).get('name','?')[:30]}")
            desc += _since_plan_note(it)
        elif kind == "gap_fill":
            desc = (f"{it.get('slot','?')} gap — "
                    f"add {fmt_inr(it.get('sip',0))}/mo to "
                    f"{(it.get('fund') or {}).get('name','?')[:30]}")
        else:
            desc = (f"{it.get('fund_name','?')[:36]} — "
                    f"add {fmt_inr(it.get('extra_sip',0))}/mo")
        console.print(f"  [{col}]{i+1}. [{label}][/{col}]  {desc}")

    console.print()
    update_choice = rich_ask_choice(
        "Mark any items as done?",
        ["No — keep all pending",
         "Mark specific items done",
         "Mark all done",
         "Dismiss all (ignore these recommendations)"],
        default=0,
    )
    if update_choice == 1:
        raw = Prompt.ask("  Enter item numbers to mark done (e.g. 1,3)").strip()
        for tok in raw.split(","):
            try:
                idx = int(tok.strip()) - 1
                update_action_plan_status(pan_hash, idx, "done")
            except (ValueError, IndexError):
                pass
        console.print("  [green]Updated.[/green]")
    elif update_choice == 2:
        for i, it in enumerate(items):
            if it.get("status") == "pending":
                update_action_plan_status(pan_hash, i, "done")
        console.print("  [green]All items marked done.[/green]")
    elif update_choice == 3:
        for i, it in enumerate(items):
            if it.get("status") == "pending":
                update_action_plan_status(pan_hash, i, "dismissed")
        console.print("  [dim]All dismissed.[/dim]")

    return True
