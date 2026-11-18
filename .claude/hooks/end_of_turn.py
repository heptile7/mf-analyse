"""Stop hook: keep docs in sync, then make sure finished work gets committed.

Runs at most one check per stop, in order:
  A. Code changed but neither README.md nor CLAUDE.md did  -> ask Claude to
     update the docs (or say in one line that none is needed).
  B. Any uncommitted, non-ignored changes                   -> ask Claude to
     invoke the `commit-push` skill.

Loop guard: each check blocks at most once per distinct working-tree state
(hash of status + diff), recorded in %TEMP%. Skipped entirely in plan mode.
Always exits 0; any error means "let the stop happen".
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

CODE_PREFIXES = ("mfa/", ".claude/hooks/", ".claude/skills/")
CODE_FILES = {"MF_analyser.py", "requirements.txt", "requirements-dev.txt"}
DOC_FILES = {"README.md", "CLAUDE.md"}


def git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout


def changed_paths(status: str) -> list:
    paths = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"').replace("\\", "/"))
    return paths


def main() -> None:
    payload = json.load(sys.stdin)
    if payload.get("permission_mode") == "plan":
        return
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()

    status = git(root, "status", "--porcelain", "--untracked-files=all")
    paths = changed_paths(status)
    if not paths:
        return

    # State fingerprint: tracked diff + untracked files' size/mtime
    h = hashlib.sha256(status.encode())
    h.update(git(root, "diff", "HEAD").encode("utf-8", "replace"))
    for p in paths:
        try:
            st = os.stat(os.path.join(root, p))
            h.update(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            pass
    fingerprint = h.hexdigest()

    state_file = os.path.join(tempfile.gettempdir(),
                              f"claude-mfa-hooks-{payload.get('session_id', 'none')}.json")
    try:
        with open(state_file) as fh:
            state = json.load(fh)
    except Exception:
        state = {}

    code = [p for p in paths if p in CODE_FILES or p.startswith(CODE_PREFIXES)]
    docs_touched = any(p in DOC_FILES for p in paths)

    if code and not docs_touched and state.get("docs") != fingerprint:
        kind = "docs"
        reason = (
            "Code changed this session but README.md and CLAUDE.md did not: "
            + ", ".join(code[:15]) + (" ..." if len(code) > 15 else "")
            + ". Update README.md (user-facing usage) and/or CLAUDE.md (layout, rules, "
              "conventions) where behaviour, usage or structure changed. If the change "
              "doesn't affect either doc, say so in one line and stop.")
    elif state.get("commit") != fingerprint:
        kind = "commit"
        reason = (
            f"Uncommitted changes ({len(paths)} files): "
            + ", ".join(paths[:15]) + (" ..." if len(paths) > 15 else "")
            + ". If this unit of work is finished, invoke the `commit-push` skill to "
              "test, guard, commit and push. If work is deliberately mid-way, say so in one line.")
    else:
        return

    state[kind] = fingerprint
    try:
        with open(state_file, "w") as fh:
            json.dump(state, fh)
    except OSError:
        return        # can't record the guard -> don't risk a loop
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
