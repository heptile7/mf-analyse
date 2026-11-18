---
name: commit-push
description: Commit and push the MF analyser repo to GitHub (public repo heptile7/mf-analyse, branch main), gated by tests, lint and a privacy guard. Use when the user says commit, push, save, sync or upload to GitHub. Also use proactively, without asking, when the end-of-turn hook reports uncommitted changes, or when a coherent unit of work is finished and verified. Do not use mid-task, while tests are knowingly failing, or in plan mode.
---

# Commit & push (fully automatic)

The user has authorised this skill to commit **and push without asking**, but only when every gate passes.

**The repo is public.** Anything pushed is visible to everyone immediately and stays in the history.

Run every step from the repo root `D:/GPT/MF_invest_anlayse`.

## 1. Anything to commit?
`git status --porcelain --untracked-files=all`. If it is empty, report "Nothing to commit" and stop.

If the changes are deliberately mid-way, for example a half-finished refactor, don't commit. Say so in one line.

## 2. Docs in sync
- Compare the changed files with the docs. If any of these changed, update `README.md` and/or `CLAUDE.md` first:
  - `mfa/**`
  - `MF_analyser.py`
  - `requirements*.txt`
  - `.claude/hooks/**`
  - `.claude/skills/**`
- Updates are needed only where behaviour, usage, layout or rules actually changed. Skip them when nothing user- or agent-relevant changed.

## 3. Gates: all must pass, or stop and report the output
```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pyflakes mfa | grep -E "undefined name|imported but unused|redefinition of|syntax error|invalid syntax" | grep -vE "'(playwright|casparser)' imported but unused"
```
- The pyflakes gate passes when that `grep` prints nothing.
- Unused locals and f-strings without placeholders are pre-existing style warnings. They are not blockers.
Never "fix" a failing gate by deleting or skipping tests.

## 4. Stage and guard
```bash
git add -A
python .claude/skills/commit-push/scripts/guard.py
```
- If the guard fails, run `git reset -q` and report its findings.
  - The guard masks PANs and emails in its output. Never print the raw values.
  - Fix the cause, for example by extending `.gitignore` or removing the value. Never bypass the guard.
- Re-run from step 3 after any fix.

## 5. Commit
- Group the staged files. If they are clearly unrelated changes, commit each group separately:
  - `git reset -q`
  - `git add <group files>`
  - re-run the guard
  - commit
- Message format:
  - An imperative subject of 72 characters or fewer that says *what* changed.
  - A blank line, then a short body that says *why*.
  - The trailer as the last line:
    ```
    Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
    ```
- Use the configured git identity. Don't pass `-c user.*`.

## 6. Sync and push
```bash
git fetch origin
git status -sb                      # if "behind": git pull --rebase origin main
git push origin HEAD:main
```
- If the rebase hits a conflict, run `git rebase --abort`, report it and stop. Never resolve it by discarding the user's work.
- If the push fails with an auth problem, report it. Git Credential Manager handles sign-in, and `gh` is not installed.

## 7. Report
In one or two lines, give:
- the commit short hash(es) and subject(s)
- `https://github.com/heptile7/mf-analyse/commit/<hash>`
- the pass counts from the gates

## Never
- `--force`, `--force-with-lease`, `--no-verify`, or amending or rebasing commits that are already pushed
- Committing `mf_portfolio_db*.json`, `.mf_salt`, CAS PDFs, caches, `.env*` or `settings.local.json`
- Pushing to any remote or branch other than `origin` / `main`
