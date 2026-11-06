"""Tail Claude Code transcripts into the append-only archive.

Order of operations is what makes it crash-safe:  write records -> flush to disk -> only then advance the cursor.
A crash in between re-reads a block and writes duplicate records, which the index drops by id.
Only complete (newline-terminated) lines are consumed; a half-written last line waits for the next run.
"""
import fcntl
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone

from . import config, wal
from .redact import redact_line

_TS = re.compile(r'"timestamp":"(\d{4}-\d{2}-\d{2})')
CURSOR_FILE = "cursors.json"


class SimulatedCrash(Exception):
    pass


def _crash_point(name: str) -> None:
    if config._env("CRASH") == name:
        raise SimulatedCrash(name)


def event_id(rel: str, gen: int, off: int) -> str:
    return hashlib.blake2b(("%s\0%d\0%d" % (rel, gen, off)).encode(), digest_size=12).hexdigest()


def sources():
    for p in sorted(config.SRC.rglob("*.jsonl")):
        rel = p.relative_to(config.SRC).as_posix()
        yield rel, p, rel.split("/", 1)[0]


def _load_cursors() -> dict:
    try:
        return json.loads((config.STATE / CURSOR_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_cursors(cur: dict) -> None:
    config.STATE.mkdir(parents=True, exist_ok=True)
    tmp = config.STATE / (CURSOR_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(cur, separators=(",", ":")).encode())
        wal._full_sync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, config.STATE / CURSOR_FILE)


def _head_hash(path, n: int) -> str:
    with open(path, "rb") as f:
        return hashlib.blake2b(f.read(n), digest_size=8).hexdigest()


class Writers:
    """Small LRU of open segments, keyed by (project, day)."""

    def __init__(self, machine: str):
        self.machine = machine
        self.open = OrderedDict()

    def get(self, project: str, day: str) -> wal.Segment:
        key = (project, day)
        seg = self.open.pop(key, None)
        if seg is None:
            seg = wal.Segment(config.ARCHIVE / self.machine / project / (day + ".seg"))
        self.open[key] = seg
        while len(self.open) > config.MAX_OPEN_SEGMENTS:
            _k, old = self.open.popitem(last=False)
            old.write_out()
            old.sync()
            old.close()
        return seg

    def commit(self) -> None:
        for seg in self.open.values():
            seg.write_out()
        _crash_point("after_write")
        for seg in self.open.values():
            if seg.dirty:
                seg.sync()
        _crash_point("after_sync")

    def close(self) -> None:
        for seg in self.open.values():
            seg.write_out()
            seg.sync()
            seg.close()
        self.open.clear()


def _ingest_source(rel, path, project, cur, writers, stats) -> None:
    st = os.stat(path)
    c = cur.get(rel) or {"off": 0, "gen": 0, "head": ""}
    off, gen = c["off"], c["gen"]
    if off and (st.st_size < off or _head_hash(path, min(off, 4096)) != c["head"]):
        gen, off = gen + 1, 0                       # file was replaced or truncated: start a new generation
        stats["restarts"] += 1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    carry = b""
    base = off
    with open(path, "rb") as f:
        f.seek(off)
        while True:
            block = f.read(config.READ_BLOCK)
            if not block:
                break
            data = carry + block
            cut = data.rfind(b"\n") + 1
            if cut == 0:
                carry = data
                continue
            carry = data[cut:]
            pos = base
            for raw in data[:cut].split(b"\n")[:-1]:
                line_off = pos
                pos += len(raw) + 1
                if not raw.strip():
                    continue
                text = redact_line(raw.decode("utf-8", "replace"), stats["redactions"])
                m = _TS.search(text)
                day = m.group(1) if m else today
                writers.get(project, day).append(
                    {"i": event_id(rel, gen, line_off), "s": rel, "g": gen, "o": line_off,
                     "t": int(time.time() * 1000), "l": text})
                stats["lines"] += 1
            stats["bytes"] += cut
            base += cut
            writers.commit()
            cur[rel] = {"off": base, "gen": gen, "head": _head_hash(path, min(base, 4096))}
            _save_cursors(cur)
            _crash_point("after_cursor")
    if base == off and rel not in cur:               # empty file: remember it so we do not rescan forever
        cur[rel] = {"off": 0, "gen": gen, "head": ""}


def ingest_all() -> dict:
    """One pass over every transcript. Safe to call at any time and from several triggers."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    lock = os.open(config.STATE / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return {"skipped": "another ingest is running"}
    t0 = time.time()
    stats = {"files": 0, "lines": 0, "bytes": 0, "restarts": 0, "redactions": {}}
    cur = _load_cursors()
    writers = Writers(config.machine_id())
    try:
        for rel, path, project in sources():
            try:
                size = os.stat(path).st_size
            except FileNotFoundError:
                continue
            if rel in cur and cur[rel]["off"] == size:
                continue
            stats["files"] += 1
            _ingest_source(rel, path, project, cur, writers, stats)
        writers.close()
        _save_cursors(cur)
    finally:
        os.close(lock)
    stats["seconds"] = round(time.time() - t0, 2)
    return stats
