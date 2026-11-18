"""Low-level helpers: atomic file writes, cache-bust flag, safe float parsing."""

from datetime import datetime
import os

from mfa.config import _DIR


def _save_output_file(write_fn, primary_path: str, auto_open: bool = False) -> str:
    """
    Write any output file reliably on Windows (atomic temp-then-replace).
    auto_open=True only for the HTML report — nothing else opens automatically.
    Returns the path actually written.
    """
    import tempfile, time as _time
    primary_dir = os.path.dirname(primary_path) or _DIR

    # Write to temp sibling first — never corrupts the target mid-write
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=primary_dir, suffix=".tmp")
        with os.fdopen(tmp_fd, "wb") as fh:
            write_fn(fh)
    except PermissionError:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=tempfile.gettempdir(), suffix=".tmp")
        with os.fdopen(tmp_fd, "wb") as fh:
            write_fn(fh)
        primary_dir  = tempfile.gettempdir()
        primary_path = os.path.join(primary_dir, os.path.basename(primary_path))

    # Evict old file to release viewer/browser lock
    if os.path.exists(primary_path):
        for _ in range(3):
            try:
                os.unlink(primary_path)
                break
            except PermissionError:
                _time.sleep(0.4)

    # Atomic rename
    try:
        os.replace(tmp_path, primary_path)
        final_path = primary_path
    except PermissionError:
        ts   = datetime.now().strftime("%H%M%S")
        ext  = os.path.splitext(primary_path)[1]
        base = os.path.splitext(os.path.basename(primary_path))[0]
        final_path = os.path.join(tempfile.gettempdir(), f"{base}_{ts}{ext}")
        os.replace(tmp_path, final_path)

    if auto_open:
        try:
            os.startfile(final_path)
        except Exception:
            pass

    return final_path


# ══════════════════════════════════════════════════════════════════════
# NAV DATA LOADING
# ══════════════════════════════════════════════════════════════════════

def _cache_busted() -> bool:
    """Returns True if the user ran 'clear cache' but files couldn't be reset."""
    import tempfile as _tf
    return os.path.exists(os.path.join(_tf.gettempdir(), "mf_cache_bust.flag"))


def _safe_float(v) -> float | None:
    if v in (None, "", "N/A", "NA", "-"):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


class CircuitBreaker:
    """Stop calling a source after `limit` consecutive failures (timeouts / 5xx).

    A down server answering each request only after a long timeout can otherwise
    turn a per-fund loop into an hours-long hang (seen with mfdata.in 522s).
    """

    def __init__(self, limit: int = 5):
        self.limit = limit
        self.failures = 0

    def result(self, ok: bool) -> None:
        self.failures = 0 if ok else self.failures + 1

    @property
    def open(self) -> bool:
        return self.failures >= self.limit
