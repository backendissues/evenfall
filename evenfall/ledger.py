"""Decision ledger: every state-changing action, tied to the request that caused it and the reasoning around it.

Derived entirely from the transcript, so it needs no extra hook and works on all past history:
  request   = the last real user message before the call, in the same transcript file
  rationale = the assistant text written after that request and before the call, plus an explicit `Why:` line if any
  status    = from the matching tool result: ok / error / denied (permission refused) / pending
Nothing is generated: every field is a verbatim excerpt with an id that resolves to the full text.
"""
import json
import re

from . import shell

READ_WORDS = re.compile(r"(^|_)(list|get|search|read|view|query|fetch|describe|inspect|status|metrics|count|find|check|show|download|output)(_|$)", re.I)
MUT_WORDS = re.compile(r"(create|update|delete|send|post|publish|deploy|write|move|trash|share|upload|set|add|remove|cancel|stop|start|run|generate|spawn|enable|disable|toggle|connect|reset|install|import)", re.I)
FILE_TOOLS = {"Write", "Edit", "NotebookEdit"}
BASH_MUT = re.compile(
    r"(^|[;&|(\s])(rm|mv|cp|rsync|mkdir|rmdir|chmod|chown|ln|touch|tee|kill|pkill|launchctl|crontab|brew|pip3?|npm|npx|yarn|dd|truncate|install)\s"
    r"|\bsed\s+-i|\bgit\s+(?:-C\s+\S+\s+)?(add|commit|push|checkout|switch|reset|merge|rebase|stash|tag|clean|restore|rm|mv|pull)\b"
    r"|\bcurl\b[^|\n]*\s-(X|d|F)\b|\bcurl\b[^|\n]*--(data|request|form)\b|\.write\(|\bopen\([^)]*['\"][wa]b?['\"]"
    r"|\bsqlite3?\b[^\n]*\b(insert|update|delete|drop|create)\b", re.I)
REDIRECT = re.compile(r"(?<![-=<>|])>{1,2}(?![=&>])\s*\S")
NOISE_REDIRECT = re.compile(r"\d*>&\d|\d?>\s*/dev/null")
DENY = re.compile(r"denied by the (auto mode )?classifier|Permission for this action was denied|doesn't want to proceed|tool use was rejected", re.I)
WHY = re.compile(r"(?:^|\n)[\s>*-]*\*{0,2}Why:\*{0,2}\s*(.+)")
OPERATION = re.compile(r'"operation":\s*"([^"]+)"')

WHY_RULE_START = "2026-11-06T07:32"     # when the Why: convention was added to CLAUDE.md; earlier sessions cannot follow it

SCHEMA = """
create table if not exists ledger(id integer primary key, ts text, session text, project text, src text, tool text,
  op text, summary text, status text, perm text, request_id integer, rationale text, why text, result_head text);
create index if not exists ledger_ts on ledger(ts);
"""


def _command(rest: str) -> str:
    m = re.search(r'"command":\s*"((?:[^"\\]|\\.)*)', rest)
    if not m:
        return rest
    try:
        return json.loads('"' + m.group(1) + '"')
    except ValueError:
        return m.group(1)


def classify(tool: str, rest: str):
    """-> (mutating, op, summary). Conservative: unknown means not mutating, so the ledger stays about real changes."""
    if tool in FILE_TOOLS:
        m = re.search(r'"(?:file_path|notebook_path)":\s*"([^"]+)"', rest)
        return True, tool, (m.group(1) if m else rest)[:300]
    if tool == "Bash":
        cmd = _command(rest)
        flat = NOISE_REDIRECT.sub("", cmd)
        if BASH_MUT.search(flat) or REDIRECT.search(flat):
            names = shell.command_names(cmd)
            first = names[0] if names else "bash"
            if first == "cd" and len(names) > 1:
                first = names[1]
            if first == "git":
                m = re.search(r"\bgit\s+(?:-C\s+\S+\s+)?([a-z-]+)", cmd)
                first = "git " + m.group(1) if m else "git"
            remote = shell.remote_op(cmd)
            op = remote or "local:" + first        # local:* ops are file edits and housekeeping, not outward-facing changes
            return True, op, " ".join(shell.strip_heredocs(cmd).split())[:300]
        return False, "", ""
    if tool.startswith("mcp__"):
        short = tool.split("__")[-1]
        ops = OPERATION.findall(rest)
        if ops:
            muts = [o for o in ops if not READ_WORDS.search(o)]
            if not muts:
                return False, "", ""
            ids = re.findall(r'"(?:domain|username|name|title)":\s*"([^"]+)"', rest)[:3]
            return True, ",".join(sorted(set(muts)))[:80], ("%s %s %s" % (tool.split("__")[1], ",".join(muts), " ".join(ids))).strip()[:300]
        if READ_WORDS.search(short) or not MUT_WORDS.search(short):
            return False, "", ""
        return True, short, ("%s %s" % (tool.split("__")[1], rest))[:300]
    return False, "", ""


