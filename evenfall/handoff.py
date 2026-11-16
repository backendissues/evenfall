"""Handoff drafts and memory staleness. No model: the ledger already holds every state-changing action with its Why line.

evenfall drafts, Claude approves and writes the project memory file; nothing here writes to the memory directory.
"""
import re
from datetime import datetime, timezone

from . import config

GENERIC = {"project", "portal", "site", "system", "plan", "sales", "memory", "build", "marketplace"}
STALE_ACTIONS = 5                 # this many outward-facing actions since the memory was last edited
COMMIT_MSG = re.compile(r'git commit[^"\']*?\s(?:-[a-zA-Z]*m|--message)[\s=]+(?:"((?:[^"\\]|\\.)*)|\'([^\']*))')   # -m, -am and --message


def _front(text: str, key: str) -> str:
    m = re.search(r"^\s*%s:\s*(.+)$" % key, text.split("\n---", 2)[0] + "\n", re.M) if text.startswith("---") else None
    return m.group(1).strip().strip('"') if m else ""


def memories() -> list:
    """[(keyword, path, modified_utc_iso)] for every project_*.md; the keyword is the first word of its slug (acme, evenfall...)."""
    out = []
    try:
        files = sorted(config.MEMORY_DIR.glob("project_*.md"))
    except OSError:
        return out
    for p in files:
        try:
            text = p.read_text()
            slug = p.stem[len("project_"):]
            words = [w for w in re.split(r"[_-]+", slug) if w and w not in GENERIC]
            if not words:
                continue
            file_mod = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            mod = max(_front(text, "modified")[:19] + "Z" if _front(text, "modified") else "", file_mod)   # a hand edit counts as much as a tool write
            out.append((words[0].lower(), p, mod))
        except OSError:
            continue
    return out


def _since_rows(db, keyword: str, since: str):
    """Outward-facing actions (not local file edits) whose text names the keyword as a whole word, oldest first.
    The project folder is not used: work on one product often happens in sessions started from another folder."""
    word = re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(keyword.lower()))
    rows = db.execute("select id, ts, status, op, summary, why, request_id from ledger where ts > ? and op not like 'local:%' "
                      "and op not in ('Write','Edit','NotebookEdit') and op not like 'claude%' order by ts", (since,)).fetchall()
    return [r for r in rows if word.search((r[4] or "").lower())]


def behind(db, hint: str) -> list:
    """[(memory file name, n actions since last edit)] for memories whose keyword appears in `hint` (cwd and project key)."""
    hint = hint.lower()
    out = []
    for kw, p, mod in memories():
        if kw not in hint:
            continue
        n = len(_since_rows(db, kw, mod))
        if n > STALE_ACTIONS:
            out.append((p.name, n))
    return out


def staleness_line(db, hint: str) -> str:
    rows = behind(db, hint)
    if not rows:
        return ""
    return "Memory notice: " + "; ".join("%s is %d outward actions behind" % r for r in rows) + ". Run `evenfall handoff` for a draft update."


def _req(db, rid) -> str:
    if not rid:
        return ""
    r = db.execute("select text from chunks where id=?", (rid,)).fetchone()
    return " ".join(r[0].split())[:140] if r else ""


def draft(db, keyword: str, since: str = "") -> str:
    kw = keyword.lower()
    if not since:
        mods = [m for k, _p, m in memories() if k == kw]
        since = mods[0] if mods else "1970-01-01"
    rows = _since_rows(db, kw, since)
    if not rows:
        return "No outward-facing actions mentioning '%s' since %s." % (keyword, since[:16])
    groups, order = {}, []
    for lid, ts, st, op, summ, why, rid in rows:
        key = why.strip() if why else ("Asked: " + _req(db, rid) if _req(db, rid) else "(no stated reason)")
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append((ts, st, op, summ))
    lines = ["Draft update for the %s memory, %s to %s (%d outward action%s). Edit before saving; keep only what is non-obvious." % (
        keyword, since[:10], rows[-1][1][:10], len(rows), "" if len(rows) == 1 else "s"), ""]
    for key in order:
        g = groups[key]
        ops = {}
        for _ts, st, op, _s in g:
            ops[op] = ops.get(op, 0) + 1
        fails = [x for x in g if x[1] in ("error", "denied")]
        msgs = []
        for _ts, st, op, summ in g:
            m = COMMIT_MSG.search(summ)
            if m and (m.group(1) or m.group(2)) and st == "ok":
                msgs.append((m.group(1) or m.group(2))[:110])
        lines.append("- %s: %s%s" % (key[:150], ", ".join("%s x%d" % kv for kv in sorted(ops.items(), key=lambda kv: -kv[1])[:4]),
                                      ("; %d failed or denied" % len(fails)) if fails else ""))
        for mmsg in list(dict.fromkeys(msgs))[:4]:
            lines.append("    commit: " + mmsg)
    lines += ["", "Also record: the commit it ended on, anything that failed and why, and open items. Add the absolute date."]
    return "\n".join(lines)
