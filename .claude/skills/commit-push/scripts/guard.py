"""Pre-commit guard for the PUBLIC repo heptile7/mf-analyse.

Checks the staged set (git diff --cached) and exits 1 with reasons if it would
publish personal data or large/generated files. Run from the repo root after
`git add -A`.
"""
import fnmatch
import os
import re
import subprocess
import sys

FORBIDDEN = [
    "mf_portfolio_db*.json", ".mf_salt", "*.pdf", "*cache*.json", "mf_amfi_master.json",
    "mf_benchmark.json", ".env", ".env.*", "*settings.local.json",
    ".mfc_browser_profile/*", "*.tmp", "mf_portfolio_report.html", "mf_portfolio_charts.png",
]
MAX_BYTES = 2 * 1024 * 1024
PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
PAN_ALLOW = {"ABCDE1234F"}                       # fake PAN used in tests
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
EMAIL_ALLOW = {"noreply@anthropic.com"}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout


def main() -> int:
    staged = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
    problems = []
    for p in staged:
        base = os.path.basename(p)
        if any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(base, pat) for pat in FORBIDDEN):
            problems.append(f"forbidden file staged: {p}")
        elif os.path.isfile(p) and os.path.getsize(p) > MAX_BYTES:
            problems.append(f"file over 2 MB: {p} ({os.path.getsize(p) // 1024} KB)")

    current = None
    for line in git("diff", "--cached", "-U0", "--no-color").splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for m in PAN_RE.findall(line):
            if m not in PAN_ALLOW:
                problems.append(f"PAN-like value in {current}: {m[:2]}***{m[-2:]}")
        for m in EMAIL_RE.findall(line):
            if m.lower() not in EMAIL_ALLOW:
                problems.append(f"email address in {current}: {m.split('@')[0][:2]}***@{m.split('@')[1]}")

    if problems:
        print("GUARD FAILED — nothing should be committed:")
        for p in dict.fromkeys(problems):
            print("  -", p)
        return 1
    print(f"guard ok: {len(staged)} staged file(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
