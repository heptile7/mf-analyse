"""PostToolUse (Bash|PowerShell) hook: remove run debris from the project.

- *.tmp in the root and mfa/ older than 10 min (younger ones may still be
  written by a running job via _save_output_file / save_portfolio_db)
- __pycache__/ and .pytest_cache/ (never inside .venv/.git/browser profile)
- mf_portfolio_report.html / mf_portfolio_charts.png older than 7 days
- DB fallback copies (mf_portfolio_db_HHMMSS.json) are only REPORTED, once
  per session — they can hold newer holdings than the main DB.

Always exits 0; stays silent unless there is something to warn about.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

TMP_MIN_AGE_S = 10 * 60
REPORT_MAX_AGE_S = 7 * 24 * 3600
SKIP_DIRS = {".venv", ".git", ".mfc_browser_profile", "node_modules"}
CACHE_DIRS = {"__pycache__", ".pytest_cache"}
DB_FALLBACK_RE = re.compile(r"^mf_portfolio_db_\d{6}\.json$")


def _older_than(path: str, age_s: float, now: float) -> bool:
    try:
        return now - os.path.getmtime(path) > age_s
    except OSError:
        return False


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError:
        pass          # locked on Windows or already gone — try again next time


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    if not os.path.isfile(os.path.join(root, "MF_analyser.py")):
        return
    now = time.time()

    for d in (root, os.path.join(root, "mfa")):
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if name.endswith(".tmp") and os.path.isfile(p) and _older_than(p, TMP_MIN_AGE_S, now):
                    _remove(p)

    for dirpath, dirnames, _ in os.walk(root):
        for name in list(dirnames):
            if name in SKIP_DIRS:
                dirnames.remove(name)
            elif name in CACHE_DIRS:
                _remove(os.path.join(dirpath, name))
                dirnames.remove(name)

    for name in ("mf_portfolio_report.html", "mf_portfolio_charts.png"):
        p = os.path.join(root, name)
        if os.path.isfile(p) and _older_than(p, REPORT_MAX_AGE_S, now):
            _remove(p)

    fallbacks = [os.path.join(root, n) for n in os.listdir(root) if DB_FALLBACK_RE.match(n)]
    tmpdir = tempfile.gettempdir()
    try:
        fallbacks += [os.path.join(tmpdir, n) for n in os.listdir(tmpdir)
                      if n.startswith("mf_portfolio_db_") and n.endswith(".json")]
    except OSError:
        pass
    if not fallbacks:
        return

    state_file = os.path.join(tmpdir, f"claude-mfa-cleanup-{payload.get('session_id', 'none')}.json")
    try:
        with open(state_file) as fh:
            warned = set(json.load(fh))
    except Exception:
        warned = set()
    new = sorted(set(fallbacks) - warned)
    if not new:
        return
    try:
        with open(state_file, "w") as fh:
            json.dump(sorted(warned | set(new)), fh)
    except OSError:
        pass
    print(json.dumps({"systemMessage":
        "Portfolio DB fallback copies found (written when mf_portfolio_db.json was locked). "
        "They may hold newer holdings than the main DB — review/merge, not deleted: "
        + "; ".join(new)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
