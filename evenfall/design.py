"""Design language: the user's standing design commands, learned from their messages, newest command wins.

No model is involved. Each user message is split into short segments; a segment that states a design wish becomes a rule
(topic, stance, the user's own words). Rules belong to a product, found from the files touched around the message.

Newest wins, but only on evidence that it was acted on:
  - a newer rule with the opposite stance on the same topic REPLACES the older one once the assistant acted on it
    (a state-changing action happened before the user's next message); until then the older rule shows as pending_override
  - a newer rule with the same stance MERGES the older (a repeat), and the repeat count is kept as reinforcement
  - a complaint in the very next message marks the rule disputed (the user had to push again)
"""
import collections
import json
import re

from . import config, prefs

SCHEMA = """
create table if not exists design_rules(
  id integer primary key, chunk_id integer, ts text, src text, session text, project text, product text,
  scope text, topic text, stance integer, text text,
  followed integer default 0, disputed integer default 0, repeats integer default 0,
  status text default 'active', replaced_by integer);
create index if not exists dr_group on design_rules(product, topic, ts);
create unique index if not exists dr_uniq on design_rules(chunk_id, topic, text);
"""

# topic, scope, needs a wish cue, label for stance +1, label for stance -1, regex for +1, regex for -1
_ANIM = r"\b(?:micro-?)?animations?\b"
TOPICS = [
    ("theme", "product", True, "dark theme", "light theme",
     r"\b(?:dark(?:er)?|midnight)\b", r"\blight (?:theme|mode|colou?rs?|background)\b|\bbright(?:er)?\b|\bwhite background\b"),
    ("font_size", "product", False, "larger text", "smaller, crisper text",
     r"\b(?:bigger|larger)\b[^.!?\n]{0,15}\b(?:font|text|type)\b|\bfonts?\b[^.!?\n]{0,20}\b(?:bigger|larger)\b|\btoo small\b|\bcan'?t read\b",
     r"\b(?:smaller|crisp(?:er|y)|tiny)\b[^.!?\n]{0,25}\b(?:fonts?|text|type)\b|\bfonts?\b[^.!?\n]{0,30}\b(?:smaller|crisp(?:er|y))\b"),
    ("micro_animation", "product", False, "keep animations", "no micro-animations",
     r"\b(?:add|want|needs?|more|use)\b[^.!?\n]{0,25}" + _ANIM,
     r"\b(?:no|don'?t need|do not need|don'?t want|dont need|dont want|remove|without|too much|stop)\b[^.!?\n]{0,40}" + _ANIM
     + r"|" + _ANIM + r"[^.!?\n]{0,30}\b(?:too much|not needed|unnecessary|annoying|remove)\b|\b(?:don'?t|dont) like the (?:live )?animation"),
    ("loading_animation", "product", False, "subtle loading animation", "no loading animation",
     r"\bloading\b[^.!?\n]{0,30}\banimation\b|\banimation\b[^.!?\n]{0,25}\bloading\b|\bloading screen\b", r"\bno loading animation\b"),
    ("density", "product", True, "show more information", "simpler, less clutter",
     r"\bmore (?:info|information|details|data)\b|\bshow more\b|\bcan be shown\b|\btoo (?:basic|empty|little)\b",
     r"\bclutter(?:ed)?\b|\btoo (?:much|busy|crowded)\b|\bstrip (?:it )?down\b|\bminimal\b|\bsimpl(?:e|er|ify)\b|\bclean(?:er)?\b|\bunnecessary\b|\bget lost\b|\blight application\b|\blightweight\b"),
    ("glass", "product", False, "glass effect", "no glass effect",
     r"\bglass(?: effect|morphism)?\b", r"\b(?:no|remove|without)\b[^.!?\n]{0,15}\bglass\b"),
    ("mobile", "product", False, "mobile friendly", "desktop only",
     r"\bmobile(?:[- ]?(?:friendly|responsive))?\b|\bresponsive\b|\bphones?\b[^.!?\n]{0,15}\b(?:too|as well)\b",
     r"\bdesktop[- ]only\b|\bskip (?:the )?mobile\b|\bno mobile\b|\bnot (?:for )?mobile\b"),
    ("alignment", "product", False, "aligned", "",
     r"\balign(?:ed|ment)?\b|\bnot aligned\b|\bsame (?:size|width|height)\b|\bevenly\b", r"(?!x)x"),
    ("blue_on_blue", "product", False, "", "avoid too much blue",
     r"(?!x)x", r"\bblue on blue\b|\btoo much blue\b"),
    ("copy_dash", "global", False, "", "no em dashes in copy", r"(?!x)x", r"\bem[- ]?dash(?:es)?\b"),
]
COMPONENTS = re.compile(r"\b(button|pill|card|chip|banner|nav|menu|header|footer|sidebar|modal|dropdown|table|chart|icon|avatar|badge|graph|tile)s?\b", re.I)
COMPILED = [(t, sc, need, lp, ln, re.compile(rp, re.I), re.compile(rn, re.I)) for t, sc, need, lp, ln, rp, rn in TOPICS]
IMPERATIVE = re.compile(r"\b(?:switch|change|use|go with|set|turn|swap|replace|add|remove|make|keep|move|put|show|hide|redo|redesign|rethink|fix|revert)\b|\bplease\b", re.I)
STRONG = {"density": re.compile(r"\bstrip (?:it )?down\b|\bnothing unnecessary\b|\bminimal\b|\bclutter(?:ed)?\b|\bget lost\b|\blight application\b|\blightweight\b|\bunnecessary\b", re.I)}
SPLIT = re.compile(r"\s\+\s|[.!?\n]+|\s[-–]\s|;")
COMPLAINT = re.compile(r"\b(?:not what|not to my|don'?t like|dont like|still|again|wrong|didn'?t|did not|not following|ignored|doesn'?t follow|do not follow|revert|undo|lost)\b", re.I)
DEFAULT_ALIASES = {}     # old or staging names of the same product; add yours with `evenfall alias old new` (stored in state/aliases.json)
NEGATOR = re.compile(r"\b(?:not|no|dont|don'?t|never|without|skip|cannot|can ?not|can'?t)\b", re.I)
MOBILE_REJECT = re.compile(r"\b(?:not|dont|don'?t|do not|never|no need|skip|without|cannot|can ?not)\b[^.!?\n]{0,40}\b(?:worry|care|bother|support|build|design|open|use|need)\b[^.!?\n]{0,40}\bmobile\b", re.I)
QUESTION_START = re.compile(r"^(?:should i|should we|can we|can you|could you|do we|do you|is there|are there|how|what|why|which|where|when|who|does|did)\b", re.I)
PRODUCT_PATH = re.compile(r"/(?:Desktop|Documents|Projects|projects|code|dev|src|work)/(?:cl/)?([A-Za-z0-9_.-]+)")
SKIP = ("<", "[", "Caveat", "Tool loaded", "Warning:")


