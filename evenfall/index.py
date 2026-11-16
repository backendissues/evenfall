"""Derived index: archive segments -> SQLite events/chunks + FTS5. Fully rebuildable from the archive.

Chunks are the 'narrative layer': what you said, what the assistant said, each tool call and a head/tail of
each result. Everything is a verbatim excerpt, so nothing here can be invented.
"""
import collections
import fcntl
import json
import os
import re
import sqlite3

from . import config, wal

SCHEMA = """
create table if not exists events(id text primary key, src text, off integer, ts text, type text,
  session text, cwd text, project text, side integer);
create table if not exists chunks(id integer primary key, event text, kind text, project text, ts text,
  src text, text text, session text, tuid text);
create virtual table if not exists chunks_fts using fts5(text, content='chunks', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
create table if not exists results(tuid text primary key, err integer, head text);
create table if not exists seg_progress(path text primary key, off integer);
create table if not exists meta(k text primary key, v text);
create index if not exists chunks_ts on chunks(ts);
create index if not exists chunks_pk on chunks(project, kind);
create index if not exists chunks_src on chunks(src, id);
create index if not exists chunks_tuid on chunks(tuid);
"""
SYS_PREFIXES = ("<system-reminder", "<command-", "<local-command", "Caveat:", "<task-notification")
KIND_WEIGHT = {"user": 1.0, "assistant": 1.0, "error": 0.9, "call": 0.8, "think": 0.8, "result": 0.7, "sys": 0.3}
STOP = set("the a an and or of to in on for is are was were be been it this that with as at by from we you i me my "
           "our not no do does did can could should would will just so if then than but what why how when where which "
           "who there their them they have has had about into over also".split())


def connect() -> sqlite3.Connection:
    config.HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.execute("pragma journal_mode=wal")
    db.execute("pragma synchronous=normal")
    db.executescript(SCHEMA)
    return db


def _clip(s: str, head=900, tail=300) -> str:
    if len(s) <= head + tail + 20:
        return s
    return s[:head] + " …[%d chars]… " % (len(s) - head - tail) + s[-tail:]


def _flat(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return ""


def chunks_from(d: dict):
    t = d.get("type")
    msg = d.get("message") or {}
    c = msg.get("content")
    out = []

    def user_text(s):
        return ("sys" if d.get("isMeta") or s.lstrip().startswith(SYS_PREFIXES) else "user", _clip(s, 6000, 1500))

    if t == "user":
        if isinstance(c, str):
            out.append(user_text(c))
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    out.append(user_text(b.get("text", "")))
                elif b.get("type") == "tool_result":
                    out.append(("error" if b.get("is_error") else "result", _clip(_flat(b.get("content"))), b.get("tool_use_id")))
    elif t == "assistant" and isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                out.append(("assistant", _clip(b.get("text", ""), 6000, 1500)))
            elif bt == "tool_use":
                out.append(("call", b.get("name", "") + " " + _clip(json.dumps(b.get("input"), ensure_ascii=False), 1200, 200), b.get("id")))
            elif bt == "thinking" and b.get("thinking"):
                out.append(("think", _clip(b["thinking"], 3000, 500)))
    return [(o[0], o[1], o[2] if len(o) > 2 else None) for o in out if o[1] and o[1].strip()]


def results_from(d: dict):
    """Every tool result, even an empty one, so an action with no output still settles as ok."""
    c = (d.get("message") or {}).get("content")
    if d.get("type") == "user" and isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                yield b["tool_use_id"], 1 if b.get("is_error") else 0, _clip(_flat(b.get("content")), 300, 100)


def index_all(db: sqlite3.Connection = None) -> dict:
    """One pass at a time: the watcher, hooks and manual runs all queue on this lock."""
    config.STATE.mkdir(parents=True, exist_ok=True)
    lock = os.open(config.STATE / "index.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _index_all(db)
    finally:
        os.close(lock)


def _index_all(db: sqlite3.Connection = None) -> dict:
    own = db is None
    db = db or connect()
    n_ev = n_ch = n_bad = n_dup = 0
    for seg in sorted(config.ARCHIVE.rglob("*.seg")):
        key = str(seg)
        row = db.execute("select off from seg_progress where path=?", (key,)).fetchone()
        start = row[0] if row else 0
        last = start
        batch = 0
        for rec, end in wal.scan(seg, start):
            last = end
            try:
                d = json.loads(rec["l"])
            except ValueError:
                n_bad += 1
                continue
            project = rec["s"].split("/", 1)[0]
            ts = d.get("timestamp") or ""
            cur = db.execute("insert or ignore into events values(?,?,?,?,?,?,?,?,?)",
                             (rec["i"], rec["s"], rec["o"], ts, d.get("type"), d.get("sessionId"), d.get("cwd"),
                              project, 1 if d.get("isSidechain") else 0))
            if cur.rowcount == 0:
                n_dup += 1
                continue
            n_ev += 1
            for tuid, err, head in results_from(d):
                db.execute("insert or replace into results values(?,?,?)", (tuid, err, head))
            for kind, text, tuid in chunks_from(d):
                c = db.execute("insert into chunks(event,kind,project,ts,src,text,session,tuid) values(?,?,?,?,?,?,?,?)",
                               (rec["i"], kind, project, ts, rec["s"], text, d.get("sessionId"), tuid))
                db.execute("insert into chunks_fts(rowid,text) values(?,?)", (c.lastrowid, text))
                n_ch += 1
            batch += 1
            if batch >= 5000:
                db.execute("insert or replace into seg_progress values(?,?)", (key, last))
                db.commit()
                batch = 0
        db.execute("insert or replace into seg_progress values(?,?)", (key, last))
        db.commit()
    from . import design, ledger
    led = ledger.build(db)
    design.build(db)
    if own:
        db.close()
    return {"events": n_ev, "chunks": n_ch, "bad_json": n_bad, "duplicates": n_dup, "ledger_new": led}


# ---------------------------------------------------------------- search
def tokens(q: str):
    seen, out = set(), []
    for t in re.findall(r"[A-Za-z0-9_]+", q.lower()):
        if t in STOP or len(t) < 2 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def match_expr(toks) -> str:
    return " OR ".join('"%s"%s' % (t, "*" if len(t) >= 4 else "") for t in toks)


def find(db, text: str, k=8, kind=None, project=None, since=None, budget=3200, exclude_session=None):
    toks = tokens(text)
    if not toks:
        return []
    sql = ("select c.id, c.ts, c.kind, c.project, c.src, snippet(chunks_fts, 0, '«', '»', ' … ', 40), "
           "bm25(chunks_fts) from chunks_fts join chunks c on c.id = chunks_fts.rowid where chunks_fts match ?")
    args = [match_expr(toks)]
    if kind:
        sql += " and c.kind in (%s)" % ",".join("?" * len(kind.split(",")))
        args += kind.split(",")
    if project:
        sql += " and c.project like ?"
        args.append("%" + project + "%")
    if since:
        sql += " and c.ts >= ?"
        args.append(since)
    if exclude_session:
        sql += " and coalesce(c.session, '') != ?"
        args.append(exclude_session)
    sql += " order by bm25(chunks_fts) limit 300"
    rows = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6] * KIND_WEIGHT.get(r[2], 0.5)) for r in db.execute(sql, args)]
    rows.sort(key=lambda r: r[6])
    out, used = [], 0
    for r in rows:
        if len(out) >= k or used + len(r[5]) > budget:
            break
        out.append(r)
        used += len(r[5])
    return out


def project_key(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)
