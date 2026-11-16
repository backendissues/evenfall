"""Standing instructions: what the user has told us about how they want things, found without any model.

A user message scores higher when it states a wish or rule (I want, don't, should, never, make it...), mentions style words
(dark, button, font, cluttered...), overlaps the topic words asked for, and is recent. Near-duplicates are collapsed.
Everything returned is the user's own wording with an id.
"""
import math
import re
import time
from datetime import datetime, timezone

WISH = re.compile(r"\b(i (?:don'?t|do not|didn'?t|dont) (?:want|like|think)|i (?:want|would want|need|prefer)|should|must|has to|needs? to|"
                  r"always|never|don'?t|dont|do not|stop|make (?:it|this|sure|everything)|keep|instead|rather|not to my liking|"
                  r"feels?|looks?|too (?:much|generic|big|small|bright)|get lost|no need|only|just)\b", re.I)
STYLE = re.compile(r"\b(ui|ux|design|button|pill|card|font|colou?rs?|dark|light|theme|sleek|clean|simple|simpler|clutter(?:ed)?|align(?:ed|ment)?|"
                   r"layout|animation|micro-?animations?|spacing|icon|nav|menu|mobile|responsive|tone|wording|copy|title|dash(?:es)?)\b", re.I)
WORD = re.compile(r"[a-z0-9']+")
SKIP = ("/", "<", "[", "Caveat", "Tool loaded", "Warning:")


def _age_days(ts: str) -> float:
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        return max(0.0, (time.time() - t) / 86400)
    except ValueError:
        return 365.0


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w     # button / buttons


def _toks(s: str) -> set:
    return set(_stem(w) for w in WORD.findall(s.lower()) if len(w) > 2)


def find_prefs(db, topic: str = "", project: str = None, k: int = 8, since: str = None):
    want = _toks(topic)
    sql = "select id, ts, text from chunks where kind='user' and length(text) between 40 and 700"
    args = []
    if project:
        sql += " and project like ?"
        args.append("%" + project + "%")
    if since:
        sql += " and ts >= ?"
        args.append(since)
    scored = []
    for cid, ts, text in db.execute(sql, args):
        t = " ".join(text.split())
        if t.startswith(SKIP) or "http" in t[:12]:
            continue
        wish, style = len(WISH.findall(t)), len(STYLE.findall(t))
        topic_hits = len(want & _toks(t)) if want else 0
        if wish == 0 and style == 0:
            continue
        if want and topic_hits == 0:
            continue
        score = 2.0 * min(wish, 4) + 1.5 * min(style, 4) + 3.0 * topic_hits
        score *= math.exp(-_age_days(ts) / 90.0)                  # newer instructions outrank older ones
        scored.append((score, cid, ts, t, _toks(t)))
    scored.sort(reverse=True)
    out = []
    for sc, cid, ts, t, tk in scored:
        if any(len(tk & o[4]) / max(1, len(tk | o[4])) > 0.6 for o in out):
            continue                                              # near-duplicate of one already chosen
        out.append((sc, cid, ts, t, tk))
        if len(out) >= k:
            break
    out.sort(key=lambda r: r[2], reverse=True)
    return [(cid, ts, t) for _sc, cid, ts, t, _tk in out]
