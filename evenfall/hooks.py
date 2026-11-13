"""Entry points for Claude Code hooks and the resident watcher.

Rules for everything in here: never raise into Claude, never print anything except the SessionStart brief,
never take long. Any failure means we print nothing and exit 0.
"""
import json
import os
import subprocess
import sys
import time

from . import _compat, brief, config, index, ingest

ERR_LOG_MAX = 200 * 1024
IDLE_EXIT_SECONDS = 15 * 60
BRIEF_EVERY_SECONDS = 10 * 60


def disabled() -> bool:
    """Kill switch: `evenfall off` creates state/disabled and every hook, the watcher and the card go quiet."""
    return (config.STATE / "disabled").exists()


def _fail(where: str) -> None:
    """Hooks must never raise into Claude, but a failure must not vanish either: append it to state/hook-errors.log."""
    try:
        import traceback
        config.STATE.mkdir(parents=True, exist_ok=True)
        log = config.STATE / "hook-errors.log"
        if log.exists() and log.stat().st_size > ERR_LOG_MAX:
            _compat.replace(log, config.STATE / "hook-errors.log.1")
        with open(log, "a", encoding="utf-8") as f:
            f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), where, traceback.format_exc()))
    except Exception:
        pass


def _stdin_json() -> dict:
    try:
        if sys.stdin and not sys.stdin.isatty():
            return json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    return {}


def _record_session(payload: dict) -> None:
    sid = payload.get("session_id")
    if not sid:
        return
    d = config.STATE / "current"
    d.mkdir(parents=True, exist_ok=True)
    (d / (sid + ".json")).write_text(json.dumps({"cwd": payload.get("cwd") or os.getcwd(), "ts": time.time(),
                                                 "transcript": payload.get("transcript_path")}), encoding="utf-8")


def current_session(cwd: str):
    """Newest recorded session started in this folder, so find can leave out what the model already has in context."""
    best = None
    d = config.STATE / "current"
    try:
        for p in d.glob("*.json"):
            m = json.loads(p.read_text(encoding="utf-8"))
            if m.get("cwd") == cwd and time.time() - m.get("ts", 0) < 36 * 3600:
                if best is None or m["ts"] > best[0]:
                    best = (m["ts"], p.stem)
    except Exception:
        return None
    return best[1] if best else None


def start_watcher() -> None:
    lock = config.STATE / "watcher.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        if not _compat.try_lock(fd):                              # held => a watcher is already running
            os.close(fd)
            return
        _compat.unlock(fd)
        os.close(fd)
        _compat.spawn_detached(brief.command("watch", background=True))
    except Exception:
        pass


def hook_start() -> None:
    try:
        if disabled():
            return
        payload = _stdin_json()
        config.STATE.mkdir(parents=True, exist_ok=True)
        _record_session(payload)
        start_watcher()
        sys.stdout.write(brief.show(payload.get("cwd") or os.getcwd()))
    except Exception:
        _fail("hook start")


def hook_sync(final: bool = False) -> None:
    """PreCompact / SessionEnd: hand the work to a detached process and return at once (hooks must stay fast)."""
    try:
        _stdin_json()
        if disabled():
            return
        _compat.spawn_detached(brief.command("sync", background=True))
    except Exception:
        _fail("hook sync")


def _code_mtime(d: str) -> float:
    try:
        return max(os.stat(os.path.join(d, f)).st_mtime for f in os.listdir(d) if f.endswith(".py"))
    except OSError:
        return 0.0


def watch() -> None:
    """Resident ingester. Holds watcher.lock for life, polls the active transcripts cheaply, exits when idle."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    if disabled():
        return
    fd = os.open(config.STATE / "watcher.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    if not _compat.try_lock(fd):
        return
    last_sig, last_change, last_brief, dirty = None, time.time(), 0.0, False
    paths, last_scan = [], 0.0
    code_dir = os.path.dirname(os.path.abspath(__file__))
    code_mtime = _code_mtime(code_dir)
    while True:
        now = time.time()
        if disabled():
            return
        if now - last_scan > 30 and _code_mtime(code_dir) != code_mtime:
            return                                           # our own code changed: exit; the next session hook starts a fresh watcher
        try:
            if now - last_scan > 30:                       # directory walk is the expensive part: do it rarely
                paths = [p for p in config.SRC.rglob("*.jsonl") if now - p.stat().st_mtime < 3600]
                last_scan = now
            sig = tuple((str(p), p.stat().st_size) for p in paths)   # per-second cost: a few stat() calls
        except OSError:
            sig = last_sig
        if sig != last_sig:
            last_sig, last_change = sig, now
            time.sleep(1.0)                                   # debounce: let Claude finish its burst of writes
            try:
                s = ingest.ingest_all()
                if "skipped" not in s and s.get("lines"):
                    db = index.connect()
                    index.index_all(db)
                    dirty = True
                    if now - last_brief > BRIEF_EVERY_SECONDS:
                        brief.build_all(db)
                        last_brief, dirty = now, False
                    db.close()
            except Exception:
                _fail("watcher ingest")
        idle = now - last_change
        if idle > IDLE_EXIT_SECONDS:
            if dirty:
                try:
                    db = index.connect()
                    brief.build_all(db)
                    db.close()
                except Exception:
                    pass
            return
        time.sleep(1.0 if idle < 60 else 5.0)