def aliases() -> dict:
    try:
        return {**DEFAULT_ALIASES, **json.loads((config.STATE / "aliases.json").read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_ALIASES)


def canon(product: str) -> str:
    return aliases().get(product, product)


def set_alias(db, old: str, new: str) -> None:
    config.STATE.mkdir(parents=True, exist_ok=True)
    cur = {}
    try:
        cur = json.loads((config.STATE / "aliases.json").read_text())
    except (OSError, ValueError):
        pass
    cur[old] = new
    (config.STATE / "aliases.json").write_text(json.dumps(cur, indent=1))
    db.executescript(SCHEMA)
    _resolve(db)
    db.commit()


def _names(db, product: str) -> list:
    c = canon(product)
    return [p for (p,) in db.execute("select distinct product from design_rules") if canon(p) == c] or [product]


def segments(text: str):
    for seg in SPLIT.split(text):
        seg = " ".join(seg.split())
        if 12 <= len(seg) <= 400:
            yield seg


def atoms(text: str):
    """[(topic, scope, stance, segment)] design commands found in one user message."""
    if len(text) > 900 or text.lstrip().startswith(SKIP):
        return []
    msg_wish = bool(prefs.WISH.search(text))
    asked = set(" ".join(q.split()) for q in re.findall(r"([^.!?\n]+)\?", text))
    out, seen = [], set()
    for seg in segments(text):
        if seg in asked or QUESTION_START.match(seg):
            continue                                                   # a question is not a command
        wish = bool(prefs.WISH.search(seg) or IMPERATIVE.search(seg)) or msg_wish
        for topic, scope, need, _lp, _ln, rp, rn in COMPILED:
            if need and not wish and not (topic in STRONG and STRONG[topic].search(seg)):
                continue
            if topic == "micro_animation" and "loading" in seg.lower():
                continue
            stance = -1 if rn.search(seg) else 0
            if not stance:
                m = rp.search(seg)
                if m:
                    if topic == "mobile":
                        stance = -1 if MOBILE_REJECT.search(seg) else 1        # "not very mobile optimised" is a complaint (+1), "not going to open it on mobile" a rejection
                    elif topic == "glass":
                        stance = -1 if NEGATOR.search(seg[max(0, m.start() - 45):m.start()]) else 1
                    else:
                        stance = 1
            if stance and (topic, seg) not in seen:
                seen.add((topic, seg))
                out.append((topic, scope, stance, seg[:220]))
        if (prefs.WISH.search(seg) or IMPERATIVE.search(seg)) and COMPONENTS.search(seg) and not any(a[3] == seg for a in out):
            noun = COMPONENTS.search(seg).group(1).lower()
            out.append(("component:" + noun, "product", 0, seg[:220]))
    return out


def product_for(db, src: str, cid: int, project: str) -> str:
    c = collections.Counter()
    for (t,) in db.execute("select text from chunks where src=? and kind='call' and id between ? and ?", (src, cid - 30, cid + 80)):
        c.update(PRODUCT_PATH.findall(t))
    if c:
        return c.most_common(1)[0][0]
    return project.replace(config.HOME_KEY + "-Desktop-cl-", "").replace(config.HOME_KEY + "-", "").replace(config.HOME_KEY, "home") or project


def build(db) -> int:
    db.executescript(SCHEMA)
    row = db.execute("select v from meta where k='design_progress'").fetchone()
    last = int(row[0]) if row else 0
    new, top = 0, last
    for cid, ts, src, session, project, text in db.execute(
            "select id, ts, src, session, project, text from chunks where kind='user' and id>? order by id", (last,)).fetchall():
        top = cid
        found = atoms(text)
        if not found:
            continue
        product = product_for(db, src, cid, project)
        for topic, scope, stance, seg in found:
            cur = db.execute("insert or ignore into design_rules(chunk_id,ts,src,session,project,product,scope,topic,stance,text) values(?,?,?,?,?,?,?,?,?,?)",
                             (cid, ts, src, session, project, "*" if scope == "global" else product, scope, topic, stance, seg))
            new += cur.rowcount
    db.execute("insert or replace into meta values('design_progress', ?)", (str(top),))
    _refresh_evidence(db)
    _resolve(db)
    db.commit()
    return new


def _refresh_evidence(db) -> None:
    """followed = the assistant acted before the user's next message; disputed = the next message complained about it."""
    for rid, cid, src, topic, followed, disputed in db.execute("select id, chunk_id, src, topic, followed, disputed from design_rules").fetchall():
        nxt = db.execute("select id, text from chunks where src=? and kind='user' and id>? order by id limit 1", (src, cid)).fetchone()
        end = nxt[0] if nxt else 1 << 60
        f = 1 if db.execute("select 1 from ledger where src=? and id>? and id<? limit 1", (src, cid, end)).fetchone() else 0
        d = 0
        if nxt and COMPLAINT.search(nxt[1]):
            word = topic.split(":")[-1].replace("_", " ")
            hit = any(a[0] == topic for a in atoms(nxt[1])) or word.split()[0] in nxt[1].lower()
            d = 1 if hit else 0
        if (f, d) != (followed, disputed):
            db.execute("update design_rules set followed=?, disputed=? where id=?", (f, d, rid))


def _resolve(db) -> None:
    db.execute("update design_rules set status='active', replaced_by=NULL, repeats=0")
    groups = collections.defaultdict(list)
    for r in db.execute("select id, product, topic, stance, ts, chunk_id, followed from design_rules order by ts, chunk_id").fetchall():
        groups[(canon(r[1]), r[2])].append(r)
    for rows in groups.values():
        cur, repeats = [], {}
        for r in rows:
            rid, _p, _t, stance, _ts, _c, _f = r
            repeats[rid] = 0
            for a in list(cur):
                if a[3] == stance:                                   # same command again: merge, keep the count
                    db.execute("update design_rules set status='merged', replaced_by=? where id=?", (rid, a[0]))
                    repeats[rid] += repeats[a[0]] + 1
                    cur.remove(a)
            cur.append(r)
        newest = {}
        for r in cur:                                                # surviving rules: at most one per stance
            newest[r[3]] = r
        for rid, n in repeats.items():
            if n:
                db.execute("update design_rules set repeats=? where id=?", (n, rid))
        for a in cur:
            if a[3] == 0:
                continue
            rivals = [b for b in cur if b[3] == -a[3] and (b[4], b[5]) > (a[4], a[5])]
            if rivals:
                b = max(rivals, key=lambda x: (x[4], x[5]))
                db.execute("update design_rules set status=?, replaced_by=? where id=?", ("overridden" if b[6] else "pending_override", b[0], a[0]))


# ---------------------------------------------------------------- reading
def rules(db, product=None, words="", show_all=False, k=40):
    sql = ("select id, chunk_id, ts, product, scope, topic, stance, text, followed, disputed, repeats, status, replaced_by "
           "from design_rules where 1=1")
    args = []
    if not show_all:
        sql += " and status in ('active','pending_override')"
    if product:
        names = _names(db, product)
        sql += " and (product in (%s) or scope='global')" % ",".join("?" * len(names))
        args += names
    toks = prefs._toks(words)
    out = []
    for r in db.execute(sql + " order by ts desc", args):
        if toks and not (toks & prefs._toks(r[5].replace("_", " ") + " " + r[7])):
            continue
        out.append(r)
    return out[:k]


def _flags(r) -> str:
    f = []
    if r[10]:
        f.append("repeated x%d" % (r[10] + 1))
    f.append("acted on" if r[8] else "not acted on yet")
    if r[9]:
        f.append("you pushed again")
    if r[11] == "pending_override":
        f.append("a newer command is waiting to be acted on")
    return ", ".join(f)


def render(db, product=None, words="", show_all=False) -> str:
    rs = rules(db, product, words, show_all)
    if not rs:
        return "no design commands found" + (" for %s" % product if product else "")
    head = "Design language%s (your commands, newest wins):" % (" for " + product if product else "")
    out = [head]
    for r in rs:
        _id, cid, ts, prod, scope, topic, stance, text, fol, dis, rep, status, rby = r
        mark = "" if status == "active" else " [%s]" % status.replace("_", " ")
        out.append("- [%d] %s %s%s: %s  (%s)" % (cid, ts[:10], topic.replace("_", " "), mark, text, _flags(r)))
        if status in ("overridden", "merged") and rby:
            nr = db.execute("select chunk_id, ts from design_rules where id=?", (rby,)).fetchone()
            if nr:
                out.append("    replaced by [%d] %s" % (nr[0], nr[1][:10]))
    return "\n".join(out)


def section(db, product: str, k: int = 8, budget: int = 1300) -> str:
    """Compact block for the session card: active rules only, one per topic, newest first."""
    try:
        rs = rules(db, product, k=60)
    except Exception:
        return ""
    seen, main, comp = set(), [], []
    for r in rs:                                                     # newest first
        if r[5] in seen:
            continue
        seen.add(r[5])
        if r[6] == 0:                                                # remarks about one component: only when pushed more than twice or disputed
            if r[10] >= 3 or r[9]:
                comp.append(r)
            continue
        main.append(r)
    lines = ["- %s [%d] %s%s" % (r[7][:150], r[1], r[2][:10], " (repeated)" if r[10] else "") for r in main[:k] + comp[:2]]
    if not lines:
        return ""
    names = _names(db, product)
    gone = db.execute("select count(*) from design_rules where status in ('overridden','merged') and (product in (%s) or scope='global')" % ",".join("?" * len(names)), names).fetchone()[0]
    text = "Design language for %s (your commands, newest wins%s; `design <topic>` shows more):\n%s" % (
        product, ", %d older replaced" % gone if gone else "", "\n".join(lines))
    return text[:budget]


def default_product(db, cwd_name: str):
    """The folder's own product if it has rules, else the product discussed most recently."""
    try:
        names = _names(db, cwd_name)
        if db.execute("select 1 from design_rules where product in (%s) limit 1" % ",".join("?" * len(names)), names).fetchone():
            return canon(cwd_name)
        r = db.execute("select product from design_rules where scope='product' order by ts desc limit 1").fetchone()
        return canon(r[0]) if r else None
    except Exception:
        return None
