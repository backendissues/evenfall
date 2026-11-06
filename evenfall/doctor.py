"""Health checks. `evenfall doctor` prints all of them; `quick()` is the cheap subset the SessionStart card uses to raise a warning."""
import fcntl
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, wal

LABEL = "com.evenfall.sync"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
SETTINGS = Path(config._env("SETTINGS") or Path.home() / ".claude" / "settings.json")
EVENTS = ("SessionStart", "PreCompact", "SessionEnd")
LAG_WARN, LAG_FAIL = 300, 1800            # seconds a transcript can run ahead of the archive


def _bin() -> str:
    from . import brief
    return brief.BIN


def hooks_installed() -> list:
    """Hook events missing an evenfall entry in Claude Code's settings."""
    try:
        hooks = json.loads(SETTINGS.read_text()).get("hooks", {})
    except (OSError, ValueError):
        return list(EVENTS)
    b = _bin()
    return [ev for ev in EVENTS if not any(h.get("command", "").startswith(b) for g in hooks.get(ev, []) for h in g.get("hooks", []))]


def lag_seconds(db) -> float:
    """How far the newest transcript write is ahead of the newest archived event (0 when caught up)."""
    try:
        newest = max(p.stat().st_mtime for p in config.SRC.rglob("*.jsonl"))
    except (ValueError, OSError):
        return 0.0
    row = db.execute("select max(ts) from events").fetchone()
    if not row or not row[0]:
        return time.time() - newest
    last = datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return max(0.0, newest - last)


def recent_errors(hours: int = 24) -> list:
    """Timestamped header lines of hook-errors.log newer than `hours`."""
    log = config.STATE / "hook-errors.log"
    try:
        lines = log.read_text().splitlines()
    except OSError:
        return []
    cutoff, out = time.time() - hours * 3600, []
    for ln in lines:
        try:
            t = datetime.strptime(ln[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if t >= cutoff:
            out.append(ln)
    return out


def last_error_line() -> str:
    log = config.STATE / "hook-errors.log"
    try:
        lines = [l for l in log.read_text().splitlines() if l.strip()]
    except OSError:
        return ""
    return lines[-1][:160] if lines else ""


def quick(db) -> list:
    """[(level, name, detail)] for the problems worth interrupting a session for: missing hooks, a lagging archive, fresh hook errors."""
    out = []
    miss = hooks_installed()
    if miss:
        out.append(("FAIL", "hooks", "missing in settings.json: " + ", ".join(miss)))
    lag = lag_seconds(db)
    if lag > LAG_FAIL:
        out.append(("FAIL", "lag", "archive is %d min behind the transcripts" % (lag // 60)))
    elif lag > LAG_WARN:
        out.append(("WARN", "lag", "archive is %d min behind the transcripts" % (lag // 60)))
    errs = recent_errors(24)
    if errs:
        out.append(("WARN", "hook errors", "%d in the last 24 h, last: %s" % (len(errs), last_error_line())))
    return out


def card_warning(db) -> str:
    try:
        bad = quick(db)
    except Exception:
        return ""
    return ("Evenfall warning: " + "; ".join("%s: %s" % (n, d) for _l, n, d in bad) + ". Run `evenfall doctor`.") if bad else ""


def _launchd_loaded() -> bool:
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
        return LABEL in r.stdout
    except Exception:
        return False


def run(db, deep: bool = False) -> list:
    res = list(quick(db))
    seen = {n for _l, n, _d in res}
    if "hooks" not in seen:
        res.insert(0, ("OK", "hooks", "SessionStart, PreCompact and SessionEnd are installed"))
    if "lag" not in seen:
        res.append(("OK", "lag", "archive is caught up with the transcripts"))
    if "hook errors" not in seen:
        res.append(("OK", "hook errors", "none in the last 24 h"))
    if (config.STATE / "disabled").exists():
        res.append(("WARN", "kill switch", "evenfall is OFF (state/disabled exists); run `evenfall on` to resume"))
    try:
        fd = os.open(config.STATE / "watcher.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            res.append(("OK", "watcher", "not running (it starts with the next session and exits when idle)"))
        except BlockingIOError:
            res.append(("OK", "watcher", "running"))
        finally:
            os.close(fd)
    except OSError:
        res.append(("WARN", "watcher", "lock file unavailable"))
    segs = sorted(config.ARCHIVE.rglob("*.seg"), key=lambda p: p.stat().st_mtime, reverse=True)
    bad = [s for s in (segs if deep else segs[:3]) if wal.valid_length(s) != s.stat().st_size]
    res.append(("FAIL", "archive", "%d segment(s) with a torn tail, e.g. %s" % (len(bad), bad[0])) if bad else
               ("OK", "archive", "%s segments have clean tails" % ("all" if deep else "the newest 3")))
    free = shutil.disk_usage(config.HOME if config.HOME.exists() else Path.home()).free / 1e9
    res.append(("FAIL" if free < 1 else "WARN" if free < 5 else "OK", "disk", "%.1f GB free" % free))
    try:
        days = json.loads(SETTINGS.read_text()).get("cleanupPeriodDays")
    except (OSError, ValueError):
        days = None
    res.append(("OK", "retention", "cleanupPeriodDays=%s" % days) if (days or 0) >= 365 else
               ("WARN", "retention", "cleanupPeriodDays=%s: Claude Code may delete transcripts before evenfall archives them" % days))
    res.append(("OK", "launchd", "catch-up agent loaded (every 60 s)") if _launchd_loaded() else
               ("WARN", "launchd", "catch-up agent not installed; run bin/install-launchd"))
    return res


def render(rows) -> str:
    return "\n".join("%-4s %-12s %s" % r for r in rows)