def build(db) -> int:
    db.executescript(SCHEMA)
    # settle rows whose result had not been indexed yet
    for lid, tuid in db.execute("select id, (select tuid from chunks where id=ledger.id) from ledger where status='pending'").fetchall():
        res = db.execute("select err, head from results where tuid=?", (tuid,)).fetchone()
        if res:
            status, perm = _status(res)
            db.execute("update ledger set status=?, perm=?, result_head=? where id=?", (status, perm, res[1][:300], lid))
    row = db.execute("select v from meta where k='ledger_progress'").fetchone()
    last = int(row[0]) if row else 0
    new = 0
    top = last
    for cid, ts, session, project, src, text, tuid in db.execute(
            "select id, ts, session, project, src, text, tuid from chunks where kind='call' and id>? order by id", (last,)).fetchall():
        top = cid
        tool, _, rest = text.partition(" ")
        mut, op, summary = classify(tool, rest)
        if not mut:
            continue
        res = db.execute("select err, head from results where tuid=?", (tuid,)).fetchone()
        status, perm = _status(res) if res else ("pending", "")
        users = [u for (u,) in db.execute("select id from chunks where src=? and id<? and kind='user' order by id desc limit 3", (src, cid)).fetchall()]
        req_id = users[0] if users else None
        lo = users[-1] if users else 0          # reasoning window: back to the third-latest user message
        texts = [t for (t,) in db.execute("select text from chunks where src=? and kind='assistant' and id>? and id<? order by id desc limit 2",
                                          (src, lo, cid)).fetchall()][::-1]
        rationale = " ¶ ".join(t.strip() for t in texts)[:900]
        why = ""
        for t in reversed(texts):
            m = None
            for m in WHY.finditer(t):
                pass
            if m:
                why = m.group(1).strip()[:400]
                break
        db.execute("insert or replace into ledger values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (cid, ts, session, project, src, tool, op, summary, status, perm, req_id, rationale, why, (res[1][:300] if res else "")))
        new += 1
    db.execute("insert or replace into meta values('ledger_progress', ?)", (str(top),))
    db.commit()
    return new


def _status(res):
    err, text = res
    if DENY.search(text[:600]):
        return "denied", text[:200]
    return ("error" if err else "ok"), ""


def actions(db, text="", since=None, project=None, tool=None, k=15, only=None):
    sql = "select id, ts, status, tool, op, summary, why, request_id from ledger where 1=1"
    args = []
    for t in re.findall(r"[A-Za-z0-9_.\-]+", text.lower()):
        sql += " and (lower(summary) like ? or lower(op) like ?)"
        args += ["%" + t + "%"] * 2
    if since:
        sql += " and ts >= ?"; args.append(since)
    if project:
        sql += " and project like ?"; args.append("%" + project + "%")
    if tool:
        sql += " and tool like ?"; args.append("%" + tool + "%")
    if only:
        sql += " and status=?"; args.append(only)
    sql += " order by ts desc limit ?"
    args.append(k)
    return db.execute(sql, args).fetchall()


def trace(db, lid: int) -> str:
    r = db.execute("select id, ts, session, tool, op, summary, status, perm, request_id, rationale, why, result_head from ledger where id=?", (lid,)).fetchone()
    if not r:
        return "no ledger entry with id %d (use `evenfall actions <words>` to list ids)" % lid
    _id, ts, session, tool, op, summary, status, perm, req_id, rationale, why, head = r
    src = db.execute("select src from chunks where id=?", (_id,)).fetchone()[0]
    users = db.execute("select id, ts, text from chunks where src=? and id<? and kind='user' order by id desc limit 3", (src, _id)).fetchall()
    out = ["ACTION  [%d] %s  %s  %s" % (_id, ts[:19].replace("T", " "), tool, status.upper()), "        " + summary]
    out.append("YOUR LAST MESSAGES BEFORE THIS (newest first):")
    for uid, uts, utext in users:
        out.append("  [%d] %s  %s" % (uid, uts[:16].replace("T", " "), " ".join(utext.split())[:300]))
    if not users:
        out.append("  none found")
    out.append("WHY     " + (why if why else "(no explicit Why: line)"))
    out.append("CONTEXT " + (" ".join(rationale.split())[:900] if rationale else "(no assistant text between the request and the action)"))
    out.append("RESULT  " + (" ".join(head.split())[:300] if head else "(pending or none)"))
    if status == "denied":
        out.append("PERMISSION denied: " + " ".join(perm.split())[:200])
    out.append("session %s  | full text: evenfall show %d --around 4" % ((session or "")[:8], _id))
    return "\n".join(out)


def audit(db) -> str:
    q = lambda s, a=(): db.execute(s, a).fetchone()[0]
    n = q("select count(*) from ledger")
    if not n:
        return "ledger is empty"
    by = dict(db.execute("select status, count(*) from ledger group by status").fetchall())
    with_req = q("select count(*) from ledger where request_id is not null")
    with_ctx = q("select count(*) from ledger where rationale != ''")
    why_n = q("select count(*) from ledger where why != ''")
    since_n = q("select count(*) from ledger where ts >= ?", (WHY_RULE_START,))
    since_why = q("select count(*) from ledger where ts >= ? and why != ''", (WHY_RULE_START,))
    pct = lambda a, b: "%.0f%%" % (100.0 * a / b) if b else "n/a"
    return ("state-changing actions: %d  (%s)\n  linked to a user request: %s\n  with assistant text before them: %s\n"
            "  with an explicit Why: line since the rule began (%s): %d of %d (%s)"
            % (n, ", ".join("%s %d" % kv for kv in sorted(by.items())), pct(with_req, n), pct(with_ctx, n),
               WHY_RULE_START[:10], since_why, since_n, pct(since_why, since_n)))
